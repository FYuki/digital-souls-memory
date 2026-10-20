"""Immutable identity values and write policy, independent of infrastructure."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from types import MappingProxyType

ASPECT_KEYS = (
    "Volatility",
    "Withdrawal",
    "Compassion",
    "Politeness",
    "Industriousness",
    "Orderliness",
    "Enthusiasm",
    "Assertiveness",
    "Intellect",
    "Openness",
)


class IdentityValidationError(ValueError):
    """An identity value violates its contract."""


def validate_aspects(values: Mapping[str, object]) -> Mapping[str, float]:
    if not isinstance(values, Mapping) or set(values) != set(ASPECT_KEYS):
        raise IdentityValidationError("Expected exactly ten aspects")
    numbers: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise IdentityValidationError("Expected numeric aspects")
        if not 0 <= value <= 1 or not isfinite(value):
            raise IdentityValidationError("Expected finite aspect values between zero and one")
        numbers[key] = value
    return MappingProxyType(numbers)


def validate_write(*, source: str, operation: str, has_profile: bool) -> None:
    if source != "registration" or operation not in ("core_identity", "temperament", "author_seed"):
        raise PermissionError("Identity writes require registration")
    if operation == "author_seed" and has_profile:
        raise IdentityValidationError("Author seed already exists")


def _nonblank(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise IdentityValidationError("Expected nonblank text")


def _versioned(character_id: str, version: int, at: datetime) -> None:
    _nonblank(character_id)
    if type(version) is not int or version < 1:
        raise IdentityValidationError("Expected positive version")
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise IdentityValidationError("Expected aware timestamp")


@dataclass(frozen=True)
class CoreIdentity:
    character_id: str
    values: tuple[str, ...] | list[str]
    prohibitions: tuple[str, ...] | list[str]
    version: int
    registered_at: datetime

    def __post_init__(self) -> None:
        _versioned(self.character_id, self.version, self.registered_at)
        for name in ("values", "prohibitions"):
            members = getattr(self, name)
            if not isinstance(members, (list, tuple)):
                raise IdentityValidationError("Expected text collection")
            for member in members:
                _nonblank(member)
            object.__setattr__(self, name, tuple(members))


@dataclass(frozen=True)
class Temperament:
    character_id: str
    description: str
    version: int
    registered_at: datetime

    def __post_init__(self) -> None:
        _versioned(self.character_id, self.version, self.registered_at)
        _nonblank(self.description)


@dataclass(frozen=True)
class PersonalityProfile:
    character_id: str
    aspects: Mapping[str, float]
    description: str
    renderer_version: str
    temperament_version: int
    version: int
    valid_from: datetime
    valid_to: datetime | None
    evidence_belief_ids: tuple[str, ...]
    change_kind: str

    def __post_init__(self) -> None:
        _versioned(self.character_id, self.version, self.valid_from)
        _versioned(self.character_id, self.temperament_version, self.valid_from)
        _nonblank(self.description)
        _nonblank(self.renderer_version)
        if self.valid_to is not None:
            _versioned(self.character_id, self.version, self.valid_to)
            if self.valid_to <= self.valid_from:
                raise IdentityValidationError("Invalid validity interval")
        if self.change_kind != "author_seed" or self.evidence_belief_ids:
            raise IdentityValidationError("Only author seed without beliefs is supported")
        object.__setattr__(self, "aspects", validate_aspects(self.aspects))
        object.__setattr__(self, "evidence_belief_ids", tuple(self.evidence_belief_ids))


def select_current_profile(
    values: Iterable[PersonalityProfile], at: datetime
) -> PersonalityProfile | None:
    return max(
        (v for v in values if v.valid_from <= at and (v.valid_to is None or at < v.valid_to)),
        key=lambda v: v.version,
        default=None,
    )
