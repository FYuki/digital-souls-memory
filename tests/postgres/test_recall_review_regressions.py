"""Synthetic reproductions for the recall/search review fixes."""

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Self, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.abc import Params, Query
from psycopg.rows import DictRow
from sqlalchemy.engine import make_url

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from tests.evidence_support import RECORDED, session, turn
from tests.memory_support import memory
from tests.postgres.test_recall_indexing import register_characters
from tests.postgres.test_recall_search import plan_nodes, publish, search

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("denied_count,max_scan", [(0, 20000), (80, 20000), (80, 1)])
def test_semantic_hnsw_returns_two_hundred_distinct_eligible_candidates(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, denied_count: int, max_scan: int
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(perceived_by=("char-c",)))
    evidence.add_turn(turn(turn_id="control"))
    with PostgresMemoryStore(app_database_url).transaction("char-c") as tx:
        for identifier, source in (("denied-template", "turn-1"), ("allowed-template", "control")):
            tx.add_memory(
                replace(
                    memory(),
                    id=identifier,
                    character_id="char-c",
                    shared_with=("char-b",),
                    source_turn_ids=(source,),
                )
            )
    index = PostgresRetrievalStore(app_database_url)
    publish(index, now=RECORDED)
    with psycopg.connect(app_database_url) as conn:
        conn.execute(
            "INSERT INTO derived_memories SELECT (jsonb_populate_record(NULL::derived_memories, "
            "to_jsonb(m)||jsonb_build_object('id','vector-'||i))).* "
            "FROM generate_series(0,1999) i JOIN derived_memories m "
            "ON m.id=CASE WHEN i<%s THEN 'denied-template' ELSE 'allowed-template' END",
            (denied_count,),
        )
        conn.execute(
            "INSERT INTO memory_sources SELECT 'char-c','vector-'||i, "
            "CASE WHEN i<%s THEN 'turn-1' ELSE 'control' END,0 FROM generate_series(0,1999) i",
            (denied_count,),
        )
        conn.execute(
            "UPDATE recall_documents d SET ready=TRUE,pending=FALSE,search_text=t.search_text, "
            "content_digest=t.content_digest,model=t.model,revision=t.revision,digest=t.digest, "
            "dimensions=t.dimensions,embedding=('['||(0.99-i/4000.0)::text||','||"
            "sqrt(1-power(0.99-i/4000.0,2))::text||%s||']')::vector, "
            "payload=t.payload||jsonb_build_object('id',d.identifier), "
            "dependencies=t.dependencies,labels=t.labels "
            "FROM generate_series(0,1999) i JOIN recall_documents t "
            "ON t.identifier=CASE WHEN i<%s THEN 'denied-template' ELSE 'allowed-template' END "
            "WHERE d.identifier='vector-'||i",
            (",0" * 1022, denied_count),
        )
        conn.execute(
            "INSERT INTO recall_document_sources SELECT 'char-c','memory','vector-'||i, "
            "CASE WHEN i<%s THEN 'turn-1' ELSE 'control' END FROM generate_series(0,1999) i",
            (denied_count,),
        )
        conn.execute("UPDATE recall_documents SET ready=FALSE WHERE identifier LIKE '%%-template'")
        assert conn.execute(
            "SELECT count(*),count(DISTINCT embedding::text) FROM recall_documents WHERE ready"
        ).fetchone() == (2000, 2000)
        conn.execute("ANALYZE recall_documents")
        conn.execute("ANALYZE derived_memories")

    nodes: list[DictRow] = []
    modes: list[str] = []
    fetched: list[int] = []
    exact_plans: list[list[DictRow]] = []

    class ObservedCursor(psycopg.ServerCursor[DictRow]):
        def execute(
            self,
            query: Query,
            params: Params | None = None,
            *,
            binary: bool | None = None,
            **kwargs: object,
        ) -> Self:
            fetched.append(0)
            if len(fetched) > 1:
                with psycopg.Cursor(self.connection) as cursor:
                    cursor.execute(sql.SQL("EXPLAIN (FORMAT JSON) ") + sql.SQL(str(query)), params)
                    row = cursor.fetchone()
                    assert row is not None
                    exact_plans.append(list(plan_nodes(row["QUERY PLAN"][0]["Plan"])))
            if not nodes:
                with psycopg.Cursor(self.connection) as cursor:
                    cursor.execute(
                        sql.SQL(
                            "EXPLAIN (FORMAT JSON) DECLARE {} NO SCROLL CURSOR WITHOUT HOLD FOR "
                        ).format(sql.Identifier(self.name))
                        + sql.SQL(str(query)),
                        params,
                    )
                    row = cursor.fetchone()
                    assert row is not None
                    nodes.extend(plan_nodes(row["QUERY PLAN"][0]["Plan"]))
                    cursor.execute("SHOW hnsw.iterative_scan")
                    row = cursor.fetchone()
                    assert row is not None
                    modes.append(row["hnsw.iterative_scan"])
            return super().execute(query, params, binary=binary, **kwargs)

        def fetchmany(self, size: int = 0) -> list[DictRow]:
            values = super().fetchmany(size)
            fetched[-1] += len(values)
            return values

    connection_factory = index._connection

    @contextmanager
    def observed_connection() -> Iterator[psycopg.Connection[DictRow]]:
        with connection_factory() as conn:
            # Exercise HNSW rather than letting a small fixture choose an exact sorted scan.
            conn.execute("SET enable_seqscan=off")
            conn.execute("SET enable_sort=off")
            conn.execute(sql.SQL("SET hnsw.max_scan_tuples={}").format(sql.Literal(max_scan)))
            conn.commit()
            conn.server_cursor_factory = ObservedCursor
            yield conn

    monkeypatch.setattr(index, "_connection", observed_connection)
    found = search(index, "%_\\", reader="char-b", now=RECORDED, limit=200)

    assert "recall_vector" in {node.get("Index Name") for node in nodes}
    assert len(found) == 200, {"count": len(found), "iterative_scan": modes}
    assert len({candidate.ref for candidate in found}) == 200
    assert all(candidate.lexical == 0 for candidate in found)
    assert {candidate.ref.memory_id for candidate in found}.isdisjoint(
        {f"vector-{i}" for i in range(denied_count)}
    )
    assert modes == ["strict_order"]
    if denied_count:
        assert exact_plans, "Rejected candidates require an uncapped distance scan"
        for plan in exact_plans:
            assert "Sort" in {node["Node Type"] for node in plan}
            assert "recall_vector" not in {node.get("Index Name") for node in plan}
    if denied_count == 0:
        assert fetched[0] == 200, "The HNSW scan itself must continue past ef_search=40"
    if max_scan == 1:
        assert 0 < fetched[0] < 200, "Exercise a short HNSW scan that requires replenishment"


