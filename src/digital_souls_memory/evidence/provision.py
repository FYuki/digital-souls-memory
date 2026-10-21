"""Immutable records of materials actually provided to Core."""

from dataclasses import dataclass
from datetime import datetime


class ProvisionConflict(Exception):
    """The provision identifier is already committed."""


class ProvisionUnavailable(Exception):
    """The provision could not be committed against its observed state."""


@dataclass(frozen=True, kw_only=True)
class SourceSpan:
    turn_id: str
    start: int
    end: int
    text: str
    actor: str
    language: str
    truncated: bool
    original_start: int
    original_end: int


@dataclass(frozen=True, kw_only=True)
class CandidateRef:
    owner_id: str
    memory_id: str | None
    anchor_id: str | None

    def __post_init__(self) -> None:
        if not self.owner_id.strip() or (self.memory_id is None) == (self.anchor_id is None):
            raise ValueError("Exactly one candidate identifier is required")

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (
            self.memory_id or self.anchor_id or "",
            self.owner_id,
            "memory" if self.memory_id is not None else "anchor",
        )


@dataclass(frozen=True, kw_only=True)
class CandidateScore:
    ref: CandidateRef
    semantic: float
    lexical: float
    recency: float
    importance: float
    self_relevance: float
    trust: float
    composite: float
    relevance: float | None


@dataclass(frozen=True, kw_only=True)
class ProvisionRecord:
    provision_id: str
    kind: str
    character_id: str
    session_id: str
    surface: str
    query: str | None
    turn_id: str | None
    memory_ids: tuple[str, ...]
    source_spans: tuple[SourceSpan, ...]
    verbatim_anchor_ids: tuple[str, ...]
    voice_exemplar_ids: tuple[str, ...]
    motivation_ids: tuple[str, ...]
    temperament_version: int | None
    personality_version: int | None
    scores: tuple[CandidateScore, ...]
    duration_ms: float
    provided_at: datetime
