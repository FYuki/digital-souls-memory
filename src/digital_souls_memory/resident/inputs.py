"""Resolve a single perceived window without adding reply targets."""

import hashlib
import json
from datetime import datetime, timedelta

from digital_souls_memory.evidence.domain import Session
from digital_souls_memory.evidence.materials import material_decision
from digital_souls_memory.recall.domain import CandidateMaterial, SourceStamp
from digital_souls_memory.resident.domain import (
    ContextKey,
    ContextSnapshot,
    ResolvedContextTurn,
    ResolvedWindow,
)


def resolve_window(
    *,
    key: ContextKey,
    session: Session,
    materials: tuple[CandidateMaterial, ...],
    notification_version: int,
    now: datetime,
    delivery_timeout: timedelta,
) -> ResolvedWindow:
    if key.session_id != session.session_id:
        raise ValueError("Context session mismatch")
    resolved = []
    dependencies = []
    signature_values: list[object] = [
        key.character_id,
        key.session_id,
        key.surface,
        notification_version,
        str(session.ended_at),
    ]
    deadlines = []
    for material in sorted(
        materials, key=lambda m: (m.turn.occurred_at, m.turn.recorded_at, m.turn.turn_id)
    ):
        turn = material.turn
        if turn.session_id != key.session_id or turn.surface != key.surface:
            continue
        decision = material_decision(
            character_id=key.character_id,
            session=material.session,
            turn=turn,
            actor_is_character=material.actor_is_character,
            receipts=material.receipt_state,
            now=now,
            delivery_timeout=delivery_timeout,
        )
        signature_values.append(
            (turn.turn_id, decision.text, decision.basis, decision.receipt_version)
        )
        if decision.basis == "pending":
            deadlines.append(decision.deadline)
        if not decision.text:
            continue
        resolved.append(ResolvedContextTurn(material=material, decision=decision))
        dependencies.append(
            SourceStamp(
                turn_id=turn.turn_id,
                character_id=key.character_id,
                basis=decision.basis,
                receipt_version=decision.receipt_version,
                available_end=len(decision.text),
                used_end=len(decision.text),
                deadline=decision.deadline,
            )
        )
    signature = hashlib.sha256(
        json.dumps(signature_values, ensure_ascii=False).encode()
    ).hexdigest()
    return ResolvedWindow(
        key=key,
        session=session,
        turns=tuple(resolved),
        snapshot=ContextSnapshot(
            key=key,
            notification_version=notification_version,
            signature=signature,
            dependencies=tuple(dependencies),
            session_ended_at=session.ended_at,
            next_due_at=min(deadlines) if deadlines else None,
        ),
    )
