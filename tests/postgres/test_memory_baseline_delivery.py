from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import ExtractionProposal, MemoryRelation
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import RECORDED, receipt, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import NEW_ID, OLD_ID, memory
from tests.postgres.test_deletion import HEADERS, seed_evidence
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_deletion_recalculation import make_worker

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


@pytest.mark.parametrize(
    "rebuild,end",
    [
        ("read", 2),
        ("read", 13),
        ("worker_failure", 2),
        ("worker_failure", 13),
        ("scoped_reference", 2),
        ("full", 2),
        ("full", 13),
    ],
)
def test_recalculated_baseline_checks_used_range_after_receipt_correction(
    database_url: str, end: int, rebuild: str
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_session(session("delivered", reports_delivery=True))
    evidence.add_turn(turn("deleted", actor="user-a"))
    evidence.add_turn(turn("remaining", session_id="delivered", text="OKSECRET-TAIL"))
    evidence.add_receipt(replace(receipt("full", 13), turn_id="remaining"))
    evidence.add_turn(turn("dependent-source", actor="user-a"))
    store = PostgresMemoryStore(database_url)
    original = replace(
        memory("summary"),
        source_turn_ids=("deleted", "remaining"),
        superseded_at=None if rebuild == "scoped_reference" else RECORDED + timedelta(days=1),
    )
    with store.transaction("char-b") as tx:
        tx.add_memory(original)
    revised = replace(original, content="SECRET-TAIL", source_turn_ids=("remaining",))
    gateway = RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            recalculation_gateway=RecordingRecalculationGateway(revised),
        )
    ) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "make-baseline", "target_turn_ids": ["deleted"]},
        )
        assert response.status_code == 200, response.json()
        assert client.get("/characters/char-b/memories").json()[0]["content"] == revised.content
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT turn_id,used_end FROM memory_baseline_dependencies "
                "WHERE character_id='char-b' AND memory_id=%s",
                (OLD_ID,),
            ).fetchall() == [("remaining", 13)]
            assert (
                connection.execute(
                    "SELECT memory_id FROM memory_generated WHERE character_id='char-b'",
                ).fetchall()
                == []
            )
        if rebuild == "scoped_reference":
            dependent = replace(
                memory("summary"),
                id=NEW_ID,
                content="依存した要約",
                source_turn_ids=("dependent-source",),
            )
            gateway.output = ExtractionProposal(
                memories=(dependent,),
                relations=(MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type="derives"),),
            )
            worker.process("char-b", "dependent-source")
            assert {v.id for v in worker.service.active("char-b")} == {OLD_ID, NEW_ID}
        before = store.get_memory("char-b", OLD_ID)
        evidence.add_receipt(
            replace(
                receipt("partial" if end == 2 else "full", end),
                turn_id="remaining",
            )
        )
        if rebuild == "scoped_reference":
            with store.transaction("char-b") as tx:
                worker.service.rebuild(tx, frozenset({OLD_ID}))
            with store.transaction("char-b") as tx:
                worker.service.rebuild(tx, frozenset({NEW_ID}))
            saved = store.get_memory("char-b", NEW_ID)
            assert saved is not None and saved.superseded_at == (
                RECORDED if end == 2 else original.superseded_at
            )
        elif rebuild == "worker_failure":
            gateway.fail = True
            worker.process("char-b", "remaining")
            state = store.get_extraction("char-b", "remaining")
            assert state is not None and state.status == "retry"
            saved = store.get_memory("char-b", OLD_ID)
            assert saved is not None and saved.superseded_at == (
                RECORDED if end == 2 else original.superseded_at
            )
        elif rebuild == "full":
            with store.transaction("char-b") as tx:
                worker.service.rebuild(tx)
            saved = store.get_memory("char-b", OLD_ID)
            assert saved is not None and saved.superseded_at == (
                RECORDED if end == 2 else original.superseded_at
            )
        response = client.get("/characters/char-b/memories")
        assert response.status_code == 200
        assert {v["id"] for v in response.json()} == (set() if end == 2 else {OLD_ID})
        if end != 2 or rebuild == "read":
            assert store.get_memory("char-b", OLD_ID) == before


@pytest.mark.parametrize("end", [2, 13])
def test_generated_candidate_future_expiry_does_not_delay_receipt_withdrawal(
    database_url: str, end: int
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_session(session("delivered", reports_delivery=True))
    evidence.add_turn(turn("remaining", session_id="delivered", text="OKSECRET-TAIL"))
    evidence.add_receipt(replace(receipt("full", 13), turn_id="remaining"))
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    candidate = replace(
        memory("summary"),
        content="SECRET-TAIL",
        source_turn_ids=("remaining",),
        superseded_at=RECORDED + timedelta(days=1),
    )
    gateway.output = (candidate,)
    worker = make_worker(database_url, store, gateway)
    worker.process("char-b", "remaining")
    with TestClient(create_app(clock=lambda: RECORDED)) as client:
        assert {v["id"] for v in client.get("/characters/char-b/memories").json()} == {OLD_ID}
        if end == 2:
            evidence.add_receipt(replace(receipt("partial", end), turn_id="remaining"))
        response = client.get("/characters/char-b/memories")
        assert response.status_code == 200
        assert {v["id"] for v in response.json()} == (set() if end == 2 else {OLD_ID})
        assert store.get_memory("char-b", OLD_ID) == candidate
        gateway.fail = True
        worker.process("char-b", "remaining")
        restored = PostgresMemoryStore(database_url)
        saved = restored.get_memory("char-b", OLD_ID)
        assert saved is not None and saved.superseded_at == (
            RECORDED if end == 2 else candidate.superseded_at
        )
        if end == 2:
            state = restored.get_extraction("char-b", "remaining")
            assert state is not None and state.status == "retry"
        assert {
            v.id for v in make_worker(database_url, restored, gateway).service.active("char-b")
        } == (set() if end == 2 else {OLD_ID})
