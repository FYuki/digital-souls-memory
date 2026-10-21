import json
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from digital_souls_memory.evidence.domain import Turn
from tests.evidence_support import INSTRUCTION, RECORDED, TEXT, receipt, session, turn
from tests.memory_support import memory

if TYPE_CHECKING:
    from tests.recall_support import RecallHarness

pytestmark = pytest.mark.it1


def request(*, form: str = "materials") -> dict[str, object]:
    return {
        "session_id": "session-1",
        "format": form,
        "query": "日本語",
        "sensitivity": "synthetic-sensitive",
        "classifier_version": "synthetic-v1",
    }


def test_recall_without_dependencies_is_unavailable_and_health_stays_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMORY_DATABASE_URL", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/characters/char-b/recall", json=request()).status_code == 503


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_http_returns_original_spans_in_both_formats_and_records_exactly_what_was_sent(
    harness: "RecallHarness", form: str
) -> None:
    h = harness
    h.seed(turn(text=TEXT), replace(memory(), content="誤った翻訳と日付"))
    response = h.client.post("/characters/char-b/recall", json=request(form=form))
    assert response.status_code == 200
    body = response.json()
    assert len(body["memories"]) == 1
    spans = body["source_spans"]
    assert [(s["turn_id"], s["text"], s["actor"], s["language"]) for s in spans] == [
        ("turn-1", TEXT, "char-c", "ja")
    ]
    for span in spans:
        assert 0 <= span["start"] < span["end"] <= len(TEXT)
        assert span["text"] == TEXT[span["start"] : span["end"]]
    if form == "assembled":
        assert TEXT in body["context"]
    h.assert_record_matches(body)
    assert h.provisions.records[body["provision_id"]].query == "日本語"


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_empty_search_saves_a_new_provision_with_empty_collections(
    harness: "RecallHarness", form: str
) -> None:
    h = harness
    h.next_id = "provision-new"
    response = h.client.post("/characters/char-b/recall", json=request(form=form))
    assert response.status_code == 200
    body = response.json()
    assert body["provision_id"] == "provision-new"
    assert body["memories"] == body["source_spans"] == body["verbatim_anchors"] == []
    h.assert_record_matches(body)
    assert h.provisions.records["provision-new"].motivation_ids == ()


def test_existing_provision_id_is_not_overwritten_or_reused_for_a_new_success(
    harness: "RecallHarness",
) -> None:
    h = harness
    h.next_id = "provision-existing"
    first = h.client.post("/characters/char-b/recall", json=request())
    assert first.status_code == 200
    before = dict(h.provisions.records)
    h.seed(turn(), memory())
    second = h.client.post("/characters/char-b/recall", json=request())
    assert h.provisions.records["provision-existing"] == before["provision-existing"]
    assert second.status_code == 409
    assert h.provisions.records == before


@pytest.mark.parametrize("failure", ["insert", "commit"])
def test_record_failure_never_returns_success_and_retry_records_once(
    harness: "RecallHarness", failure: str
) -> None:
    h = harness
    h.provisions.fail_at = failure
    response = h.client.post("/characters/char-b/recall", json=request())
    assert not response.is_success
    assert response.status_code == 503
    assert h.provisions.records == {}
    h.provisions.fail_at = None
    retry = h.client.post("/characters/char-b/recall", json=request())
    assert retry.is_success
    h.assert_record_matches(retry.json())
    assert len(h.provisions.records) == 1


@pytest.mark.parametrize(
    "changes,remove",
    [
        ({}, ("query",)),
        ({"turn_id": "turn-1"}, ()),
        ({"format": "unknown"}, ()),
        ({"query": "  "}, ()),
        ({}, ("session_id",)),
        ({}, ("sensitivity",)),
        ({}, ("classifier_version",)),
        ({"as_of": RECORDED.isoformat()}, ()),
        ({"unexpected": True}, ()),
    ],
)
def test_invalid_requests_are_rejected_before_search_or_provision(
    harness: "RecallHarness", changes: dict[str, object], remove: tuple[str, ...]
) -> None:
    h = harness
    payload = request() | changes
    for field in remove:
        del payload[field]
    response = h.client.post("/characters/char-b/recall", json=payload)
    assert response.status_code == 422
    assert h.retriever.calls == []
    assert h.provisions.records == {}


