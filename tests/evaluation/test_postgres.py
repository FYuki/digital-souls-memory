import os
from collections.abc import Callable
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from digital_souls_memory.evaluation.adapters.postgres import PostgresSearch, disposable_database

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("extension", ["pg_bigm", "pgroonga"])
def test_adapter_searches_components_and_stored_originals_in_isolated_cases(
    extension: str,
    candidate: dict[str, object],
    case: dict[str, object],
    protocol: dict[str, object],
) -> None:
    from copy import deepcopy

    candidate["extension"] = extension
    case["protocol"] = protocol
    case["query"] = "青い駅"
    case["memories"][0]["content"] = "青い駅"  # type: ignore[index]
    before = deepcopy(case)
    name = "memory_eval_" + uuid4().hex
    with PostgresSearch(
        candidate, admin_url=os.environ["MEMORY_TEST_DATABASE_URL"], name=name
    ) as search:
        search.index(case, ((1.0, 0.0),))
        hits = search.search((1.0, 0.0), limit=5)
        assert hits[0]["memory_id"] == "m1"
        assert hits[0]["components"]["semantic"] == 1.0  # type: ignore[index]
        assert hits[0]["components"]["lexical"] == 1.0  # type: ignore[index]
        assert hits[0]["source_spans"][0]["text"] == "約束は2026年11月3日、『青い駅』で。"  # type: ignore[index]
        assert search.measurements["index_bytes"] > 0
        second = deepcopy(case)
        second["id"] = "case-b"
        second["turns"][0]["text"] = "赤い港"  # type: ignore[index]
        search.index(second, ((1.0, 0.0),))
        assert search.search((1.0, 0.0), limit=5)[0]["source_spans"][0]["text"] == "赤い港"  # type: ignore[index]
        assert case == before
    with psycopg.connect(os.environ["MEMORY_TEST_DATABASE_URL"], autocommit=True) as admin:
        assert admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchall() == []


@pytest.mark.parametrize("extension", ["pg_bigm", "pgroonga"])
def test_adapter_closes_connection_before_database_disposal_on_search_failure(
    extension: str,
    candidate: dict[str, object],
    case: dict[str, object],
    protocol: dict[str, object],
    turn: dict[str, object],
) -> None:
    candidate["extension"] = extension
    turn["actor"] = "character"
    case["protocol"] = protocol
    name = "memory_eval_" + uuid4().hex
    with pytest.raises(psycopg.errors.UndefinedTable):
        with PostgresSearch(
            candidate, admin_url=os.environ["MEMORY_TEST_DATABASE_URL"], name=name
        ) as search:
            search.index(case, ((1.0, 0.0),))
            hits = search.search((1.0, 0.0), limit=5)
            assert hits[0]["source_spans"][0]["actor"] == "character"  # type: ignore[index]
            search.connection.execute("DROP TABLE memories")
            search.search((1.0, 0.0), limit=5)
    assert search.connection.closed
    with psycopg.connect(os.environ["MEMORY_TEST_DATABASE_URL"], autocommit=True) as admin:
        assert admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchall() == []


@pytest.mark.parametrize("extension", ["pg_bigm", "pgroonga"])
def test_runner_real_database_results_round_trip_without_model_quality_claims(
    extension: str,
    make_suite: Callable[[], dict[str, object]],
    candidate: dict[str, object],
    case: dict[str, object],
    memory: dict[str, object],
) -> None:
    from digital_souls_memory.evaluation.adapters.fixture import FixtureEmbedding
    from digital_souls_memory.evaluation.models import record, rows
    from digital_souls_memory.evaluation.report import public_report, validate_public_report
    from digital_souls_memory.evaluation.runner import run_evaluation

    candidate["extension"] = extension
    fixture: dict[str, object] = {
        "vectors": {
            "passage": {str(memory["content"]): [1, 0]},
            "query": {str(case["query"]): [1, 0]},
        }
    }

    def embedding(settings: dict[str, object]) -> FixtureEmbedding:
        return FixtureEmbedding(settings, fixture)

    def search(settings: dict[str, object]) -> PostgresSearch:
        return PostgresSearch(settings, admin_url=os.environ["MEMORY_TEST_DATABASE_URL"])

    report = run_evaluation(
        make_suite(),
        [candidate],
        mode="fixture",
        embedding_factory=embedding,
        search_factory=search,
    )
    public = public_report(report)
    validate_public_report(public, suite=make_suite(), candidates=[candidate])
    assert public["quality_passed"] is None
    for run in rows(public["runs"]):
        assert run["execution_error"] is None
        assert run["passed"] is True
        result = rows(run["case_results"])[0]
        assert record(result["database"])["index_bytes"] > 0  # type: ignore[operator]
        assert rows(result["hits"])[0]["memory_id"] == "m1"


