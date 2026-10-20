"""Storage, rendering and consistency review contracts."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from digital_souls_memory.identity.domain import CoreIdentity, PersonalityProfile, Temperament


class IdentityConflict(Exception):
    """A version or initial seed already exists."""


class IdentityNotFound(Exception):
    """The requested version or reference is absent."""


class IdentityUnavailable(Exception):
    """A required dependency cannot serve the operation."""


class IdentityInconsistent(Exception):
    """The reviewer rejected the registration."""


@dataclass(frozen=True)
class ReviewResult:
    consistent: bool
    reason: str


class IdentityReader(Protocol):
    def get_core(self, version: int | None = None) -> CoreIdentity | None: ...
    def get_temperament(self, version: int | None = None) -> Temperament | None: ...
    def get_profile(self, version: int | None = None) -> PersonalityProfile | None: ...


class IdentityTransaction(IdentityReader, Protocol):
    def add_core(self, value: CoreIdentity) -> None: ...
    def add_temperament(self, value: Temperament) -> None: ...
    def add_profile(self, value: PersonalityProfile) -> None: ...


class IdentityStore(Protocol):
    def transaction(
        self, character_id: str, *, source: str
    ) -> AbstractContextManager[IdentityTransaction]: ...
    def get_core(self, character_id: str, version: int | None = None) -> CoreIdentity | None: ...
    def get_temperament(
        self, character_id: str, version: int | None = None
    ) -> Temperament | None: ...
    def get_profile(
        self, character_id: str, version: int | None = None
    ) -> PersonalityProfile | None: ...
    def get_current(
        self, character_id: str, at: datetime
    ) -> tuple[CoreIdentity | None, Temperament | None, PersonalityProfile | None]: ...


class LLMGateway(Protocol):
    def render_personality(
        self, *, aspects: Mapping[str, float], temperament: Temperament, renderer_version: str
    ) -> str: ...


class Reviewer(Protocol):
    def review_identity(
        self, *, temperament: Temperament, profile: PersonalityProfile
    ) -> ReviewResult: ...
