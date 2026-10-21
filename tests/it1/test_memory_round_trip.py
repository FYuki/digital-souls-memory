from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory.memory.domain import DerivedMemory, ExtractionProposal, MemoryRelation
from digital_souls_memory.memory.extraction import LocalOnly
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.memory.worker import MemoryWorker
from digital_souls_memory.memory_http import memory_router
from tests.evidence_support import OCCURRED, TEXT, receipt, session, turn
from tests.it1.test_memory_extraction import Harness
from tests.it1.test_memory_worker import setup_worker
from tests.memory_fakes import FakeMemoryStore
from tests.memory_support import NEW_ID, OLD_ID, memory

pytestmark = pytest.mark.it1


def setup_round_trip() -> tuple[Harness, FakeMemoryStore, MemoryService, MemoryWorker]:
    h, store, service, worker = setup_worker()
    h.evidence.add_session(session("question-session", reports_delivery=True))
    h.evidence.add_turn(turn(turn_id="question", session_id="question-session"))
    h.evidence.add_turn(turn(actor="user-a", text="うん、そうだよ", reply_to_turn_id="question"))
    h.gateway.output = (memory(),)
    return h, store, service, worker


def test_worker_sends_direct_reply_context_without_ancestors_or_latest_speaker() -> None:
    h, _, service, worker = setup_worker()
    h.evidence.add_turn(turn(turn_id="ancestor", actor="user-z", text="祖先の発言"))
    h.evidence.add_turn(
        turn(turn_id="question", text="コーヒーが好き？", reply_to_turn_id="ancestor")
    )
    h.evidence.add_turn(turn(actor="user-a", text="うん、そうだよ", reply_to_turn_id="question"))
    h.evidence.add_turn(turn(turn_id="unrelated", actor="user-z", text="別の会話"))
    h.gateway.output = (memory(),)
    worker.process("char-b", "turn-1")
    sent = h.gateway.local_calls[-1].turns
    assert len(sent) == 2
    assert {v.turn_id: v.text for v in sent} == {
        "turn-1": "うん、そうだよ",
        "question": "コーヒーが好き？",
    }
    (saved,) = service.active("char-b")
    assert saved.source_turn_ids == ("turn-1",)
    assert saved.epistemics.independent_evidence_count == 1


@pytest.mark.parametrize("reply_to", [None, "absent"])
def test_no_or_missing_reply_target_still_extracts_single_turn(reply_to: str | None) -> None:
    h, _, service, worker = setup_worker()
    h.evidence.add_turn(turn(actor="user-a", reply_to_turn_id=reply_to))
    h.gateway.output = (memory(),)
    worker.process("char-b", "turn-1")
    assert service.active("char-b") == (memory(),)
    assert [(v.turn_id, v.text) for v in h.gateway.local_calls[-1].turns] == [("turn-1", TEXT)]


@pytest.mark.parametrize(
    "condition,expected",
    [
        ("outside-perception", None),
        ("other-thought", None),
        ("pending", None),
        ("none", None),
        ("partial", TEXT[:9]),
        ("full", TEXT),
        ("timeout", TEXT),
        ("speaker", TEXT),
        ("declared-full", TEXT),
    ],
)
def test_reply_context_uses_its_own_perception_session_and_delivery(
    condition: str, expected: str | None
) -> None:
    h, _, service, worker = setup_worker()
    h.evidence.add_session(
        session("question-session", reports_delivery=condition != "declared-full")
    )
    question = turn(turn_id="question", session_id="question-session")
    if condition == "outside-perception":
        question = replace(question, perceived_by=("char-c",))
    elif condition == "other-thought":
        question = replace(question, kind="thought", address_mode="self", perceived_by=("char-c",))
    elif condition == "speaker":
        question = replace(question, actor="char-b")
    h.evidence.add_turn(question)
    h.evidence.add_turn(turn(actor="user-a", text="短い返答", reply_to_turn_id="question"))
    if condition in ("none", "partial", "full", "speaker"):
        status, end = {
            "none": ("none", 0),
            "partial": ("partial", 9),
            "full": ("full", len(TEXT)),
            "speaker": ("none", 0),
        }[condition]
        h.evidence.add_receipt(replace(receipt(status, end), turn_id="question"))
    if condition == "timeout":
        h.now += timedelta(minutes=10)
    h.gateway.output = (memory(),)
    worker.process("char-b", "turn-1")
    assert len(service.active("char-b")) == 1
    assert {v.turn_id: v.text for v in h.gateway.local_calls[-1].turns} == (
        {"turn-1": "短い返答"} if expected is None else {"turn-1": "短い返答", "question": expected}
    )
    assert h.gateway.cloud_calls == []


