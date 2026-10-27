"""Measure the standard HTTP list against disposable synthetic PostgreSQL data."""

from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from statistics import median
from threading import Barrier, get_ident
from time import perf_counter
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.abc import Params, Query
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.inputs import ResolvedInput
from tests.evidence_support import RECORDED, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import memory
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_memory_fact_lookup import explain_queries, plan_nodes
from tests.postgres.test_recall_indexing import register_characters

pytestmark = pytest.mark.postgres


def seed_list_copies(url: str, start: int, stop: int, resolved: ResolvedInput) -> None:
    """Bulk-copy a completed synthetic extraction, excluding setup from SQL timing."""
    with psycopg.connect(url) as connection:
        for table, key, changes in (
            ("turns", "turn_id", "jsonb_build_object('turn_id','origin-' || n)"),
            ("memory_extractions", "turn_id", "jsonb_build_object('turn_id','origin-' || n)"),
            (
                "memory_input_dependencies",
                "extraction_turn_id",
                "jsonb_build_object('extraction_turn_id','origin-' || n,'turn_id','origin-' || n)",
            ),
            (
                "derived_memories",
                "id",
                "jsonb_build_object('id','list-' || n,'content','合成の記憶 ' || n)",
            ),
        ):
            connection.execute(
                sql.SQL(
                    "INSERT INTO {table} SELECT "
                    "(jsonb_populate_record(NULL::{table},to_jsonb(seed) || {changes})).* "
                    "FROM {table} seed CROSS JOIN generate_series(%s::integer,%s::integer) n "
                    "WHERE {key}=%s"
                ).format(
                    table=sql.Identifier(table), key=sql.Identifier(key), changes=sql.SQL(changes)
                ),
                (start, stop - 1, "list-0" if key == "id" else "turn-1"),
            )
        identifiers = [f"origin-{n}" for n in range(start, stop)]
        signatures = [
            ResolvedInput(
                tuple(replace(v, turn=replace(v.turn, turn_id=identifier)) for v in resolved.turns)
            ).signature(1)
            for identifier in identifiers
        ]
        connection.execute(
            "UPDATE memory_extractions e SET signature=s.signature "
            "FROM unnest(%s::text[],%s::text[]) s(turn_id,signature) "
            "WHERE e.character_id='char-b' AND e.turn_id=s.turn_id",
            (identifiers, signatures),
        )
        for table in ("memory_generated", "memory_candidates"):
            connection.execute(
                sql.SQL(
                    "INSERT INTO {}(character_id,memory_id,turn_id,payload) "
                    "SELECT character_id,'list-' || n,'origin-' || n,"
                    "payload || jsonb_build_object('id','list-' || n,'content','合成の記憶 ' || n) "
                    "FROM {} CROSS JOIN generate_series(%s::integer,%s::integer) n "
                    "WHERE character_id='char-b' AND memory_id='list-0'"
                ).format(sql.Identifier(table), sql.Identifier(table)),
                (start, stop - 1),
            )
        for table in ("memory_sources", "memory_generated_sources", "memory_candidate_sources"):
            connection.execute(
                sql.SQL(
                    "INSERT INTO {}(character_id,memory_id,turn_id,position) "
                    "SELECT character_id,'list-' || n,'origin-' || n,position "
                    "FROM {} CROSS JOIN generate_series(%s::integer,%s::integer) n "
                    "WHERE character_id='char-b' AND memory_id='list-0'"
                ).format(sql.Identifier(table), sql.Identifier(table)),
                (start, stop - 1),
            )
        for table in ("memory_candidates", "memory_generated", "derived_memories"):
            connection.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))


