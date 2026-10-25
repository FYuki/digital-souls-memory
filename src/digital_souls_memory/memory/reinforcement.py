"""Aggregate surviving body evidence and accepted source contributions."""

from collections.abc import Mapping
from dataclasses import replace

from digital_souls_memory.evidence.domain import Turn
from digital_souls_memory.memory.domain import DerivedMemory, SourceContribution
from digital_souls_memory.memory.evidence import independent_evidence_count


def aggregate_sources(
    value: DerivedMemory,
    body_sources: tuple[str, ...],
    contributions: tuple[SourceContribution, ...],
    turns: Mapping[str, Turn],
) -> DerivedMemory | None:
    sources = tuple(dict.fromkeys((*body_sources, *(c.source_turn_id for c in contributions))))
    if not sources:
        return None
    events = tuple(c.occurred_at for c in contributions)
    if value.last_reinforced_at is not None:
        events = (*events, value.last_reinforced_at)
    return replace(
        value,
        source_turn_ids=sources,
        last_reinforced_at=max(events, default=None),
        epistemics=replace(
            value.epistemics,
            independent_evidence_count=independent_evidence_count(tuple(turns[i] for i in sources)),
        ),
    )
