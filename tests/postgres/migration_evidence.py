"""Synthetic SQL fixtures for the explicit 0004–0010 evidence contract."""

import psycopg
from psycopg.types.json import Jsonb

from digital_souls_memory.evidence.domain import Session, Turn
from tests.evidence_support import RECORDED


def seed_evidence_before_actor_registry(
    url: str, *, characters: tuple[str, ...], session: Session, turns: tuple[Turn, ...]
) -> None:
    with psycopg.connect(url) as connection:
        for character in characters:
            connection.execute(
                'INSERT INTO core_identities(character_id,"values",prohibitions,version,'
                "registered_at) VALUES (%s,ARRAY['合成価値'],ARRAY[]::text[],1,%s)",
                (character, RECORDED),
            )
        connection.execute(
            "INSERT INTO sessions(session_id,started_at,ended_at,surface,reports_delivery) "
            "VALUES (%s,%s,%s,%s,%s)",
            (
                session.session_id,
                session.started_at,
                session.ended_at,
                session.surface,
                session.reports_delivery,
            ),
        )
        for turn in turns:
            connection.execute(
                "INSERT INTO turns(turn_id,session_id,surface,actor,kind,text,perceived_by,"
                "addressees,address_mode,reply_to_turn_id,language,register,sensitivity,"
                "classifier_version,occurred_at,recorded_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    turn.turn_id,
                    turn.session_id,
                    turn.surface,
                    turn.actor,
                    turn.kind,
                    turn.text,
                    list(turn.perceived_by),
                    list(turn.addressees),
                    turn.address_mode,
                    turn.reply_to_turn_id,
                    turn.language,
                    Jsonb(dict(turn.register)),
                    turn.sensitivity,
                    turn.classifier_version,
                    turn.occurred_at,
                    turn.recorded_at,
                ),
            )
