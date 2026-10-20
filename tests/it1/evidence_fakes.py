"""Evidence port fake, scoped to the Evidence HTTP tests."""

from collections.abc import Iterator
from contextlib import contextmanager

from digital_souls_memory.evidence.domain import DeliveryReceipt, Session, Turn, can_perceive
from digital_souls_memory.evidence.ports import EvidenceConflict


class FakeEvidenceStore:
    """Store operations only; validation and HTTP conversion stay in production code."""

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.turns: dict[str, Turn] = {}
        self.receipts: list[DeliveryReceipt] = []
        self.writes = 0

    def add_session(self, value: Session) -> None:
        if value.session_id in self.sessions:
            raise EvidenceConflict(value.session_id)
        self.sessions[value.session_id] = value
        self.writes += 1

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

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
