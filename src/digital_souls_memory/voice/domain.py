"""Immutable approved text and references; no model or infrastructure dependencies."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType


class VoiceValidationError(ValueError):
    """A voice material violates its registration contract."""


def _nonblank(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise VoiceValidationError("Expected nonblank text")


def _registration(character_id: str, identifier: str, approved_by: str, at: datetime) -> None:
    for value in (character_id, identifier, approved_by):
        _nonblank(value)
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise VoiceValidationError("Expected aware timestamp")


@dataclass(frozen=True, kw_only=True)
class VoiceExemplar:
    character_id: str
    exemplar_id: str
    text: str
    source: str
    turn_id: str | None
    situation_tags: tuple[str, ...]
    emotion_tags: tuple[str, ...]
    register: Mapping[str, str]
    trait_version: int
    approved_by: str
    registered_at: datetime

    def __post_init__(self) -> None:
        _registration(self.character_id, self.exemplar_id, self.approved_by, self.registered_at)
        _nonblank(self.text)
        if self.source == "canonical_seed":
            if self.turn_id is not None:
                raise VoiceValidationError("Canonical seed cannot reference a turn")
        elif self.source == "turn_id":
            if self.turn_id is None:
                raise VoiceValidationError("Turn source requires a reference")
            _nonblank(self.turn_id)
        else:
            raise VoiceValidationError("Invalid source")
        if type(self.trait_version) is not int or self.trait_version < 1:
            raise VoiceValidationError("Expected positive trait version")
        for name in ("situation_tags", "emotion_tags"):
            tags = getattr(self, name)
            if not isinstance(tags, (list, tuple)):
                raise VoiceValidationError("Expected text collection")
            for tag in tags:
                _nonblank(tag)
            object.__setattr__(self, name, tuple(tags))
        if not isinstance(self.register, Mapping) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in self.register.items()
        ):
            raise VoiceValidationError("Expected string register metadata")
        object.__setattr__(self, "register", MappingProxyType(dict(self.register)))


@dataclass(frozen=True, kw_only=True)
class VerbatimAnchor:
    character_id: str
    anchor_id: str
    turn_id: str
    kind: str
    reason: str
    approved_by: str
    registered_at: datetime

    def __post_init__(self) -> None:
        _registration(self.character_id, self.anchor_id, self.approved_by, self.registered_at)
        _nonblank(self.turn_id)
        _nonblank(self.reason)
        if self.kind not in ("identity_statement", "commitment", "nickname", "anniversary"):
            raise VoiceValidationError("Invalid anchor kind")
