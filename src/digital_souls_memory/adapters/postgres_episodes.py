"""Transaction-local episode batch persistence and restoration."""

from dataclasses import asdict
from datetime import datetime

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.recall_records import json_record, restore_stamp
from digital_souls_memory.episodes.domain import Episode, TurnSpan
from digital_souls_memory.evidence.ports import EvidenceConflict
from digital_souls_memory.resident.domain import ContextKey


def replace_episodes(
    connection: psycopg.Connection[DictRow], key: ContextKey, values: tuple[Episode, ...]
) -> None:
    ids = [value.episode_id for value in values]
    if (
        len(set(ids)) != len(ids)
        or connection.execute(
            "SELECT 1 FROM episodes WHERE episode_id=ANY(%s) LIMIT 1", (ids,)
        ).fetchone()
        is not None
    ):
        raise EvidenceConflict()
    connection.execute(
        "DELETE FROM episodes WHERE character_id=%s AND session_id=%s AND surface=%s",
        (key.character_id, key.session_id, key.surface),
    )
    for value in values:
        connection.execute(
            "INSERT INTO "
            "episodes(episode_id,character_id,session_id,surface,topic,time_start,time_end,"
            "model,prompt_version,created_at,input_signature,dependencies) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                value.episode_id,
                value.character_id,
                value.session_id,
                value.surface,
                value.topic,
                *value.time_range,
                value.model,
                value.prompt_version,
                value.created_at,
                value.input_signature,
                Jsonb(json_record({"values": [asdict(v) for v in value.dependencies]})),
            ),
        )
        for position, span in enumerate(value.turn_span):
            connection.execute(
                'INSERT INTO episode_spans(episode_id,position,turn_id,"start","end") '
                "VALUES(%s,%s,%s,%s,%s)",
                (value.episode_id, position, span.turn_id, span.start, span.end),
            )


def read_episodes(connection: psycopg.Connection[DictRow], key: ContextKey) -> tuple[Episode, ...]:
    rows = connection.execute(
        "SELECT * FROM episodes WHERE character_id=%s AND session_id=%s AND surface=%s "
        "ORDER BY created_at,episode_id",
        (key.character_id, key.session_id, key.surface),
    ).fetchall()
    values = []
    for row in rows:
        spans = connection.execute(
            'SELECT turn_id,"start","end" FROM episode_spans WHERE episode_id=%s ORDER BY position',
            (row["episode_id"],),
        ).fetchall()
        start: datetime = row.pop("time_start")
        end: datetime = row.pop("time_end")
        values.append(
            Episode(
                **(
                    row
                    | {
                        "time_range": (start, end),
                        "turn_span": tuple(TurnSpan(**span) for span in spans),
                        "dependencies": tuple(
                            restore_stamp(v) for v in row["dependencies"]["values"]
                        ),
                    }
                )
            )
        )
    return tuple(values)
