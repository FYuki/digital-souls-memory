from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from digital_souls_memory.evidence.provision import ResidentCandidateScore
from digital_souls_memory.identity.domain import PersonalityProfile, Temperament
from digital_souls_memory.voice.domain import VoiceExemplar
from tests.evidence_support import RECORDED, STARTED, TEXT, receipt, session, turn
from tests.identity_support import ASPECT_KEYS, aspects
from tests.it1.test_recall import request as recall_request
from tests.voice_support import exemplar

if TYPE_CHECKING:
    from tests.recall_support import RecallHarness

pytestmark = pytest.mark.it1
ROOT = "/characters/char-b/resident"
REQUEST = {"session_id": "session-1"}


@pytest.fixture
def resident(harness: "RecallHarness") -> "RecallHarness":
    harness.evidence.sessions["session-1"] = replace(session(reports_delivery=False), ended_at=None)
    harness.identity.temperaments[("char-b", 1)] = Temperament("char-b", "合成気質", 1, STARTED)
    harness.identity.profiles[("char-b", 1)] = PersonalityProfile(
        character_id="char-b",
        aspects=aspects(),
        description="合成人格の記述",
        renderer_version="synthetic-v1",
        temperament_version=1,
        version=1,
        valid_from=STARTED,
        valid_to=None,
        evidence_belief_ids=(),
        change_kind="author_seed",
    )
    return harness


def test_resident_returns_identity_descriptions_profile_voice_and_empty_motivations(
    resident: "RecallHarness",
) -> None:
    response = resident.client.post(ROOT, json=REQUEST)
    assert response.is_success
    body = response.json()
    assert body["core_identity"]["values"] == ["誠実"]
    assert body["temperament"]["description"] == "合成気質"
    assert body["personality_profile"]["description"] == "合成人格の記述"
    assert body["character_profile"]["entries"] == []
    assert body["motivations"] == []
    assert body["voice_exemplars"] == []
    assert (body["temperament_version"], body["personality_version"]) == (1, 1)
    assert set(body) == {
        "provision_id",
        "core_identity",
        "temperament",
        "personality_profile",
        "character_profile",
        "motivations",
        "voice_exemplars",
        "temperament_version",
        "personality_version",
    }
    profile = body["personality_profile"]
    assert profile == {
        "description": "合成人格の記述",
        "version": 1,
        "renderer_version": "synthetic-v1",
    }
    assert "aspects" not in profile
    assert set(profile).isdisjoint(ASPECT_KEYS)


@pytest.mark.parametrize("first", ["resident", "recall"])
def test_resident_and_recall_share_selection_across_new_versions_and_next_session(
    resident: "RecallHarness", first: str
) -> None:
    h = resident
    path, payload = (
        (ROOT, REQUEST) if first == "resident" else ("/characters/char-b/recall", recall_request())
    )
    initial = h.client.post(path, json=payload)
    assert initial.is_success
    assert initial.json()["personality_version"] == 1
    h.now += timedelta(days=1)
    old = h.identity.profiles[("char-b", 1)]
    h.identity.profiles[("char-b", 1)] = replace(old, valid_to=h.now)
    h.identity.profiles[("char-b", 2)] = replace(old, version=2, valid_from=h.now)
    h.identity.temperaments[("char-b", 2)] = Temperament("char-b", "新版気質", 2, h.now)
    for path, payload in ((ROOT, REQUEST), ("/characters/char-b/recall", recall_request())):
        response = h.client.post(path, json=payload)
        assert response.is_success
        assert (response.json()["temperament_version"], response.json()["personality_version"]) == (
            1,
            1,
        )
    h.evidence.add_session(
        replace(session("next-session", reports_delivery=False), started_at=h.now, ended_at=None)
    )
    for path, payload in ((ROOT, REQUEST), ("/characters/char-b/recall", recall_request())):
        response = h.client.post(path, json=payload | {"session_id": "next-session"})
        assert response.is_success
        assert (response.json()["temperament_version"], response.json()["personality_version"]) == (
            2,
            2,
        )


