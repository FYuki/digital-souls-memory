import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.identity.domain import PersonalityProfile, Temperament
from digital_souls_memory.recall.inputs import LocalOnly
from tests.evidence_support import INSTRUCTION, RECORDED, TEXT, receipt, session, turn
from tests.identity_support import aspects
from tests.memory_support import memory
from tests.recall_fixtures import candidate_for
from tests.recall_support import RecallHarness
from tests.resident_profile_support import (
    ControlledCounter,
    FakeProfileStore,
    StoredCandidates,
    profile_client,
)

pytestmark = pytest.mark.it1
PROFILE = "/characters/char-b/profile"
RESIDENT = "/characters/char-b/resident"
REQUEST = {"session_id": "session-1"}


def entry(*, end: int, entry_id: str = "profile-1") -> dict[str, object]:
    return {"entry_id": entry_id, "kind": "turn", "turn_id": "turn-1", "start": 0, "end": end}


@pytest.fixture
def configured(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[
    tuple[
        RecallHarness,
        FakeProfileStore,
        StoredCandidates,
        ControlledCounter,
        Callable[[], TestClient],
    ]
]:
    for name in ("MEMORY_DATABASE_URL", "MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    h = RecallHarness()
    h.evidence.sessions["session-1"] = replace(session(reports_delivery=False), ended_at=None)
    h.evidence.add_turn(turn())
    profiles, candidates, counter = FakeProfileStore(), StoredCandidates(), ControlledCounter()
    try:
        yield (
            h,
            profiles,
            candidates,
            counter,
            lambda: profile_client(h, profiles, candidates, counter),
        )
    finally:
        h.client.close()


# The client factory is invoked in the test body so a missing production injection
# is reported as that test's failure, rather than a collection or fixture error.
type Configured = tuple[
    RecallHarness, FakeProfileStore, StoredCandidates, ControlledCounter, Callable[[], TestClient]
]


def test_profile_unconfigured_has_an_empty_zero_budget(configured: Configured) -> None:
    *_, make_client = configured
    with make_client() as client:
        response = client.post(RESIDENT, json=REQUEST)
        assert response.is_success
        profile = response.json()["character_profile"]
        assert profile["entries"] == []
        assert profile["token_limit"] == 0


def test_profile_exact_token_limit_roundtrips_unicode_without_rewriting(
    configured: Configured,
) -> None:
    h, _, _, counter, make_client = configured
    with make_client() as client:
        response = client.put(PROFILE, json={"token_limit": 7, "entries": [entry(end=len(TEXT))]})
        assert response.is_success
        resident = client.post(RESIDENT, json=REQUEST)
        assert resident.is_success
        profile = resident.json()["character_profile"]
        assert profile["token_limit"] == 7
        assert response.json() == profile
        assert set(profile) == {"character_id", "token_limit", "entries", "version"}
        assert profile["entries"][0] == {
            "entry_id": "profile-1",
            "kind": "turn",
            "text": TEXT,
            "owner_id": None,
            "memory_id": None,
            "source_spans": [
                {
                    "turn_id": "turn-1",
                    "start": 0,
                    "end": len(TEXT),
                    "text": TEXT,
                    "actor": "char-c",
                    "language": "ja",
                    "truncated": False,
                    "original_start": 0,
                    "original_end": len(TEXT),
                }
            ],
        }
        assert [(v["entry_id"], v["text"]) for v in profile["entries"]] == [("profile-1", TEXT)]
        assert counter.inputs
        assert all(isinstance(value, LocalOnly) for value in counter.inputs)
        counted = json.loads(counter.inputs[-1].texts[0])
        assert counted == profile["entries"]
        assert "日本語" in counter.inputs[-1].texts[0]
        assert "🙂" in counter.inputs[-1].texts[0]
        assert h.evidence.get_turn("turn-1") == turn()


@pytest.mark.parametrize("failure", ["over-budget", "tokenizer"])
def test_profile_failed_replacement_preserves_the_previous_configuration(
    configured: Configured, failure: str
) -> None:
    _, profiles, _, counter, make_client = configured
    with make_client() as client:
        assert client.put(PROFILE, json={"token_limit": 7, "entries": [entry(end=3)]}).is_success
        before = profiles.values.copy()
        if failure == "over-budget":
            counter.tokens = 8
        else:
            counter.fail = True
        failed = client.put(PROFILE, json={"token_limit": 7, "entries": [entry(end=4)]})
        assert not failed.is_success
        if failure == "over-budget":
            assert failed.status_code == 422
        assert profiles.values == before
        counter.fail, counter.tokens = False, 7
        assert client.put(PROFILE, json={"token_limit": 7, "entries": [entry(end=4)]}).is_success


@pytest.mark.parametrize(
    "change",
    [
        {"turn_id": "missing"},
        {"start": -1},
        {"end": 0},
        {"end": len(TEXT) + 1},
        {"text": "unregistered replacement"},
    ],
)
def test_profile_rejects_untraceable_or_out_of_range_entries_atomically(
    configured: Configured, change: dict[str, object]
) -> None:
    _, profiles, _, _, make_client = configured
    with make_client() as client:
        assert client.put(PROFILE, json={"token_limit": 7, "entries": [entry(end=3)]}).is_success
        before = profiles.values.copy()
        response = client.put(
            PROFILE,
            json={
                "token_limit": 7,
                "entries": [entry(end=3), entry(end=4, entry_id="invalid") | change],
            },
        )
        assert not response.is_success
        assert profiles.values == before


def test_profile_rejects_duplicate_entry_ids_without_replacing_configuration(
    configured: Configured,
) -> None:
    _, profiles, _, _, make_client = configured
    with make_client() as client:
        response = client.put(
            PROFILE, json={"token_limit": 7, "entries": [entry(end=3), entry(end=4)]}
        )
        assert response.status_code == 422
        assert profiles.values == {}


def test_profile_does_not_inherit_the_turn_recall_five_entry_limit(configured: Configured) -> None:
    *_, make_client = configured
    entries = [entry(end=3, entry_id=f"profile-{number}") for number in range(6)]
    with make_client() as client:
        assert client.put(PROFILE, json={"token_limit": 7, "entries": entries}).is_success
        response = client.post(RESIDENT, json=REQUEST)
        assert response.is_success
        assert {v["entry_id"] for v in response.json()["character_profile"]["entries"]} == {
            v["entry_id"] for v in entries
        }


@pytest.mark.parametrize(
    "source",
    [
        turn(perceived_by=("char-c",), addressees=("char-b",), address_mode="direct"),
        turn(actor="synthetic-user", perceived_by=()),
        turn(kind="thought", perceived_by=("char-c",)),
        turn(perceived_by=("char-c",), addressees=("user-a",), address_mode="direct"),
    ],
    ids=["addressee-without-perception", "no-characters", "other-thought", "user-whisper"],
)
def test_profile_cannot_be_configured_from_unperceived_evidence(
    configured: Configured, source: object
) -> None:
    from digital_souls_memory.evidence.domain import Turn

    assert isinstance(source, Turn)
    h, profiles, _, _, make_client = configured
    h.evidence.turns["turn-1"] = source
    with make_client() as client:
        response = client.put(PROFILE, json={"token_limit": 7, "entries": [entry(end=3)]})
        assert not response.is_success
        assert profiles.values == {}


@pytest.mark.parametrize("kind,mode", [("utterance", "self"), ("thought", "broadcast")])
def test_profile_self_and_thought_use_the_actors_full_original_text(
    configured: Configured, kind: str, mode: str
) -> None:
    h, _, _, _, make_client = configured
    h.evidence.sessions["session-1"] = replace(session(), ended_at=None)
    h.evidence.turns["turn-1"] = turn(
        actor="char-b", kind=kind, address_mode=mode, perceived_by=("char-b",)
    )
    with make_client() as client:
        assert client.put(
            PROFILE, json={"token_limit": 7, "entries": [entry(end=len(TEXT))]}
        ).is_success
        response = client.post(RESIDENT, json=REQUEST)
        assert response.is_success
        assert [v["text"] for v in response.json()["character_profile"]["entries"]] == [TEXT]


def test_profile_saved_full_entry_is_withdrawn_on_partial_receipt_and_restored_on_full(
    configured: Configured,
) -> None:
    h, profiles, _, _, make_client = configured
    h.evidence.sessions["session-1"] = replace(session(), ended_at=None)
    h.now = RECORDED + timedelta(minutes=10)
    with make_client() as client:
        assert client.put(
            PROFILE, json={"token_limit": 7, "entries": [entry(end=len(TEXT))]}
        ).is_success
        first = client.post(RESIDENT, json=REQUEST)
        assert first.is_success
        assert [v["text"] for v in first.json()["character_profile"]["entries"]] == [TEXT]
        saved_config = profiles.values.copy()
        previous = h.provisions.records.copy()
        assert client.post(
            "/turns/turn-1/receipts",
            json={
                "status": "partial",
                "spoken_until": 3,
                "reported_by": "window",
                "reported_at": h.now.isoformat(),
            },
        ).is_success
        following = client.post(RESIDENT, json=REQUEST)
        assert following.is_success
        assert following.json()["character_profile"]["entries"] == []
        assert profiles.values == saved_config
        assert all(h.provisions.get(key) == value for key, value in previous.items())
        h.evidence.add_receipt(receipt("full", len(TEXT)))
        restored = client.post(RESIDENT, json=REQUEST)
        assert restored.is_success
        assert [v["text"] for v in restored.json()["character_profile"]["entries"]] == [TEXT]
        assert h.evidence.get_turn("turn-1") == turn()


def test_profile_unreceived_turn_waits_until_the_configured_delivery_deadline(
    configured: Configured,
) -> None:
    h, profiles, _, _, make_client = configured
    h.evidence.sessions["session-1"] = replace(session(), ended_at=None)
    with make_client() as client:
        assert not client.put(
            PROFILE, json={"token_limit": 7, "entries": [entry(end=3)]}
        ).is_success
        assert profiles.values == {}
        h.now = RECORDED + timedelta(minutes=10)
        assert client.put(PROFILE, json={"token_limit": 7, "entries": [entry(end=3)]}).is_success


def test_profile_resident_recount_failure_cannot_return_an_unrecorded_success(
    configured: Configured,
) -> None:
    h, _, _, counter, make_client = configured
    with make_client() as client:
        assert client.put(PROFILE, json={"token_limit": 7, "entries": [entry(end=3)]}).is_success
        counter.tokens = 8
        before = h.provisions.records.copy()
        assert client.post(RESIDENT, json=REQUEST).status_code == 503
        assert h.provisions.records == before
        counter.tokens = 7
        assert client.post(RESIDENT, json=REQUEST).is_success


def test_profile_instruction_content_preserves_identity_and_author_seed(
    configured: Configured,
) -> None:
    h, _, _, _, make_client = configured
    h.evidence.turns["turn-1"] = turn(text=INSTRUCTION)
    h.identity.temperaments[("char-b", 1)] = Temperament("char-b", "合成気質", 1, RECORDED)
    h.identity.profiles[("char-b", 1)] = PersonalityProfile(
        character_id="char-b",
        aspects=aspects(),
        description="合成人格の記述",
        renderer_version="synthetic-v1",
        temperament_version=1,
        version=1,
        valid_from=RECORDED,
        valid_to=None,
        evidence_belief_ids=(),
        change_kind="author_seed",
    )
    cores, temperaments, personalities = (
        h.identity.cores.copy(),
        h.identity.temperaments.copy(),
        h.identity.profiles.copy(),
    )
    with make_client() as client:
        assert client.put(
            PROFILE, json={"token_limit": 7, "entries": [entry(end=len(INSTRUCTION))]}
        ).is_success
        response = client.post(RESIDENT, json=REQUEST)
        assert response.is_success
        assert [v["text"] for v in response.json()["character_profile"]["entries"]] == [INSTRUCTION]
        assert h.identity.cores == cores
        assert h.identity.temperaments == temperaments
        assert h.identity.profiles == personalities


def test_profile_uses_saved_candidates_without_embedding_reranking_or_reinforcement(
    configured: Configured,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.memory.use_cases import MemoryService

    def reject_recomputation(*args: object, **kwargs: object) -> None:
        pytest.fail("Resident retrieval must use saved eligibility and indexes")

    monkeypatch.setattr(MemoryService, "active", reject_recomputation)
    monkeypatch.setattr(MemoryService, "rebuild", reject_recomputation)
    h, _, candidates, _, make_client = configured
    value = candidate_for(memory(), sources=(turn(),))
    candidates.values[value.ref] = value
    h.retriever.values.append(value)
    ref = {
        "entry_id": "profile-memory",
        "kind": "memory",
        "owner_id": "char-b",
        "memory_id": memory().id,
    }
    before = candidates.values.copy()
    with make_client() as client:
        assert client.put(PROFILE, json={"token_limit": 7, "entries": [ref]}).is_success
        response = client.post(RESIDENT, json=REQUEST)
        assert response.is_success
        assert [v["entry_id"] for v in response.json()["character_profile"]["entries"]] == [
            "profile-memory"
        ]
        saved_entry = response.json()["character_profile"]["entries"][0]
        assert (saved_entry["owner_id"], saved_entry["memory_id"]) == ("char-b", memory().id)
        assert set(saved_entry["source_spans"][0]) == {
            "turn_id",
            "start",
            "end",
            "text",
            "actor",
            "language",
            "truncated",
            "original_start",
            "original_end",
        }
        assert candidates.calls
        assert all(refs == (value.ref,) for refs in candidates.calls)
        assert candidates.values == before
        record = h.provisions.get(response.json()["provision_id"])
        assert record is not None and record.memory_ids == (memory().id,)
        assert h.embedding.calls == []
        assert h.reranker.calls == []
        assert h.retriever.calls == []


@pytest.mark.parametrize("unavailable", ["missing", "private"])
def test_profile_rejects_missing_or_private_saved_memory_without_replacing_configuration(
    configured: Configured, unavailable: str
) -> None:
    _, profiles, candidates, _, make_client = configured
    value = candidate_for(
        replace(memory(), character_id="char-c", shared_with=()), sources=(turn(),)
    )
    if unavailable == "private":
        candidates.values[value.ref] = value
    with make_client() as client:
        assert client.put(PROFILE, json={"token_limit": 7, "entries": [entry(end=3)]}).is_success
        before = profiles.values.copy()
        response = client.put(
            PROFILE,
            json={
                "token_limit": 7,
                "entries": [
                    {
                        "entry_id": "private",
                        "kind": "memory",
                        "owner_id": "char-c",
                        "memory_id": memory().id,
                    }
                ],
            },
        )
        assert not response.is_success
        assert profiles.values == before


def test_profile_withdraws_a_saved_entry_when_its_stored_candidate_is_no_longer_available(
    configured: Configured,
) -> None:
    h, profiles, candidates, _, make_client = configured
    value = candidate_for(memory(), sources=(turn(),))
    candidates.values[value.ref] = value
    h.retriever.values.append(value)
    with make_client() as client:
        assert client.put(
            PROFILE,
            json={
                "token_limit": 7,
                "entries": [
                    {
                        "entry_id": "saved",
                        "kind": "memory",
                        "owner_id": "char-b",
                        "memory_id": memory().id,
                    }
                ],
            },
        ).is_success
        first = client.post(RESIDENT, json=REQUEST)
        assert first.is_success
        assert [v["entry_id"] for v in first.json()["character_profile"]["entries"]] == ["saved"]
        before = profiles.values.copy()
        candidates.values.clear()
        following = client.post(RESIDENT, json=REQUEST)
        assert following.is_success
        assert following.json()["character_profile"]["entries"] == []
        assert profiles.values == before
        record = h.provisions.get(following.json()["provision_id"])
        assert record is not None and record.memory_ids == ()