def test_turn_query_uses_the_permitted_original_and_its_saved_sensitivity(
    harness: "RecallHarness",
) -> None:
    h = harness
    h.seed(turn(), memory())
    payload = {"session_id": "session-1", "format": "materials", "turn_id": "turn-1"}
    response = h.client.post("/characters/char-b/recall", json=payload)
    assert response.is_success
    value = h.embedding.calls[-1][0]
    assert value.texts == (TEXT,)
    assert [(v.sensitivity, v.classifier_version) for v in value.labels] == [
        ("synthetic-sensitive", "synthetic-v1")
    ]
    h.assert_record_matches(response.json())
    assert h.provisions.records[response.json()["provision_id"]].turn_id == "turn-1"


@pytest.mark.parametrize("identifier", ["missing", "private"])
def test_missing_or_unperceived_origin_turn_does_not_reach_models(
    harness: "RecallHarness", identifier: str
) -> None:
    h = harness
    h.evidence.add_turn(turn(turn_id="private", perceived_by=("char-c",)))
    response = h.client.post(
        "/characters/char-b/recall",
        json={"session_id": "session-1", "format": "materials", "turn_id": identifier},
    )
    assert response.status_code == 404
    assert h.embedding.calls == []
    assert h.provisions.records == {}


@pytest.mark.parametrize(
    "source,reader,allowed",
    [
        (turn(), "char-b", True),
        (turn(), "char-c", True),
        (turn(), "char-a", False),
        (turn(actor="user-a", perceived_by=()), "char-b", False),
        (
            turn(perceived_by=("char-c",), address_mode="direct", addressees=("user-a",)),
            "char-b",
            False,
        ),
        (turn(address_mode="direct", addressees=("char-b",)), "char-b", True),
        (
            turn(perceived_by=("char-c",), address_mode="direct", addressees=("char-b",)),
            "char-b",
            False,
        ),
        (turn(kind="thought", perceived_by=("char-c",), address_mode="self"), "char-b", False),
        (turn(kind="thought", perceived_by=("char-c",), address_mode="self"), "char-c", True),
        (turn(address_mode="self"), "char-b", True),
    ],
)
def test_recall_obeys_perception_including_self_speech_whispers_and_thought(
    harness: "RecallHarness", source: Turn, reader: str, allowed: bool
) -> None:
    h = harness
    h.seed(source, replace(memory(), character_id=reader))
    response = h.client.post(f"/characters/{reader}/recall", json=request())
    assert response.is_success
    body = response.json()
    assert [s["turn_id"] for s in body["source_spans"]] == ([source.turn_id] if allowed else [])
    assert [m["id"] for m in body["memories"]] == ([memory().id] if allowed else [])


@pytest.mark.parametrize("kind", ["fact", "preference"])
@pytest.mark.parametrize(
    "shared,perceived,allowed", [(True, True, True), (False, True, False), (True, False, False)]
)
def test_sharing_requires_both_memory_permission_and_source_perception(
    harness: "RecallHarness", kind: str, shared: bool, perceived: bool, allowed: bool
) -> None:
    h = harness
    h.seed(
        turn(perceived_by=("char-b", "char-c") if perceived else ("char-c",)),
        replace(memory(kind), character_id="char-c", shared_with=("char-b",) if shared else ()),
    )
    response = h.client.post("/characters/char-b/recall", json=request())
    assert response.is_success
    assert [m["id"] for m in response.json()["memories"]] == ([memory().id] if allowed else [])


