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
    return tuple(values)


@dataclass(frozen=True, kw_only=True)
class Session:
    session_id: str
    started_at: datetime
    ended_at: datetime | None
    surface: str
    participants: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonblank(self.session_id)
        _nonblank(self.surface)
        _aware(self.started_at)
        if self.ended_at is not None:
            _aware(self.ended_at)
        object.__setattr__(self, "participants", _members(self.participants))


@dataclass(frozen=True, kw_only=True)
class Turn:
    turn_id: str
    session_id: str
    surface: str
    actor: str
    kind: str
    text: str
    audience: tuple[str, ...]
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
        if self.kind not in ("utterance", "observation"):
            raise EvidenceValidationError("Invalid turn kind")
        if not isinstance(self.text, str):
            raise EvidenceValidationError("Expected text")
        if not isinstance(self.register, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.register.items()
        ):
            raise EvidenceValidationError("Expected string register metadata")
        object.__setattr__(self, "register", MappingProxyType(dict(self.register)))
        object.__setattr__(self, "audience", _members(self.audience))
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
    return session.session_id == turn.session_id and (
        character_id == turn.actor
        or character_id in (turn.audience if turn.audience else session.participants)
    )


def validate_receipt(receipt: DeliveryReceipt, text: str) -> None:
    length = len(text)
    end = receipt.spoken_until
    if not (
        (receipt.status == "full" and end == length)
        or (receipt.status == "partial" and 0 < end < length)
        or (receipt.status == "none" and end == 0)
    ):
        raise EvidenceValidationError("Delivery offset does not match text and status")
