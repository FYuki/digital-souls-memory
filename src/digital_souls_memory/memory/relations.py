"""Resolve historical identifiers only within their original extraction."""

from dataclasses import replace

from digital_souls_memory.memory.domain import DerivedMemory, MemoryRelation, MemoryValidationError
from digital_souls_memory.memory.ports import MemoryContribution


def equivalent_ids(
    history: tuple[MemoryContribution, ...],
    current: tuple[MemoryContribution, ...],
) -> dict[str, str]:
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
    return replacements


def resolve_relations(
    proposed: tuple[MemoryRelation, ...],
    history: tuple[MemoryContribution, ...],
    current: tuple[MemoryContribution, ...],
) -> tuple[MemoryRelation, ...]:
    replacements = equivalent_ids(history, current)
    return tuple(
        replace(
            r,
            from_id=replacements.get(r.from_id, r.from_id),
            to_id=replacements.get(r.to_id, r.to_id),
        )
        for r in proposed
    )


def dependency_order(
    candidates: tuple[DerivedMemory, ...], proposed: tuple[MemoryRelation, ...]
) -> tuple[DerivedMemory, ...]:
    by_id = {v.id: v for v in candidates}
    dependencies = {
        v.id: [r.to_id for r in proposed if r.from_id == v.id and r.to_id in by_id]
        for v in candidates
    }
    ordered: list[DerivedMemory] = []
    done: set[str] = set()
    visiting: set[str] = set()
    for candidate in candidates:
        stack = [(candidate.id, False)]
        while stack:
            identifier, expanded = stack.pop()
            if identifier in done:
                continue
            if expanded:
                visiting.remove(identifier)
                done.add(identifier)
                ordered.append(by_id[identifier])
                continue
            if identifier in visiting:
                raise MemoryValidationError("Cyclic memory dependency")
            visiting.add(identifier)
            stack.append((identifier, True))
            stack.extend((target, False) for target in reversed(dependencies[identifier]))
    return tuple(ordered)
