from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.memory.domain import (
    MemoryRelation,
    MemoryValidationError,
    memory_valid_at,
)
from digital_souls_memory.memory.ports import MemoryContribution
from digital_souls_memory.memory.relations import (
    normalize_relations,
    resolve_relations,
    resolve_representative,
)
from tests.evidence_support import RECORDED
from tests.memory_support import memory

pytestmark = pytest.mark.ut


def test_representative_chain_normalizes_only_relation_targets() -> None:
    representatives = {"b": "a", "c": "b"}
    relations = (
        MemoryRelation(from_id="b", to_id="a", type="extends"),
        MemoryRelation(from_id="d", to_id="c", type="updates"),
    )
    assert resolve_representative("c", representatives) == "a"
    assert normalize_relations(relations, representatives) == (
        relations[0],
        replace(relations[1], to_id="a"),
    )
    assert representatives == {"b": "a", "c": "b"}


@pytest.mark.parametrize("representatives", [{"a": "a"}, {"a": "b", "b": "a"}])
def test_representative_cycles_are_rejected(representatives: dict[str, str]) -> None:
    with pytest.raises(MemoryValidationError):
        resolve_representative("a", representatives)


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
