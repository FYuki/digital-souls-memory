from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory.memory.domain import ExtractionProposal, MemoryRelation
from digital_souls_memory.memory_http import memory_router
from tests.evidence_support import RECORDED, turn
from tests.it1.test_memory_worker import setup_worker
from tests.memory_support import NEW_ID, OLD_ID, memory

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("source_type", ["inferred", "direct_user"])
def test_worker_does_not_publish_promoted_update(source_type: str) -> None:
    h, store, service, worker = setup_worker()
    h.evidence.add_turn(turn(actor="user-a"))
    worker.process("char-b", "turn-1")
    h.evidence.add_turn(replace(turn("turn-2", actor="user-a"), occurred_at=RECORDED))
    candidate = replace(
        memory(),
        id=NEW_ID,
        object="tea",
        content="紅茶",
        source_turn_ids=("turn-2",),
        valid_from=RECORDED,
        epistemics=replace(memory().epistemics, source_type=source_type, trust_tier="A"),
    )
    h.gateway.output = ExtractionProposal(
        memories=(candidate,),
        relations=(MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type="updates"),),
    )
    worker.process("char-b", "turn-2")
    app = FastAPI()
    app.include_router(memory_router(lambda: service))
    with TestClient(app) as client:
        response = client.get("/characters/char-b/memories")
        assert response.status_code == 200
        assert [(v["id"], v["epistemics"]["trust_tier"]) for v in response.json()] == [
            (OLD_ID, "D")
        ]
    assert store.get_memory("char-b", NEW_ID) is None


def test_reextracting_same_fact_with_new_ids_preserves_processed_update() -> None:
    h, store, service, worker = setup_worker()
    h.evidence.add_turn(turn(actor="user-a"))
    worker.process("char-b", "turn-1")
    h.evidence.add_turn(replace(turn("turn-2", actor="user-a"), occurred_at=RECORDED))
    updated = replace(
        memory(),
        id=NEW_ID,
        object="tea",
        content="紅茶",
        source_turn_ids=("turn-2",),
        valid_from=RECORDED,
    )
    h.gateway.output = ExtractionProposal(
        memories=(updated,),
        relations=(MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type="updates"),),
    )
    worker.process("char-b", "turn-2")
    assert service.active("char-b") == (updated,)
    completed = store.get_extraction("char-b", "turn-2")
    for identifier in ("replacement-one", "replacement-two"):
        h.now += timedelta(seconds=1)
        h.gateway.output = (
            replace(
                memory(),
                id=identifier,
                derivation=replace(memory().derivation, created_at=h.now),
            ),
        )
        worker.service.requeue("turn-1")
        worker.process("char-b", "turn-1")
        assert service.active("char-b") == (updated,)
        assert store.get_extraction("char-b", "turn-2") == completed
        with store.list_relations("char-b", NEW_ID) as relations:
            assert tuple(relations) == (
                MemoryRelation(from_id=NEW_ID, to_id=identifier, type="updates"),
            )
