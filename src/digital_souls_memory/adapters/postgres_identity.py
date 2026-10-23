"""Explicit SQL with character-scoped registration transactions."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import psycopg
from psycopg import sql
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.postgres_connections import PostgresConnections
from digital_souls_memory.identity.domain import (
    CoreIdentity,
    IdentityValidationError,
    PersonalityProfile,
    Temperament,
    validate_write,
)
from digital_souls_memory.identity.ports import (
    IdentityConflict,
    IdentityNotFound,
    IdentityTransaction,
    IdentityUnavailable,
)


class _IdentityTransaction:
    def __init__(
        self, connection: psycopg.Connection[DictRow], character_id: str, source: str
    ) -> None:
        self.connection = connection
        self.character_id = character_id
        self.source = source

    def _get(self, table: str, version: int | None) -> DictRow | None:
        query = sql.SQL("SELECT * FROM {} WHERE character_id = %s").format(sql.Identifier(table))
        params: tuple[str | int, ...] = (self.character_id,)
        if version is not None:
            query += sql.SQL(" AND version = %s")
            params += (version,)
        query += sql.SQL(" ORDER BY version DESC LIMIT 1")
        return self.connection.execute(query, params).fetchone()

    def get_core(self, version: int | None = None) -> CoreIdentity | None:
        row = self._get("core_identities", version)
        return CoreIdentity(**row) if row is not None else None

    def get_temperament(self, version: int | None = None) -> Temperament | None:
        row = self._get("temperaments", version)
        return Temperament(**row) if row is not None else None

    def get_profile(self, version: int | None = None) -> PersonalityProfile | None:
        row = self._get("personality_profiles", version)
        return PersonalityProfile(**row) if row is not None else None

    def _authorize(self, character_id: str, operation: str) -> None:
        validate_write(source=self.source, operation=operation, has_profile=False)
        if character_id != self.character_id:
            raise PermissionError("Transaction belongs to another character")

    def add_core(self, value: CoreIdentity) -> None:
        self._authorize(value.character_id, "core_identity")
        self.connection.execute(
            'INSERT INTO core_identities (character_id, "values", prohibitions, '
            "version, registered_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (
                value.character_id,
                list(value.values),
                list(value.prohibitions),
                value.version,
                value.registered_at,
            ),
        )

    def add_temperament(self, value: Temperament) -> None:
        self._authorize(value.character_id, "temperament")
        self.connection.execute(
            "INSERT INTO temperaments (character_id, description, version, registered_at) "
            "VALUES (%s,%s,%s,%s)",
            (value.character_id, value.description, value.version, value.registered_at),
        )

    def add_profile(self, value: PersonalityProfile) -> None:
        self._authorize(value.character_id, "author_seed")
        if self.get_profile() is not None:
            raise IdentityConflict()
        self.connection.execute(
            "INSERT INTO personality_profiles (character_id, aspects, description, "
            "renderer_version, "
            "temperament_version, version, valid_from, valid_to, evidence_belief_ids, change_kind) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                value.character_id,
                Jsonb(dict(value.aspects)),
                value.description,
                value.renderer_version,
                value.temperament_version,
                value.version,
                value.valid_from,
                value.valid_to,
                list(value.evidence_belief_ids),
                value.change_kind,
            ),
        )


class PostgresIdentityStore:
    def __init__(self, url: str, *, connections: PostgresConnections | None = None) -> None:
        if not url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise IdentityUnavailable()
        self._connections = connections if connections is not None else PostgresConnections(url)

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with self._connections.connection() as connection:
                yield connection
        except psycopg.errors.UniqueViolation as error:
            raise IdentityConflict() from error
        except psycopg.errors.ForeignKeyViolation as error:
            raise IdentityNotFound() from error
        except (
            psycopg.errors.CheckViolation,
            psycopg.errors.NotNullViolation,
            psycopg.DataError,
        ) as error:
            raise IdentityValidationError("Invalid persisted identity") from error
        except psycopg.Error as error:
            raise IdentityUnavailable() from error

    @contextmanager
    def transaction(self, character_id: str, *, source: str) -> Iterator[IdentityTransaction]:
        with self._connection() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (character_id,)
            )
            row = connection.execute(
                "SELECT kind FROM actor_kinds WHERE actor_id=%s", (character_id,)
            ).fetchone()
            if row is not None and row["kind"] != "character":
                raise IdentityConflict()
            yield _IdentityTransaction(connection, character_id, source)

    def get_core(self, character_id: str, version: int | None = None) -> CoreIdentity | None:
        with self._connection() as connection:
            return _IdentityTransaction(connection, character_id, "read").get_core(version)

    def get_temperament(self, character_id: str, version: int | None = None) -> Temperament | None:
        with self._connection() as connection:
            return _IdentityTransaction(connection, character_id, "read").get_temperament(version)

    def get_profile(
        self, character_id: str, version: int | None = None
    ) -> PersonalityProfile | None:
        with self._connection() as connection:
            return _IdentityTransaction(connection, character_id, "read").get_profile(version)

    def get_current(
        self, character_id: str, at: datetime
    ) -> tuple[CoreIdentity | None, Temperament | None, PersonalityProfile | None]:
        with self._connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            reader = _IdentityTransaction(connection, character_id, "read")
            core = reader.get_core()
            temperament = reader.get_temperament()
            row = connection.execute(
                "SELECT * FROM personality_profiles WHERE character_id=%s AND valid_from <= %s "
                "AND (valid_to IS NULL OR %s < valid_to) ORDER BY version DESC LIMIT 1",
                (character_id, at, at),
            ).fetchone()
            return core, temperament, PersonalityProfile(**row) if row is not None else None
