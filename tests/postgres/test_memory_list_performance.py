"""Measure the standard HTTP list against disposable synthetic PostgreSQL data."""

from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, get_ident
from time import perf_counter
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.abc import Params, Query
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from tests.evidence_support import RECORDED, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import memory
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_recall_indexing import register_characters

pytestmark = pytest.mark.postgres


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
