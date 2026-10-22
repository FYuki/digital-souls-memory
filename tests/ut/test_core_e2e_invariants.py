"""M5 gates also apply to the character's explicit restatement of recalled facts."""

from dataclasses import replace

import pytest

from tests.evidence_support import TEXT, turn
from tests.it1.test_memory_extraction import Harness
from tests.memory_support import NEW_ID, memory

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("low_confidence", [False, True])
def test_self_restatement_keeps_confidence_gate_and_source_attribution(
    low_confidence: bool,
) -> None:
    h = Harness(reports_delivery=False)
    original = turn(turn_id="original", actor="user-a", text="コーヒーが好きです")
    restatement = turn(
        actor="char-b", reply_to_turn_id="original", address_mode="direct", addressees=("user-a",)
    )
    h.evidence.add_turn(original)
    h.evidence.add_turn(restatement)
    candidate = replace(memory(), subject="char-b", content=TEXT)
    if low_confidence:
        candidate = replace(
            candidate,
            epistemics=replace(
                candidate.epistemics,
                confidence_features=replace(
                    candidate.epistemics.confidence_features,
                    retrieval=0.0,
                    source=0.0,
                    contradiction=1.0,
                    temporal=0.0,
                    corroboration=0.0,
                    self_assessment=1.0,
                ),
            ),
        )
    h.gateway.output = (candidate,)
    result = h.extractor.propose(character_id="char-b", turn_ids=(restatement.turn_id,))
    assert [v.id for v in result.memories] == ([] if low_confidence else [candidate.id])
    assert [(v.turn_id, v.text) for v in h.gateway.local_calls[0].turns] == [
        (restatement.turn_id, TEXT),
        (original.turn_id, original.text),
    ]
    if not low_confidence:
        assert result.memories[0].source_turn_ids == (restatement.turn_id,)
        assert result.memories[0].epistemics == candidate.epistemics
    assert h.evidence.get_turn(original.turn_id) == original
    assert h.evidence.get_turn(restatement.turn_id) == restatement


def test_heard_self_speech_keeps_fact_but_rejects_relationship_and_commitment() -> None:
    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn(address_mode="self"))
    fact = replace(memory(), subject="char-c")
    h.gateway.output = (
        fact,
        replace(memory("relationship"), id=NEW_ID),
        replace(memory("commitment"), id="00000000-0000-4000-8000-000000000003"),
    )
    result = h.extractor.propose(character_id="char-b", turn_ids=("turn-1",))
    assert result.memories == (fact,)
    assert [(v.turn_id, v.text) for v in h.gateway.local_calls[0].turns] == [("turn-1", TEXT)]
