"""Validate a complete proposed batch before allocating and persisting episodes."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from digital_souls_memory.episodes.domain import Episode, EpisodeProposal
from digital_souls_memory.resident.domain import ResolvedWindow


def build_episodes(
    proposal: EpisodeProposal,
    window: ResolvedWindow,
    *,
    now: datetime,
    id_factory: Callable[[], str],
) -> tuple[Episode, ...]:
    if not isinstance(proposal, EpisodeProposal):
        raise ValueError("Invalid episode proposal")
    proposal = replace(proposal)
    turns = {value.material.turn.turn_id: value for value in window.turns}
    for part in proposal.episodes:
        replace(part)
        for span in part.turn_span:
            replace(span)
            source = turns.get(span.turn_id)
            if (
                source is None
                or source.decision.text is None
                or span.end > len(source.decision.text)
            ):
                raise ValueError("Episode span is outside model input")
    values = []
    for part in proposal.episodes:
        times = [turns[span.turn_id].material.turn.occurred_at for span in part.turn_span]
        values.append(
            Episode(
                episode_id=id_factory(),
                character_id=window.key.character_id,
                session_id=window.key.session_id,
                surface=window.key.surface,
                topic=part.topic,
                turn_span=part.turn_span,
                time_range=(min(times), max(times)),
                model=proposal.model,
                prompt_version=proposal.prompt_version,
                created_at=now,
                input_signature=window.snapshot.signature,
                dependencies=window.snapshot.dependencies,
            )
        )
    if len({value.episode_id for value in values}) != len(values):
        raise ValueError("Duplicate episode identifier")
    return tuple(values)
