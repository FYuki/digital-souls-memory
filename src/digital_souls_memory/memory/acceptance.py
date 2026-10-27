"""Resolve restatements once, inside the owner's storage transaction."""

from dataclasses import dataclass, replace
from datetime import datetime
from graphlib import CycleError, TopologicalSorter
from heapq import heappop, heappush

from digital_souls_memory.memory.domain import (
    DerivedMemory,
    ExtractionProposal,
    MemoryRelation,
    MemoryValidationError,
    RelationEffect,
    SourceContribution,
)
from digital_souls_memory.memory.inputs import ResolvedInput
from digital_souls_memory.memory.ports import MemoryTransaction


@dataclass(frozen=True)
class AcceptedExtraction:
    memories: tuple[DerivedMemory, ...]
    relations: tuple[MemoryRelation, ...]
    sources: tuple[SourceContribution, ...]
    effects: tuple[RelationEffect, ...]


def accept(
    tx: MemoryTransaction, proposal: ExtractionProposal, resolved: ResolvedInput, now: datetime
) -> AcceptedExtraction:
    origin = resolved.turns[0].turn.turn_id
    turns = {v.turn.turn_id: v.turn for v in resolved.available}
    replacements: dict[str, str] = {}
    targets: dict[tuple[str, str | None, str | None, str | None], DerivedMemory] = {}
    memories = []
    sources: list[SourceContribution] = []
    for value in _acceptance_order(proposal):
        tx.validate_candidate(origin, value)
        key = (value.character_id, value.subject, value.predicate, value.object)
        matches = (
            ((targets[key],) if key in targets else tx.fact_matches(value, now))
            if value.kind == "fact"
            else ()
        )
        if matches:
            existing = min(matches, key=lambda v: (v.valid_from, v.recorded_at, v.id))
            targets[key] = existing
            replacements[value.id] = existing.id
            sources.extend(
                SourceContribution(
                    character_id=value.character_id,
                    fact_id=existing.id,
                    extraction_turn_id=origin,
                    source_turn_id=identifier,
                    occurred_at=turns[identifier].occurred_at,
                )
                for identifier in value.source_turn_ids
            )
        else:
            memories.append(value)
            if value.kind == "fact":
                targets[key] = value
    by_id = {v.id: v for v in proposal.memories}
    relations = []
    effects = []
    for relation in proposal.relations:
        source = replacements.get(relation.from_id, relation.from_id)
        target = replacements.get(relation.to_id, relation.to_id)
        if source == target:
            continue
        rewritten = replace(relation, from_id=source, to_id=target)
        if relation.from_id in replacements:
            effects.append(
                RelationEffect(
                    turn_id=origin,
                    relation=rewritten,
                    candidate=by_id[relation.from_id],
                )
            )
        else:
            relations.append(rewritten)
    return AcceptedExtraction(
        tuple(memories),
        tuple(dict.fromkeys(relations)),
        tuple(dict.fromkeys(sources)),
        tuple(dict.fromkeys(effects)),
    )


def _acceptance_order(proposal: ExtractionProposal) -> tuple[DerivedMemory, ...]:
    by_id = {v.id: v for v in proposal.memories}
    dependencies = {
        v.id: {r.to_id for r in proposal.relations if r.from_id == v.id and r.to_id in by_id}
        for v in proposal.memories
    }
    sorter = TopologicalSorter(dependencies)
    try:
        sorter.prepare()
    except CycleError as error:
        raise MemoryValidationError("Cyclic memory dependency") from error
    ready: list[str] = []
    for identifier in sorter.get_ready():
        heappush(ready, identifier)
    ordered = []
    while ready:
        identifier = heappop(ready)
        ordered.append(by_id[identifier])
        sorter.done(identifier)
        for available in sorter.get_ready():
            heappush(ready, available)
    return tuple(ordered)
