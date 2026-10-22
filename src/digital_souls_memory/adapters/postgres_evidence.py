"""Explicit SQL persistence for immutable evidence."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.postgres_connections import PostgresConnections
from digital_souls_memory.evidence.domain import (
    DeliveryReceipt,
    EvidenceValidationError,
    Session,
    SessionEnding,
    Turn,
)
from digital_souls_memory.evidence.ports import (
    EvidenceConflict,
    EvidenceNotFound,
    EvidenceUnavailable,
)


class PostgresEvidenceStore:
    def __init__(self, url: str, *, connections: PostgresConnections | None = None) -> None:
        if not url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise EvidenceUnavailable()
        self._connections = connections if connections is not None else PostgresConnections(url)

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with self._connections.connection() as connection:
                yield connection
        except psycopg.errors.UniqueViolation as error:
            raise EvidenceConflict() from error
        except psycopg.errors.ForeignKeyViolation as error:
            if error.diag.constraint_name == "turns_reply_to_turn_id_fkey":
                raise EvidenceValidationError("Reply target does not exist") from error
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
                "INSERT INTO sessions "
                "(session_id, started_at, ended_at, surface, reports_delivery) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    value.session_id,
                    value.started_at,
                    value.ended_at,
                    value.surface,
                    value.reports_delivery,
                ),
            )

    def get_session(self, session_id: str) -> Session | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT s.session_id,s.started_at,coalesce(e.ended_at,s.ended_at) AS ended_at,"
                "s.surface,s.reports_delivery FROM sessions s LEFT JOIN session_endings e "
                "ON e.session_id=s.session_id WHERE s.session_id = %s",
                (session_id,),
            ).fetchone()
        return Session(**row) if row is not None else None

    def add_ending(self, value: SessionEnding) -> tuple[SessionEnding, bool]:
        with self._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('recall-state',0))")
            original = connection.execute(
                "SELECT ended_at FROM sessions WHERE session_id=%s FOR UPDATE", (value.session_id,)
            ).fetchone()
            if original is None:
                raise EvidenceNotFound()
            previous = connection.execute(
                "SELECT * FROM session_endings WHERE session_id=%s", (value.session_id,)
            ).fetchone()
            if previous is not None:
                result = SessionEnding(**previous)
                if (result.ended_at, result.reported_by) != (value.ended_at, value.reported_by):
                    raise EvidenceConflict()
                return result, False
            if original["ended_at"] is not None and original["ended_at"] != value.ended_at:
                raise EvidenceConflict()
            connection.execute(
                "INSERT INTO session_endings(session_id,ended_at,reported_by,reported_at) "
                "VALUES(%s,%s,%s,%s)",
                (value.session_id, value.ended_at, value.reported_by, value.reported_at),
            )
        return value, True

    def list_endings(self, session_id: str) -> tuple[SessionEnding, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM session_endings WHERE session_id=%s", (session_id,)
            ).fetchall()
        return tuple(SessionEnding(**row) for row in rows)

    def add_turn(self, value: Turn) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO turns (turn_id, session_id, surface, actor, kind, text, perceived_by, "
                "addressees, address_mode, reply_to_turn_id, "
                "language, register, sensitivity, classifier_version, occurred_at, recorded_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    value.turn_id,
                    value.session_id,
                    value.surface,
                    value.actor,
                    value.kind,
                    value.text,
                    list(value.perceived_by),
                    list(value.addressees),
                    value.address_mode,
                    value.reply_to_turn_id,
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
                    "SELECT t.* FROM turns t WHERE %s = ANY(t.perceived_by) "
                    "ORDER BY t.occurred_at, t.recorded_at, t.turn_id",
                    (character_id,),
                )
                yield (Turn(**row) for row in cursor)
