"""Evidence values, independent of HTTP and persistence."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType


class EvidenceValidationError(ValueError):
    """An evidence value violates its contract."""


def _nonblank(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceValidationError("Expected a nonblank string")


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise EvidenceValidationError("Expected a timezone-aware timestamp")


def _members(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise EvidenceValidationError("Expected a collection of identifiers")
    for value in values:
        _nonblank(value)
    if len(set(values)) != len(values):
        raise EvidenceValidationError("Duplicate identifiers")
    return tuple(values)


def resolve_address_mode(
    *, address_mode: str | None, actor_is_character: bool, addressees: tuple[str, ...]
) -> str:
    if address_mode is None and not actor_is_character:
        address_mode = "broadcast"
    if address_mode not in ("direct", "broadcast", "self"):
        raise EvidenceValidationError("Invalid address mode")
    if (address_mode == "direct") != bool(addressees):
        raise EvidenceValidationError("Address mode does not match addressees")
    return address_mode


@dataclass(frozen=True, kw_only=True)
class Session:
    session_id: str
    started_at: datetime
    ended_at: datetime | None
    surface: str
    reports_delivery: bool

    def __post_init__(self) -> None:
        _nonblank(self.session_id)
        _nonblank(self.surface)
        _aware(self.started_at)
        if self.ended_at is not None:
            _aware(self.ended_at)
        if type(self.reports_delivery) is not bool:
            raise EvidenceValidationError("Expected a boolean delivery declaration")


@dataclass(frozen=True, kw_only=True)
class Turn:
    turn_id: str
    session_id: str
    surface: str
    actor: str
    kind: str
    text: str
    perceived_by: tuple[str, ...]
    addressees: tuple[str, ...]
    address_mode: str
    reply_to_turn_id: str | None
    language: str
    register: Mapping[str, str]
    sensitivity: str
    classifier_version: str
    occurred_at: datetime
    recorded_at: datetime

    def __post_init__(self) -> None:
        for value in (
            self.turn_id,
            self.session_id,
            self.surface,
            self.actor,
            self.language,
            self.sensitivity,
            self.classifier_version,
        ):
            _nonblank(value)
        if self.kind not in ("utterance", "observation", "thought"):
            raise EvidenceValidationError("Invalid turn kind")
        if not isinstance(self.text, str):
            raise EvidenceValidationError("Expected text")
        if not isinstance(self.register, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.register.items()
        ):
            raise EvidenceValidationError("Expected string register metadata")
        object.__setattr__(self, "register", MappingProxyType(dict(self.register)))
        object.__setattr__(self, "perceived_by", _members(self.perceived_by))
        object.__setattr__(self, "addressees", _members(self.addressees))
        resolve_address_mode(
            address_mode=self.address_mode, actor_is_character=True, addressees=self.addressees
        )
        if self.reply_to_turn_id is not None:
            _nonblank(self.reply_to_turn_id)
        if self.kind == "thought" and self.perceived_by != (self.actor,):
            raise EvidenceValidationError("Thought requires exactly actor perception")
        _aware(self.occurred_at)
        _aware(self.recorded_at)


@dataclass(frozen=True, kw_only=True)
class DeliveryReceipt:
    turn_id: str
    status: str
    spoken_until: int
    reported_by: str
    reported_at: datetime

    def __post_init__(self) -> None:
        _nonblank(self.turn_id)
        _nonblank(self.reported_by)
        _aware(self.reported_at)
        if self.status not in ("full", "partial", "none"):
            raise EvidenceValidationError("Invalid delivery status")
        if type(self.spoken_until) is not int or self.spoken_until < 0:
            raise EvidenceValidationError("Expected a nonnegative integer offset")


def can_perceive(character_id: str, session: Session, turn: Turn) -> bool:
    return session.session_id == turn.session_id and character_id in turn.perceived_by


def validate_receipt(receipt: DeliveryReceipt, text: str) -> None:
    length = len(text)
    end = receipt.spoken_until
    if not (
        (receipt.status == "full" and end == length)
        or (receipt.status == "partial" and 0 < end < length)
        or (receipt.status == "none" and end == 0)
    ):
        raise EvidenceValidationError("Delivery offset does not match text and status")
