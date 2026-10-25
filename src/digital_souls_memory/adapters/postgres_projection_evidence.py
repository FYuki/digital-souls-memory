"""Batch evidence inputs shared by listing and transactional projection writes."""

from datetime import datetime, timedelta

import psycopg
from psycopg.rows import DictRow

from digital_souls_memory.evidence.domain import Session, Turn, can_perceive
from digital_souls_memory.evidence.materials import ReceiptState, material_decision
from digital_souls_memory.memory.inputs import ResolvedTurn, input_eligible, resolve_input
from digital_souls_memory.memory.projection import (
    ProjectionEvidence,
    ProjectionInput,
    extraction_origins,
    source_ids,
)


def read_projection_evidence(
    connection: psycopg.Connection[DictRow],
    snapshot: ProjectionInput,
    character_id: str,
    now: datetime,
    *,
    delivery_timeout: timedelta,
    conversation_surfaces: frozenset[str],
    comment_surfaces: frozenset[str],
) -> ProjectionEvidence:
    origins = extraction_origins(snapshot)
    rows = connection.execute(
        "SELECT t.*,evidence_is_character(t.actor) AS registered, "
        "EXISTS(SELECT 1 FROM turns reply WHERE reply.reply_to_turn_id=t.turn_id "
        "AND reply.actor=%s AND %s=ANY(reply.perceived_by)) AS replied, "
        "r.count,r.spoken_until FROM turns t "
        "CROSS JOIN LATERAL (SELECT count(*) AS count, "
        "(SELECT spoken_until FROM delivery_receipts WHERE turn_id=t.turn_id "
        "ORDER BY receipt_id DESC LIMIT 1) AS spoken_until "
        "FROM delivery_receipts WHERE turn_id=t.turn_id) r "
        "WHERE t.turn_id=ANY(%s) OR t.turn_id IN "
        "(SELECT reply_to_turn_id FROM turns WHERE turn_id=ANY(%s))",
        (character_id, character_id, list(source_ids(snapshot)), list(origins)),
    ).fetchall()
    sessions = {
        row["session_id"]: Session(**row)
        for row in connection.execute(
            "SELECT * FROM sessions WHERE session_id=ANY(%s)",
            (list({row["session_id"] for row in rows}),),
        ).fetchall()
    }
    values = {}
    for row in rows:
        payload = dict(row)
        registered = payload.pop("registered")
        replied = payload.pop("replied")
        receipts = ReceiptState(payload.pop("spoken_until"), payload.pop("count"))
        turn = Turn(**payload)
        session = sessions[turn.session_id]
        decision = (
            material_decision(
                character_id=character_id,
                session=session,
                turn=turn,
                actor_is_character=registered,
                receipts=receipts,
                now=now,
                delivery_timeout=delivery_timeout,
            )
            if can_perceive(character_id, session, turn)
            else None
        )
        values[turn.turn_id] = ResolvedTurn(
            turn,
            decision,
            input_eligible(
                turn,
                actor_is_character=registered,
                has_character_reply=replied,
                conversation_surfaces=conversation_surfaces,
                comment_surfaces=comment_surfaces,
            ),
        )
    return ProjectionEvidence(
        {identifier: value.turn for identifier, value in values.items()},
        {
            identifier: value.decision.text if value.decision else None
            for identifier, value in values.items()
        },
        {origin: resolve_input((origin,), values) for origin in origins},
    )
