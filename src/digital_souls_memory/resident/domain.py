"""Immutable context snapshots and worker ownership values."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from digital_souls_memory.evidence.domain import Session, _aware, _nonblank
from digital_souls_memory.evidence.materials import MaterialDecision
from digital_souls_memory.evidence.provision import SourceSpan
from digital_souls_memory.recall.domain import CandidateMaterial, DocumentStamp, SourceStamp

Operation = Literal["state", "episodes"]


@dataclass(frozen=True, kw_only=True)
class ContextKey:
    character_id: str
    session_id: str
    surface: str

    def __post_init__(self) -> None:
        for value in (self.character_id, self.session_id, self.surface):
            _nonblank(value)


@dataclass(frozen=True, kw_only=True)
class ContextSnapshot:
    key: ContextKey
    notification_version: int
    signature: str
    dependencies: tuple[SourceStamp, ...]
    session_ended_at: datetime | None
    next_due_at: datetime | None


@dataclass(frozen=True, kw_only=True)
class ResolvedContextTurn:
    material: CandidateMaterial
    decision: MaterialDecision


@dataclass(frozen=True, kw_only=True)
class ResolvedWindow:
    key: ContextKey
    session: Session
    turns: tuple[ResolvedContextTurn, ...]
    snapshot: ContextSnapshot


@dataclass(frozen=True, kw_only=True)
class StateProposal:
    mood: str | None
    relationship_temperature: str | None
    current_topic: str | None

    def __post_init__(self) -> None:
        for value in (self.mood, self.relationship_temperature, self.current_topic):
            if value is not None:
                _nonblank(value)


@dataclass(frozen=True, kw_only=True)
class CharacterState(StateProposal):
    character_id: str
    session_id: str
    surface: str
    updated_at: datetime
    expires_at: datetime
    input_signature: str
    dependencies: tuple[SourceStamp, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        ContextKey(character_id=self.character_id, session_id=self.session_id, surface=self.surface)
        _aware(self.updated_at)
        _aware(self.expires_at)
        if self.expires_at <= self.updated_at:
            raise ValueError("State expiry must follow its update")


@dataclass(frozen=True, kw_only=True)
class ContextClaim:
    operation: Operation
    key: ContextKey
    token: str
    lease_until: datetime


@dataclass(frozen=True, kw_only=True)
class ContextWorkState:
    operation: Operation
    key: ContextKey
    status: str
    signature: str | None
    completed_signature: str | None
    token: str | None
    lease_until: datetime | None
    retry_at: datetime | None
    next_due_at: datetime | None
    error: str | None


@dataclass(frozen=True, kw_only=True)
class ContextWorkItem:
    operation: Operation
    key: ContextKey


@dataclass(frozen=True, kw_only=True)
class ContextWorkCursor:
    surface: str
    session_id: str
    character_id: str
    operation: Operation


@dataclass(frozen=True, kw_only=True)
class ContextWorkPage:
    items: tuple[ContextWorkItem, ...]
    next_after: ContextWorkCursor | None


@dataclass(frozen=True, kw_only=True)
class ProfileReference:
    entry_id: str
    kind: str
    turn_id: str | None = None
    start: int | None = None
    end: int | None = None
    owner_id: str | None = None
    memory_id: str | None = None

    def __post_init__(self) -> None:
        _nonblank(self.entry_id)
        if self.kind == "turn":
            if self.turn_id is None:
                raise ValueError("Missing source turn")
            _nonblank(self.turn_id)
            if (
                type(self.start) is not int
                or type(self.end) is not int
                or not 0 <= self.start < self.end
                or self.owner_id is not None
                or self.memory_id is not None
            ):
                raise ValueError("Invalid source span")
        elif self.kind == "memory":
            if self.owner_id is None or self.memory_id is None:
                raise ValueError("Missing memory reference")
            _nonblank(self.owner_id)
            _nonblank(self.memory_id)
            if any(v is not None for v in (self.turn_id, self.start, self.end)):
                raise ValueError("Invalid memory reference")
        else:
            raise ValueError("Invalid profile reference kind")


@dataclass(frozen=True, kw_only=True)
class CharacterProfileConfig:
    character_id: str
    token_limit: int
    entries: tuple[ProfileReference, ...]
    version: int

    def __post_init__(self) -> None:
        _nonblank(self.character_id)
        if type(self.token_limit) is not int or self.token_limit < 0:
            raise ValueError("Invalid profile token limit")
        if len({v.entry_id for v in self.entries}) != len(self.entries):
            raise ValueError("Duplicate profile entry ID")


@dataclass(frozen=True, kw_only=True)
class ProfileEntry:
    entry_id: str
    kind: str
    text: str
    source_spans: tuple[SourceSpan, ...]
    owner_id: str | None
    memory_id: str | None


@dataclass(frozen=True, kw_only=True)
class CharacterProfile:
    character_id: str
    token_limit: int
    entries: tuple[ProfileEntry, ...]
    version: int


@dataclass(frozen=True, kw_only=True)
class ProfileSnapshot:
    character_id: str
    version: int
    documents: tuple[DocumentStamp, ...]
    sources: tuple[SourceStamp, ...]
    observed_at: datetime