def test_resident_does_not_fill_a_saved_null_selection_from_later_registration(
    resident: "RecallHarness",
) -> None:
    h = resident
    h.identity.temperaments.clear()
    h.identity.profiles.clear()
    initial = h.client.post("/characters/char-b/recall", json=recall_request())
    assert initial.is_success
    assert initial.json()["temperament_version"] is None
    h.identity.temperaments[("char-b", 1)] = Temperament("char-b", "後日登録", 1, STARTED)
    response = h.client.post(ROOT, json=REQUEST)
    assert response.is_success
    assert response.json()["temperament_version"] is None
    assert response.json()["personality_version"] is None
    assert response.json()["temperament"] is None
    assert response.json()["personality_profile"] is None


def test_resident_records_a_new_id_alongside_an_existing_turn_provision(
    resident: "RecallHarness",
) -> None:
    h = resident
    h.next_id = "provision-turn-1"
    assert h.client.post("/characters/char-b/recall", json=recall_request()).is_success
    original = h.provisions.get("provision-turn-1")
    assert original is not None
    h.next_id = "provision-resident-1"
    response = h.client.post(ROOT, json=REQUEST)
    assert response.is_success
    body = response.json()
    assert body["provision_id"] == "provision-resident-1"
    record = h.provisions.get(body["provision_id"])
    assert record is not None
    assert (record.kind, record.character_id, record.session_id, record.surface) == (
        "resident",
        "char-b",
        "session-1",
        "synthetic-chat",
    )
    assert (record.temperament_version, record.personality_version) == (1, 1)
    assert record.query is None and record.turn_id is None
    assert record.motivation_ids == record.voice_exemplar_ids == ()
    assert record.provided_at == h.now
    assert record.duration_ms >= 0
    assert h.provisions.get("provision-turn-1") == original


def test_resident_id_collision_does_not_overwrite_a_turn_provision(
    resident: "RecallHarness",
) -> None:
    h = resident
    h.next_id = "provision-turn-1"
    assert h.client.post("/characters/char-b/recall", json=recall_request()).is_success
    h.next_id = "provision-resident-control"
    assert h.client.post(ROOT, json=REQUEST).is_success
    h.next_id = "provision-turn-1"
    before = h.provisions.records.copy()
    response = h.client.post(ROOT, json=REQUEST)
    assert not response.is_success
    assert h.provisions.records == before


@pytest.mark.parametrize("failure", ["insert", "commit"])
def test_resident_cannot_succeed_without_a_committed_record_and_can_retry(
    resident: "RecallHarness", failure: str
) -> None:
    h = resident
    h.provisions.fail_at = failure
    failed = h.client.post(ROOT, json=REQUEST)
    assert failed.status_code == 503
    assert h.provisions.records == {}
    h.provisions.fail_at = None
    response = h.client.post(ROOT, json=REQUEST)
    assert response.is_success
    assert h.provisions.get(response.json()["provision_id"]) is not None


def test_resident_returns_approved_voice_verbatim_and_records_its_id(
    resident: "RecallHarness",
) -> None:
    h = resident
    approved = replace(exemplar(), character_id="char-b")
    with h.voice.transaction("char-b") as tx:
        tx.add_exemplar(approved)
    response = h.client.post(ROOT, json=REQUEST)
    assert response.is_success
    body = response.json()
    assert [(v["exemplar_id"], v["text"]) for v in body["voice_exemplars"]] == [
        (approved.exemplar_id, approved.text)
    ]
    record = h.provisions.get(body["provision_id"])
    assert record is not None
    assert record.voice_exemplar_ids == (approved.exemplar_id,)
    assert body["voice_exemplars"][0]["register"] == dict(approved.register)


