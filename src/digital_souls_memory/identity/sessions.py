"""Shared, session-start identity selection for recall APIs."""

from dataclasses import dataclass
from typing import Protocol

from digital_souls_memory.evidence.domain import Session
from digital_souls_memory.identity.domain import PersonalityProfile, Temperament


@dataclass(frozen=True, kw_only=True)
class SessionIdentity:
    session_id: str
    character_id: str
    temperament_version: int | None
    personality_version: int | None


class SessionIdentityStore(Protocol):
    def select(self, *, character_id: str, session: Session) -> SessionIdentity: ...


def choose_session_identity(
    *,
    character_id: str,
    session: Session,
    temperaments: tuple[Temperament, ...],
    profiles: tuple[PersonalityProfile, ...],
) -> SessionIdentity:
    temperament = max(
        (
            v.version
            for v in temperaments
            if v.character_id == character_id and v.registered_at <= session.started_at
        ),
        default=None,
    )
    personality = max(
        (
            v.version
            for v in profiles
            if v.character_id == character_id
            and v.valid_from <= session.started_at
            and (v.valid_to is None or session.started_at < v.valid_to)
        ),
        default=None,
    )
    return SessionIdentity(
        session_id=session.session_id,
        character_id=character_id,
        temperament_version=temperament,
        personality_version=personality,
    )
