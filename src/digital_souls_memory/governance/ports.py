"""Authorization, recalculation and atomic deletion boundaries."""

from datetime import datetime
from typing import Protocol

from digital_souls_memory.governance.domain import (
    AuthorPrincipal,
    DeletionAttempt,
    DeletionCommand,
    DeletionRequest,
    MemoryReplacement,
)
from digital_souls_memory.memory.domain import DerivedMemory
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly


class AuthorUnauthorized(PermissionError):
    """The supplied credential does not establish author authority."""


class AuthorUnavailable(Exception):
    """Trusted author configuration is unavailable."""


class Authorizer(Protocol):
    def authorize(self, authorization: str | None) -> AuthorPrincipal: ...


class RecalculationGateway(Protocol):
    def recalculate_local(
        self, value: LocalOnly, *, memory_id: str, kind: str
    ) -> DerivedMemory | None: ...

    def recalculate_cloud(
        self, value: CloudInput, *, memory_id: str, kind: str
    ) -> DerivedMemory | None: ...


class DeletionStore(Protocol):
    def begin(
        self, command: DeletionCommand, *, principal: AuthorPrincipal, now: datetime
    ) -> DeletionAttempt: ...

    def commit(
        self,
        attempt: DeletionAttempt,
        *,
        replacements: tuple[MemoryReplacement, ...],
        now: datetime,
    ) -> DeletionRequest: ...

    def fail(self, attempt: DeletionAttempt, *, error: str, now: datetime) -> None: ...

    def get(self, request_id: str) -> DeletionRequest | None: ...