@pytest.mark.parametrize("state", ["expired", "superseded", "future"])
def test_inactive_memories_are_not_presented_as_current_facts(
    harness: "RecallHarness", state: str
) -> None:
    h = harness
    value = memory()
    if state == "expired":
        value = replace(value, valid_to=RECORDED)
    elif state == "superseded":
        value = replace(value, superseded_at=RECORDED)
    else:
        value = replace(value, valid_from=RECORDED + timedelta(days=1))
    h.seed(turn(), value)
    response = h.client.post("/characters/char-b/recall", json=request())
    assert response.is_success
    assert response.json()["memories"] == response.json()["source_spans"] == []


def test_same_app_follows_pending_timeout_and_late_receipts_without_exposing_old_text(
    harness: "RecallHarness",
) -> None:
    from tests.recall_fixtures import candidate_for

    h = harness
    h.evidence.sessions["session-1"] = session(reports_delivery=True)
    h.seed(turn(), memory())
    for elapsed in (0, 599, 600):
        h.now = RECORDED + timedelta(seconds=elapsed)
        response = h.client.post("/characters/char-b/recall", json=request())
        assert response.is_success
        assert [s["text"] for s in response.json()["source_spans"]] == (
            [TEXT] if elapsed == 600 else []
        )
    h.evidence.add_receipt(receipt("partial", 9))
    response = h.client.post("/characters/char-b/recall", json=request())
    assert response.is_success
    assert response.json()["memories"] == response.json()["source_spans"] == []
    h.evidence.add_receipt(receipt("none", 0))
    response = h.client.post("/characters/char-b/recall", json=request())
    assert response.is_success
    assert response.json()["memories"] == response.json()["source_spans"] == []
    h.retriever.values = [
        candidate_for(replace(memory(), character_id="char-c"), sources=(turn(),))
    ]
    own = h.client.post("/characters/char-c/recall", json=request())
    assert own.is_success
    assert [s["text"] for s in own.json()["source_spans"]] == [TEXT]


def test_repeated_recall_preserves_reinforcement_sources_trust_and_identity(
    harness: "RecallHarness",
) -> None:
    h = harness
    h.seed(turn(text=INSTRUCTION), replace(memory(), content=INSTRUCTION))
    before = (
        tuple(h.retriever.values),
        h.identity.get_current("char-b", h.now),
        dict(h.evidence.turns),
    )
    ids = []
    for _ in range(3):
        response = h.client.post("/characters/char-b/recall", json=request())
        assert response.is_success
        assert [s["text"] for s in response.json()["source_spans"]] == [INSTRUCTION]
        ids.append(response.json()["provision_id"])
    assert len(set(ids)) == 3
    assert (
        tuple(h.retriever.values),
        h.identity.get_current("char-b", h.now),
        dict(h.evidence.turns),
    ) == before


def test_query_dependency_failure_is_visible_and_does_not_create_a_provision(
    harness: "RecallHarness",
) -> None:
    h = harness
    h.embedding.fail = True
    failed = h.client.post("/characters/char-b/recall", json=request())
    assert not failed.is_success
    assert h.provisions.records == {}
    h.embedding.fail = False
    assert h.client.post("/characters/char-b/recall", json=request()).is_success


