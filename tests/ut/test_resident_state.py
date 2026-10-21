from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest

from digital_souls_memory.api import create_app
from tests.evidence_support import INSTRUCTION, TEXT

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("value", [None, TEXT, INSTRUCTION])
def test_state_proposal_preserves_descriptions_as_data(value: str | None) -> None:
    from digital_souls_memory.resident.domain import StateProposal

    proposal = StateProposal(mood=value, relationship_temperature=value, current_topic=value)
    assert (proposal.mood, proposal.relationship_temperature, proposal.current_topic) == (
        value,
        value,
        value,
    )


@pytest.mark.parametrize("field", ["mood", "relationship_temperature", "current_topic"])
@pytest.mark.parametrize("invalid", ["", " \t\n", 0.7, {"author_seed": True}])
def test_state_proposal_rejects_blank_or_structured_state_descriptions(
    field: str, invalid: object, rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    from digital_souls_memory.resident.domain import StateProposal

    values: dict[str, str | None] = {
        "mood": "穏やか",
        "relationship_temperature": None,
        "current_topic": "合成の話題",
    }
    values[field] = cast(str, invalid)
    with rejected():
        StateProposal(**values)


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_state_ttl_must_be_positive(
    ttl: timedelta, rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    with rejected():
        create_app(state_ttl=ttl)


@pytest.mark.parametrize("change", ["expiry", "ending", "receipt", "new-turn"])
def test_saved_context_reader_rechecks_the_same_artifacts_after_evidence_changes(
    change: str,
) -> None:
    from digital_souls_memory.adapters.identity_characters import IdentityCharacters
    from digital_souls_memory.evidence.domain import SessionEnding
    from digital_souls_memory.evidence.materials import EvidenceMaterials
    from digital_souls_memory.resident.states import ContextReader
    from tests.context_gateway_support import EpisodeGateway
    from tests.context_store_support import FakeContextStore
    from tests.evidence_support import RECORDED, receipt, turn
    from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities
    from tests.ut.test_context_worker import resolved_window, worker_for

    window = resolved_window()
    evidence, identities = FakeEvidenceStore(), registered_identities()
    evidence.add_session(replace(window.session, reports_delivery=True))
    for value in window.turns:
        evidence.add_turn(value.material.turn)
    store, gateway = FakeContextStore(window), EpisodeGateway()
    now = [RECORDED + timedelta(minutes=10)]
    worker = worker_for(store, gateway, ("episode-1", "episode-2"), lambda: now[0])
    worker.process_state(window.key)
    worker.process_episodes(window.key)
    reader = ContextReader(
        store,
        evidence,
        EvidenceMaterials(
            evidence, IdentityCharacters(lambda: identities), lambda: now[0], timedelta(minutes=10)
        ),
        lambda: now[0],
    )
    state, episodes = reader.state(window.key), reader.episodes(window.key)
    assert state is not None and len(episodes) == 2
    for result in episodes:
        assert tuple((s.turn_id, s.start, s.end) for s in result.source_spans) == tuple(
            (s.turn_id, s.start, s.end) for s in result.episode.turn_span
        )
        for span in result.source_spans:
            original = evidence.get_turn(span.turn_id)
            assert original is not None
            assert (span.text, span.actor, span.language) == (
                original.text[span.start : span.end],
                original.actor,
                original.language,
            )
    if change == "expiry":
        now[0] = state.expires_at
    elif change == "ending":
        evidence.add_ending(
            SessionEnding(
                session_id=window.key.session_id,
                ended_at=now[0],
                reported_at=now[0],
                reported_by="synthetic-window",
            )
        )
    elif change == "receipt":
        evidence.add_receipt(receipt("none", 0))
    else:
        evidence.add_turn(turn("new-turn", text="新しい原文"))
    assert reader.state(window.key) == (state if change == "new-turn" else None)
    assert reader.episodes(window.key) == (() if change == "receipt" else episodes)
    assert store.get_state(window.key) == state
    assert store.list_episodes(window.key) == tuple(v.episode for v in episodes)
