"""Batch material restoration and source stamps preserve delivery decisions."""

from dataclasses import asdict, fields
from datetime import timedelta
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg.rows import DictRow

from digital_souls_memory.adapters.recall_records import material_stamp, read_materials
from digital_souls_memory.evidence.materials import ReceiptState
from digital_souls_memory.recall.domain import CandidateMaterial
from tests.evidence_support import RECORDED, session, turn

pytestmark = pytest.mark.ut


def test_batch_restores_materials_without_supplementing_missing_sources() -> None:
    connection = MagicMock()
    source = turn(text="日本語")
    source_session = session()
    payload = asdict(source_session)
    for key in ("started_at", "ended_at"):
        timestamp = getattr(source_session, key)
        payload[key] = timestamp.isoformat() if timestamp is not None else None
    connection.execute.return_value.fetchall.return_value = [
        {field.name: getattr(source, field.name) for field in fields(source)}
        | {
            "session": payload,
            "registered": True,
            "count": 3,
            "spoken_until": 1,
        }
    ]
    result = read_materials(
        cast(psycopg.Connection[DictRow], connection), (source.turn_id, "missing")
    )
    assert result == {
        source.turn_id: CandidateMaterial(
            turn=source,
            session=source_session,
            actor_is_character=True,
            receipt_state=ReceiptState(1, 3),
        )
    }
    assert connection.execute.call_count == 1
    assert connection.execute.call_args.args[1] == ([source.turn_id, "missing"],)
    connection.reset_mock()
    assert read_materials(cast(psycopg.Connection[DictRow], connection), ()) == {}
    connection.execute.assert_not_called()


@pytest.mark.parametrize("reader,end,version", [("char-b", 1, 3), ("char-c", 3, 0)])
def test_material_stamp_keeps_reader_range_and_receipt_version(
    reader: str,
    end: int,
    version: int,
) -> None:
    material = CandidateMaterial(
        turn=turn(text="日本語"),
        session=session(),
        actor_is_character=True,
        receipt_state=ReceiptState(1, 3),
    )
    stamp = material_stamp(
        material, character_id=reader, used_end=1, now=RECORDED, timeout=timedelta(minutes=10)
    )
    assert stamp is not None
    assert (stamp.character_id, stamp.available_end, stamp.used_end, stamp.receipt_version) == (
        reader,
        end,
        1,
        version,
    )
    pending = CandidateMaterial(
        turn=material.turn,
        session=material.session,
        actor_is_character=True,
        receipt_state=ReceiptState(None, 0),
    )
    if reader == "char-b":
        assert (
            material_stamp(
                pending,
                character_id=reader,
                used_end=None,
                now=RECORDED,
                timeout=timedelta(minutes=10),
            )
            is None
        )
