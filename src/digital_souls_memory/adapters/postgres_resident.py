"""SQL persistence for profile configuration and independent context jobs."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from uuid import uuid4

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.postgres_episodes import read_episodes, replace_episodes
from digital_souls_memory.adapters.recall_records import (
    current_stamp,
    json_record,
    read_material,
    restore_stamp,
)
from digital_souls_memory.episodes.domain import Episode
from digital_souls_memory.evidence.domain import Session
from digital_souls_memory.evidence.ports import (
    EvidenceConflict,
    EvidenceNotFound,
    EvidenceUnavailable,
)
from digital_souls_memory.evidence.provision import ProvisionUnavailable
from digital_souls_memory.resident.domain import (
    CharacterProfileConfig,
    CharacterState,
    ContextClaim,
    ContextKey,
    ContextSnapshot,
    ContextWorkCursor,
    ContextWorkItem,
    ContextWorkPage,
    ContextWorkState,
    Operation,
    ProfileReference,
    ProfileSnapshot,
    ResolvedWindow,
)
from digital_souls_memory.resident.inputs import resolve_window


def _lock(connection: psycopg.Connection[DictRow]) -> None:
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('recall-state',0))")


def _params(key: ContextKey) -> tuple[str, str, str]:
    return key.character_id, key.session_id, key.surface


_WORK_DUE = """
    (w.lease_until IS NULL OR w.lease_until<=%(now)s) AND (
        w.operation IS NULL OR w.evaluated_version IS DISTINCT FROM coalesce(n.version,0)
        OR w.delivery_timeout IS DISTINCT FROM %(timeout)s
        OR w.next_due_at<=%(now)s
        OR w.status='pending' OR w.status='running'
        OR (w.status='retry' AND (w.retry_at IS NULL OR w.retry_at<=%(now)s))
    )
