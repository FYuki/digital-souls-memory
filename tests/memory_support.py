"""Synthetic derived values; original evidence remains in evidence_support."""

from dataclasses import replace

from digital_souls_memory.memory.domain import (
    ConfidenceFeatures,
    Derivation,
    DerivedMemory,
    Epistemics,
    IdentityLinks,
)
from tests.evidence_support import OCCURRED, RECORDED

KINDS = ("fact", "preference", "relationship", "commitment", "summary", "key")
OLD_ID = "00000000-0000-4000-8000-000000000001"
NEW_ID = "00000000-0000-4000-8000-000000000002"


def features() -> ConfidenceFeatures:
    return ConfidenceFeatures(
        retrieval=0.8,
        source=0.7,
        contradiction=0.1,
        temporal=0.9,
        corroboration=0.6,
        self_assessment=0.95,
    )


def epistemics() -> Epistemics:
    return Epistemics(
        source_type="inferred",
        trust_tier="D",
        confidence_features=features(),
        independent_evidence_count=1,
        contradiction_ids=(),
    )


def memory(kind: str = "fact") -> DerivedMemory:
    value = DerivedMemory(
        id=OLD_ID,
        character_id="char-b",
        kind=kind,
        content="利用者Aはコーヒーを好む",
        source_turn_ids=("turn-1",),
        derivation=Derivation(
            model="synthetic-model", prompt_version="synthetic-v1", created_at=RECORDED
        ),
        valid_from=OCCURRED,
        valid_to=None,
        recorded_at=RECORDED,
        superseded_at=None,
        last_reinforced_at=None,
        epistemics=epistemics(),
        identity_links=IdentityLinks(
            self_relevance=0.4, value_links=("誠実",), relationship_links=("user-a",)
        ),
        subject="user-a" if kind in ("fact", "preference") else None,
        predicate="likes" if kind in ("fact", "preference") else None,
        object="coffee" if kind in ("fact", "preference") else None,
        shared_with=("char-c",) if kind in ("fact", "preference") else (),
        counterpart="user-a" if kind == "relationship" else None,
        affection=0.8 if kind == "relationship" else None,
        trust=0.2 if kind == "relationship" else None,
        status="friend" if kind == "relationship" else None,
        deadline=RECORDED if kind == "commitment" else None,
        verbatim_turn_id="turn-1" if kind == "commitment" else None,
    )
    if kind in ("summary", "key"):
        return replace(value, content="飲み物の好み")
    return value
