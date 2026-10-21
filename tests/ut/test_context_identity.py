from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from digital_souls_memory.identity.domain import PersonalityProfile, Temperament
from tests.context_gateway_support import EpisodeGateway
from tests.context_store_support import FakeContextStore
from tests.evidence_support import INSTRUCTION, STARTED
from tests.identity_support import aspects
from tests.it1.evidence_fakes import registered_identities
from tests.it1.identity_fakes import FakeIdentityStore
from tests.ut.test_context_worker import resolved_window, worker_for

if TYPE_CHECKING:
    from digital_souls_memory.resident.domain import ResolvedWindow

pytestmark = pytest.mark.ut


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> FakeIdentityStore:
    values = registered_identities()
    values.temperaments[("char-b", 1)] = Temperament("char-b", "合成気質", 1, STARTED)
    values.profiles[("char-b", 1)] = PersonalityProfile(
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

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Short-term context must not open an identity write transaction")

    monkeypatch.setattr(FakeIdentityStore, "transaction", forbidden)
    return values


def instruction_window() -> "ResolvedWindow":
    window = resolved_window()
    first = window.turns[0]
    return replace(
        window,
        turns=(
            replace(
                first,
                material=replace(
                    first.material, turn=replace(first.material.turn, text=INSTRUCTION)
                ),
                decision=replace(first.decision, text=INSTRUCTION),
            ),
        ),
        snapshot=replace(
            window.snapshot,
            dependencies=(
                replace(
                    window.snapshot.dependencies[0],
                    available_end=len(INSTRUCTION),
                    used_end=len(INSTRUCTION),
                ),
            ),
        ),
    )


def test_state_update_cannot_change_core_identity_or_temperament(
    identity: FakeIdentityStore,
) -> None:
    window = instruction_window()
    store, gateway = FakeContextStore(window), EpisodeGateway()
    before = (identity.cores.copy(), identity.temperaments.copy())
    worker_for(store, gateway, ()).process_state(window.key)
    state = store.get_state(window.key)
    assert state is not None and state.current_topic == INSTRUCTION
    assert (identity.cores, identity.temperaments) == before


def test_episode_generation_cannot_replace_personality_aspects_or_author_seed(
    identity: FakeIdentityStore,
) -> None:
    window = instruction_window()
    store, gateway = FakeContextStore(window), EpisodeGateway()
    before = identity.profiles.copy()
    worker_for(store, gateway, ("episode-1",)).process_episodes(window.key)
    (episode,) = store.list_episodes(window.key)
    assert episode.topic == INSTRUCTION
    assert identity.profiles == before
    profile = identity.get_profile("char-b", 1)
    assert profile is not None and dict(profile.aspects) == aspects()
    assert profile.change_kind == "author_seed" and profile.version == 1


def test_instruction_in_context_is_data_and_cannot_execute_ledger_or_identity_writes(
    identity: FakeIdentityStore,
) -> None:
    from tests.it1.evidence_fakes import FakeEvidenceStore

    window = instruction_window()
    evidence = FakeEvidenceStore()
    evidence.add_session(window.session)
    evidence.add_turn(window.turns[0].material.turn)
    before = (evidence.turns.copy(), identity.cores.copy(), identity.profiles.copy())
    store, gateway = FakeContextStore(window), EpisodeGateway()
    worker = worker_for(store, gateway, ("episode-1",))
    worker.process_state(window.key)
    worker.process_episodes(window.key)
    state = store.get_state(window.key)
    assert state is not None and state.current_topic == INSTRUCTION
    assert [value.topic for value in store.list_episodes(window.key)] == [INSTRUCTION]
    assert (evidence.turns, identity.cores, identity.profiles) == before
    assert [(t.text, t.language) for _, v in gateway.calls for t in v.turns] == [
        (INSTRUCTION, "ja")
    ]
