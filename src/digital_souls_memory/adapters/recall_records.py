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
    ResidentCandidateRef,
    ResidentCandidateScore,
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
    return read_materials(connection, (turn_id,)).get(turn_id)


def read_materials(
    connection: psycopg.Connection[DictRow], turn_ids: tuple[str, ...]
) -> dict[str, CandidateMaterial]:
    if not turn_ids:
        return {}
    rows = connection.execute(
        "SELECT t.*,row_to_json(s) AS session, "
        "(EXISTS(SELECT 1 FROM core_identities c WHERE c.character_id=t.actor) OR "
        "EXISTS(SELECT 1 FROM temperaments c WHERE c.character_id=t.actor)) AS registered, "
        "r.count,r.spoken_until FROM turns t JOIN sessions s USING(session_id) "
        "CROSS JOIN LATERAL (SELECT count(*) AS count, "
        "(SELECT spoken_until FROM delivery_receipts WHERE turn_id=t.turn_id "
        "ORDER BY receipt_id DESC LIMIT 1) AS spoken_until "
        "FROM delivery_receipts WHERE turn_id=t.turn_id) r WHERE t.turn_id=ANY(%s)",
        (list(turn_ids),),
    ).fetchall()
    result = {}
    for row in rows:
        payload = dict(row)
        session = payload.pop("session")
        registered = payload.pop("registered")
        receipts = ReceiptState(payload.pop("spoken_until"), payload.pop("count"))
        for key in ("started_at", "ended_at"):
            if session[key] is not None:
                session[key] = datetime.fromisoformat(session[key])
        result[payload["turn_id"]] = CandidateMaterial(
            turn=Turn(**payload),
            session=Session(**session),
            actor_is_character=registered,
            receipt_state=receipts,
        )
    return result


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
    return material_stamp(
        material, character_id=character_id, used_end=used_end, now=now, timeout=timeout
    )


def material_stamp(
    material: CandidateMaterial,
    *,
    character_id: str,
    used_end: int | None,
    now: datetime,
    timeout: timedelta,
) -> SourceStamp | None:
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
        turn_id=material.turn.turn_id,
        character_id=character_id,
        basis=decision.basis,
        receipt_version=decision.receipt_version,
        available_end=len(decision.text),
        used_end=len(decision.text) if used_end is None else used_end,
        deadline=decision.deadline,
    )


def provision_payload(value: ProvisionRecord) -> dict[str, object]:
    payload = asdict(value)
    if value.kind == "turn":
        del payload["profile_entry_ids"]
        del payload["core_identity_version"]
    return json_record(payload)


def restore_provision(payload: DictRow) -> ProvisionRecord:
    scores = tuple(
        ResidentCandidateScore(**(v | {"ref": ResidentCandidateRef(**v["ref"])}))
        if payload["kind"] == "resident"
        else CandidateScore(**(v | {"ref": CandidateRef(**v["ref"])}))
        for v in payload["scores"]
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
                "profile_entry_ids": tuple(payload.get("profile_entry_ids", ())),
            }
        )
    )
