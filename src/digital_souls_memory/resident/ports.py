"""Context model and persistence boundaries."""

from datetime import datetime
from typing import Protocol

from digital_souls_memory.episodes.domain import Episode, EpisodeProposal
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from digital_souls_memory.recall.domain import CandidateRef, RecallCandidate
from digital_souls_memory.resident.domain import (
    CharacterProfileConfig,
    CharacterState,
    ContextClaim,
    ContextKey,
    ContextSnapshot,
    ContextWorkCursor,
    ContextWorkPage,
    ContextWorkState,
    Operation,
    ProfileSnapshot,
    ResolvedWindow,
    StateProposal,
)


class ProfileStore(Protocol):
    def get(self, character_id: str) -> CharacterProfileConfig | None: ...
    def replace(
        self, config: CharacterProfileConfig, *, snapshot: ProfileSnapshot
    ) -> CharacterProfileConfig: ...


class StoredCandidateReader(Protocol):
    def get_candidates(
        self, *, refs: tuple[CandidateRef, ...], now: datetime
    ) -> tuple[RecallCandidate, ...]: ...


class ContextGateway(Protocol):
    def update_state_local(self, value: LocalOnly) -> StateProposal: ...
    def update_state_cloud(self, value: CloudInput) -> StateProposal: ...
    def split_episodes_local(self, value: LocalOnly) -> EpisodeProposal: ...
    def split_episodes_cloud(self, value: CloudInput) -> EpisodeProposal: ...


class ContextStore(Protocol):
    def load_window(self, key: ContextKey, *, now: datetime) -> ResolvedWindow: ...
    def work_page(
        self, *, after: ContextWorkCursor | None, now: datetime, limit: int
    ) -> ContextWorkPage: ...
    def claim(
        self, *, operation: Operation, key: ContextKey, now: datetime
    ) -> ContextClaim | None: ...
    def begin(self, claim: ContextClaim, *, snapshot: ContextSnapshot, now: datetime) -> bool: ...
    def complete_state(
        self,
        claim: ContextClaim,
        value: CharacterState | None,
        *,
        snapshot: ContextSnapshot,
        now: datetime,
    ) -> bool: ...
    def complete_episodes(
        self,
        claim: ContextClaim,
        values: tuple[Episode, ...],
        *,
        snapshot: ContextSnapshot,
        now: datetime,
    ) -> bool: ...
    def fail(self, claim: ContextClaim, *, error: str, retry_at: datetime | None) -> None: ...
    def get_work(self, operation: Operation, key: ContextKey) -> ContextWorkState | None: ...
    def get_state(self, key: ContextKey) -> CharacterState | None: ...
    def list_episodes(self, key: ContextKey) -> tuple[Episode, ...]: ...
