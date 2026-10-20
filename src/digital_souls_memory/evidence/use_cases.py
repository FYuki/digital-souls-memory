"""Evidence registration and retrieval through the storage port."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime

from digital_souls_memory.evidence.domain import DeliveryReceipt, Session, Turn, validate_receipt
from digital_souls_memory.evidence.ports import EvidenceNotFound, EvidenceStore

Clock = Callable[[], datetime]


class EvidenceService:
    def __init__(self, store: EvidenceStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def add_session(self, value: Session) -> Session:
        self._store.add_session(value)
        return value

    def get_session(self, session_id: str) -> Session:
        value = self._store.get_session(session_id)
        if value is None:
            raise EvidenceNotFound()
        return value

    def add_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        surface: str,
        actor: str,
        kind: str,
        text: str,
        audience: tuple[str, ...],
        language: str,
        register: Mapping[str, str],
        sensitivity: str,
        classifier_version: str,
        occurred_at: datetime,
    ) -> Turn:
        value = Turn(
            turn_id=turn_id,
            session_id=session_id,
            surface=surface,
            actor=actor,
            kind=kind,
            text=text,
            audience=audience,
            language=language,
            register=register,
            sensitivity=sensitivity,
            classifier_version=classifier_version,
            occurred_at=occurred_at,
            recorded_at=self._clock(),
        )
        self.get_session(session_id)
        self._store.add_turn(value)
        return value

    def get_turn(self, turn_id: str) -> Turn:
        value = self._store.get_turn(turn_id)
        if value is None:
            raise EvidenceNotFound()
        return value

    def add_receipt(self, value: DeliveryReceipt) -> DeliveryReceipt:
        turn = self.get_turn(value.turn_id)
        validate_receipt(value, turn.text)
        self._store.add_receipt(value)
        return value

    @contextmanager
    def list_receipts(self, turn_id: str) -> Iterator[Iterator[DeliveryReceipt]]:
        self.get_turn(turn_id)
        with self._store.list_receipts(turn_id) as values:
            yield values

    def list_perceived_turns(self, character_id: str) -> AbstractContextManager[Iterator[Turn]]:
        return self._store.list_perceived_turns(character_id)
