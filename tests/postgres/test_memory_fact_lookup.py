"""Fact identity and actual PostgreSQL scan bounds for projection scope lookup."""

from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import TypedDict, cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.abc import Params

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.memory.domain import DerivedMemory
from tests.evidence_support import session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import NEW_ID, OLD_ID, memory
from tests.postgres.test_memory_continuous_ingestion import (
    LockMeasurements,
    extraction_worker,
)
from tests.postgres.test_recall_indexing import register_characters

pytestmark = pytest.mark.postgres


def save_lookup_memory(url: str, value: DerivedMemory, origin: str) -> None:
    with PostgresMemoryStore(url).transaction(value.character_id) as tx:
        tx.add_memory(value)
    if origin == "generated":
        with psycopg.connect(url) as connection:
            connection.execute(
                "INSERT INTO memory_generated(character_id,memory_id,turn_id,payload) "
                "SELECT character_id,memory_id,%s,payload FROM memory_baselines "
                "WHERE character_id=%s AND memory_id=%s",
                (value.source_turn_ids[0], value.character_id, value.id),
            )
            connection.execute(
                "INSERT INTO memory_generated_sources SELECT * FROM memory_baseline_sources "
                "WHERE character_id=%s AND memory_id=%s",
                (value.character_id, value.id),
            )
            connection.execute(
                "DELETE FROM memory_baseline_sources WHERE character_id=%s AND memory_id=%s",
                (value.character_id, value.id),
            )
            connection.execute(
                "DELETE FROM memory_baselines WHERE character_id=%s AND memory_id=%s",
                (value.character_id, value.id),
            )


@pytest.mark.parametrize("seed_origin", ["generated", "baseline"])
@pytest.mark.parametrize("other_origin", ["generated", "baseline"])
def test_lookup_preserves_complete_fact_identity_and_owner(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    seed_origin: str,
    other_origin: str,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(actor="user-a"))
    save_lookup_memory(app_database_url, memory(), seed_origin)
    candidates = (
        replace(memory(), id="equal"),
        replace(memory(), id="subject", subject="user-b"),
        replace(memory(), id="predicate", predicate="dislikes"),
        replace(memory(), id="object", object="tea", content="coffee"),
        replace(memory("preference"), id="kind"),
        replace(memory(), id="owner", character_id="char-c"),
    )
    for candidate in candidates:
        save_lookup_memory(app_database_url, candidate, other_origin)
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        assert tx.projection_neighbors(frozenset({OLD_ID})) == {OLD_ID, "equal"}


@pytest.mark.parametrize("same_fact", [True, False], ids=["P1", "N1"])
@pytest.mark.parametrize("origin", ["generated", "baseline"])
def test_worker_reinforces_only_matching_fact_attributes(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    same_fact: bool,
    origin: str,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(actor="user-a"))
    worker = extraction_worker(app_database_url)
    gateway = RecordingGateway()
    worker.extractor.gateway = gateway
    if origin == "generated":
        worker.process("char-b", "turn-1")
    else:
        with worker.service.store.transaction("char-b") as tx:
            tx.add_memory(memory())
    before = worker.service.store.get_memory("char-b", OLD_ID)
    evidence.add_turn(turn("turn-2", actor="user-a"))
    gateway.output = (
        replace(
            memory(),
            id=NEW_ID,
            source_turn_ids=("turn-2",),
            object="coffee" if same_fact else "tea",
            content="coffee",
        ),
    )
    worker.process("char-b", "turn-2")
    old = worker.service.store.get_memory("char-b", OLD_ID)
    new = worker.service.store.get_memory("char-b", NEW_ID)
    assert old is not None and new is not None
    if same_fact:
        assert old.source_turn_ids == ("turn-1", "turn-2")
        assert old.last_reinforced_at is not None
        assert new.superseded_at is not None
        with worker.service.store.list_relations("char-b", NEW_ID) as relations:
            assert [(r.from_id, r.to_id, r.type) for r in relations] == [
                (NEW_ID, OLD_ID, "extends")
            ]
    else:
        assert old == before
        assert new.superseded_at is None
        assert {v.id for v in worker.service.active("char-b")} == {OLD_ID, NEW_ID}


