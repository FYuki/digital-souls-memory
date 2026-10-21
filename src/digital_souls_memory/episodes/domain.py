"""Episode proposals carry grounded half-open Unicode code point spans."""

from dataclasses import dataclass
from datetime import datetime

from digital_souls_memory.evidence.domain import _nonblank
from digital_souls_memory.recall.domain import SourceStamp


@dataclass(frozen=True, kw_only=True)
class TurnSpan:
    turn_id: str
    start: int
    end: int

    def __post_init__(self) -> None:
        _nonblank(self.turn_id)
        if (
            type(self.start) is not int
            or type(self.end) is not int
            or not 0 <= self.start < self.end
        ):
            raise ValueError("Invalid episode span")


@dataclass(frozen=True, kw_only=True)
class EpisodePart:
    topic: str
    turn_span: tuple[TurnSpan, ...]

    def __post_init__(self) -> None:
        _nonblank(self.topic)
        if (
            not isinstance(self.turn_span, tuple)
            or not self.turn_span
            or any(not isinstance(span, TurnSpan) for span in self.turn_span)
        ):
            raise ValueError("Episode requires source spans")


@dataclass(frozen=True, kw_only=True)
class EpisodeProposal:
    model: str
    prompt_version: str
    episodes: tuple[EpisodePart, ...]

    def __post_init__(self) -> None:
        _nonblank(self.model)
        _nonblank(self.prompt_version)
        if (
            not isinstance(self.episodes, tuple)
            or not self.episodes
            or any(not isinstance(part, EpisodePart) for part in self.episodes)
        ):
            raise ValueError("Episode proposal requires parts")


@dataclass(frozen=True, kw_only=True)
class Episode(EpisodePart):
    episode_id: str
    character_id: str
    session_id: str
    surface: str
    time_range: tuple[datetime, datetime]
    model: str
    prompt_version: str
    created_at: datetime
    input_signature: str
    dependencies: tuple[SourceStamp, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        for value in (
            self.episode_id,
            self.character_id,
            self.session_id,
            self.surface,
            self.model,
            self.prompt_version,
            self.input_signature,
        ):
            _nonblank(value)
