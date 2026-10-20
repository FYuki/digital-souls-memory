"""Explicit SQL persistence with character-scoped atomic initial registration."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from digital_souls_memory.identity.ports import ReviewResult
from digital_souls_memory.voice.domain import VerbatimAnchor, VoiceExemplar, VoiceValidationError
from digital_souls_memory.voice.ports import (
    VoiceConflict,
    VoiceNotFound,
    VoiceTransaction,
    VoiceUnavailable,
)


class _VoiceTransaction:
    def __init__(self, connection: psycopg.Connection[DictRow], character_id: str) -> None:
        self.connection = connection
        self.character_id = character_id

    def get_initial_check(self) -> ReviewResult | None:
        row = self.connection.execute(
            "SELECT consistent, reason FROM voice_initial_checks WHERE character_id=%s",
            (self.character_id,),
        ).fetchone()
        return ReviewResult(**row) if row is not None else None

    def save_initial_check(self, result: ReviewResult) -> None:
        self.connection.execute(
            "INSERT INTO voice_initial_checks (character_id, consistent, reason) VALUES (%s,%s,%s)",
            (self.character_id, result.consistent, result.reason),
        )

    def _authorize(self, character_id: str) -> None:
        if self.character_id != character_id:
            raise PermissionError("Wrong transaction character")

    def add_exemplar(self, value: VoiceExemplar) -> None:
        self._authorize(value.character_id)
        self.connection.execute(
            "INSERT INTO voice_exemplars (character_id, exemplar_id, text, source, turn_id, "
            "situation_tags, emotion_tags, register, trait_version, approved_by, registered_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                value.character_id,
                value.exemplar_id,
                value.text,
                value.source,
                value.turn_id,
                list(value.situation_tags),
                list(value.emotion_tags),
                Jsonb(dict(value.register)),
                value.trait_version,
                value.approved_by,
                value.registered_at,
            ),
        )

    def add_anchor(self, value: VerbatimAnchor) -> None:
        self._authorize(value.character_id)
        self.connection.execute(
            "INSERT INTO verbatim_anchors (character_id, anchor_id, turn_id, kind, reason, "
            "approved_by, registered_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                value.character_id,
                value.anchor_id,
                value.turn_id,
                value.kind,
                value.reason,
                value.approved_by,
                value.registered_at,
            ),
        )


class PostgresVoiceStore:
    def __init__(self, url: str) -> None:
        if not url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise VoiceUnavailable()
        self._url = url.replace("postgresql+psycopg://", "postgresql://", 1)

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with psycopg.connect(self._url, row_factory=dict_row, connect_timeout=5) as connection:
                yield connection
        except psycopg.errors.UniqueViolation as error:
            raise VoiceConflict() from error
        except psycopg.errors.ForeignKeyViolation as error:
            raise VoiceNotFound() from error
        except (
            psycopg.errors.CheckViolation,
            psycopg.errors.NotNullViolation,
            psycopg.DataError,
        ) as error:
            raise VoiceValidationError("Invalid persisted voice") from error
        except psycopg.Error as error:
            raise VoiceUnavailable() from error

    @contextmanager
    def transaction(self, character_id: str) -> Iterator[VoiceTransaction]:
        with self._connection() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (character_id,)
            )
            yield _VoiceTransaction(connection, character_id)

    def get_exemplar(self, character_id: str, exemplar_id: str) -> VoiceExemplar | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM voice_exemplars WHERE character_id=%s AND exemplar_id=%s",
                (character_id, exemplar_id),
            ).fetchone()
        return VoiceExemplar(**row) if row is not None else None

    def get_anchor(self, character_id: str, anchor_id: str) -> VerbatimAnchor | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM verbatim_anchors WHERE character_id=%s AND anchor_id=%s",
                (character_id, anchor_id),
            ).fetchone()
        return VerbatimAnchor(**row) if row is not None else None

    @contextmanager
    def _list(self, table: str, identifier: str, character_id: str) -> Iterator[Iterator[DictRow]]:
        with self._connection() as connection:
            with connection.cursor(name="voice_materials") as cursor:
                cursor.itersize = 128
                cursor.execute(
                    sql.SQL(
                        "SELECT * FROM {} WHERE character_id=%s ORDER BY registered_at, {}"
                    ).format(sql.Identifier(table), sql.Identifier(identifier)),
                    (character_id,),
                )
                yield iter(cursor)

    @contextmanager
    def list_exemplars(self, character_id: str) -> Iterator[Iterator[VoiceExemplar]]:
        with self._list("voice_exemplars", "exemplar_id", character_id) as rows:
            yield (VoiceExemplar(**row) for row in rows)

    @contextmanager
    def list_anchors(self, character_id: str) -> Iterator[Iterator[VerbatimAnchor]]:
        with self._list("verbatim_anchors", "anchor_id", character_id) as rows:
            yield (VerbatimAnchor(**row) for row in rows)
