"""Evidence registration and retrieval through the storage port."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime

from digital_souls_memory.evidence.domain import (
    DeliveryReceipt,
    EvidenceValidationError,
    Session,
    SessionEnding,
    Turn,
    resolve_address_mode,
    validate_receipt,
)
from digital_souls_memory.evidence.ports import (
    CharacterRegistry,
    EvidenceNotFound,
    EvidenceStore,
    EvidenceUnavailable,
)

Clock = Callable[[], datetime]


class EvidenceService:
    def __init__(
        self, store: EvidenceStore, clock: Clock, characters: CharacterRegistry | None = None
    ) -> None:
        self._store = store
        self._clock = clock
        self._characters = characters

    def add_session(self, value: Session) -> Session:
        self._store.add_session(value)
        return value

    def get_session(self, session_id: str) -> Session:
        value = self._store.get_session(session_id)
        if value is None:
            raise EvidenceNotFound()
        return value

    def add_ending(
        self, *, session_id: str, ended_at: datetime, reported_by: str
    ) -> tuple[SessionEnding, bool]:
        self.get_session(session_id)
        return self._store.add_ending(
            SessionEnding(
                session_id=session_id,
                ended_at=ended_at,
                reported_by=reported_by,
                reported_at=self._clock(),
            )
        )

    def list_endings(self, session_id: str) -> tuple[SessionEnding, ...]:
        self.get_session(session_id)
        return self._store.list_endings(session_id)

    def add_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        surface: str,
        actor: str,
        kind: str,
        text: str,
        perceived_by: tuple[str, ...],
        addressees: tuple[str, ...],
        address_mode: str | None,
        reply_to_turn_id: str | None,
        language: str,
        register: Mapping[str, str],
        sensitivity: str,
        classifier_version: str,
        occurred_at: datetime,
    ) -> Turn:
        if self._characters is None:
            raise EvidenceUnavailable()
        actor_is_character = self._characters.is_character(actor)
        mode = resolve_address_mode(
            address_mode=address_mode, actor_is_character=actor_is_character, addressees=addressees
        )
        value = Turn(
            turn_id=turn_id,
            session_id=session_id,
            surface=surface,
            actor=actor,
            kind=kind,
            text=text,
            perceived_by=perceived_by,
            addressees=addressees,
            address_mode=mode,
            reply_to_turn_id=reply_to_turn_id,
            language=language,
            register=register,
            sensitivity=sensitivity,
            classifier_version=classifier_version,
            occurred_at=occurred_at,
            recorded_at=self._clock(),
        )
        if any(
            not (
                actor_is_character
                if identifier == actor
                else self._characters.is_character(identifier)
            )
            for identifier in value.perceived_by
        ):
            raise EvidenceValidationError("Perception requires registered characters")
        if actor_is_character and actor not in value.perceived_by:
            raise EvidenceValidationError("Character actor must perceive its own turn")
        if kind == "thought" and not actor_is_character:
            raise EvidenceValidationError("Thought requires a registered character actor")
        if reply_to_turn_id is not None and self._store.get_turn(reply_to_turn_id) is None:
            raise EvidenceValidationError("Reply target does not exist")
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
        session = self.get_session(turn.session_id)
        if not session.reports_delivery or turn.kind == "thought":
            raise EvidenceValidationError("Turn does not accept delivery receipts")
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
