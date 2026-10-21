from dataclasses import replace

import pytest

from digital_souls_memory.adapters.recall_records import provision_payload, restore_provision
from digital_souls_memory.evidence.provision import (
    ProvisionRecord,
    ResidentCandidateRef,
    ResidentCandidateScore,
)
from tests.evidence_support import RECORDED

pytestmark = pytest.mark.ut


def test_turn_payload_keeps_its_existing_fields_and_resident_references_roundtrip() -> None:
    turn = ProvisionRecord(
        provision_id="turn-provision",
        kind="turn",
        character_id="char-b",
        session_id="session-1",
        surface="synthetic-chat",
        query="合成検索",
        turn_id=None,
        memory_ids=(),
        source_spans=(),
        verbatim_anchor_ids=(),
        voice_exemplar_ids=(),
        motivation_ids=(),
        temperament_version=None,
        personality_version=None,
        scores=(),
        duration_ms=10,
        provided_at=RECORDED,
    )
    payload = provision_payload(turn)
    assert "profile_entry_ids" not in payload and "core_identity_version" not in payload
    assert restore_provision(payload) == turn
    resident = replace(
        turn,
        provision_id="resident-provision",
        kind="resident",
        query=None,
        core_identity_version=1,
        profile_entry_ids=("profile-1",),
        scores=(
            ResidentCandidateScore(
                ref=ResidentCandidateRef(kind="profile", identifier="profile-1"), lexical=None
            ),
        ),
    )
    resident_payload = provision_payload(resident)
    assert resident_payload["core_identity_version"] == 1
    assert resident_payload["profile_entry_ids"] == ["profile-1"]
    assert restore_provision(resident_payload) == resident
