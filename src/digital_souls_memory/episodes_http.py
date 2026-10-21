"""HTTP representations of restored episodes."""

from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from digital_souls_memory.resident.domain import ContextKey
from digital_souls_memory.resident.states import ContextReader, EpisodeReadResult
from digital_souls_memory.resident_http import DependencyOutput, ResidentRoute


class TurnSpanOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    turn_id: str
    start: int
    end: int


class EpisodeSourceOutput(TurnSpanOutput):
    text: str
    actor: str
    language: str


class EpisodeOutput(BaseModel):
    episode_id: str
    character_id: str
    session_id: str
    surface: str
    topic: str
    turn_span: tuple[TurnSpanOutput, ...]
    time_range: tuple[datetime, datetime]
    model: str
    prompt_version: str
    created_at: datetime
    input_signature: str
    dependencies: tuple[DependencyOutput, ...]
    source_spans: tuple[EpisodeSourceOutput, ...]

    @classmethod
    def from_result(cls, value: EpisodeReadResult) -> "EpisodeOutput":
        episode = value.episode
        return cls(
            episode_id=episode.episode_id,
            character_id=episode.character_id,
            session_id=episode.session_id,
            surface=episode.surface,
            topic=episode.topic,
            turn_span=tuple(TurnSpanOutput.model_validate(v) for v in episode.turn_span),
            time_range=episode.time_range,
            model=episode.model,
            prompt_version=episode.prompt_version,
            created_at=episode.created_at,
            input_signature=episode.input_signature,
            dependencies=tuple(DependencyOutput.model_validate(v) for v in episode.dependencies),
            source_spans=tuple(EpisodeSourceOutput.model_validate(v) for v in value.source_spans),
        )


class EpisodesOutput(BaseModel):
    episodes: tuple[EpisodeOutput, ...]


def episodes_router(reader: Callable[[], ContextReader]) -> APIRouter:
    router = APIRouter(route_class=ResidentRoute)

    @router.get("/characters/{character_id}/sessions/{session_id}/episodes")
    def get_episodes(character_id: str, session_id: str, surface: str) -> EpisodesOutput:
        values = reader().episodes(
            ContextKey(character_id=character_id, session_id=session_id, surface=surface)
        )
        return EpisodesOutput(episodes=tuple(EpisodeOutput.from_result(v) for v in values))

    return router
