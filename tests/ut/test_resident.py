from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.identity.domain import PersonalityProfile, Temperament
from tests.evidence_support import INSTRUCTION, RECORDED, STARTED, TEXT, session, turn
from tests.identity_support import ASPECT_KEYS, aspects
from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities
from tests.it1.voice_fakes import FakeVoiceStore
from tests.memory_support import memory
from tests.recall_fixtures import candidate_for
from tests.recall_support import FakeProvisionStore, FakeSessionIdentityStore, StepClock
from tests.resident_profile_support import ControlledCounter, FakeProfileStore, StoredCandidates
from tests.voice_support import exemplar

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("has_selection", [False, True])
def test_resident_service_projects_selected_descriptions_without_personality_numbers(
    has_selection: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.resident.use_cases import ResidentService

    evidence, identity = FakeEvidenceStore(), registered_identities()
    evidence.add_session(replace(session(reports_delivery=False), ended_at=None))
    selections = FakeSessionIdentityStore(identity)
    if not has_selection:
        owner = evidence.get_session("session-1")
        assert owner is not None
        selections.select(character_id="char-b", session=owner)
    identity.temperaments[("char-b", 1)] = Temperament("char-b", "合成気質", 1, STARTED)
    identity.profiles[("char-b", 1)] = PersonalityProfile(
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
    voice = FakeVoiceStore()
    if has_selection:
        with voice.transaction("char-b") as tx:
            tx.add_exemplar(replace(exemplar(), character_id="char-b"))
    before = (identity.cores.copy(), identity.temperaments.copy(), identity.profiles.copy())

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Resident provision must not write identity")

    monkeypatch.setattr(identity, "transaction", forbidden)
    provisions = FakeProvisionStore(lambda snapshot: True)
    service = ResidentService(
        evidence=evidence,
        identity=identity,
        characters=IdentityCharacters(lambda: identity),
        profiles=FakeProfileStore(),
        candidates=StoredCandidates(),
        voice=voice,
        counter=ControlledCounter(),
        provisions=provisions,
        selections=selections,
        clock=lambda: RECORDED,
        monotonic=StepClock(),
        id_factory=lambda: "resident-1",
        delivery_timeout=timedelta(minutes=10),
        non_sensitive_labels=frozenset(),
    )
    result = service.provide(character_id="char-b", session_id="session-1", query=None)
    assert result.motivations == ()
    assert result.core_identity is not None
    assert result.core_identity.values == ("誠実",)
    if has_selection:
        assert result.temperament is not None and result.personality_profile is not None
        assert result.temperament.description == "合成気質"
        assert result.personality_profile.description == "合成人格"
        assert not hasattr(result.personality_profile, "aspects")
        assert all(not hasattr(result.personality_profile, key) for key in ASPECT_KEYS)
        assert [(v.exemplar_id, v.text) for v in result.voice_exemplars] == [
            ("voice-1", exemplar().text)
        ]
    else:
        assert result.temperament is None and result.personality_profile is None
        assert result.temperament_version is None and result.personality_version is None
    assert (identity.cores, identity.temperaments, identity.profiles) == before
    record = provisions.get("resident-1")
    assert record is not None and record.kind == "resident"
    assert (record.temperament_version, record.personality_version) == (
        result.temperament_version,
        result.personality_version,
    )


def test_profile_fact_replacement_keeps_personality_and_author_seed_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.resident.profiles import ProfileService

    identity = registered_identities()
    identity.profiles[("char-b", 1)] = PersonalityProfile(
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
    evidence = FakeEvidenceStore()
    evidence.add_session(replace(session(reports_delivery=False), ended_at=None))
    evidence.add_turn(turn(text=TEXT))
    evidence.add_turn(turn("turn-2", text=INSTRUCTION))
    profiles = FakeProfileStore()
    candidates = StoredCandidates()
    before = identity.profiles.copy()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Profile facts must not write personality or author_seed")

    monkeypatch.setattr(identity, "transaction", forbidden)
    service = ProfileService(
        evidence=evidence,
        characters=IdentityCharacters(lambda: identity),
        store=profiles,
        candidates=candidates,
        counter=ControlledCounter(),
        clock=lambda: RECORDED,
        delivery_timeout=timedelta(minutes=10),
        non_sensitive_labels=frozenset(),
    )
    for turn_id, text in (("turn-1", TEXT), ("turn-2", INSTRUCTION)):
        value = replace(memory(), id="memory-" + turn_id, content=text, source_turn_ids=(turn_id,))
        candidate = candidate_for(value, sources=(evidence.turns[turn_id],))
        candidates.values[candidate.ref] = candidate
        service.replace(
            character_id="char-b",
            token_limit=7,
            entries=(
                {
                    "entry_id": "profile-1",
                    "kind": "memory",
                    "owner_id": "char-b",
                    "memory_id": value.id,
                },
            ),
        )
        resolved = service.get(character_id="char-b")
        assert [v.text for v in resolved.entries] == [text]
        assert identity.profiles == before
    assert evidence.turns["turn-1"].text == TEXT
    assert evidence.turns["turn-2"].text == INSTRUCTION


@pytest.mark.parametrize("failure", ["missing-source", "inaccessible", "over-budget", "counter"])
def test_profile_service_rejects_unusable_sources_or_count_without_replacing_saved_config(
    failure: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    from digital_souls_memory.resident.profiles import ProfileService

    identity, evidence = registered_identities(), FakeEvidenceStore()
    evidence.add_session(replace(session(reports_delivery=False), ended_at=None))
    evidence.add_turn(turn())
    evidence.add_turn(turn("hidden", perceived_by=("char-c",)))
    profiles, counter = FakeProfileStore(), ControlledCounter()
    service = ProfileService(
        evidence=evidence,
        characters=IdentityCharacters(lambda: identity),
        store=profiles,
        candidates=StoredCandidates(),
        counter=counter,
        clock=lambda: RECORDED,
        delivery_timeout=timedelta(minutes=10),
        non_sensitive_labels=frozenset(),
    )
    entry = {
        "entry_id": "profile-1",
        "kind": "turn",
        "turn_id": "turn-1",
        "start": 0,
        "end": len(TEXT),
    }
    service.replace(character_id="char-b", token_limit=7, entries=(entry,))
    assert [v.text for v in service.get(character_id="char-b").entries] == [TEXT]
    before = profiles.values.copy()
    if failure == "missing-source":
        entry = entry | {"turn_id": "missing"}
    elif failure == "inaccessible":
        entry = entry | {"turn_id": "hidden"}
    elif failure == "over-budget":
        counter.tokens = 8
    else:
        counter.fail = True
    with rejected():
        service.replace(character_id="char-b", token_limit=7, entries=(entry,))
    assert profiles.values == before
