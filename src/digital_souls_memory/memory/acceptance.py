"""Resolve restatements once, inside the owner's storage transaction."""

from dataclasses import dataclass, replace
from datetime import datetime

from digital_souls_memory.memory.domain import (
    DerivedMemory,
    ExtractionProposal,
    MemoryRelation,
    RelationEffect,
    SourceContribution,
)
from digital_souls_memory.memory.inputs import ResolvedInput
from digital_souls_memory.memory.ports import MemoryTransaction
from digital_souls_memory.memory.relations import dependency_order


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
    memories = []
    sources: list[SourceContribution] = []
    for value in dependency_order(proposal.memories, proposal.relations):
        tx.validate_candidate(origin, value)
        matches = tx.fact_matches(value, now) if value.kind == "fact" else ()
        if matches:
            existing = matches[-1]
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
                    candidate=replace(by_id[relation.from_id], id=source),
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
