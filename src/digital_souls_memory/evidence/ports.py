"""Append-only evidence storage contract."""

from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Protocol

from digital_souls_memory.evidence.domain import DeliveryReceipt, Session, SessionEnding, Turn


class EvidenceError(Exception):
    """Failure at the evidence storage boundary."""


class EvidenceConflict(EvidenceError):
    """An identifier already exists."""


class EvidenceNotFound(EvidenceError):
    """The requested evidence or reference does not exist."""


class EvidenceUnavailable(EvidenceError):
    """Storage cannot serve the operation."""


class EvidenceStore(Protocol):
    def turn_registration(
        self, actor: str, characters: "CharacterRegistry"
    ) -> AbstractContextManager["TurnRegistration"]: ...
    def add_session(self, value: Session) -> None: ...
    def get_session(self, session_id: str) -> Session | None: ...
    def add_ending(self, value: SessionEnding) -> tuple[SessionEnding, bool]: ...
    def list_endings(self, session_id: str) -> tuple[SessionEnding, ...]: ...
    def add_turn(self, value: Turn) -> None: ...
    def get_turn(self, turn_id: str) -> Turn | None: ...
    def add_receipt(self, value: DeliveryReceipt) -> None: ...
    def list_receipts(self, turn_id: str) -> AbstractContextManager[Iterator[DeliveryReceipt]]: ...
    def list_perceived_turns(self, character_id: str) -> AbstractContextManager[Iterator[Turn]]: ...


class CharacterRegistry(Protocol):
    def is_character(self, identifier: str) -> bool: ...


class TurnRegistration(CharacterRegistry, Protocol):
    def add_turn(self, value: Turn) -> None: ...
    def get_turn(self, turn_id: str) -> Turn | None: ...
    def get_session(self, session_id: str) -> Session | None: ...