def test_resident_query_changes_voice_order_and_records_each_candidates_lexical_score(
    resident: "RecallHarness",
) -> None:
    h = resident
    with h.voice.transaction("char-b") as tx:
        for identifier, text in (("voice-a", "cd"), ("voice-z", "ab cd")):
            tx.add_exemplar(
                replace(exemplar(), character_id="char-b", exemplar_id=identifier, text=text)
            )
    for query, expected, scores in (
        ("ab cd", ("voice-z", "voice-a"), {"voice-a": 0.5, "voice-z": 1.0}),
        (None, ("voice-a", "voice-z"), {"voice-a": None, "voice-z": None}),
    ):
        h.next_id = "resident-query" if query is not None else "resident-no-query"
        response = h.client.post(
            ROOT, json=REQUEST | ({"query": query} if query is not None else {})
        )
        assert response.is_success
        assert tuple(v["exemplar_id"] for v in response.json()["voice_exemplars"]) == expected
        record = h.provisions.get(response.json()["provision_id"])
        assert record is not None and record.query == query
        assert record.voice_exemplar_ids == expected
        actual = {}
        for score in record.scores:
            assert isinstance(score, ResidentCandidateScore)
            actual[score.ref.identifier] = score.lexical
        assert actual == scores


def test_resident_withdraws_voice_after_receipt_correction_in_the_same_app(
    resident: "RecallHarness",
) -> None:
    h = resident
    h.evidence.sessions["session-1"] = replace(session(), ended_at=None)
    h.evidence.add_turn(turn())
    approved = replace(
        exemplar(), character_id="char-b", source="turn_id", turn_id="turn-1", text=TEXT
    )
    with h.voice.transaction("char-b") as tx:
        tx.add_exemplar(approved)
    h.now = RECORDED + timedelta(minutes=10)
    first = h.client.post(ROOT, json=REQUEST)
    assert first.is_success
    assert [v["text"] for v in first.json()["voice_exemplars"]] == [TEXT]
    original_record = h.provisions.get(first.json()["provision_id"])
    h.evidence.add_receipt(receipt("partial", 3))
    following = h.client.post(ROOT, json=REQUEST)
    assert following.is_success
    assert following.json()["voice_exemplars"] == []
    record = h.provisions.get(following.json()["provision_id"])
    assert record is not None and record.voice_exemplar_ids == ()
    assert h.provisions.get(first.json()["provision_id"]) == original_record
    assert h.evidence.get_turn("turn-1") == turn()


def test_resident_does_not_inherit_voice_from_another_character(
    resident: "RecallHarness",
) -> None:
    with resident.voice.transaction("char-a") as tx:
        tx.add_exemplar(exemplar())
    response = resident.client.post(ROOT, json=REQUEST)
    assert response.is_success
    assert response.json()["voice_exemplars"] == []


def test_resident_missing_session_cannot_create_a_provision(resident: "RecallHarness") -> None:
    assert resident.client.post(ROOT, json=REQUEST).is_success
    before = resident.provisions.records.copy()
    response = resident.client.post(ROOT, json={"session_id": "missing"})
    assert response.status_code == 404
    assert resident.provisions.records == before


def test_resident_validates_required_session_before_provision(resident: "RecallHarness") -> None:
    response = resident.client.post(ROOT, json={})
    assert response.status_code == 422
    assert resident.provisions.records == {}


def test_voice_correction_between_selection_and_snapshot_cannot_succeed(
    resident: "RecallHarness",
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = resident
    h.evidence.sessions["session-1"] = replace(session(), ended_at=None)
    h.evidence.add_turn(turn())
    h.now = RECORDED + timedelta(minutes=10)
    with h.voice.transaction("char-b") as tx:
        tx.add_exemplar(
            replace(
                exemplar(), character_id="char-b", source="turn_id", turn_id="turn-1", text=TEXT
            )
        )
    original = h.voice.list_exemplars

    @contextmanager
    def correct_after_selection(character_id: str) -> Iterator[Iterator[VoiceExemplar]]:
        with original(character_id) as values:
            yield values
        h.evidence.add_receipt(receipt("none", 0))

    monkeypatch.setattr(h.voice, "list_exemplars", correct_after_selection)
    assert h.client.post(ROOT, json=REQUEST).status_code == 503
    assert h.provisions.records == {}
    monkeypatch.setattr(h.voice, "list_exemplars", original)
    response = h.client.post(ROOT, json=REQUEST)
    assert response.is_success and response.json()["voice_exemplars"] == []
