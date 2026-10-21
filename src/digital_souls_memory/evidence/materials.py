"""Resolve perception and current delivery range without changing the evidence ledger."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from digital_souls_memory.evidence.domain import DeliveryReceipt, Session, Turn, can_perceive
from digital_souls_memory.evidence.ports import CharacterRegistry, EvidenceStore
from digital_souls_memory.evidence.use_cases import Clock


@dataclass(frozen=True)
class MaterialDecision:
    text: str | None
    basis: str
    receipt_version: int
    deadline: datetime


@dataclass(frozen=True)
class ReceiptState:
    spoken_until: int | None
    count: int


def aggregate_receipts(receipts: Iterable[DeliveryReceipt]) -> ReceiptState:
    spoken_until = None
    count = 0
    for receipt in receipts:
        spoken_until = receipt.spoken_until
        count += 1
    return ReceiptState(spoken_until, count)


def material_decision(
    *,
    character_id: str,
    session: Session,
    turn: Turn,
    actor_is_character: bool,
    receipts: Iterable[DeliveryReceipt] | ReceiptState,
    now: datetime,
    delivery_timeout: timedelta,
) -> MaterialDecision:
    deadline = turn.recorded_at + delivery_timeout
    if not can_perceive(character_id, session, turn):
        return MaterialDecision(None, "inaccessible", 0, deadline)
    if (
        not session.reports_delivery
        or not actor_is_character
        or turn.kind != "utterance"
        or character_id == turn.actor
    ):
        basis = "declared_full" if not session.reports_delivery else "original"
        return MaterialDecision(turn.text, basis, 0, deadline)
    state = receipts if isinstance(receipts, ReceiptState) else aggregate_receipts(receipts)
    if state.spoken_until is not None:
        return MaterialDecision(
            turn.text[: state.spoken_until], "receipt_confirmed", state.count, deadline
        )
    if now >= deadline:
        return MaterialDecision(turn.text, "assumed_full", 0, deadline)
    return MaterialDecision(None, "pending", 0, deadline)


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
    return material_decision(
        character_id=character_id,
        session=session,
        turn=turn,
        actor_is_character=actor_is_character,
        receipts=receipts,
        now=now,
        delivery_timeout=delivery_timeout,
    ).text


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
        decision = self.get_decision(character_id, turn_id)
        return decision.text if decision is not None else None

    def get_decision(self, character_id: str, turn_id: str) -> MaterialDecision | None:
        turn = self.store.get_turn(turn_id)
        if turn is None:
            return None
        session = self.store.get_session(turn.session_id)
        if session is None or not can_perceive(character_id, session, turn):
            return None
        with self.store.list_receipts(turn_id) as receipts:
            return material_decision(
                character_id=character_id,
                session=session,
                turn=turn,
                actor_is_character=self.characters.is_character(turn.actor),
                receipts=receipts,
                now=self.clock(),
                delivery_timeout=self.delivery_timeout,
            )
