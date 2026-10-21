from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.identity.domain import PersonalityProfile, Temperament
from digital_souls_memory.identity.sessions import choose_session_identity
from digital_souls_memory.recall.domain import ProvisionSnapshot
from digital_souls_memory.recall.eligibility import snapshot_matches
from tests.evidence_support import RECORDED, STARTED, session, turn
from tests.identity_support import aspects
from tests.memory_support import memory
from tests.recall_fixtures import candidate_for

pytestmark = pytest.mark.ut


def test_session_chooses_versions_at_its_start_with_half_open_profile_validity() -> None:
    previous = PersonalityProfile(
        character_id="char-b",
        aspects=aspects(),
        description="合成人格",
        renderer_version="synthetic",
        temperament_version=1,
        version=1,
        valid_from=STARTED - timedelta(days=1),
        valid_to=STARTED,
        evidence_belief_ids=(),
        change_kind="author_seed",
    )
    active = replace(previous, version=2, valid_from=STARTED, valid_to=None)
    following = replace(active, version=3, valid_from=RECORDED)
    temperaments = (
        Temperament("char-b", "開始時", 1, STARTED),
        Temperament("char-b", "開始後", 2, RECORDED),
        Temperament("other", "別キャラ", 9, STARTED),
    )
    profiles = (following, previous, active, replace(active, character_id="other", version=9))
    before = (tuple(replace(v) for v in temperaments), tuple(replace(v) for v in profiles))
    selected = choose_session_identity(
        character_id="char-b", session=session(), temperaments=temperaments, profiles=profiles
    )
    assert (selected.session_id, selected.character_id) == ("session-1", "char-b")
    assert (selected.temperament_version, selected.personality_version) == (1, 2)
    assert (temperaments, profiles) == before


def test_absent_session_versions_are_explicit_nulls() -> None:
    selected = choose_session_identity(
        character_id="char-b", session=session(), temperaments=(), profiles=()
    )
    assert (selected.temperament_version, selected.personality_version) == (None, None)


@pytest.mark.parametrize(
    "change",
    [
        "unchanged",
        "document_version",
        "document_digest",
        "vector_space",
        "receipt",
        "available_end",
        "personality",
        "temperament",
        "missing_document",
        "missing_source",
    ],
)
def test_provision_snapshot_rejects_changed_dependencies_or_selected_versions(change: str) -> None:
    candidate = candidate_for(memory(), sources=(turn(),))
    expected = ProvisionSnapshot(
        character_id="char-b",
        session_id="session-1",
        temperament_version=1,
        personality_version=1,
        documents=(candidate.document,),
        sources=candidate.dependencies,
    )
    actual = expected
    if change == "document_version":
        actual = replace(expected, documents=(replace(candidate.document, version=2),))
    elif change == "document_digest":
        actual = replace(
            expected, documents=(replace(candidate.document, content_digest="c" * 64),)
        )
    elif change == "vector_space":
        actual = replace(
            expected,
            documents=(
                replace(
                    candidate.document, space=replace(candidate.document.space, digest="d" * 64)
                ),
            ),
        )
    elif change == "receipt":
        actual = replace(
            expected,
            sources=(
                replace(candidate.dependencies[0], receipt_version=1, basis="receipt_confirmed"),
            ),
        )
    elif change == "available_end":
        actual = replace(expected, sources=(replace(candidate.dependencies[0], available_end=9),))
    elif change == "personality":
        actual = replace(expected, personality_version=2)
    elif change == "temperament":
        actual = replace(expected, temperament_version=None)
    elif change == "missing_document":
        actual = replace(expected, documents=())
    elif change == "missing_source":
        actual = replace(expected, sources=())
    assert snapshot_matches(expected, actual) is (change == "unchanged")