@pytest.mark.parametrize("condition", ["pending", "none", "outside-perception"])
def test_unavailable_new_turn_does_not_extract_just_the_reply_target(condition: str) -> None:
    h, _, service, worker = setup_worker()
    h.evidence.add_session(session("reply-session", reports_delivery=True))
    h.evidence.add_turn(turn(turn_id="question", actor="user-a"))
    h.evidence.add_turn(
        turn(
            session_id="reply-session",
            reply_to_turn_id="question",
            perceived_by=("char-c",) if condition == "outside-perception" else ("char-b", "char-c"),
        )
    )
    if condition == "none":
        h.evidence.add_receipt(receipt("none", 0))
    h.gateway.output = ()
    worker.process("char-b", "turn-1")
    assert service.active("char-b") == ()
    assert h.gateway.local_calls == []
    assert h.gateway.cloud_calls == []


def test_sensitive_reply_context_routes_public_new_turn_to_local_gateway() -> None:
    h, _, _, worker = setup_worker()
    h.evidence.add_turn(turn(turn_id="question", text="合成機微原文"))
    h.evidence.add_turn(
        turn(actor="user-a", reply_to_turn_id="question", sensitivity="synthetic-public")
    )
    h.gateway.output = (memory(),)
    worker.process("char-b", "turn-1")
    assert h.gateway.cloud_calls == []
    assert {v.turn_id: v.text for v in h.gateway.local_calls[-1].turns} == {
        "turn-1": TEXT,
        "question": "合成機微原文",
    }


@pytest.mark.parametrize("forbidden", ["outside-input", "question"])
def test_input_external_or_filtered_context_sources_reject_whole_batch(forbidden: str) -> None:
    h, store, service, worker = setup_round_trip()
    h.evidence.add_turn(turn(turn_id="outside-input", actor="user-a"))
    invalid = replace(memory(), id=NEW_ID, source_turn_ids=("turn-1", forbidden))
    h.gateway.output = (memory(), invalid)
    worker.process("char-b", "turn-1")
    assert service.active("char-b") == ()
    assert store.values == {}
    state = store.get_extraction("char-b", "turn-1")
    assert state is not None and state.status == "failed"


def test_round_trip_duplicate_processing_and_explicit_requeue_do_not_multiply_effects() -> None:
    h, store, service, worker = setup_round_trip()
    h.now += timedelta(minutes=10)
    relation = MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type="derives")
    h.gateway.output = ExtractionProposal(
        memories=(memory(), replace(memory("summary"), id=NEW_ID)), relations=(relation,)
    )
    worker.process("char-b", "turn-1")
    before = service.active("char-b")
    assert len(before) == 2
    for _ in range(2):
        worker.process("char-b", "turn-1")
        store.requeue("turn-1")
        worker.process("char-b", "turn-1")
    assert service.active("char-b") == before
    assert len(store.values) == 2
    with store.list_relations("char-b", NEW_ID) as values:
        assert tuple(values) == (relation,)
    assert {v.turn_id for v in h.gateway.local_calls[-1].turns} == {"turn-1", "question"}


