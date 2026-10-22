"""Atomic snapshot revalidation, INSERT and commit of provision records."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.postgres_connections import PostgresConnections
from digital_souls_memory.adapters.postgres_resident import check_profile_snapshot
from digital_souls_memory.adapters.recall_records import (
    material_stamp,
    provision_payload,
    read_materials,
    restore_provision,
)
from digital_souls_memory.evidence.domain import Session
from digital_souls_memory.evidence.provision import (
    ProvisionConflict,
    ProvisionRecord,
    ProvisionUnavailable,
)
from digital_souls_memory.identity.sessions import SessionIdentity
from digital_souls_memory.recall.domain import DocumentStamp, ProvisionSnapshot, VectorSpace
from digital_souls_memory.recall.eligibility import snapshot_matches
from digital_souls_memory.resident.domain import ProfileSnapshot
from digital_souls_memory.voice.domain import VoiceExemplar


class PostgresSessionIdentityStore:
    def __init__(self, url: str, *, connections: PostgresConnections | None = None) -> None:
        self._connections = connections if connections is not None else PostgresConnections(url)

    def select(self, *, character_id: str, session: Session) -> SessionIdentity:
        try:
            with self._connections.connection() as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (character_id,)
                )
                connection.execute(
                    "INSERT INTO session_identity_selections(session_id,character_id,"
                    "temperament_version,personality_version) SELECT %s,%s,"
                    "(SELECT max(version) FROM temperaments WHERE character_id=%s AND "
                    "registered_at<=%s),"
                    "(SELECT max(version) FROM personality_profiles WHERE character_id=%s "
                    "AND valid_from<=%s AND (valid_to IS NULL OR %s<valid_to)) "
                    "ON CONFLICT(session_id,character_id) DO NOTHING",
                    (
                        session.session_id,
                        character_id,
                        character_id,
                        session.started_at,
                        character_id,
                        session.started_at,
                        session.started_at,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM session_identity_selections WHERE session_id=%s AND "
                    "character_id=%s",
                    (session.session_id, character_id),
                ).fetchone()
                assert row is not None
                return SessionIdentity(**row)
        except psycopg.Error as error:
            raise ProvisionUnavailable() from error


class PostgresProvisionStore:
    def __init__(
        self,
        url: str,
        *,
        delivery_timeout: timedelta = timedelta(minutes=10),
        connections: PostgresConnections | None = None,
    ) -> None:
        self._connections = connections if connections is not None else PostgresConnections(url)
        self.timeout = delivery_timeout

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with self._connections.connection() as connection:
                yield connection
        except psycopg.errors.UniqueViolation as error:
            raise ProvisionConflict() from error
        except psycopg.Error as error:
            raise ProvisionUnavailable() from error

    def save(self, record: ProvisionRecord, *, snapshot: ProvisionSnapshot) -> None:
        with self._connection() as connection:
            if record.kind == "resident":
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (snapshot.character_id,),
                )
            # Writers acquire this gate before touching notification or document rows.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('recall-state',0))")
            connection.execute(
                "SELECT turn_id FROM memory_notifications WHERE turn_id=ANY(%s) ORDER BY "
                "turn_id FOR UPDATE",
                (sorted({v.turn_id for v in snapshot.sources}),),
            ).fetchall()
            selection = connection.execute(
                "SELECT * FROM session_identity_selections WHERE session_id=%s AND character_id=%s",
                (snapshot.session_id, snapshot.character_id),
            ).fetchone()
            if selection is None:
                raise ProvisionUnavailable()
            if record.kind == "resident":
                assert snapshot.profile_version is not None
                check_profile_snapshot(
                    connection,
                    ProfileSnapshot(
                        character_id=snapshot.character_id,
                        version=snapshot.profile_version,
                        documents=snapshot.documents,
                        sources=snapshot.sources,
                        observed_at=record.provided_at,
                    ),
                    self.timeout,
                )
                core = connection.execute(
                    "SELECT max(version) AS version FROM core_identities WHERE character_id=%s",
                    (snapshot.character_id,),
                ).fetchone()
                if core is None or core["version"] != snapshot.core_identity_version:
                    raise ProvisionUnavailable()
                for expected in snapshot.voice_exemplars:
                    voice = connection.execute(
                        "SELECT * FROM voice_exemplars WHERE character_id=%s AND exemplar_id=%s",
                        (snapshot.character_id, expected.exemplar_id),
                    ).fetchone()
                    if voice is None or VoiceExemplar(**voice) != expected:
                        raise ProvisionUnavailable()
            materials = read_materials(
                connection, tuple(dict.fromkeys(v.turn_id for v in snapshot.sources))
            )
            sources = []
            for stamp in snapshot.sources:
                material = materials.get(stamp.turn_id)
                if material is None:
                    raise ProvisionUnavailable()
                actual = material_stamp(
                    material,
                    character_id=stamp.character_id,
                    used_end=stamp.used_end,
                    now=record.provided_at,
                    timeout=self.timeout,
                )
                if actual is None or actual.used_end > actual.available_end:
                    raise ProvisionUnavailable()
                sources.append(actual)
            documents = {}
            ordered = sorted(snapshot.documents, key=lambda v: v.ref.sort_key)
            keys = [
                {
                    "owner_id": d.ref.owner_id,
                    "kind": "memory" if d.ref.memory_id is not None else "anchor",
                    "identifier": d.ref.memory_id or d.ref.anchor_id,
                    "position": i,
                }
                for i, d in enumerate(ordered)
            ]
            rows = connection.execute(
                "SELECT d.* FROM jsonb_to_recordset(%s) AS k(owner_id text,kind text,"
                "identifier text,position int) JOIN recall_documents d "
                "ON (d.owner_id,d.kind,d.identifier)=(k.owner_id,k.kind,k.identifier) "
                "ORDER BY k.position FOR UPDATE OF d",
                (Jsonb(keys),),
            ).fetchall()
            by_key = {(r["owner_id"], r["kind"], r["identifier"]): r for r in rows}
            for document in ordered:
                row = by_key.get(
                    (
                        document.ref.owner_id,
                        "memory" if document.ref.memory_id is not None else "anchor",
                        document.ref.memory_id or document.ref.anchor_id,
                    )
                )
                if row is None or not row["ready"]:
                    raise ProvisionUnavailable()
                documents[document.ref] = DocumentStamp(
                    ref=document.ref,
                    version=row["version"],
                    content_digest=row["content_digest"],
                    space=VectorSpace(
                        model=row["model"],
                        revision=row["revision"],
                        digest=row["digest"],
                        dimensions=row["dimensions"],
                    ),
                )
            actual_snapshot = ProvisionSnapshot(
                character_id=selection["character_id"],
                session_id=selection["session_id"],
                temperament_version=selection["temperament_version"],
                personality_version=selection["personality_version"],
                documents=tuple(documents[v.ref] for v in snapshot.documents),
                sources=tuple(sources),
                profile_version=snapshot.profile_version,
                core_identity_version=snapshot.core_identity_version,
                voice_exemplars=snapshot.voice_exemplars,
            )
            if not snapshot_matches(snapshot, actual_snapshot):
                raise ProvisionUnavailable()
            connection.execute(
                "INSERT INTO provision_records(provision_id,payload) VALUES(%s,%s)",
                (record.provision_id, Jsonb(provision_payload(record))),
            )

    def get(self, provision_id: str) -> ProvisionRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM provision_records WHERE provision_id=%s", (provision_id,)
            ).fetchone()
            return restore_provision(row["payload"]) if row is not None else None
