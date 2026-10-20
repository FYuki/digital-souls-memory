"""Explicit SQL persistence for immutable evidence."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from digital_souls_memory.evidence.domain import (
    DeliveryReceipt,
    EvidenceValidationError,
    Session,
    Turn,
)
from digital_souls_memory.evidence.ports import (
    EvidenceConflict,
    EvidenceNotFound,
    EvidenceUnavailable,
)


class PostgresEvidenceStore:
    def __init__(self, url: str) -> None:
        if not url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise EvidenceUnavailable()
        self._url = url.replace("postgresql+psycopg://", "postgresql://", 1)

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with psycopg.connect(self._url, row_factory=dict_row, connect_timeout=5) as connection:
                yield connection
        except psycopg.errors.UniqueViolation as error:
            raise EvidenceConflict() from error
        except psycopg.errors.ForeignKeyViolation as error:
            raise EvidenceNotFound() from error
        except (
            psycopg.errors.CheckViolation,
            psycopg.errors.NotNullViolation,
            psycopg.DataError,
        ) as error:
            raise EvidenceValidationError("Invalid persisted evidence value") from error
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def add_session(self, value: Session) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO sessions (session_id, started_at, ended_at, surface, participants) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    value.session_id,
                    value.started_at,
                    value.ended_at,
                    value.surface,
                    list(value.participants),
                ),
            )

    def get_session(self, session_id: str) -> Session | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        return Session(**row) if row is not None else None

    def add_turn(self, value: Turn) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO turns (turn_id, session_id, surface, actor, kind, text, audience, "
                "language, register, sensitivity, classifier_version, occurred_at, recorded_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    value.turn_id,
                    value.session_id,
                    value.surface,
                    value.actor,
                    value.kind,
                    value.text,
                    list(value.audience),
                    value.language,
                    Jsonb(dict(value.register)),
                    value.sensitivity,
                    value.classifier_version,
                    value.occurred_at,
                    value.recorded_at,
                ),
            )

    def get_turn(self, turn_id: str) -> Turn | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM turns WHERE turn_id = %s",
                (turn_id,),
            ).fetchone()
        return Turn(**row) if row is not None else None

    def add_receipt(self, value: DeliveryReceipt) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO delivery_receipts "
                "(turn_id, status, spoken_until, reported_by, reported_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    value.turn_id,
                    value.status,
                    value.spoken_until,
                    value.reported_by,
                    value.reported_at,
                ),
            )

    @contextmanager
    def list_receipts(self, turn_id: str) -> Iterator[Iterator[DeliveryReceipt]]:
        with self._connection() as connection:
            with connection.cursor(name="evidence_receipts") as cursor:
                cursor.itersize = 128
                cursor.execute(
                    "SELECT turn_id, status, spoken_until, reported_by, reported_at "
                    "FROM delivery_receipts WHERE turn_id = %s ORDER BY receipt_id",
                    (turn_id,),
                )
                yield (DeliveryReceipt(**row) for row in cursor)

    @contextmanager
    def list_perceived_turns(self, character_id: str) -> Iterator[Iterator[Turn]]:
        with self._connection() as connection:
            with connection.cursor(name="evidence_turns") as cursor:
                cursor.itersize = 128
                cursor.execute(
                    "SELECT t.* FROM turns t JOIN sessions s ON s.session_id = t.session_id "
                    "WHERE t.actor = %s OR %s = ANY(t.audience) "
                    "OR (cardinality(t.audience) = 0 AND %s = ANY(s.participants)) "
                    "ORDER BY t.occurred_at, t.recorded_at, t.turn_id",
                    (character_id, character_id, character_id),
                )
                yield (Turn(**row) for row in cursor)