PlanNode = TypedDict(
    "PlanNode",
    {
        "Plans": list["PlanNode"],
        "Node Type": str,
        "Relation Name": str,
        "Index Name": str,
        "Actual Rows": float,
        "Actual Loops": int,
        "Rows Removed by Filter": int,
    },
    total=False,
)


def plan_nodes(node: PlanNode) -> Iterator[PlanNode]:
    yield node
    for child in node.get("Plans", []):
        yield from plan_nodes(child)


def explain_queries(url: str, queries: list[tuple[str, Params | None]]) -> list[PlanNode]:
    plans = []
    with psycopg.connect(url) as connection:
        for query, params in queries:
            row = connection.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query,
                params,
            ).fetchone()
            assert row is not None
            plans.append(cast(list[dict[str, PlanNode]], row[0])[0]["Plan"])
    return plans


def test_fact_lookup_scan_is_bounded_and_migration_preserves_records(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    register_characters()
    evidence = PostgresEvidenceStore(database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(actor="user-a"))
    save_lookup_memory(database_url, memory(), "generated")
    save_lookup_memory(database_url, replace(memory(), id="equal"), "baseline")
    evidence.add_turn(turn("measured", actor="user-a", text="fixed-change"))
    worker = extraction_worker(database_url)
    worker.process("char-b", "measured")
    scans: dict[int, list[PlanNode]] = {}
    queries = []
    seeded = 0
    for count in (20, 100, 300):
        for number in range(seeded, count):
            for origin in ("generated", "baseline"):
                save_lookup_memory(
                    database_url,
                    replace(
                        memory(),
                        id=f"{origin}-{number}",
                        object=f"unrelated-{number}",
                    ),
                    origin,
                )
        seeded = count
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute("ANALYZE memory_generated")
            connection.execute("ANALYZE memory_baselines")
        repeated = []
        for _ in range(3):
            worker.service.store.requeue("measured")
            with monkeypatch.context() as patch:
                measured = LockMeasurements(patch)
                worker.process("char-b", "measured")
            state = worker.service.store.get_extraction("char-b", "measured")
            assert state is not None and state.status == "done"
            assert len(measured.hold_seconds) == 2 and not measured.acquired
            repeated.append(
                {
                    "wait_seconds": measured.wait_seconds,
                    "hold_seconds": measured.hold_seconds,
                }
            )
        record_property(f"worker_lock_{count}", repeated)
        queries = measured.neighbor_queries
        assert queries
        plans = explain_queries(database_url, queries)
        nodes = [node for plan in plans for node in plan_nodes(plan)]
        scans[count] = [
            node
            for node in nodes
            if node.get("Relation Name")
            in (
                "memory_generated",
                "memory_baselines",
            )
        ]
        record_property(f"scan_{count}", plans)
    # Candidate scans must use the Fact keys, rather than evaluating every owner's Fact.
    indexes = {node.get("Index Name") for node in scans[300]}
    assert {"memory_generated_fact_lookup", "memory_baselines_fact_lookup"} <= indexes, scans
    inspected = {
        count: sum(
            (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
            for node in nodes
        )
        for count, nodes in scans.items()
    }
    # Tiny tables can be cheaper to scan; unrelated growth must not grow the scan.
    assert inspected[300] <= inspected[100] <= inspected[20], inspected
    config = Config("alembic.ini")
    with psycopg.connect(database_url) as connection:
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY memory_id").fetchall()
            for table in ("memory_generated", "memory_baselines")
        }
    command.downgrade(config, "0009")
    try:
        with PostgresMemoryStore(database_url).transaction("char-b") as tx:
            assert tx.projection_neighbors(frozenset({OLD_ID})) == {OLD_ID, "equal"}
        without = explain_queries(database_url, queries)
        record_property("without_fact_indexes", without)
        assert any(
            node.get("Node Type") == "Seq Scan"
            and node.get("Relation Name") in ("memory_generated", "memory_baselines")
            for plan in without
            for node in plan_nodes(plan)
        )
    finally:
        command.upgrade(config, "head")
    with psycopg.connect(database_url) as connection:
        for table, rows in before.items():
            assert (
                connection.execute(f"SELECT * FROM {table} ORDER BY memory_id").fetchall() == rows
            )
    with PostgresMemoryStore(database_url).transaction("char-b") as tx:
        assert tx.projection_neighbors(frozenset({OLD_ID})) == {OLD_ID, "equal"}
