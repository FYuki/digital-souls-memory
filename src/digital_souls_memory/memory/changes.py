"""Pure temporal updates and event-time reinforcement."""

from dataclasses import replace
from datetime import datetime

from digital_souls_memory.evidence.domain import Turn
from digital_souls_memory.memory.domain import DerivedMemory, MemoryValidationError
from digital_souls_memory.memory.evidence import independent_evidence_count


def reinforce(
    value: DerivedMemory, source: Turn, *, source_turns: tuple[Turn, ...], relation_type: str
) -> DerivedMemory:
    if value.kind == "commitment" or relation_type not in ("same_fact", "extends"):
        return value
    if value.character_id not in source.perceived_by:
        raise MemoryValidationError("Evidence outside perception")
    sources = tuple(dict.fromkeys((*value.source_turn_ids, source.turn_id)))
    if set(sources) != {v.turn_id for v in source_turns}:
        raise MemoryValidationError("Incomplete supporting turns")
    at = max(value.last_reinforced_at or source.occurred_at, source.occurred_at)
    return replace(
        value,
        source_turn_ids=sources,
        last_reinforced_at=at,
        epistemics=replace(
            value.epistemics, independent_evidence_count=independent_evidence_count(source_turns)
        ),
    )


def update_fact(
    old: DerivedMemory, candidate: DerivedMemory
) -> tuple[DerivedMemory, DerivedMemory]:
    if candidate.epistemics.trust_tier < old.epistemics.trust_tier:
        raise MemoryValidationError("Invalid fact update")
    return _close_fact_period(old, candidate), candidate


def _close_fact_period(old: DerivedMemory, candidate: DerivedMemory) -> DerivedMemory:
    if (
        old.id == candidate.id
        or old.character_id != candidate.character_id
        or old.kind != "fact"
        or candidate.kind != "fact"
        or (old.subject, old.predicate) != (candidate.subject, candidate.predicate)
        or candidate.valid_from < old.valid_from
    ):
        raise MemoryValidationError("Invalid fact update")
    return replace(old, valid_to=candidate.valid_from)


def supersede(value: DerivedMemory, *, at: datetime) -> DerivedMemory:
    return replace(value, superseded_at=at)


def apply_temporal_effect(
    value: DerivedMemory,
    candidate: DerivedMemory,
    *,
    relation_type: str,
    source_turns: tuple[Turn, ...],
    at: datetime,
) -> DerivedMemory:
    if relation_type == "updates":
        return _close_fact_period(value, candidate)
    if relation_type == "contradicts":
        return supersede(value, at=at)
    if relation_type != "extends":
        raise MemoryValidationError("Invalid temporal effect")
    for identifier in candidate.source_turn_ids:
        source = next(t for t in source_turns if t.turn_id == identifier)
        value = reinforce(
            value,
            source,
            source_turns=tuple(
                t for t in source_turns if t.turn_id in (*value.source_turn_ids, identifier)
            ),
            relation_type="extends",
        )
    return value