def test_http_list_candidate_lookup_avoids_correlated_work_at_one_thousand_memories(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn("turn-1"))
    store = PostgresMemoryStore(app_database_url)
    gateway = RecordingGateway()
    gateway.output = (replace(memory("summary"), id="list-0", content="合成の記憶 0"),)
    worker = make_worker(app_database_url, store, gateway)
    worker.process("char-b", "turn-1")
    resolved = worker.service.inputs.resolve("char-b", ("turn-1",))
    state = store.get_extraction("char-b", "turn-1")
    assert state is not None and state.status == "done", state
    client = TestClient(create_app(clock=lambda: RECORDED, extraction_gateway=gateway))
    execute = cast(Callable[..., psycopg.Cursor[DictRow]], psycopg.Cursor.execute)
    queries: list[tuple[str, Params | None]] = []
    durations: list[float] = []
    all_queries: list[tuple[str, Params | None]] = []
    all_durations: list[float] = []

    def observed_execute(
        cursor: psycopg.Cursor[DictRow],
        query: Query,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> psycopg.Cursor[DictRow]:
        rendered = (
            query.as_string(cursor.connection) if isinstance(query, sql.Composable) else query
        )
        target = (
            isinstance(rendered, str)
            and rendered.lstrip().upper().startswith(("SELECT", "WITH"))
            and "memory_candidates" in rendered
            and "memory_generated" in rendered
        )
        started = perf_counter()
        result = execute(cursor, query, params, prepare=prepare, binary=binary)
        elapsed = perf_counter() - started
        all_queries.append((str(rendered), params))
        all_durations.append(elapsed)
        if target:
            queries.append((cast(str, rendered), params))
            durations.append(elapsed)
        return result

    observations: dict[int, dict[str, int | float]] = {}
    repeated_subplans: dict[int, list[dict[str, object]]] = {}
    for start, count in ((1, 100), (100, 1000)):
        seed_list_copies(app_database_url, start, count, resolved)
        expected = {f"list-{n}": f"合成の記憶 {n}" for n in range(count)}
        # Warm the same application and PostgreSQL buffers before taking three samples.
        assert client.get("/characters/char-b/memories").status_code == 200
        samples: list[float] = []
        counts: list[int] = []
        all_samples: list[float] = []
        all_counts: list[int] = []
        http_times: list[float] = []
        for _ in range(3):
            queries.clear()
            durations.clear()
            all_queries.clear()
            all_durations.clear()
            with monkeypatch.context() as patch:
                patch.setattr(psycopg.Cursor, "execute", observed_execute)
                started = perf_counter()
                response = client.get("/characters/char-b/memories")
                http_times.append(perf_counter() - started)
            assert response.status_code == 200
            assert len(response.json()) == count
            assert {v["id"]: v["content"] for v in response.json()} == expected
            assert queries, "The HTTP request must exercise the production candidate lookup"
            samples.append(sum(durations))
            counts.append(len(queries))
            all_samples.append(sum(all_durations))
            all_counts.append(len(all_queries))
        plans = explain_queries(app_database_url, queries)
        repeated_subplans[count] = [
            cast(dict[str, object], node)
            for plan in plans
            for node in plan_nodes(plan)
            if str(cast(dict[str, object], node).get("Subplan Name", "")).startswith("SubPlan")
            and node["Actual Loops"] > 1
        ]
        observations[count] = {
            "queries": max(counts),
            "sql_seconds": median(samples),
            "all_queries": max(all_counts),
            "all_sql_seconds": median(all_samples),
            "http_seconds": median(http_times),
            "correlated_loops": max(
                (cast(int, node["Actual Loops"]) for node in repeated_subplans[count]), default=0
            ),
            "explain_ms": sum(
                cast(float, cast(dict[str, object], plan)["Actual Total Time"]) for plan in plans
            ),
        }
        record_property(f"candidate_lookup_{count}", observations[count])
        record_property(f"candidate_lookup_plans_{count}", plans)
        candidate_plans = explain_queries(
            app_database_url,
            [
                (query, params)
                for query, params in all_queries
                if query.lstrip().upper().startswith(("SELECT", "WITH"))
                and "FROM memory_candidates c " in query
                and "memory_extractions" in query
            ],
        )
        record_property(f"candidate_plans_{count}", candidate_plans)
        record_property(
            f"all_sql_timings_{count}",
            list(zip((query for query, _ in all_queries), all_durations, strict=True)),
        )
        print(f"candidate-lookup count={count}: {observations[count]}")
    assert observations[1000]["queries"] <= observations[100]["queries"], observations
    # Reject proportional latency growth; the 10ms allowance absorbs sub-ms timer noise.
    assert observations[1000]["sql_seconds"] <= 5 * observations[100]["sql_seconds"] + 0.010, (
        observations
    )
    assert not any(repeated_subplans.values()), repeated_subplans
    assert observations[1000]["all_queries"] <= observations[100]["all_queries"], observations
    # Detect the reported ~840ms regression with headroom for CI scheduling noise.
    assert observations[1000]["all_sql_seconds"] <= (
        5 * observations[100]["all_sql_seconds"] + 0.050
    ), observations


class ListDatabaseMeasurements:
    """Keep SQL submissions separate from server-side fetch operations."""

    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.counts: Counter[str] = Counter()
        self.connections: list[psycopg.Connection[DictRow]] = []
        connect = cast(Callable[..., psycopg.Connection[DictRow]], psycopg.connect)
        execute = cast(Callable[..., psycopg.Cursor[DictRow]], psycopg.Cursor.execute)
        server_execute = psycopg.ServerCursor.execute
        fetchmany = psycopg.ServerCursor.fetchmany

        def observed_connect(conninfo: str = "", **kwargs: object) -> psycopg.Connection[DictRow]:
            connection = connect(conninfo, **kwargs)
            self.connections.append(connection)
            self.counts["connections"] += 1
            return connection

        def observed_execute(
            cursor: psycopg.Cursor[DictRow],
            query: Query,
            params: Params | None = None,
            *,
            prepare: bool | None = None,
            binary: bool | None = None,
        ) -> psycopg.Cursor[DictRow]:
            self.counts["sql"] += 1
            return execute(cursor, query, params, prepare=prepare, binary=binary)

        def observed_server_execute(
            cursor: psycopg.ServerCursor[DictRow],
            query: Query,
            params: Params | None = None,
            *,
            binary: bool | None = None,
            **kwargs: object,
        ) -> psycopg.ServerCursor[DictRow]:
            self.counts["sql"] += 1
            return server_execute(cursor, query, params, binary=binary, **kwargs)

        def observed_fetchmany(
            cursor: psycopg.ServerCursor[DictRow], size: int = 0
        ) -> list[DictRow]:
            self.counts["fetch"] += 1
            return fetchmany(cursor, size)

        patch.setattr(psycopg, "connect", observed_connect)
        patch.setattr(psycopg.Cursor, "execute", observed_execute)
        patch.setattr(psycopg.ServerCursor, "execute", observed_server_execute)
        patch.setattr(psycopg.ServerCursor, "fetchmany", observed_fetchmany)

    def assert_released(self) -> None:
        assert all(
            connection.closed or connection.info.transaction_status == TransactionStatus.IDLE
            for connection in self.connections
        )


@pytest.mark.parametrize("shared_source", [False, True], ids=["distinct-sources", "shared-source"])
def test_http_list_connections_and_sql_do_not_grow_with_memory_count(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    shared_source: bool,
    record_property: Callable[[str, object], None],
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    if shared_source:
        evidence.add_turn(turn("shared"))
    store = PostgresMemoryStore(app_database_url)
    gateway = RecordingGateway()
    worker = make_worker(app_database_url, store, gateway)
    # No lifespan: seed synchronously, excluding model calls and background polling from GET.
    client = TestClient(create_app(clock=lambda: RECORDED, extraction_gateway=gateway))
    observations: dict[int, dict[str, int | float]] = {}
    expected: dict[str, str] = {}
    for start, count in ((0, 100), (100, 1000)):
        for index in range(start, count):
            origin = f"origin-{index:04}"
            source = "shared" if shared_source else origin
            evidence.add_turn(turn(origin, reply_to_turn_id="shared" if shared_source else None))
            candidate = replace(
                memory("summary"),
                id=str(uuid5(NAMESPACE_URL, origin)),
                content=f"合成の記憶 {index}",
                source_turn_ids=(source,),
            )
            gateway.output = (candidate,)
            worker.process("char-b", origin)
            expected[candidate.id] = candidate.content
        calls_before = len(gateway.local_calls)
        with monkeypatch.context() as patch:
            measured = ListDatabaseMeasurements(patch)
            started = perf_counter()
            response = client.get("/characters/char-b/memories")
            elapsed = perf_counter() - started
        observations[count] = dict(measured.counts) | {"seconds": elapsed}
        record_property(str(count), observations[count])
        print(f"memory-list shared_source={shared_source} count={count}: {observations[count]}")
        assert response.status_code == 200
        assert len(response.json()) == count
        assert {v["id"]: v["content"] for v in response.json()} == expected
        assert len(gateway.local_calls) == calls_before
        measured.assert_released()
    assert observations[1000]["connections"] <= observations[100]["connections"], observations
    assert observations[1000]["sql"] <= observations[100]["sql"], observations


def test_http_list_database_failure_releases_connections_and_can_retry(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn())
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        tx.add_memory(memory())
    client = TestClient(create_app(clock=lambda: RECORDED), raise_server_exceptions=False)
    with monkeypatch.context() as patch:
        measured = ListDatabaseMeasurements(patch)
        execute = cast(Callable[..., psycopg.Cursor[DictRow]], psycopg.Cursor.execute)
        fail = True

        def fail_once(
            cursor: psycopg.Cursor[DictRow],
            query: Query,
            params: Params | None = None,
            *,
            prepare: bool | None = None,
            binary: bool | None = None,
        ) -> psycopg.Cursor[DictRow]:
            nonlocal fail
            if fail:
                fail = False
                raise psycopg.OperationalError("Synthetic database failure")
            return execute(cursor, query, params, prepare=prepare, binary=binary)

        patch.setattr(psycopg.Cursor, "execute", fail_once)
        response = client.get("/characters/char-b/memories")
        assert response.status_code == 503
        measured.assert_released()
        following = client.get("/characters/char-b/memories")
        assert following.status_code == 200
        assert [v["id"] for v in following.json()] == [memory().id]
        measured.assert_released()


def test_parallel_http_lists_keep_character_results_and_release_connections(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn())
    store = PostgresMemoryStore(app_database_url)
    for character in ("char-b", "char-c"):
        with store.transaction(character) as tx:
            tx.add_memory(replace(memory(), character_id=character, content=character))
    client = TestClient(create_app(clock=lambda: RECORDED), raise_server_exceptions=False)
    # Initialize the cached service before overlapping requests on that same application.
    assert client.get("/characters/char-b/memories").status_code == 200
    reached_query = Barrier(2)
    seen: set[int] = set()
    with monkeypatch.context() as patch:
        measured = ListDatabaseMeasurements(patch)
        execute = cast(Callable[..., psycopg.Cursor[DictRow]], psycopg.Cursor.execute)

        def overlapping_execute(
            cursor: psycopg.Cursor[DictRow],
            query: Query,
            params: Params | None = None,
            *,
            prepare: bool | None = None,
            binary: bool | None = None,
        ) -> psycopg.Cursor[DictRow]:
            thread = get_ident()
            if thread not in seen:
                seen.add(thread)
                reached_query.wait(timeout=10)
            return execute(cursor, query, params, prepare=prepare, binary=binary)

        patch.setattr(psycopg.Cursor, "execute", overlapping_execute)
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = {
                character: pool.submit(client.get, f"/characters/{character}/memories")
                for character in ("char-b", "char-c")
            }
            for character, pending in requests.items():
                response = pending.result(timeout=20)
                assert response.status_code == 200
                assert [(v["character_id"], v["content"]) for v in response.json()] == [
                    (character, character)
                ]
        measured.assert_released()