def test_full_text_extensions_and_vector_search_share_one_database() -> None:
    admin_url = os.environ["MEMORY_TEST_DATABASE_URL"]
    name = "memory_eval_" + uuid4().hex
    with disposable_database(admin_url, name=name) as database_url:
        with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as connection:
            for extension in ("vector", "pg_bigm", "pgroonga"):
                connection.execute(sql.SQL("CREATE EXTENSION {}").format(sql.Identifier(extension)))
            assert {
                row[0]
                for row in connection.execute(
                    "SELECT extname FROM pg_extension "
                    "WHERE extname IN ('vector','pg_bigm','pgroonga')"
                ).fetchall()
            } == {"vector", "pg_bigm", "pgroonga"}
            connection.execute(
                "CREATE TABLE synthetic_docs (id text, body text, embedding vector(2))"
            )
            connection.execute(
                "CREATE INDEX synthetic_bigm ON synthetic_docs USING gin (body gin_bigm_ops)"
            )
            connection.execute(
                "CREATE INDEX synthetic_pgroonga ON synthetic_docs USING pgroonga (body)"
            )
            connection.execute(
                "INSERT INTO synthetic_docs VALUES (%s, %s, %s::vector), (%s, %s, %s::vector)",
                ("m1", "青い駅で会う", "[1,0]", "m2", "赤い港へ行く", "[0,1]"),
            )
            assert connection.execute(
                "SELECT id FROM synthetic_docs WHERE body LIKE %s", ("%青い駅%",)
            ).fetchall() == [("m1",)]
            assert connection.execute(
                "SELECT id FROM synthetic_docs WHERE body &@ %s", ("青い駅",)
            ).fetchall() == [("m1",)]
            assert connection.execute(
                "SELECT id FROM synthetic_docs ORDER BY embedding <=> %s::vector LIMIT 1",
                ("[1,0]",),
            ).fetchone() == ("m1",)
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        assert (
            admin.execute("SELECT datname FROM pg_database WHERE datname = %s", (name,)).fetchall()
            == []
        )


@pytest.mark.parametrize("fail_inside", [False, True], ids=["success", "failure"])
def test_owned_database_is_removed_at_both_terminal_paths(fail_inside: bool) -> None:
    admin_url = os.environ["MEMORY_TEST_DATABASE_URL"]
    name = "memory_eval_" + uuid4().hex

    class SyntheticFailure(Exception):
        pass

    def exercise() -> None:
        with disposable_database(admin_url, name=name) as database_url:
            with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as connection:
                connection.execute("CREATE TABLE synthetic_fixture (id text)")
            if fail_inside:
                raise SyntheticFailure

    if fail_inside:
        with pytest.raises(SyntheticFailure):
            exercise()
    else:
        exercise()
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        assert (
            admin.execute("SELECT datname FROM pg_database WHERE datname = %s", (name,)).fetchall()
            == []
        )


def test_existing_database_collision_preserves_its_records(
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    admin_url = os.environ["MEMORY_TEST_DATABASE_URL"]
    name = "memory_eval_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            existing_url = make_conninfo(admin_url, dbname=name)
            with psycopg.connect(existing_url, autocommit=True, connect_timeout=5) as existing:
                existing.execute("CREATE TABLE sentinel (id text)")
                existing.execute("INSERT INTO sentinel VALUES (%s)", ("keep",))
            with rejected():
                with disposable_database(admin_url, name=name):
                    pytest.fail("A resource owned by another caller must not be acquired")
            with psycopg.connect(existing_url, autocommit=True, connect_timeout=5) as existing:
                assert existing.execute("SELECT id FROM sentinel").fetchall() == [("keep",)]
        finally:
            # This test owns the sentinel database; the adapter does not.
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
