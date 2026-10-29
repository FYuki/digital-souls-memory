"""Only the migration interprets historical absorbed identifiers."""

from collections.abc import Callable
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
from tests.postgres.restatement_0013_support import (
    legacy_context_deletion,
    legacy_snapshot,
    project_legacy_chain,
    save_legacy_projection,
)
from tests.postgres.test_deletion import seed_evidence
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_memory_continuous_ingestion import publish_and_search
from tests.postgres.test_memory_restatement import assert_consistent

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("same_target", [False, True])
def test_upgrade_preserves_normal_relation_and_absorbed_candidate_effect(
    database_url: str, same_target: bool
) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.downgrade(config, "0013")
    evidence = seed_evidence(database_url)
    for source in ("turn-a", "turn-b", "turn-x", "turn-y"):
        evidence.add_turn(turn(source, actor="user-a"))
    inputs = make_worker(
        database_url, PostgresMemoryStore(database_url), RecordingGateway()
    ).service.inputs
    a = replace(memory(), id="a", source_turn_ids=("turn-a",))
    b = replace(a, id="b")
    x = replace(a, id="x", object="water", source_turn_ids=("turn-x",))
    y = replace(a, id="y", object="juice", source_turn_ids=("turn-y",))
    snapshot, old_evidence = legacy_snapshot(inputs, (x, y, a, b))
    snapshot = replace(
        snapshot,
        proposed=(
            MemoryRelation(from_id="a", to_id="x", type="derives"),
            MemoryRelation(from_id="b", to_id="x" if same_target else "y", type="derives"),
            MemoryRelation(from_id="b", to_id="a", type="extends"),
        ),
    )
    result = old_evidence.project(snapshot, RECORDED, select_representatives=True)
    assert result.representatives == {"b": "a"}
    save_legacy_projection(database_url, inputs, snapshot, result)
    command.upgrade(config, "0014")
    store = PostgresMemoryStore(database_url)
    with store.transaction("char-b") as tx:
        effects = [v for v in tx.relation_effects() if v.relation.type == "derives"]
        assert len(effects) == 1
        assert effects[0].candidate == b
        assert effects[0].relation == MemoryRelation(
            from_id="a", to_id="x" if same_target else "y", type="derives"
        )
        assert MemoryRelation(from_id="a", to_id="x", type="derives") in tx.proposed_relations()
    command.upgrade(config, "head")
    assert_consistent(
        make_worker(database_url, store, RecordingGateway()).service, database_url, {"a", "x", "y"}
    )


def test_upgrade_reprojects_legacy_chain_and_previously_rejected_referrer(
    database_url: str,
) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.downgrade(config, "0013")
    evidence = seed_evidence(database_url)
    for source in ("turn-a", "turn-b", "turn-d", "turn-z"):
        evidence.add_turn(turn(source, actor="user-a"))
    inputs = make_worker(
        database_url, PostgresMemoryStore(database_url), RecordingGateway()
    ).service.inputs
    a = replace(memory(), id="a", source_turn_ids=("turn-a",))
    b = replace(a, id="b", source_turn_ids=("turn-b",))
    d = replace(memory("summary"), id="d", source_turn_ids=("turn-d",))
    z = replace(a, id="z", source_turn_ids=("turn-z",))
    snapshot, projected = project_legacy_chain(inputs, a, b, d, z)
    save_legacy_projection(database_url, inputs, snapshot, projected)
    with psycopg.connect(database_url) as conn:
        assert set(
            conn.execute(
                "SELECT absorbed_id,representative_id FROM memory_representatives"
            ).fetchall()
        ) == {("b", "a"), ("a", "z")}
        assert conn.execute(
            "SELECT from_id,to_id FROM memory_candidate_relations WHERE type='derives'"
        ).fetchall() == [("d", "b")]
        assert conn.execute(
            "SELECT superseded_at FROM derived_memories WHERE id='d'"
        ).fetchone() == (RECORDED,)
        assert {
            r[0]
            for r in conn.execute(
                "SELECT turn_id FROM memory_sources WHERE memory_id='z'"
            ).fetchall()
        } == {"turn-a", "turn-z"}
    command.upgrade(config, "0014")
    store = PostgresMemoryStore(database_url)
    saved_z, saved_d = store.get_memory("char-b", "z"), store.get_memory("char-b", "d")
    assert saved_z is not None and saved_d is not None
    assert set(saved_z.source_turn_ids) == {"turn-a", "turn-b", "turn-z"}
    assert saved_z.last_reinforced_at == OCCURRED
    assert saved_d.superseded_at is None
    with store.list_relations("char-b", "d") as relations:
        assert tuple(relations) == (MemoryRelation(from_id="d", to_id="z", type="derives"),)
    worker = make_worker(database_url, store, RecordingGateway())
    command.upgrade(config, "head")
    assert_consistent(worker.service, database_url, {"z", "d"})
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            conversation_surfaces=frozenset({"synthetic-chat"}),
        )
    ) as client:
        listed = client.get("/characters/char-b/memories").json()
        assert {v["id"] for v in listed} == {"z", "d"}
        assert set(next(v for v in listed if v["id"] == "z")["source_turn_ids"]) == {
            "turn-a",
            "turn-b",
            "turn-z",
        }


