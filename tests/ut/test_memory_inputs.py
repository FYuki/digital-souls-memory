from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.memory.domain import MemoryValidationError
from digital_souls_memory.memory.inputs import MemoryInputs
from tests.evidence_support import RECORDED, TEXT, receipt, session, turn
from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities

pytestmark = pytest.mark.ut


def test_resolved_round_trip_keeps_text_and_signature_after_delivery_changes() -> None:
    evidence = FakeEvidenceStore()
    identities = registered_identities()
    now = RECORDED
    evidence.add_session(session())
    evidence.add_turn(turn(turn_id="question"))
    evidence.add_turn(turn(actor="user-a", reply_to_turn_id="question"))
    materials = EvidenceMaterials(
        evidence, IdentityCharacters(lambda: identities), lambda: now, timedelta(minutes=10)
    )
    inputs = MemoryInputs(
        evidence,
        materials,
        conversation_surfaces=frozenset({"synthetic-chat"}),
        comment_surfaces=frozenset(),
    )
    pending = inputs.resolve("char-b", ("turn-1",))
    assert [v.turn.turn_id for v in pending.available] == ["turn-1"]
    before = pending.signature(1)
    now += timedelta(minutes=10)
    full = inputs.resolve("char-b", ("turn-1",))
    assert [v.decision.text for v in full.available if v.decision] == [TEXT, TEXT]
    assert full.signature(1) != before
    evidence.add_receipt(replace(receipt("partial", 9), turn_id="question"))
    partial = inputs.resolve("char-b", ("turn-1",))
    assert partial.available[-1].decision is not None
    assert partial.available[-1].decision.text == TEXT[:9]
    dependencies = partial.dependencies()
    assert [
        (v.turn_id, v.basis, v.available_end, v.used_end, v.receipt_version) for v in dependencies
    ] == [
        ("turn-1", "original", len(TEXT), len(TEXT), 0),
        ("question", "receipt_confirmed", 9, 9, 1),
    ]
    assert partial.signature(1) != full.signature(1)
    assert pending.signature(1) == before
    assert full.available[-1].decision is not None
    assert full.available[-1].decision.text == TEXT


def test_explicit_round_trip_does_not_expand_reply_context_ancestors() -> None:
    evidence = FakeEvidenceStore()
    identities = registered_identities()
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(turn_id="ancestor"))
    evidence.add_turn(turn(turn_id="question", reply_to_turn_id="ancestor"))
    evidence.add_turn(turn(actor="user-a", reply_to_turn_id="question"))
    inputs = MemoryInputs(
        evidence,
        EvidenceMaterials(
            evidence,
            IdentityCharacters(lambda: identities),
            lambda: RECORDED,
            timedelta(minutes=10),
        ),
        conversation_surfaces=frozenset({"synthetic-chat"}),
        comment_surfaces=frozenset(),
    )
    resolved = inputs.resolve("char-b", ("turn-1", "question"))
    assert [v.turn.turn_id for v in resolved.available] == ["turn-1", "question"]
    assert inputs.resolve("char-b", ("absent",)).available == ()


def test_input_classifications_must_not_overlap() -> None:
    evidence = FakeEvidenceStore()
    identities = registered_identities()
    materials = EvidenceMaterials(
        evidence, IdentityCharacters(lambda: identities), lambda: RECORDED, timedelta(minutes=10)
    )
    with pytest.raises(MemoryValidationError):
        MemoryInputs(
            evidence,
            materials,
            conversation_surfaces=frozenset({"chat"}),
            comment_surfaces=frozenset({"chat"}),
        )
