from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.memory.domain import (
    MemoryRelation,
    memory_valid_at,
)
from digital_souls_memory.memory.ports import MemoryContribution
from digital_souls_memory.memory.relations import resolve_relations
from tests.evidence_support import RECORDED
from tests.memory_support import memory

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "case", ["equivalent", "different", "origin", "owner", "ambiguous", "current"]
)
def test_relation_resolves_only_one_equivalent_candidate_from_same_origin(case: str) -> None:
    old = MemoryContribution("turn-1", memory())
    candidate = replace(
        old.memory,
        id="replacement",
        epistemics=replace(
            old.memory.epistemics,
            confidence_features=replace(
                old.memory.epistemics.confidence_features, self_assessment=0.90
            ),
        ),
        derivation=replace(old.memory.derivation, created_at=RECORDED + timedelta(seconds=1)),
    )
    if case == "different":
        candidate = replace(candidate, object="tea")
    if case == "owner":
        candidate = replace(candidate, character_id="char-other")
    current: tuple[MemoryContribution, ...] = (
        MemoryContribution("other" if case == "origin" else "turn-1", candidate),
    )
    if case == "ambiguous":
        current += (MemoryContribution("turn-1", replace(candidate, id="also-equivalent")),)
    if case == "current":
        current += (old,)
    relation = MemoryRelation(from_id="updated", to_id=old.memory.id, type="updates")
    assert resolve_relations((relation,), (old,), current) == (
        replace(relation, to_id=candidate.id) if case == "equivalent" else relation,
    )


@pytest.mark.parametrize("field", ["valid_to", "superseded_at"])
def test_memory_valid_at_uses_half_open_interval(field: str) -> None:
    start = RECORDED
    end = start + timedelta(seconds=1)
    value = replace(
        memory(),
        valid_from=start,
        valid_to=end if field == "valid_to" else None,
        superseded_at=end if field == "superseded_at" else None,
    )
    assert not memory_valid_at(value, start - timedelta(microseconds=1))
    assert memory_valid_at(value, start)
    assert memory_valid_at(value, end - timedelta(microseconds=1))
    assert not memory_valid_at(value, end)