def test_same_session_keeps_selected_personality_after_new_version_and_next_session_selects_it(
    harness: "RecallHarness",
) -> None:
    from digital_souls_memory.identity.domain import PersonalityProfile, Temperament
    from tests.evidence_support import STARTED
    from tests.identity_support import aspects

    h = harness
    h.identity.temperaments[("char-b", 1)] = Temperament("char-b", "合成気質", 1, STARTED)
    profile = PersonalityProfile(
        character_id="char-b",
        aspects=aspects(),
        description="合成人格",
        renderer_version="synthetic-v1",
        temperament_version=1,
        version=1,
        valid_from=STARTED,
        valid_to=None,
        evidence_belief_ids=(),
        change_kind="author_seed",
    )
    h.identity.profiles[("char-b", 1)] = profile
    first = h.client.post("/characters/char-b/recall", json=request())
    assert first.is_success
    assert first.json()["personality_version"] == 1
    h.now += timedelta(days=1)
    h.identity.profiles[("char-b", 1)] = replace(profile, valid_to=h.now)
    h.identity.profiles[("char-b", 2)] = replace(profile, version=2, valid_from=h.now)
    same = h.client.post("/characters/char-b/recall", json=request())
    assert same.is_success
    assert same.json()["personality_version"] == 1
    h.assert_record_matches(same.json())
    h.evidence.add_session(
        replace(session("next-session", reports_delivery=False), started_at=h.now, ended_at=None)
    )
    following = h.client.post(
        "/characters/char-b/recall", json=request() | {"session_id": "next-session"}
    )
    assert following.is_success
    assert following.json()["personality_version"] == 2


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_registered_anchor_is_returned_verbatim_and_recorded_with_its_source(
    harness: "RecallHarness", form: str
) -> None:
    from tests.recall_fixtures import anchor_candidate
    from tests.voice_support import anchor

    h = harness
    h.seed(turn(), memory())
    value = replace(anchor(), character_id="char-b")
    with h.voice.transaction("char-b") as tx:
        tx.add_anchor(value)
    h.retriever.anchors.append(anchor_candidate(value, source=turn()))
    response = h.client.post("/characters/char-b/recall", json=request(form=form))
    assert response.is_success
    assert [
        (a["anchor_id"], a["turn_id"], a["text"]) for a in response.json()["verbatim_anchors"]
    ] == [(value.anchor_id, "turn-1", TEXT)]
    h.assert_record_matches(response.json())


def test_missing_source_is_not_replaced_with_the_derived_text(harness: "RecallHarness") -> None:
    from tests.recall_fixtures import candidate_for

    h = harness
    h.retriever.values.append(candidate_for(memory(), sources=(turn(),)))
    response = h.client.post("/characters/char-b/recall", json=request())
    assert response.is_success
    assert response.json()["memories"] == response.json()["source_spans"] == []
    h.assert_record_matches(response.json())


def test_receipt_during_reranking_cannot_commit_the_previously_available_text(
    harness: "RecallHarness", monkeypatch: pytest.MonkeyPatch
) -> None:
    from digital_souls_memory.recall.domain import RerankInput, RerankScore

    h = harness
    h.evidence.sessions["session-1"] = session(reports_delivery=True)
    h.now += timedelta(minutes=10)
    h.seed(turn(), memory())
    original_rerank = h.reranker.rerank

    def correct_delivery(value: RerankInput) -> tuple[RerankScore, ...]:
        h.evidence.add_receipt(receipt("none", 0))
        return original_rerank(value)

    monkeypatch.setattr(h.reranker, "rerank", correct_delivery)
    response = h.client.post("/characters/char-b/recall", json=request())
    assert h.reranker.calls
    assert response.status_code == 503
    assert h.provisions.records == {}


def test_session_persists_an_absent_personality_selection_after_later_registration(
    harness: "RecallHarness",
) -> None:
    from digital_souls_memory.identity.domain import Temperament

    h = harness
    first = h.client.post("/characters/char-b/recall", json=request())
    assert first.is_success
    assert first.json()["temperament_version"] is None
    assert first.json()["personality_version"] is None
    h.identity.temperaments[("char-b", 1)] = Temperament("char-b", "後日登録した気質", 1, h.now)
    again = h.client.post("/characters/char-b/recall", json=request())
    assert again.is_success
    assert again.json()["temperament_version"] is None
    assert again.json()["personality_version"] is None
    h.assert_record_matches(again.json())


def test_session_selects_temperament_at_its_start_instead_of_latest_registration(
    harness: "RecallHarness",
) -> None:
    from digital_souls_memory.identity.domain import Temperament
    from tests.evidence_support import STARTED

    h = harness
    h.identity.temperaments[("char-b", 1)] = Temperament("char-b", "開始時の気質", 1, STARTED)
    h.identity.temperaments[("char-b", 2)] = Temperament("char-b", "開始後の気質", 2, RECORDED)
    response = h.client.post("/characters/char-b/recall", json=request())
    assert response.is_success
    assert response.json()["temperament_version"] == 1
    h.assert_record_matches(response.json())


