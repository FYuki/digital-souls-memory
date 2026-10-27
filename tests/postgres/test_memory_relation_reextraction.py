from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import ExtractionProposal, MemoryRelation
from tests.evidence_support import RECORDED, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import NEW_ID, OLD_ID, memory
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_recall_indexing import register_characters

pytestmark = pytest.mark.postgres


def test_promoted_update_is_not_persisted_or_published(database_url: str) -> None:
    register_characters()
    evidence = PostgresEvidenceStore(database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(actor="user-a"))
    evidence.add_turn(replace(turn("turn-2", actor="user-a"), occurred_at=RECORDED))
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    worker.process("char-b", "turn-1")
    candidate = replace(
        memory(),
        id=NEW_ID,
        object="tea",
        content="紅茶",
        valid_from=RECORDED,
        source_turn_ids=("turn-2",),
        epistemics=replace(memory().epistemics, trust_tier="A"),
    )
    gateway.output = ExtractionProposal(
        memories=(candidate,),
        relations=(MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type="updates"),),
    )
    worker.process("char-b", "turn-2")
    restored = PostgresMemoryStore(database_url)
    assert restored.get_memory("char-b", NEW_ID) is None
    assert make_worker(database_url, restored, gateway).service.active("char-b") == (memory(),)


@pytest.mark.parametrize("replacement", ["equivalent", "collision", "different", "other_origin"])
def test_persisted_update_resolves_only_equivalent_reextraction(
    database_url: str, replacement: str
) -> None:
    register_characters()
    evidence = PostgresEvidenceStore(database_url)
    evidence.add_session(session(reports_delivery=False))
    for identifier in ("turn-1", "turn-2", "turn-3"):
        evidence.add_turn(
            replace(
                turn(identifier, actor="user-a"),
                occurred_at=RECORDED if identifier == "turn-2" else memory().valid_from,
            )
        )
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    worker.process("char-b", "turn-1")
    updated = replace(
        memory(),
        id=NEW_ID,
        content="紅茶",
        object="tea",
        source_turn_ids=("turn-2",),
        valid_from=RECORDED,
    )
    gateway.output = ExtractionProposal(
        memories=(updated,),
        relations=(MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type="updates"),),
    )
    worker.process("char-b", "turn-2")
    assert worker.service.active("char-b") == (updated,)
    completed = store.get_extraction("char-b", "turn-2")
    occupied = replace(
        memory(), id="occupied", content="別の事実", object="water", source_turn_ids=("turn-3",)
    )
    if replacement == "collision":
        gateway.output = (occupied,)
        worker.process("char-b", "turn-3")
    if replacement == "other_origin":
        gateway.output = (replace(memory(), id="unrelated-coffee", source_turn_ids=("turn-3",)),)
        worker.process("char-b", "turn-3")
        gateway.output = ()
    else:
        gateway.output = (
            replace(
                memory(),
                id="occupied" if replacement == "collision" else "replacement",
                epistemics=replace(
                    memory().epistemics,
                    confidence_features=replace(
                        memory().epistemics.confidence_features, self_assessment=0.90
                    ),
                ),
                content="別内容" if replacement in ("collision", "different") else memory().content,
                object="juice" if replacement in ("collision", "different") else memory().object,
            ),
        )
    worker.service.requeue("turn-1")
    worker.process("char-b", "turn-1")
    restored = PostgresMemoryStore(database_url)
    reader = make_worker(database_url, restored, gateway).service
    assert restored.get_extraction("char-b", "turn-2") == completed
    with restored.list_relations("char-b", NEW_ID) as relations:
        saved_relations = tuple(relations)
    if replacement == "equivalent":
        assert reader.active("char-b") == (updated,)
        assert saved_relations == (
            MemoryRelation(from_id=NEW_ID, to_id="replacement", type="updates"),
        )
    else:
        assert all(
            r.to_id not in ("occupied", "replacement", "unrelated-coffee") for r in saved_relations
        )
        if replacement == "collision":
            assert restored.get_memory("char-b", occupied.id) == occupied
            state = restored.get_extraction("char-b", "turn-1")
            assert state is not None and state.status == "failed"
        else:
            assert NEW_ID not in {v.id for v in reader.active("char-b")}


def test_http_list_and_persisted_update_survive_reextraction_after_repetition(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    original = replace(memory(), id="a-original")
    repetition = replace(original, id="b-repetition", source_turn_ids=("turn-2",))
    updated = replace(
        original,
        id="c-update",
        object="tea",
        content="紅茶",
        valid_from=RECORDED,
        source_turn_ids=("turn-3",),
    )
    for identifier in ("turn-1", "turn-2", "turn-3"):
        evidence.add_turn(
            replace(
                turn(identifier, actor="user-a"),
                occurred_at=RECORDED if identifier == "turn-3" else original.valid_from,
            )
        )
    store, gateway = PostgresMemoryStore(app_database_url), RecordingGateway()
    worker = make_worker(app_database_url, store, gateway)
    client = TestClient(
        create_app(clock=lambda: RECORDED, conversation_surfaces=frozenset({"synthetic-chat"}))
    )
    gateway.output = (original,)
    worker.process("char-b", "turn-1")
    first = client.get("/characters/char-b/memories")
    assert first.status_code == 200
    assert [(v["id"], v["object"]) for v in first.json()] == [(original.id, "coffee")]
    gateway.output = (repetition,)
    worker.process("char-b", "turn-2")
    repeated = client.get("/characters/char-b/memories")
    assert repeated.status_code == 200
    assert [(v["id"], v["source_turn_ids"]) for v in repeated.json()] == [
        (original.id, ["turn-1", "turn-2"])
    ]
    assert store.get_memory("char-b", repetition.id) is None
    gateway.output = ExtractionProposal(
        memories=(updated,),
        relations=(MemoryRelation(from_id=updated.id, to_id=original.id, type="updates"),),
    )
    worker.process("char-b", "turn-3")
    before = client.get("/characters/char-b/memories")
    assert before.status_code == 200
    assert [(v["id"], v["object"]) for v in before.json()] == [(updated.id, "tea")]
    with store.list_relations("char-b", updated.id) as relations:
        assert tuple(relations) == (
            MemoryRelation(from_id=updated.id, to_id=original.id, type="updates"),
        )

    gateway.output = (replace(original, id="z-reextracted"),)
    requested = client.post("/turns/turn-1/extractions")
    assert requested.status_code == 202
    worker.process("char-b", "turn-1")
    restored = PostgresMemoryStore(app_database_url)
    for identifier in ("turn-1", "turn-2", "turn-3"):
        state = restored.get_extraction("char-b", identifier)
        assert state is not None and state.status == "done"
    after = client.get("/characters/char-b/memories")
    assert after.status_code == 200
    with restored.list_relations("char-b", updated.id) as relations:
        saved_relations = tuple(relations)
    assert ([(v["id"], v["object"]) for v in after.json()], saved_relations) == (
        [(updated.id, "tea")],
        (MemoryRelation(from_id=updated.id, to_id="z-reextracted", type="updates"),),
    )