def test_upgrade_keeps_recalculated_baseline_sources_despite_retry_origin(
    database_url: str,
) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.downgrade(config, "0013")
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-a", actor="user-a"))
    evidence.add_turn(turn("question", actor="user-a"))
    evidence.add_turn(turn("turn-b", actor="user-a", reply_to_turn_id="question"))
    inputs = make_worker(
        database_url, PostgresMemoryStore(database_url), RecordingGateway()
    ).service.inputs
    a = replace(memory(), id="a", source_turn_ids=("turn-a",))
    b = replace(a, id="b", source_turn_ids=("turn-b",))
    snapshot, old_evidence = legacy_snapshot(inputs, (a, b))
    result = old_evidence.project(snapshot, RECORDED, select_representatives=True)
    save_legacy_projection(database_url, inputs, snapshot, result)
    revised = replace(result.memories["a"], content="残存出典からの再計算")
    legacy_context_deletion(database_url, inputs, revised, result.memories["b"])
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT turn_id FROM turns WHERE turn_id='question'").fetchall() == []
        assert conn.execute(
            "SELECT status FROM memory_extractions WHERE turn_id='turn-b'"
        ).fetchone() == ("retry",)
        assert {
            r[0]
            for r in conn.execute(
                "SELECT turn_id FROM memory_baseline_sources WHERE memory_id='a'"
            ).fetchall()
        } == {"turn-a", "turn-b"}
        assert conn.execute(
            "SELECT memory_id FROM memory_generated WHERE turn_id='turn-b'"
        ).fetchall() == [
            ("b",),
        ]
    command.upgrade(config, "0014")
    store = PostgresMemoryStore(database_url)
    saved = store.get_memory("char-b", "a")
    assert saved is not None
    assert set(saved.source_turn_ids) == {"turn-a", "turn-b"}
    assert (saved.content, saved.last_reinforced_at, saved.valid_to, saved.superseded_at) == (
        revised.content,
        revised.last_reinforced_at,
        revised.valid_to,
        revised.superseded_at,
    )
    service = make_worker(database_url, store, RecordingGateway()).service
    command.upgrade(config, "head")
    assert_consistent(service, database_url, {"a"})
    with TestClient(create_app(clock=lambda: RECORDED)) as client:
        listed = client.get("/characters/char-b/memories").json()
        assert len(listed) == 1
        assert set(listed[0]["source_turn_ids"]) == {"turn-a", "turn-b"}
        assert listed[0]["content"] == revised.content


def test_upgrade_rejects_cyclic_representatives_without_partial_conversion(
    database_url: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.downgrade(config, "0013")
    evidence = seed_evidence(database_url)
    for source in ("turn-a", "turn-b"):
        evidence.add_turn(turn(source, actor="user-a"))
    inputs = make_worker(
        database_url, PostgresMemoryStore(database_url), RecordingGateway()
    ).service.inputs
    a = replace(memory(), id="a", source_turn_ids=("turn-a",))
    b = replace(a, id="b", source_turn_ids=("turn-b",))
    snapshot, old_evidence = legacy_snapshot(inputs, (a, b))
    result = old_evidence.project(snapshot, RECORDED, select_representatives=True)
    save_legacy_projection(database_url, inputs, snapshot, result)
    with psycopg.connect(database_url) as conn:
        conn.execute("INSERT INTO memory_representatives VALUES('char-b','a','b')")
        before = conn.execute("SELECT * FROM derived_memories ORDER BY id").fetchall()
        sources = conn.execute(
            "SELECT * FROM memory_sources ORDER BY memory_id,position"
        ).fetchall()
    with rejected():
        command.upgrade(config, "0014")
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0013",)
        assert set(
            conn.execute(
                "SELECT absorbed_id,representative_id FROM memory_representatives"
            ).fetchall()
        ) == {("a", "b"), ("b", "a")}
        assert conn.execute("SELECT * FROM derived_memories ORDER BY id").fetchall() == before
        assert (
            conn.execute("SELECT * FROM memory_sources ORDER BY memory_id,position").fetchall()
            == sources
        )
        assert conn.execute(
            "SELECT to_regclass('public.memory_source_contributions')"
        ).fetchone() == (None,)


def test_upgrade_preserves_different_spo_extends_and_identifier_text(database_url: str) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.downgrade(config, "0013")
    evidence = seed_evidence(database_url)
    for source in ("turn-b", "turn-z"):
        evidence.add_turn(turn(source, actor="user-a"))
    inputs = make_worker(
        database_url, PostgresMemoryStore(database_url), RecordingGateway()
    ).service.inputs
    z = replace(memory(), id="z", source_turn_ids=("turn-z",))
    b = replace(
        z, id="b", object="tea", content="Bという文字を含むtea", source_turn_ids=("turn-b",)
    )
    snapshot, old_evidence = legacy_snapshot(inputs, (z, b))
    relation = MemoryRelation(from_id="b", to_id="z", type="extends")
    snapshot = replace(snapshot, proposed=(relation,))
    result = old_evidence.project(snapshot, RECORDED, select_representatives=True)
    save_legacy_projection(database_url, inputs, snapshot, result)
    command.upgrade(config, "0014")
    store = PostgresMemoryStore(database_url)
    saved = store.get_memory("char-b", "b")
    assert saved is not None and saved.content == b.content and saved.object == "tea"
    assert saved == result.memories["b"]
    with store.list_relations("char-b", "b") as relations:
        assert tuple(relations) == (relation,)
    command.upgrade(config, "head")
    assert_consistent(
        make_worker(database_url, store, RecordingGateway()).service, database_url, {"z"}
    )


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
    command.upgrade(config, "head")
    with psycopg.connect(database_url) as conn:
        before_revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
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
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == before_revision
    assert store.get_memory("char-b", "a") == before
    assert_consistent(worker.service, database_url, {"a"})


def test_empty_contribution_downgrade_can_upgrade_again(database_url: str) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.upgrade(config, "0014")
    command.downgrade(config, "0013")
    command.upgrade(config, "0014")
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0014",)
