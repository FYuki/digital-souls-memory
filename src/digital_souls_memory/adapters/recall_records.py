"""Explicit conversions and transaction-local evidence reads for recall persistence."""

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import cast

import psycopg
from psycopg.rows import DictRow

from digital_souls_memory.evidence.domain import Session, Turn
from digital_souls_memory.evidence.materials import ReceiptState, material_decision
from digital_souls_memory.evidence.provision import (
    CandidateRef,
    CandidateScore,
    ProvisionRecord,
    SourceSpan,
)
from digital_souls_memory.recall.domain import CandidateMaterial, SourceStamp


def json_record(value: object) -> dict[str, object]:
    return cast(dict[str, object], json.loads(json.dumps(value, default=_timestamp)))


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unsupported persistent value")


def restore_stamp(value: dict[str, object]) -> SourceStamp:
    return SourceStamp(
        turn_id=str(value["turn_id"]),
        character_id=str(value["character_id"]),
        basis=str(value["basis"]),
        receipt_version=int(str(value["receipt_version"])),
        available_end=int(str(value["available_end"])),
        used_end=int(str(value["used_end"])),
        deadline=datetime.fromisoformat(str(value["deadline"])),
    )


def read_material(
    connection: psycopg.Connection[DictRow], turn_id: str
) -> CandidateMaterial | None:
    row = connection.execute("SELECT * FROM turns WHERE turn_id=%s", (turn_id,)).fetchone()
    if row is None:
        return None
    turn = Turn(**row)
    session = connection.execute(
        "SELECT * FROM sessions WHERE session_id=%s", (turn.session_id,)
    ).fetchone()
    if session is None:
        return None
    actor = connection.execute(
        "SELECT 1 FROM core_identities WHERE character_id=%s LIMIT 1", (turn.actor,)
    ).fetchone()
    receipts = connection.execute(
        "SELECT (SELECT count(*) FROM delivery_receipts WHERE turn_id=%s) AS count, "
        "(SELECT spoken_until FROM delivery_receipts WHERE turn_id=%s "
        "ORDER BY receipt_id DESC LIMIT 1) AS spoken_until",
        (turn_id, turn_id),
    ).fetchone()
    assert receipts is not None
    return CandidateMaterial(
        turn=turn,
        session=Session(**session),
        actor_is_character=actor is not None,
        receipt_state=ReceiptState(spoken_until=receipts["spoken_until"], count=receipts["count"]),
    )


def current_stamp(
    connection: psycopg.Connection[DictRow],
    *,
    turn_id: str,
    character_id: str,
    used_end: int | None,
    now: datetime,
    timeout: timedelta,
) -> SourceStamp | None:
    material = read_material(connection, turn_id)
    if material is None:
        return None
    decision = material_decision(
        character_id=character_id,
        session=material.session,
        turn=material.turn,
        actor_is_character=material.actor_is_character,
        receipts=material.receipt_state,
        now=now,
        delivery_timeout=timeout,
    )
    if decision.text is None:
        return None
    return SourceStamp(
        turn_id=turn_id,
        character_id=character_id,
        basis=decision.basis,
        receipt_version=decision.receipt_version,
        available_end=len(decision.text),
        used_end=len(decision.text) if used_end is None else used_end,
        deadline=decision.deadline,
    )


def provision_payload(value: ProvisionRecord) -> dict[str, object]:
    return json_record(asdict(value))


def restore_provision(payload: DictRow) -> ProvisionRecord:
    scores = tuple(
        CandidateScore(**(v | {"ref": CandidateRef(**v["ref"])})) for v in payload["scores"]
    )
    return ProvisionRecord(
        **(
            payload
            | {
                "scores": scores,
                "source_spans": tuple(SourceSpan(**v) for v in payload["source_spans"]),
                "memory_ids": tuple(payload["memory_ids"]),
                "verbatim_anchor_ids": tuple(payload["verbatim_anchor_ids"]),
                "voice_exemplar_ids": tuple(payload["voice_exemplar_ids"]),
                "motivation_ids": tuple(payload["motivation_ids"]),
                "provided_at": datetime.fromisoformat(payload["provided_at"]),
            }
        )
    )
