"""Only the migration interprets historical absorbed identifiers."""

from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.memory_records import memory_record
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore, copy_payload
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import DerivedMemory, MemoryRelation
from tests.evidence_support import OCCURRED, RECORDED, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import memory
from tests.postgres.test_deletion import seed_evidence
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_memory_continuous_ingestion import publish_and_search
from tests.postgres.test_memory_restatement import assert_consistent

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "history", ["mapping", "applied", "chain", "deleted-baseline", "source-relation"]
)
def test_upgrade_converts_absorptions_and_persisted_relation_targets(
    database_url: str, history: str
) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.downgrade(config, "0013")
    evidence = seed_evidence(database_url)
    for number in range(1, 6 if history == "source-relation" else 5):
        if number == 2 and history == "deleted-baseline":
            continue
        evidence.add_turn(turn(f"turn-{number}", actor="user-a"))
    root = replace(memory(), id="z")
    expected_sources = (
        {"turn-1", "turn-3"} if history == "deleted-baseline" else {"turn-1", "turn-2", "turn-3"}
    )
    values: tuple[DerivedMemory, ...] = (
        root,
        replace(
            root,
            id="a",
            source_turn_ids=("turn-3" if history == "deleted-baseline" else "turn-2",),
            superseded_at=RECORDED,
        ),
        replace(root, id="b", source_turn_ids=("turn-3",), superseded_at=RECORDED),
        replace(memory("summary"), id="d", source_turn_ids=("turn-4",)),
    )
    if history == "source-relation":
        values = (*values, replace(root, id="x", object="water", source_turn_ids=("turn-5",)))
    # Write the 0013 schema directly; never execute the new memory runtime on it.
    with psycopg.connect(database_url) as conn:
        for value in values:
            persisted = (
                replace(
                    value,
                    source_turn_ids=tuple(sorted(expected_sources)),
                    last_reinforced_at=OCCURRED,
                )
                if value.id == "z"
                else value
            )
            record = memory_record(persisted)
            record["epistemics"] = Jsonb(record["epistemics"])
            record["identity_links"] = Jsonb(record["identity_links"])
            conn.execute(
                sql.SQL("INSERT INTO derived_memories ({}) VALUES ({})").format(
                    sql.SQL(",").join(map(sql.Identifier, record)),
                    sql.SQL(",").join(sql.Placeholder() for _ in record),
                ),
                tuple(record.values()),
            )
            for position, source in enumerate(persisted.source_turn_ids):
                conn.execute(
                    "INSERT INTO memory_sources VALUES ('char-b',%s,%s,%s)",
                    (value.id, source, position),
                )
            if value.id in ("z", "d", "x") or history == "deleted-baseline":
                baseline = persisted if history == "deleted-baseline" else value
                conn.execute(
                    "INSERT INTO memory_baselines VALUES ('char-b',%s,%s)",
                    (value.id, Jsonb(copy_payload(baseline))),
                )
                for position, source in enumerate(baseline.source_turn_ids):
                    conn.execute(
                        "INSERT INTO memory_baseline_sources VALUES ('char-b',%s,%s,%s)",
                        (value.id, source, position),
                    )
            else:
                conn.execute(
                    "INSERT INTO memory_generated VALUES ('char-b',%s,%s,%s)",
                    (
                        value.id,
                        value.source_turn_ids[0],
                        Jsonb(copy_payload(replace(value, superseded_at=None))),
                    ),
                )
                conn.execute(
                    "INSERT INTO memory_generated_sources VALUES ('char-b',%s,%s,0)",
                    (value.id, value.source_turn_ids[0]),
                )
        for absorbed, target in (("a", "z"), ("b", "a" if history == "chain" else "z")):
            if history != "applied":
                conn.execute(
                    "INSERT INTO memory_representatives VALUES ('char-b',%s,%s)", (absorbed, target)
                )
            if history != "mapping":
                conn.execute(
                    "INSERT INTO memory_relations VALUES ('char-b',%s,%s,'extends')",
                    (absorbed, target),
                )
        conn.execute("INSERT INTO memory_relations VALUES ('char-b','d','b','derives')")
        if history == "source-relation":
            conn.execute("INSERT INTO memory_relations VALUES ('char-b','b','x','derives')")
    command.upgrade(config, "0014")
    command.upgrade(config, "head")
    store = PostgresMemoryStore(database_url)
    worker = make_worker(database_url, store, RecordingGateway())
    expected = {"z", "d", "x"} if history == "source-relation" else {"z", "d"}
    assert_consistent(worker.service, database_url, expected)
    saved = store.get_memory("char-b", "z")
    assert saved is not None
    assert set(saved.source_turn_ids) == expected_sources
    assert saved.last_reinforced_at == OCCURRED
    assert (saved.id, saved.valid_from, saved.derivation, saved.epistemics.trust_tier) == (
        root.id,
        root.valid_from,
        root.derivation,
        root.epistemics.trust_tier,
    )
    with store.list_relations("char-b", "d") as relations:
        assert tuple(relations) == (MemoryRelation(from_id="d", to_id="z", type="derives"),)
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT to_regclass('public.memory_representatives')").fetchone() == (
            None,
        )
        if history == "deleted-baseline":
            assert conn.execute("SELECT turn_id FROM turns WHERE turn_id='turn-2'").fetchall() == []
    if history == "deleted-baseline":
        with TestClient(create_app(clock=lambda: RECORDED)) as client:
            assert client.post("/turns/turn-3/extractions").status_code == 202
        remaining = store.get_memory("char-b", "z")
        assert remaining is not None and remaining.source_turn_ids == ("turn-1",)
        assert remaining.last_reinforced_at is None
        assert_consistent(worker.service, database_url, {"z", "d"})
    if history == "source-relation":
        with psycopg.connect(database_url) as conn:
            assert conn.execute(
                "SELECT turn_id,from_id,to_id FROM memory_candidate_relations "
                "WHERE effect IS NOT NULL"
            ).fetchall() == [("turn-3", "z", "x")]
        worker.service.requeue("turn-3")
        assert_consistent(worker.service, database_url, expected)
        with store.list_relations("char-b", "x") as relations:
            assert tuple(relations) == ()


def test_populated_downgrade_refuses_loss_and_preserves_revision_and_recall(
    database_url: str,
) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    # This explicit revision also distinguishes a missing migration from a no-op downgrade.
    command.upgrade(config, "0014")
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    root = replace(memory(), id="a")
    for number in (1, 2):
        source = f"turn-{number}"
        evidence.add_turn(turn(source, actor="user-a"))
        gateway.output = (replace(root, id="a" if number == 1 else "b", source_turn_ids=(source,)),)
        worker.process("char-b", source)
    before = store.get_memory("char-b", "a")
    assert before is not None and set(before.source_turn_ids) == {"turn-1", "turn-2"}
    assert publish_and_search(database_url) == {"a"}
    with pytest.raises(Exception, match="(?i)contribut|出典"):
        command.downgrade(config, "0013")
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0014",)
    assert store.get_memory("char-b", "a") == before
    assert_consistent(worker.service, database_url, {"a"})


def test_empty_contribution_downgrade_can_upgrade_again(database_url: str) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.upgrade(config, "0014")
    command.downgrade(config, "0013")
    command.upgrade(config, "0014")
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0014",)
