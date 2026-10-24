"""Resolve historical identifiers only within their original extraction."""

from dataclasses import replace

from digital_souls_memory.memory.domain import MemoryRelation, MemoryValidationError
from digital_souls_memory.memory.ports import MemoryContribution


def resolve_representative(identifier: str, representatives: dict[str, str]) -> str:
    seen: set[str] = set()
    while identifier in representatives:
        if identifier in seen:
            raise MemoryValidationError("Cyclic representative mapping")
        seen.add(identifier)
        identifier = representatives[identifier]
    return identifier


def normalize_relations(
    relations: tuple[MemoryRelation, ...], representatives: dict[str, str]
) -> tuple[MemoryRelation, ...]:
    return tuple(
        replace(r, to_id=resolve_representative(r.to_id, representatives)) for r in relations
    )


def resolve_relations(
    proposed: tuple[MemoryRelation, ...],
    history: tuple[MemoryContribution, ...],
    current: tuple[MemoryContribution, ...],
) -> tuple[MemoryRelation, ...]:
    current_ids = {v.memory.id for v in current}
    replacements: dict[str, str] = {}
    for previous in history:
        if previous.memory.id in current_ids:
            continue
        matches = [
            value.memory.id
            for value in current
            if value.turn_id == previous.turn_id
            and replace(
                previous.memory,
                id=value.memory.id,
                derivation=value.memory.derivation,
                recorded_at=value.memory.recorded_at,
                epistemics=replace(
                    previous.memory.epistemics,
                    confidence_features=value.memory.epistemics.confidence_features,
                ),
            )
            == value.memory
        ]
        if len(matches) == 1:
            replacements[previous.memory.id] = matches[0]
    return tuple(replace(r, to_id=replacements.get(r.to_id, r.to_id)) for r in proposed)