@pytest.mark.parametrize("resolution", ["timeout", "receipt"])
def test_pending_context_becomes_available_and_reprocesses_same_reply(resolution: str) -> None:
    h, _, service, worker = setup_round_trip()
    worker.process("char-b", "turn-1")
    assert {v.turn_id for v in h.gateway.local_calls[-1].turns} == {"turn-1"}
    assert len(service.active("char-b")) == 1
    h.now += timedelta(seconds=599)
    worker.process("char-b", "turn-1")
    assert len(h.gateway.local_calls) == 1
    if resolution == "timeout":
        h.now += timedelta(seconds=1)
    else:
        h.evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id="question"))
    h.gateway.output = (replace(memory(), id=NEW_ID, content="文脈を含めた結果", object="context"),)
    worker.process("char-b", "turn-1")
    assert len(h.gateway.local_calls) == 2
    assert {v.turn_id: v.text for v in h.gateway.local_calls[-1].turns} == {
        "turn-1": "うん、そうだよ",
        "question": TEXT,
    }
    assert [v.content for v in service.active("char-b")] == ["文脈を含めた結果"]


@pytest.mark.parametrize("status,end", [("partial", 9), ("none", 0)])
def test_late_context_receipt_withdraws_old_result_before_failed_reextraction_and_retry(
    status: str, end: int
) -> None:
    h, store, service, worker = setup_round_trip()
    h.now += timedelta(minutes=10)
    worker.process("char-b", "turn-1")
    assert service.active("char-b") == (memory(),)
    h.evidence.add_receipt(replace(receipt(status, end), turn_id="question"))
    app = FastAPI()
    app.include_router(memory_router(lambda: service))
    with TestClient(app) as client:
        assert client.get("/characters/char-b/memories").json() == []
        h.gateway.fail = True
        worker.process("char-b", "turn-1")
        assert service.active("char-b") == ()
        assert client.get("/characters/char-b/memories").json() == []
        old = store.get_memory("char-b", OLD_ID)
        assert old is not None and old.superseded_at is not None
        h.gateway.fail = False
        h.now += timedelta(seconds=1)
        corrected = replace(memory(), id=NEW_ID, content="配達範囲に沿う結果", object="corrected")
        h.gateway.output = (corrected,)
        worker.process("char-b", "turn-1")
        assert service.active("char-b") == (corrected,)
        assert [v["content"] for v in client.get("/characters/char-b/memories").json()] == [
            corrected.content
        ]
    assert {v.turn_id: v.text for v in h.gateway.local_calls[-1].turns} == (
        {"turn-1": "うん、そうだよ", "question": TEXT[:end]}
        if end
        else {"turn-1": "うん、そうだよ"}
    )


def test_context_receipt_during_model_call_discards_output_and_concurrent_duplicate() -> None:
    h, store, service, worker = setup_round_trip()
    h.now += timedelta(minutes=10)
    started, release = Event(), Event()
    original = h.gateway.extract_local

    def blocked(value: LocalOnly) -> tuple[DerivedMemory, ...] | ExtractionProposal:
        started.set()
        assert release.wait(3)
        return original(value)

    h.gateway.extract_local = blocked  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(worker.process, "char-b", "turn-1")
        try:
            assert started.wait(2)
            worker.process("char-b", "turn-1")
            h.evidence.add_receipt(replace(receipt("none", 0), turn_id="question"))
        finally:
            release.set()
        pending.result(timeout=3)
    assert service.active("char-b") == ()
    assert store.values == {}
    assert len(h.gateway.local_calls) == 1
    h.gateway.extract_local = original  # type: ignore[method-assign]
    worker.process("char-b", "turn-1")
    assert service.active("char-b") == (memory(),)
    assert {v.turn_id for v in h.gateway.local_calls[-1].turns} == {"turn-1"}