@pytest.fixture
def empty_measurement_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    url = os.environ["MEMORY_TEST_DATABASE_URL"]
    name = "recall_measurement_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as owner:
        owner.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with monkeypatch.context() as environment:
                measurement_url = (
                    make_url(url).set(database=name).render_as_string(hide_password=False)
                )
                environment.setenv("MEMORY_TEST_DATABASE_URL", measurement_url)
                environment.setenv("MEMORY_DATABASE_URL", measurement_url)
                yield
        finally:
            owner.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.mark.parametrize("previously_set", [False, True], ids=["unset", "set"])
@pytest.mark.parametrize("raises", [False, True], ids=["normal", "exception"])
def test_measurement_fixture_restores_database_environment(
    pytester: pytest.Pytester, previously_set: bool, raises: bool
) -> None:
    pytester.makeconftest(
        f"""
import os
import sys
import pytest

sys.path.insert(0, {str(Path(__file__).parents[2])!r})
from tests.postgres.test_recall_review_regressions import empty_measurement_database

@pytest.fixture
def original_environment(monkeypatch):
    original_test_url = os.environ["MEMORY_TEST_DATABASE_URL"]
    if {previously_set!r}:
        monkeypatch.setenv("MEMORY_DATABASE_URL", original_test_url)
    else:
        monkeypatch.delenv("MEMORY_DATABASE_URL", raising=False)
    yield
    restored = (
        os.environ.get("MEMORY_DATABASE_URL") == original_test_url
        if {previously_set!r} else "MEMORY_DATABASE_URL" not in os.environ
    )
    assert restored, "Database environment must be restored after fixture teardown"
    test_url_restored = os.environ["MEMORY_TEST_DATABASE_URL"] == original_test_url
    assert test_url_restored

@pytest.fixture
def measured_database(original_environment, empty_measurement_database):
    yield
"""
    )
    pytester.makepyfile(
        f"""
import os

def test_measurement(measured_database):
    os.environ["MEMORY_DATABASE_URL"] = os.environ["MEMORY_TEST_DATABASE_URL"]
    if {raises!r}:
        raise RuntimeError("Synthetic measurement failure after environment assignment")
"""
    )
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=int(not raises), failed=int(raises), errors=0)
    assert result.ret == (pytest.ExitCode.TESTS_FAILED if raises else pytest.ExitCode.OK)


