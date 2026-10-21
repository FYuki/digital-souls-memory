"""Evidence port fake, scoped to the Evidence HTTP tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING

from digital_souls_memory.evidence.domain import DeliveryReceipt, Session, Turn, can_perceive
from digital_souls_memory.evidence.ports import EvidenceConflict, EvidenceNotFound
from tests.it1.identity_fakes import FakeIdentityStore

if TYPE_CHECKING:
    from digital_souls_memory.evidence.domain import SessionEnding


class FakeEvidenceStore:
    """Store operations only; validation and HTTP conversion stay in production code."""

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.turns: dict[str, Turn] = {}
        self.receipts: list[DeliveryReceipt] = []
        self.writes = 0
        self.endings: dict[str, SessionEnding] = {}

    def add_session(self, value: Session) -> None:
        if value.session_id in self.sessions:
            raise EvidenceConflict(value.session_id)
        self.sessions[value.session_id] = value
        self.writes += 1

    def get_session(self, session_id: str) -> Session | None:
        original = self.sessions.get(session_id)
        ending = self.endings.get(session_id)
        return replace(original, ended_at=ending.ended_at) if original and ending else original

    def add_ending(self, value: "SessionEnding") -> "tuple[SessionEnding, bool]":
        original = self.sessions.get(value.session_id)
        if original is None:
            raise EvidenceNotFound()
        previous = self.endings.get(value.session_id)
        if previous is not None:
            if (previous.ended_at, previous.reported_by) != (value.ended_at, value.reported_by):
                raise EvidenceConflict()
            return previous, False
        if original.ended_at is not None and original.ended_at != value.ended_at:
            raise EvidenceConflict()
        self.endings[value.session_id] = value
        self.writes += 1
        return value, True

    def list_endings(self, session_id: str) -> "tuple[SessionEnding, ...]":
        value = self.endings.get(session_id)
        return (value,) if value is not None else ()

    def add_turn(self, value: Turn) -> None:
        if value.turn_id in self.turns:
            raise EvidenceConflict(value.turn_id)
        self.turns[value.turn_id] = value
        self.writes += 1

    def get_turn(self, turn_id: str) -> Turn | None:
        return self.turns.get(turn_id)

    def add_receipt(self, value: DeliveryReceipt) -> None:
        self.receipts.append(value)
        self.writes += 1

    @contextmanager
    def list_receipts(self, turn_id: str) -> Iterator[Iterator[DeliveryReceipt]]:
        yield (value for value in self.receipts if value.turn_id == turn_id)

    @contextmanager
    def list_perceived_turns(self, character_id: str) -> Iterator[Iterator[Turn]]:
        yield (
            value
            for value in sorted(
                self.turns.values(),
                key=lambda value: (value.occurred_at, value.recorded_at, value.turn_id),
            )
            if can_perceive(character_id, self.sessions[value.session_id], value)
        )


def registered_identities() -> "FakeIdentityStore":
    from digital_souls_memory.identity.domain import CoreIdentity
    from tests.evidence_support import RECORDED

    store = FakeIdentityStore()
    for identifier in ("char-a", "char-b", "char-c", "char-z", "char_a", "char_b"):
        store.cores[(identifier, 1)] = CoreIdentity(identifier, ("誠実",), (), 1, RECORDED)
    return store
