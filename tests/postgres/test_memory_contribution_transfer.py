"""Only projected equivalent candidates can receive durable sources and effects."""

from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import ExtractionProposal, MemoryRelation
from tests.evidence_support import RECORDED, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import memory
from tests.postgres.test_deletion import HEADERS, seed_evidence
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_memory_continuous_ingestion import publish_and_search
from tests.postgres.test_memory_restatement import assert_consistent
from tests.postgres.test_restatement_remediation import Recalculate

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


@pytest.mark.parametrize(
    "restatement,content,historical_destination",
    [
        (False, "a", False),
        (True, "a", False),
        (True, "a と `z`", False),
        (True, "a", True),
    ],
    ids=["without-support", "with-support", "literal-content", "retired-destination"],
)
def test_unprojected_equivalent_preserves_independent_candidates_and_consistency(
    database_url: str, restatement: bool, content: str, historical_destination: bool
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    x = replace(memory(), id="x", object="water", source_turn_ids=("turn-x",))
    a = replace(memory(), id="a", valid_from=RECORDED)
    c = replace(memory(), id="c", object="milk", source_turn_ids=("turn-c",))
    d = replace(memory(), id="d", object="juice", source_turn_ids=("turn-d",))
    for candidate in (x, a, c, d):
        origin = candidate.source_turn_ids[0]
        evidence.add_turn(turn(origin, actor="user-a"))
        if candidate.id == "a" and historical_destination:
            gateway.output = (replace(a, id="z"),)
            worker.process("char-b", origin)
            state = store.get_extraction("char-b", origin)
            assert state is not None and state.status == "done"
            worker.service.requeue(origin)
        gateway.output = ExtractionProposal(
            memories=(candidate,),
            relations=(MemoryRelation(from_id="d", to_id="c", type="contradicts"),)
            if candidate.id == "d"
            else (),
        )
        worker.process("char-b", origin)
        state = store.get_extraction("char-b", origin)
        assert state is not None and state.status == "done"
    if restatement:
        evidence.add_turn(replace(turn("turn-b", actor="user-a"), occurred_at=RECORDED))
        gateway.output = ExtractionProposal(
            memories=(replace(a, id="b", source_turn_ids=("turn-b",)),),
            relations=(MemoryRelation(from_id="b", to_id="x", type="updates"),),
        )
        worker.process("char-b", "turn-b")
        state = store.get_extraction("char-b", "turn-b")
        assert state is not None and state.status == "done"
    before = store.get_memory("char-b", "x")
    assert before is not None and before.valid_to == (RECORDED if restatement else None)
    invalidated = store.get_memory("char-b", "c")
    assert invalidated is not None and invalidated.superseded_at is not None
    assert publish_and_search(database_url) == ({"a", "d"} if restatement else {"a", "d", "x"})

    z = replace(a, id="z")
    summary = replace(memory("summary"), id="summary", content=content)
    independent = replace(memory("key"), id="key", content="独立した有効な候補")
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            recalculation_gateway=Recalculate(x, a, c, d, z, summary, independent),
        )
    ) as client:
        assert client.post("/turns/turn-1/extractions").status_code == 202
        gateway.output = ExtractionProposal(
            memories=(z, summary, independent),
            relations=(MemoryRelation(from_id="z", to_id="c", type="derives"),),
        )
        worker.process("char-b", "turn-1")

        # Observe the committed partial rebuild before a full projection or recall.
        state = store.get_extraction("char-b", "turn-1")
        assert state is not None and state.status == "done", state
        assert state.error is None
        assert store.get_memory("char-b", "summary") == summary
        assert store.get_memory("char-b", "key") == independent
        rejected = store.get_memory("char-b", "z")
        assert rejected is None or rejected.superseded_at is not None
        assert store.get_memory("char-b", "x") == x
        with psycopg.connect(database_url) as conn:
            sources = conn.execute(
                "SELECT fact_id,extraction_turn_id,source_turn_id,occurred_at "
                "FROM memory_source_contributions WHERE character_id='char-b'"
            ).fetchall()
            assert all(row == ("a", "turn-b", "turn-b", RECORDED) for row in sources)
            effects = conn.execute(
                "SELECT turn_id,from_id,to_id,type,effect->>'id' "
                "FROM memory_candidate_relations "
                "WHERE character_id='char-b' AND effect IS NOT NULL"
            ).fetchall()
            assert all(row == ("turn-b", "a", "x", "updates", "a") for row in effects)
        expected = {"x", "d", "summary", "key"}
        assert_consistent(worker.service, database_url, expected)
        if restatement:
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-support", "target_turn_ids": ["turn-b"]},
            )
            assert response.status_code == 200, response.json()
            assert response.json()["status"] == "done"
            assert store.get_memory("char-b", "x") == x
            assert_consistent(worker.service, database_url, expected)
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-origin", "target_turn_ids": ["turn-1"]},
        )
        assert response.status_code == 200, response.json()
        assert response.json()["status"] == "done"
        assert_consistent(worker.service, database_url, {"x", "d"})


def test_unprojected_equivalent_target_does_not_receive_restatement_effect(
    database_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    x = replace(memory(), id="x", object="water", source_turn_ids=("turn-x",))
    a = replace(memory(), id="a", valid_from=RECORDED)
    c = replace(memory(), id="c", object="milk", source_turn_ids=("turn-c",))
    d = replace(memory(), id="d", object="juice", source_turn_ids=("turn-d",))
    for candidate in (x, a, c, d):
        origin = candidate.source_turn_ids[0]
        evidence.add_turn(turn(origin, actor="user-a"))
        gateway.output = ExtractionProposal(
            memories=(candidate,),
            relations=(MemoryRelation(from_id="d", to_id="c", type="contradicts"),)
            if candidate.id == "d"
            else (),
        )
        worker.process("char-b", origin)
        state = store.get_extraction("char-b", origin)
        assert state is not None and state.status == "done"
    evidence.add_turn(turn("turn-b", actor="user-a"))
    gateway.output = ExtractionProposal(
        memories=(replace(a, id="b", source_turn_ids=("turn-b",)),),
        relations=(MemoryRelation(from_id="b", to_id="x", type="updates"),),
    )
    worker.process("char-b", "turn-b")
    state = store.get_extraction("char-b", "turn-b")
    assert state is not None and state.status == "done"
    closed = store.get_memory("char-b", "x")
    assert closed is not None and closed.valid_to == RECORDED
    assert publish_and_search(database_url) == {"a", "d"}

    z = replace(x, id="z")
    summary = replace(memory("summary"), id="summary", source_turn_ids=("turn-x",))
    worker.service.requeue("turn-x")
    gateway.output = ExtractionProposal(
        memories=(z, summary),
        relations=(MemoryRelation(from_id="z", to_id="c", type="derives"),),
    )
    worker.process("char-b", "turn-x")
    state = store.get_extraction("char-b", "turn-x")
    assert state is not None and state.status == "done", state
    assert store.get_memory("char-b", "summary") == summary
    assert store.get_memory("char-b", "z") is None
    with psycopg.connect(database_url) as conn:
        effects = conn.execute(
            "SELECT from_id,to_id,effect->>'id' FROM memory_candidate_relations "
            "WHERE character_id='char-b' AND effect IS NOT NULL"
        ).fetchall()
        assert all(row == ("a", "x", "a") for row in effects)
    assert_consistent(worker.service, database_url, {"a", "d", "summary"})
