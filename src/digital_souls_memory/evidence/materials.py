"""Resolve perception and current delivery range without changing the evidence ledger."""

from collections.abc import Iterable
from datetime import datetime, timedelta

from digital_souls_memory.evidence.domain import DeliveryReceipt, Session, Turn, can_perceive
from digital_souls_memory.evidence.ports import CharacterRegistry, EvidenceStore
from digital_souls_memory.evidence.use_cases import Clock


def available_text(
    *,
    character_id: str,
    session: Session,
    turn: Turn,
    actor_is_character: bool,
    receipts: Iterable[DeliveryReceipt],
    now: datetime,
    delivery_timeout: timedelta,
) -> str | None:
    if not can_perceive(character_id, session, turn):
        return None
    if (
        not session.reports_delivery
        or not actor_is_character
        or turn.kind != "utterance"
        or character_id == turn.actor
    ):
        return turn.text
    latest = None
    for receipt in receipts:
        latest = receipt
    if latest is not None:
        return turn.text[: latest.spoken_until]
    if now >= turn.recorded_at + delivery_timeout:
        return turn.text
    return None


class EvidenceMaterials:
    def __init__(
        self,
        store: EvidenceStore,
        characters: CharacterRegistry,
        clock: Clock,
        delivery_timeout: timedelta,
    ) -> None:
        self.store = store
        self.characters = characters
        self.clock = clock
        self.delivery_timeout = delivery_timeout

    def get_text(self, character_id: str, turn_id: str) -> str | None:
        turn = self.store.get_turn(turn_id)
        if turn is None:
            return None
        session = self.store.get_session(turn.session_id)
        if session is None or not can_perceive(character_id, session, turn):
            return None
        with self.store.list_receipts(turn_id) as receipts:
            return available_text(
                character_id=character_id,
                session=session,
                turn=turn,
                actor_is_character=self.characters.is_character(turn.actor),
                receipts=receipts,
                now=self.clock(),
                delivery_timeout=self.delivery_timeout,
            )