"""


def check_profile_snapshot(
    connection: psycopg.Connection[DictRow], snapshot: ProfileSnapshot, timeout: timedelta
) -> None:
    row = connection.execute(
        "SELECT version FROM character_profiles WHERE character_id=%s", (snapshot.character_id,)
    ).fetchone()
    if (row["version"] if row is not None else 0) != snapshot.version:
        raise ProvisionUnavailable()
    for stamp in snapshot.sources:
        actual = current_stamp(
            connection,
            turn_id=stamp.turn_id,
            character_id=stamp.character_id,
            used_end=stamp.used_end,
            now=snapshot.observed_at,
            timeout=timeout,
        )
        if actual != stamp:
            raise ProvisionUnavailable()
    for doc in snapshot.documents:
        row = connection.execute(
            "SELECT * FROM recall_documents WHERE owner_id=%s AND kind='memory' AND identifier=%s",
            (doc.ref.owner_id, doc.ref.memory_id),
        ).fetchone()
        if (
            row is None
            or not row["ready"]
            or (
                row["version"],
                row["content_digest"],
                row["model"],
                row["revision"],
                row["digest"],
                row["dimensions"],
            )
            != (
                doc.version,
                doc.content_digest,
                doc.space.model,
                doc.space.revision,
                doc.space.digest,
                doc.space.dimensions,
            )
        ):
            raise ProvisionUnavailable()


class PostgresProfileStore:
    def __init__(self, url: str, *, delivery_timeout: timedelta = timedelta(minutes=10)) -> None:
        self.url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        self.timeout = delivery_timeout

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with psycopg.connect(self.url, row_factory=dict_row, connect_timeout=5) as connection:
                yield connection
        except psycopg.Error as error:
            raise ProvisionUnavailable() from error

    def get(self, character_id: str) -> CharacterProfileConfig | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM character_profiles WHERE character_id=%s FOR SHARE", (character_id,)
            ).fetchone()
            if row is None:
                return None
            entries = connection.execute(
                'SELECT entry_id,kind,turn_id,"start","end",owner_id,memory_id '
                "FROM profile_entries WHERE character_id=%s ORDER BY position",
                (character_id,),
            ).fetchall()
            return CharacterProfileConfig(
                **row, entries=tuple(ProfileReference(**v) for v in entries)
            )

    def replace(
        self, config: CharacterProfileConfig, *, snapshot: ProfileSnapshot
    ) -> CharacterProfileConfig:
        with self._connection() as connection:
            _lock(connection)
            check_profile_snapshot(connection, snapshot, self.timeout)
            connection.execute(
                "INSERT INTO character_profiles(character_id,version,token_limit) VALUES(%s,%s,%s) "
                "ON CONFLICT(character_id) DO UPDATE SET "
                "version=EXCLUDED.version,token_limit=EXCLUDED.token_limit",
                (config.character_id, snapshot.version + 1, config.token_limit),
            )
            connection.execute(
                "DELETE FROM profile_entries WHERE character_id=%s", (config.character_id,)
            )
            for position, ref in enumerate(config.entries):
                connection.execute(
                    "INSERT INTO "
                    'profile_entries(character_id,position,entry_id,kind,turn_id,"start","end",owner_id,memory_id)'
                    " "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        config.character_id,
                        position,
                        ref.entry_id,
                        ref.kind,
                        ref.turn_id,
                        ref.start,
                        ref.end,
                        ref.owner_id,
                        ref.memory_id,
                    ),
                )
        return config


class PostgresContextStore:
    def __init__(self, url: str, *, delivery_timeout: timedelta = timedelta(minutes=10)) -> None:
        self.url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        self.timeout = delivery_timeout

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with psycopg.connect(self.url, row_factory=dict_row, connect_timeout=5) as connection:
                yield connection
        except psycopg.errors.UniqueViolation as error:
            raise EvidenceConflict() from error
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error

    def _load(
        self, connection: psycopg.Connection[DictRow], key: ContextKey, now: datetime
    ) -> ResolvedWindow:
        session = connection.execute(
            "SELECT s.session_id,s.started_at,s.surface,s.reports_delivery,"
            "coalesce(e.ended_at,s.ended_at) AS ended_at FROM sessions s LEFT JOIN "
            "session_endings e USING(session_id) "
            "WHERE s.session_id=%s",
            (key.session_id,),
        ).fetchone()
        if session is None:
            raise EvidenceNotFound()
        notification = connection.execute(
            "SELECT version FROM context_notifications WHERE character_id=%s AND "
            "session_id=%s AND surface=%s",
            _params(key),
        ).fetchone()
        rows = connection.execute(
            "SELECT turn_id FROM turns WHERE session_id=%s AND surface=%s AND %s=ANY(perceived_by) "
            "ORDER BY occurred_at,recorded_at,turn_id",
            (key.session_id, key.surface, key.character_id),
        ).fetchall()
        materials = []
        for row in rows:
            value = read_material(connection, row["turn_id"])
            if value is not None:
                materials.append(value)
        return resolve_window(
            key=key,
            session=Session(**session),
            materials=tuple(materials),
            notification_version=notification["version"] if notification is not None else 0,
            now=now,
            delivery_timeout=self.timeout,
        )

    def load_window(self, key: ContextKey, *, now: datetime) -> ResolvedWindow:
        with self._connection() as connection:
            return self._load(connection, key, now)

    def work_page(
        self, *, after: ContextWorkCursor | None, now: datetime, limit: int
    ) -> ContextWorkPage:
        with self._connection() as connection:
            cursor = (
                (after.surface, after.session_id, after.character_id, after.operation)
                if after
                else ("", "", "", "")
            )
            rows = connection.execute(
                "SELECT n.*,o.operation FROM context_notifications n CROSS JOIN "
                "(VALUES ('episodes'),('state')) AS o(operation) LEFT JOIN context_work w "
                "ON (w.character_id,w.session_id,w.surface,w.operation)="
                "(n.character_id,n.session_id,n.surface,o.operation) WHERE "
                "(n.surface,n.session_id,n.character_id,o.operation)>"
                "(%(surface)s,%(session)s,%(character)s,%(operation)s) AND "
                + _WORK_DUE
                + " ORDER BY n.surface,n.session_id,n.character_id,o.operation LIMIT %(limit)s",
                dict(zip(("surface", "session", "character", "operation"), cursor, strict=True))
                | {"now": now, "timeout": self.timeout, "limit": limit + 1},
            ).fetchall()
        selected = rows[:limit]
        last = selected[-1] if selected else None
        return ContextWorkPage(
            items=tuple(
                ContextWorkItem(
                    operation=row["operation"],
                    key=ContextKey(
                        character_id=row["character_id"],
                        session_id=row["session_id"],
                        surface=row["surface"],
                    ),
                )
                for row in selected
            ),
            next_after=ContextWorkCursor(
                surface=last["surface"],
                session_id=last["session_id"],
                character_id=last["character_id"],
                operation=last["operation"],
            )
            if len(rows) > limit and last is not None
            else None,
        )

    def claim(self, *, operation: Operation, key: ContextKey, now: datetime) -> ContextClaim | None:
        with self._connection() as connection:
            _lock(connection)
            metadata = self._metadata(connection, key)
            selected = connection.execute(
                "SELECT 1 FROM (SELECT %(version)s::bigint AS version) n "
                "LEFT JOIN context_work w ON w.operation=%(operation)s "
                "AND w.character_id=%(character)s AND w.session_id=%(session)s "
                "AND w.surface=%(surface)s WHERE " + _WORK_DUE,
                {
                    "version": metadata["version"],
                    "operation": operation,
                    "character": key.character_id,
                    "session": key.session_id,
                    "surface": key.surface,
                    "now": now,
                    "timeout": self.timeout,
                },
            ).fetchone()
            if selected is None:
                return None
            claim = ContextClaim(
                operation=operation,
                key=key,
                token=uuid4().hex,
                lease_until=now + timedelta(minutes=5),
            )
            connection.execute(
                "INSERT INTO "
                "context_work(operation,character_id,session_id,surface,status,token,lease_until,"
                "evaluated_version,delivery_timeout)"
                " "
                "VALUES(%s,%s,%s,%s,'running',%s,%s,%s,%s) ON "
                "CONFLICT(operation,character_id,session_id,surface) DO UPDATE SET "
                "status='running',token=EXCLUDED.token,lease_until=EXCLUDED.lease_until,"
                "evaluated_version=EXCLUDED.evaluated_version,"
                "delivery_timeout=EXCLUDED.delivery_timeout,next_due_at=NULL,retry_at=NULL,error=NULL",
                (
                    operation,
                    *_params(key),
                    claim.token,
                    claim.lease_until,
                    metadata["version"],
                    self.timeout,
                ),
            )
            return claim

    def _metadata(self, connection: psycopg.Connection[DictRow], key: ContextKey) -> DictRow:
        row = connection.execute(
            "SELECT coalesce(n.version,0) AS version,coalesce(e.ended_at,s.ended_at) AS ended_at "
            "FROM sessions s LEFT JOIN session_endings e USING(session_id) "
            "LEFT JOIN context_notifications n ON n.session_id=s.session_id "
            "AND n.character_id=%s AND n.surface=%s WHERE s.session_id=%s",
            (key.character_id, key.surface, key.session_id),
        ).fetchone()
        if row is None:
            raise EvidenceNotFound()
        return row

    def _owns(
        self,
        connection: psycopg.Connection[DictRow],
        claim: ContextClaim,
        snapshot: ContextSnapshot,
        now: datetime,
    ) -> bool:
        _lock(connection)
        row = connection.execute(
            "SELECT token,lease_until FROM context_work WHERE operation=%s AND character_id=%s AND "
            "session_id=%s AND surface=%s FOR UPDATE",
            (claim.operation, *_params(claim.key)),
        ).fetchone()
        metadata = self._metadata(connection, claim.key)
        return (
            row is not None
            and row["token"] == claim.token
            and row["lease_until"] is not None
            and now < row["lease_until"]
            and snapshot.key == claim.key
            and metadata["version"] == snapshot.notification_version
            and metadata["ended_at"] == snapshot.session_ended_at
            and snapshot.delivery_timeout == self.timeout
            and (snapshot.next_due_at is None or now < snapshot.next_due_at)
        )

    def begin(self, claim: ContextClaim, *, snapshot: ContextSnapshot, now: datetime) -> bool:
        with self._connection() as connection:
            if not self._owns(connection, claim, snapshot, now):
                return False
            connection.execute(
                "UPDATE context_work SET signature=%s,evaluated_version=%s,delivery_timeout=%s,"
                "next_due_at=%s WHERE operation=%s AND "
                "character_id=%s AND session_id=%s AND surface=%s AND token=%s",
                (
                    snapshot.signature,
                    snapshot.notification_version,
                    snapshot.delivery_timeout,
                    snapshot.next_due_at,
                    claim.operation,
                    *_params(claim.key),
                    claim.token,
                ),
            )
            return True

    def _done(
        self,
        connection: psycopg.Connection[DictRow],
        claim: ContextClaim,
        snapshot: ContextSnapshot,
    ) -> None:
        connection.execute(
            "UPDATE context_work SET "
            "status='done',signature=%s,completed_signature=%s,token=NULL,lease_until=NULL,"
            "retry_at=NULL,next_due_at=%s,error=NULL WHERE operation=%s AND character_id=%s"
            " AND session_id=%s AND surface=%s AND token=%s",
            (
                snapshot.signature,
                snapshot.signature,
                snapshot.next_due_at,
                claim.operation,
                *_params(claim.key),
                claim.token,
            ),
        )

    def complete_state(
        self,
        claim: ContextClaim,
        value: CharacterState | None,
        *,
        snapshot: ContextSnapshot,
        now: datetime,
    ) -> bool:
        with self._connection() as connection:
            if not self._owns(connection, claim, snapshot, now):
                return False
            connection.execute(
                "DELETE FROM character_states WHERE character_id=%s AND session_id=%s AND "
                "surface=%s",
                _params(claim.key),
            )
            if value is not None:
                connection.execute(
                    "INSERT INTO "
                    "character_states(character_id,session_id,surface,mood,relationship_temperature,current_topic,updated_at,expires_at,input_signature,dependencies)"
                    " "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        *_params(claim.key),
                        value.mood,
                        value.relationship_temperature,
                        value.current_topic,
                        value.updated_at,
                        value.expires_at,
                        value.input_signature,
                        Jsonb(json_record({"values": [asdict(v) for v in value.dependencies]})),
                    ),
                )
            self._done(connection, claim, snapshot)
            return True

    def complete_episodes(
        self,
        claim: ContextClaim,
        values: tuple[Episode, ...],
        *,
        snapshot: ContextSnapshot,
        now: datetime,
    ) -> bool:
        with self._connection() as connection:
            if not self._owns(connection, claim, snapshot, now):
                return False
            replace_episodes(connection, claim.key, values)
            self._done(connection, claim, snapshot)
            return True

    def fail(self, claim: ContextClaim, *, error: str, retry_at: datetime | None) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE context_work SET "
                "status=%s,error=%s,retry_at=%s,token=NULL,lease_until=NULL "
                "WHERE operation=%s AND character_id=%s AND session_id=%s AND surface=%s "
                "AND token=%s",
                (
                    "retry" if retry_at is not None else "failed",
                    error,
                    retry_at,
                    claim.operation,
                    *_params(claim.key),
                    claim.token,
                ),
            )

    def get_work(self, operation: Operation, key: ContextKey) -> ContextWorkState | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT "
                "status,signature,completed_signature,token,lease_until,retry_at,next_due_at,error,"
                "evaluated_version,delivery_timeout"
                " "
                "FROM context_work WHERE operation=%s AND character_id=%s AND session_id=%s"
                " AND surface=%s",
                (operation, *_params(key)),
            ).fetchone()
            return (
                ContextWorkState(operation=operation, key=key, **row) if row is not None else None
            )

    def get_state(self, key: ContextKey) -> CharacterState | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM character_states WHERE character_id=%s AND session_id=%s AND"
                " surface=%s",
                _params(key),
            ).fetchone()
            return (
                CharacterState(
                    **(
                        row
                        | {
                            "dependencies": tuple(
                                restore_stamp(v) for v in row["dependencies"]["values"]
                            )
                        }
                    )
                )
                if row is not None
                else None
            )

    def list_episodes(self, key: ContextKey) -> tuple[Episode, ...]:
        with self._connection() as connection:
            return read_episodes(connection, key)
