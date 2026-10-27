"""Current list content through the standard PostgreSQL application wiring."""

from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.memory.extraction import MemoryExtractor
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.memory.worker import MemoryWorker
from digital_souls_memory.memory_http import MemoryResponse
from tests.evidence_support import RECORDED, TEXT, receipt, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import memory
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_recall_indexing import register_characters

pytestmark = pytest.mark.postgres


def test_http_list_returns_comment_memory_after_this_characters_reply(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(actor="user-a", text="コーヒーが好きです", perceived_by=("char-b",)))
    conversation_surfaces: frozenset[str] = frozenset()
    comment_surfaces = frozenset({"synthetic-chat"})
    inputs = MemoryInputs(
        evidence,
        EvidenceMaterials(
            evidence,
            IdentityCharacters(lambda: PostgresIdentityStore(app_database_url)),
            lambda: RECORDED,
            timedelta(minutes=10),
        ),
        conversation_surfaces=conversation_surfaces,
        comment_surfaces=comment_surfaces,
    )
    candidate = memory("summary")
    gateway = RecordingGateway()
    gateway.output = (candidate,)
    store = PostgresMemoryStore(app_database_url)
    worker = MemoryWorker(
        MemoryService(store, inputs, lambda: RECORDED),
        MemoryExtractor(inputs=inputs, gateway=gateway, non_sensitive_labels=frozenset()),
    )
    worker.process("char-b", "turn-1")
    client = TestClient(
        create_app(
            clock=lambda: RECORDED,
            conversation_surfaces=conversation_surfaces,
            comment_surfaces=comment_surfaces,
        )
    )
    before = client.get("/characters/char-b/memories")
    assert before.status_code == 200
    assert before.json() == []

    evidence.add_turn(
        turn(
            "own-reply",
            actor="char-b",
            perceived_by=("char-b",),
            address_mode="direct",
            addressees=("user-a",),
            reply_to_turn_id="turn-1",
        )
    )
    worker.process("char-b", "turn-1")
    state = store.get_extraction("char-b", "turn-1")
    assert state is not None and state.status == "done"
    with store.transaction("char-b") as tx:
        assert tuple(v.memory for v in tx.candidates_for_turns(frozenset({"turn-1"}))) == (
            candidate,
        )
    after = client.get("/characters/char-b/memories")
    assert after.status_code == 200
    assert [(v["id"], v["content"], v["source_turn_ids"]) for v in after.json()] == [
        (candidate.id, "飲み物の好み", ["turn-1"])
    ]


@pytest.mark.parametrize("end_field", ["valid_to", "superseded_at", "none"])
def test_http_list_preserves_content_and_time_boundaries_in_same_app(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, end_field: str
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn())
    start = RECORDED + timedelta(seconds=1)
    end = start + timedelta(seconds=2)
    candidate = replace(
        memory(),
        valid_from=start,
        valid_to=end if end_field == "valid_to" else None,
        superseded_at=end if end_field == "superseded_at" else None,
    )
    store = PostgresMemoryStore(app_database_url)
    with store.transaction("char-b") as tx:
        tx.add_memory(candidate)
    now = RECORDED
    client = TestClient(create_app(clock=lambda: now))
    for at, visible in (
        (start - timedelta(microseconds=1), False),
        (start, True),
        (end - timedelta(microseconds=1), True),
        (end, end_field == "none"),
    ):
        now = at
        response = client.get("/characters/char-b/memories")
        assert response.status_code == 200
        assert response.json() == (
            [MemoryResponse.model_validate(candidate).model_dump(mode="json")] if visible else []
        )
    assert store.get_memory("char-b", candidate.id) == candidate


@pytest.mark.parametrize("question_perceived", [False, True])
@pytest.mark.parametrize("support_includes_question", [False, True])
def test_http_list_reply_uses_its_own_session_and_current_receipt(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    question_perceived: bool,
    support_includes_question: bool,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_session(session("question-session", reports_delivery=True))
    evidence.add_turn(
        turn(
            "question",
            session_id="question-session",
            perceived_by=("char-b", "char-c") if question_perceived else ("char-c",),
            addressees=("char-b",),
            address_mode="direct",
        )
    )
    evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id="question"))
    evidence.add_turn(turn(actor="user-a", reply_to_turn_id="question", text="うん、そうだよ"))
    sources = ("turn-1", "question") if support_includes_question else ("turn-1",)
    gateway = RecordingGateway()
    candidate = replace(memory("summary"), source_turn_ids=sources)
    gateway.output = (candidate,)
    store = PostgresMemoryStore(app_database_url)
    worker = make_worker(app_database_url, store, gateway)
    worker.process("char-b", "turn-1")
    client = TestClient(
        create_app(clock=lambda: RECORDED, conversation_surfaces=frozenset({"synthetic-chat"}))
    )
    before = client.get("/characters/char-b/memories")
    assert before.status_code == 200
    accepted = question_perceived or not support_includes_question
    assert [v["id"] for v in before.json()] == ([candidate.id] if accepted else [])
    if accepted:
        assert before.json()[0]["source_turn_ids"] == list(sources)
        expected = MemoryResponse.model_validate(candidate).model_dump(mode="json")
        assert before.json()[0]["epistemics"] == expected["epistemics"]
    saved = store.get_memory("char-b", candidate.id)
    evidence.add_receipt(replace(receipt("none", 0), turn_id="question"))
    after = client.get("/characters/char-b/memories")
    assert after.status_code == 200
    assert [v["id"] for v in after.json()] == (
        [candidate.id] if accepted and not question_perceived else []
    )
    assert store.get_memory("char-b", candidate.id) == saved
