"""Deletion commands, immutable snapshots and audit results."""

from dataclasses import dataclass
from datetime import datetime

from digital_souls_memory.evidence.domain import Turn
from digital_souls_memory.memory.domain import DerivedMemory, MemoryValidationError, nonblank
from digital_souls_memory.recall.domain import CandidateMaterial, SourceStamp


@dataclass(frozen=True, kw_only=True)
class AuthorPrincipal:
    author_id: str

    def __post_init__(self) -> None:
        nonblank(self.author_id)


@dataclass(frozen=True, kw_only=True)
class DeletionCommand:
    request_id: str
    target_turn_ids: tuple[str, ...] = ()
    user_id: str | None = None

    def __post_init__(self) -> None:
        nonblank(self.request_id)
        if bool(self.target_turn_ids) == (self.user_id is not None):
            raise MemoryValidationError("Specify exactly one selection")
        if self.user_id is not None:
            nonblank(self.user_id)
        for identifier in self.target_turn_ids:
            nonblank(identifier)
        object.__setattr__(self, "target_turn_ids", tuple(sorted(set(self.target_turn_ids))))


def select_target_turn_ids(turns: tuple[Turn, ...], command: DeletionCommand) -> tuple[str, ...]:
    if command.user_id is not None:
        return tuple(
            sorted(
                {
                    t.turn_id
                    for t in turns
                    if t.actor == command.user_id or command.user_id in t.addressees
                }
            )
        )
    return tuple(sorted({t.turn_id for t in turns if t.turn_id in command.target_turn_ids}))


@dataclass(frozen=True, kw_only=True)
class MemoryRef:
    character_id: str
    memory_id: str


@dataclass(frozen=True, kw_only=True)
class StoredReference:
    table: str
    key: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class MemoryCopy:
    table: str
    memory: DerivedMemory
    origin_turn_id: str | None = None

    def __post_init__(self) -> None:
        if self.table not in (
            "derived_memories",
            "memory_candidates",
            "memory_generated",
            "memory_baselines",
        ):
            raise MemoryValidationError("Invalid memory copy")


@dataclass(frozen=True, kw_only=True)
class MemoryReplacement:
    copy: MemoryCopy
    value: DerivedMemory | None
    dependencies: tuple[SourceStamp, ...] = ()


@dataclass(frozen=True, kw_only=True)
class DeletionSnapshot:
    target_turn_ids: tuple[str, ...]
    copies: tuple[MemoryCopy, ...]
    materials: tuple[CandidateMaterial, ...]
    references: tuple[StoredReference, ...]
    fingerprint: str
    observed_at: datetime


@dataclass(frozen=True, kw_only=True)
class CascadeResult:
    deleted_turn_ids: tuple[str, ...]
    recalculated_memories: tuple[MemoryRef, ...]
    invalidated_memories: tuple[MemoryRef, ...]
    invalidated_provision_ids: tuple[str, ...]
    removed_references: tuple[StoredReference, ...]


@dataclass(frozen=True, kw_only=True)
class DeletionRequest:
    command: DeletionCommand
    requested_by: str
    target_turn_ids: tuple[str, ...]
    status: str
    cascade_result: CascadeResult | None
    error: str | None
    requested_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, kw_only=True)
class DeletionAttempt:
    request: DeletionRequest
    token: str | None
    snapshot: DeletionSnapshot | None