def test_measurement_captures_reader_queries_and_finishes_all_three_sizes(
    empty_measurement_database: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # main owns migrations in a separate empty synthetic database.
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2]))
    measure_recall = import_module("tools.measure_recall")

    actual: list[tuple[str, str]] = []
    explained: list[tuple[str, str]] = []
    execute = cast(Callable[..., psycopg.Cursor[DictRow]], psycopg.Cursor.execute)
    server_execute = psycopg.ServerCursor.execute

    def observe(query: Query, params: Params | None, conn: psycopg.Connection[DictRow]) -> None:
        text = (
            query
            if isinstance(query, str)
            else query.as_string(conn)
            if isinstance(query, (sql.SQL, sql.Composed))
            else str(query)
        )
        if "recall_documents" not in text:
            return
        if text.startswith("EXPLAIN"):
            statement = text.split(" FOR ", 1)[1] if "DECLARE " in text else text.split(") ", 1)[1]
            explained.append((statement, str(params)))
        elif text.startswith(("SELECT", "WITH")):
            actual.append((text, str(params)))

    def observed_execute(
        cursor: psycopg.Cursor[DictRow],
        query: Query,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> psycopg.Cursor[DictRow]:
        observe(query, params, cursor.connection)
        return execute(cursor, query, params, prepare=prepare, binary=binary)

    def observed_server_execute(
        cursor: psycopg.ServerCursor[DictRow],
        query: Query,
        params: Params | None = None,
        *,
        binary: bool | None = None,
        **kwargs: object,
    ) -> psycopg.ServerCursor[DictRow]:
        observe(query, params, cursor.connection)
        return server_execute(cursor, query, params, binary=binary, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", observed_execute)
    monkeypatch.setattr(psycopg.ServerCursor, "execute", observed_server_execute)
    monkeypatch.setattr(measure_recall, "Path", lambda name: tmp_path / Path(name).name)
    measure_recall.main()

    assert set(explained) <= set(actual)
    assert any("embedding <=>" in text and "LIKE likequery" not in text for text, _ in explained)
    assert any("LIKE likequery" in text for text, _ in explained)
    assert any("JOIN verbatim_anchors" in text for text, _ in explained)
    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [result["count"] for result in results] == [100, 1000, 10000]
    for result in results:
        assert result["warmup"] == 5 and result["iterations"] == 20
        assert result["returned_memories"] == 5
        plans = json.loads(Path(result["sql_plan"]).read_text())
        assert len(plans) >= 3, "Semantic, lexical and associated anchor SQL must be captured"