def test_sensitive_source_labels_survive_public_query_reranking_and_final_json(
    harness: "RecallHarness",
) -> None:
    from digital_souls_memory.recall.inputs import LocalOnly

    h = harness
    source = turn(sensitivity="private-source", classifier_version="source-v2")
    h.seed(source, memory())
    response = h.client.post(
        "/characters/char-b/recall",
        json=request()
        | {
            "sensitivity": "public",
            "classifier_version": "query-v3",
        },
    )
    assert response.is_success
    rerank = h.reranker.calls[-1]
    assert isinstance(rerank.documents[0].input, LocalOnly)
    assert ("private-source", "source-v2") in {
        (v.sensitivity, v.classifier_version) for v in rerank.documents[0].input.labels
    }
    final_inputs = [
        value for value in h.counter.inputs if json.loads(value.texts[0]) == response.json()
    ]
    assert final_inputs
    for value in final_inputs:
        assert isinstance(value, LocalOnly)
        assert {(v.sensitivity, v.classifier_version) for v in value.labels} >= {
            ("public", "query-v3"),
            ("private-source", "source-v2"),
        }


def test_final_save_snapshot_covers_returned_document_and_origin_dependencies(
    harness: "RecallHarness",
) -> None:
    h = harness
    h.seed(turn(), memory())
    h.evidence.add_turn(turn(turn_id="origin", actor="user-a"))
    response = h.client.post(
        "/characters/char-b/recall",
        json={
            "session_id": "session-1",
            "turn_id": "origin",
            "format": "materials",
        },
    )
    assert response.is_success
    snapshot = h.provisions.snapshots[-1]
    assert snapshot.documents == (h.retriever.values[0].document,)
    assert {v.turn_id for v in snapshot.sources} == {"turn-1", "origin"}
    assert (snapshot.character_id, snapshot.session_id) == ("char-b", "session-1")
    assert (snapshot.temperament_version, snapshot.personality_version) == (
        response.json()["temperament_version"],
        response.json()["personality_version"],
    )


def test_receipt_change_inside_save_is_rejected_without_research_or_partial_record(
    harness: "RecallHarness",
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.evidence.provision import ProvisionRecord
    from digital_souls_memory.recall.domain import ProvisionSnapshot

    h = harness
    h.evidence.sessions["session-1"] = session(reports_delivery=True)
    h.now += timedelta(minutes=10)
    h.seed(turn(), memory())
    original_save = h.provisions.save

    def changed(record: ProvisionRecord, *, snapshot: ProvisionSnapshot) -> None:
        h.evidence.add_receipt(receipt("none", 0))
        original_save(record, snapshot=snapshot)

    monkeypatch.setattr(h.provisions, "save", changed)
    response = h.client.post("/characters/char-b/recall", json=request())
    assert response.status_code == 503
    assert h.provisions.records == {}
    assert len(h.retriever.calls) == 1


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_anchor_candidate_can_be_returned_without_a_derived_memory(
    harness: "RecallHarness",
    form: str,
) -> None:
    from tests.recall_fixtures import anchor_candidate
    from tests.voice_support import anchor

    h = harness
    h.evidence.add_turn(turn())
    value = replace(anchor(), character_id="char-b")
    h.retriever.values.append(anchor_candidate(value, source=turn()))
    response = h.client.post("/characters/char-b/recall", json=request(form=form))
    assert response.is_success
    body = response.json()
    assert body["memories"] == []
    assert [(v["anchor_id"], v["turn_id"], v["text"]) for v in body["verbatim_anchors"]] == [
        (value.anchor_id, "turn-1", TEXT),
    ]
    assert {v["turn_id"] for v in body["source_spans"]} == {"turn-1"}
    h.assert_record_matches(body)