def test_http_read_keeps_context_state_and_memory_in_the_same_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.evidence.materials import MaterialDecision

    h, _, service, worker = setup_round_trip()
    h.now += timedelta(minutes=10)
    worker.process("char-b", "turn-1")
    assert service.active("char-b") == (memory(),)
    h.evidence.add_receipt(replace(receipt("partial", 9), turn_id="question"))
    captured, release = Event(), Event()
    original = h.materials.get_decision

    def pause_once(character: str, turn_id: str) -> MaterialDecision | None:
        if not captured.is_set():
            captured.set()
            assert release.wait(3)
        return original(character, turn_id)

    monkeypatch.setattr(h.materials, "get_decision", pause_once)
    app = FastAPI()
    app.include_router(memory_router(lambda: service))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.get, "/characters/char-b/memories")
        try:
            assert captured.wait(2)
            corrected = replace(memory(), id=NEW_ID, content="補正後の結果", object="corrected")
            h.gateway.output = (corrected,)
            worker.process("char-b", "turn-1")
            assert service.active("char-b") == (corrected,)
        finally:
            release.set()
        response = pending.result(timeout=3)
        assert response.is_success
        assert response.json() == []
        assert [v["content"] for v in client.get("/characters/char-b/memories").json()] == [
            corrected.content
        ]


@pytest.mark.parametrize("actor,expected", [("user-a", 1), ("user-z", 2)])
def test_restatement_rebuild_counts_speakers_without_changing_trust_or_other_features(
    actor: str, expected: int
) -> None:
    h, store, service, worker = setup_worker()
    h.evidence.add_turn(turn(actor="user-a"))
    h.gateway.output = (memory(),)
    worker.process("char-b", "turn-1")
    before = service.active("char-b")[0]
    happened = OCCURRED + timedelta(hours=1)
    h.evidence.add_turn(
        replace(
            turn(
                turn_id="second",
                actor=actor,
                kind="utterance" if actor == "user-a" else "observation",
            ),
            occurred_at=happened,
        )
    )
    h.gateway.output = (
        replace(memory(), id=NEW_ID, source_turn_ids=("second",), valid_from=happened),
    )
    worker.process("char-b", "second")
    for _ in range(2):
        with store.transaction("char-b") as tx:
            service.rebuild(tx)
        (saved,) = service.active("char-b")
        assert saved.id == before.id
        assert set(saved.source_turn_ids) == {"turn-1", "second"}
        assert saved.last_reinforced_at == happened
        assert saved.epistemics.independent_evidence_count == expected
        assert replace(saved.epistemics, independent_evidence_count=1) == before.epistemics


@pytest.mark.parametrize("origin", ["baseline", "candidate"])
def test_rebuild_recomputes_single_value_from_remaining_sources(origin: str) -> None:
    h, store, service, worker = setup_worker()
    h.evidence.add_turn(turn(turn_id="second", actor="user-a"))
    h.evidence.add_turn(turn(actor="user-a", reply_to_turn_id="second"))
    value = replace(
        memory(),
        source_turn_ids=("turn-1", "second"),
        epistemics=replace(memory().epistemics, independent_evidence_count=8),
    )
    if origin == "candidate":
        h.gateway.output = ()
        worker.process("char-b", "turn-1")
    with store.transaction("char-b") as tx:
        if origin == "baseline":
            tx.add_memory(value)
        else:
            tx.set_candidates("turn-1", (value,))
        service.rebuild(tx)
    saved = store.get_memory("char-b", OLD_ID)
    assert saved is not None
    assert saved.epistemics.independent_evidence_count == 1
    assert replace(saved.epistemics, independent_evidence_count=8) == value.epistemics
    remaining = replace(
        value, id=OLD_ID if origin == "baseline" else NEW_ID, source_turn_ids=("second",)
    )
    with store.transaction("char-b") as tx:
        if origin == "baseline":
            tx.replace_memory(remaining)
        else:
            tx.set_candidates("turn-1", (remaining,))
        service.rebuild(tx)
    saved = store.get_memory("char-b", remaining.id)
    assert saved is not None and saved.source_turn_ids == ("second",)
    assert saved.epistemics.independent_evidence_count == 1
