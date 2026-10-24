"""Leased author deletion with snapshot validation and one atomic commit."""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.embedding_cleanup import collect_embeddings
from digital_souls_memory.adapters.memory_records import restore_memory
from digital_souls_memory.adapters.postgres_memory import (
    COPY_SOURCE_TABLES,
    _MemoryTransaction,
    copy_payload,
    copy_sources,
    save_copy_sources,
)
from digital_souls_memory.adapters.recall_records import json_record, read_material
from digital_souls_memory.governance.domain import (
    AuthorPrincipal,
    CascadeResult,
    DeletionAttempt,
    DeletionCommand,
    DeletionRequest,
    DeletionSnapshot,
    MemoryCopy,
    MemoryRef,
    MemoryReplacement,
    StoredReference,
    TemporalEffect,
)
from digital_souls_memory.memory.domain import MemoryRelation, MemoryValidationError
from digital_souls_memory.memory.ports import MemoryConflict, MemoryUnavailable
from digital_souls_memory.memory.relations import normalize_relations


def _request(row: DictRow) -> DeletionRequest:
    cascade = row["cascade_result"]
    result = (
        None
        if cascade is None
        else CascadeResult(
            deleted_turn_ids=tuple(cascade["deleted_turn_ids"]),
            recalculated_memories=tuple(MemoryRef(**r) for r in cascade["recalculated_memories"]),
            invalidated_memories=tuple(MemoryRef(**r) for r in cascade["invalidated_memories"]),
            invalidated_provision_ids=tuple(cascade["invalidated_provision_ids"]),
            removed_references=tuple(
                StoredReference(table=r["table"], key=tuple(r["key"]))
                for r in cascade["removed_references"]
            ),
        )
    )
    return DeletionRequest(
        command=DeletionCommand(**row["command"]),
        requested_by=row["requested_by"],
        target_turn_ids=tuple(row["target_turn_ids"]),
        status=row["status"],
        cascade_result=result,
        error=row["error"],
        requested_at=row["requested_at"],
        completed_at=row["completed_at"],
    )


class PostgresDeletionStore:
    def __init__(self, url: str) -> None:
        if not url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise MemoryUnavailable()
        self._url = url.replace("postgresql+psycopg://", "postgresql://", 1)

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with psycopg.connect(self._url, row_factory=dict_row, connect_timeout=5) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('recall-state',0))"
                )
                yield connection
        except psycopg.Error as error:
            raise MemoryUnavailable() from error

    def begin(
        self, command: DeletionCommand, *, principal: AuthorPrincipal, now: datetime
    ) -> DeletionAttempt:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM deletion_requests WHERE request_id=%s FOR UPDATE",
                (command.request_id,),
            ).fetchone()
            if row is not None:
                previous = _request(row)
                if previous.command != command or previous.requested_by != principal.author_id:
                    raise MemoryConflict()
                if previous.status == "done":
                    return DeletionAttempt(request=previous, token=None, snapshot=None)
                if previous.status == "running" and row["lease_until"] > now:
                    raise MemoryConflict()
                targets = previous.target_turn_ids
            else:
                if command.user_id is not None:
                    rows = connection.execute(
                        "SELECT turn_id FROM turns WHERE actor=%s OR %s=ANY(addressees) "
                        "ORDER BY turn_id",
                        (command.user_id, command.user_id),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT turn_id FROM turns WHERE turn_id=ANY(%s) ORDER BY turn_id",
                        (list(command.target_turn_ids),),
                    ).fetchall()
                targets = tuple(r["turn_id"] for r in rows)
            token = uuid4().hex
            row = connection.execute(
                "INSERT INTO deletion_requests(request_id,command,requested_by,target_turn_ids,"
                "status,token,lease_until,requested_at) VALUES(%s,%s,%s,%s,'running',%s,%s,%s) "
                "ON CONFLICT(request_id) DO UPDATE SET status='running',token=EXCLUDED.token,"
                "lease_until=EXCLUDED.lease_until,error=NULL,completed_at=NULL RETURNING *",
                (
                    command.request_id,
                    Jsonb(asdict(command)),
                    principal.author_id,
                    list(targets),
                    token,
                    now + timedelta(minutes=5),
                    now,
                ),
            ).fetchone()
            assert row is not None
            return DeletionAttempt(
                request=_request(row),
                token=token,
                snapshot=self._snapshot(connection, targets, now),
            )

    def _snapshot(
        self, connection: psycopg.Connection[DictRow], targets: tuple[str, ...], now: datetime
    ) -> DeletionSnapshot:
        copies = []
        dependent = connection.execute(
            "WITH RECURSIVE affected(character_id,memory_id) AS ("
            "SELECT c.character_id,c.memory_id FROM memory_candidates c "
            "JOIN memory_input_dependencies d ON d.character_id=c.character_id "
            "AND d.extraction_turn_id=c.turn_id WHERE d.turn_id=ANY(%s) "
            "UNION SELECT g.character_id,g.memory_id FROM memory_generated g "
            "JOIN memory_input_dependencies d ON d.character_id=g.character_id "
            "AND d.extraction_turn_id=g.turn_id WHERE d.turn_id=ANY(%s) "
            "UNION SELECT character_id,memory_id FROM memory_baseline_dependencies "
            "WHERE turn_id=ANY(%s) "
            "), effects(character_id,memory_id) AS (SELECT * FROM affected UNION "
            "SELECT r.character_id,r.to_id FROM memory_relations r JOIN effects e "
            "ON r.character_id=e.character_id AND r.from_id=e.memory_id "
            "WHERE r.type IN ('extends','updates','contradicts')) "
            "SELECT character_id,memory_id FROM effects ORDER BY character_id,memory_id",
            (list(targets), list(targets), list(targets)),
        ).fetchall()
        dependent_owners = [r["character_id"] for r in dependent]
        dependent_ids = [r["memory_id"] for r in dependent]
        rows = connection.execute(
            "SELECT DISTINCT m.* FROM derived_memories m LEFT JOIN memory_sources s ON "
            "s.character_id=m.character_id AND s.memory_id=m.id WHERE s.turn_id=ANY(%s) "
            "OR (m.character_id,m.id) IN (SELECT * FROM unnest(%s::text[],%s::text[]))",
            (list(targets), dependent_owners, dependent_ids),
        ).fetchall()
        for row in rows:
            copies.append(
                MemoryCopy(
                    table="derived_memories",
                    memory=_MemoryTransaction(connection, row["character_id"])._restore(row),
                )
            )
        for table, sources in COPY_SOURCE_TABLES.items():
            origin_condition = "OR p.turn_id=ANY(%s)" if table != "memory_baselines" else ""
            rows = connection.execute(
                sql.SQL(
                    "SELECT DISTINCT p.* FROM {} p LEFT JOIN {} s USING(character_id,memory_id) "
                    "WHERE (p.character_id,p.memory_id) IN "
                    "(SELECT * FROM unnest(%s::text[],%s::text[])) OR s.turn_id=ANY(%s) "
                    + origin_condition
                ).format(sql.Identifier(table), sql.Identifier(sources)),
                (dependent_owners, dependent_ids, list(targets), list(targets))
                if origin_condition
                else (dependent_owners, dependent_ids, list(targets)),
            ).fetchall()
            for row in rows:
                copies.append(
                    MemoryCopy(
                        table=table,
                        memory=restore_memory(
                            row["payload"],
                            copy_sources(connection, table, row["character_id"], row["memory_id"]),
                        ),
                        origin_turn_id=row["turn_id"] if table != "memory_baselines" else None,
                    )
                )
        prepared = []
        for copy in copies:
            if copy.table != "derived_memories":
                prepared.append(copy)
                continue
            owner, identifier = copy.memory.character_id, copy.memory.id
            baseline = connection.execute(
                "SELECT payload,temporal_origin FROM memory_baselines "
                "WHERE character_id=%s AND memory_id=%s",
                (owner, identifier),
            ).fetchone()
            if baseline is not None:
                base = restore_memory(
                    baseline["payload"] | baseline["temporal_origin"],
                    copy_sources(connection, "memory_baselines", owner, identifier),
                )
            else:
                raw = connection.execute(
                    "SELECT g.payload,EXISTS(SELECT 1 FROM memory_relations r "
                    "WHERE r.character_id=g.character_id AND r.from_id=g.memory_id "
                    "AND r.type='extends') AS reinforcement_history, "
                    "NOT EXISTS(SELECT 1 FROM memory_candidates c "
                    "WHERE c.character_id=g.character_id AND c.memory_id=g.memory_id) AS retired "
                    "FROM memory_generated g WHERE g.character_id=%s AND g.memory_id=%s",
                    (owner, identifier),
                ).fetchone()
                if raw is None:
                    raise MemoryValidationError("Missing temporal baseline")
                base = restore_memory(
                    raw["payload"], copy_sources(connection, "memory_generated", owner, identifier)
                )
                if raw["reinforcement_history"] or raw["retired"]:
                    base = replace(base, superseded_at=copy.memory.superseded_at)
            effects = []
            for row in connection.execute(
                "SELECT c.memory_id,c.payload,r.type FROM memory_candidates c "
                "JOIN memory_relations r ON r.character_id=c.character_id "
                "AND r.from_id=c.memory_id WHERE r.character_id=%s AND r.to_id=%s "
                "AND r.type IN ('updates','contradicts','extends') "
                "AND NOT c.turn_id=ANY(%s) AND NOT EXISTS(SELECT 1 "
                "FROM memory_input_dependencies d WHERE d.character_id=c.character_id "
                "AND d.extraction_turn_id=c.turn_id AND d.turn_id=ANY(%s)) "
                "ORDER BY c.payload->>'valid_from', c.memory_id",
                (owner, identifier, list(targets), list(targets)),
            ).fetchall():
                candidate = restore_memory(
                    row["payload"],
                    copy_sources(connection, "memory_candidates", owner, row["memory_id"]),
                )
                if set(candidate.source_turn_ids).intersection(targets):
                    continue
                applied_at = copy.memory.superseded_at if row["type"] == "contradicts" else now
                if applied_at is None:
                    raise MemoryValidationError("Missing contradiction application time")
                effects.append(
                    TemporalEffect(
                        candidate=candidate,
                        relation_type=row["type"],
                        applied_at=applied_at,
                    )
                )
            prepared.append(replace(copy, temporal_base=base, temporal_effects=tuple(effects)))
        copies = prepared
        copies.sort(key=lambda c: (c.table, c.memory.character_id, c.memory.id))
        identifiers = sorted(
            {
                i
                for c in copies
                for i in (
                    *c.memory.source_turn_ids,
                    *(i for e in c.temporal_effects for i in e.candidate.source_turn_ids),
                )
            }
            - set(targets)
        )
        materials = tuple(m for i in identifiers if (m := read_material(connection, i)) is not None)
        # Include all durable inputs to the direct cascade; only their digest leaves this read.
        digest = hashlib.sha256()
        for table, keys in (
            ("turns", ("turn_id",)),
            ("delivery_receipts", ("receipt_id",)),
            ("derived_memories", ("character_id", "id")),
            ("memory_sources", ("character_id", "memory_id", "turn_id")),
            ("memory_candidates", ("character_id", "memory_id")),
            ("memory_generated", ("character_id", "memory_id")),
            ("memory_baselines", ("character_id", "memory_id")),
            ("memory_candidate_sources", ("character_id", "memory_id", "turn_id")),
            ("memory_generated_sources", ("character_id", "memory_id", "turn_id")),
            ("memory_baseline_sources", ("character_id", "memory_id", "turn_id")),
            ("memory_input_dependencies", ("character_id", "extraction_turn_id", "turn_id")),
            ("memory_baseline_dependencies", ("character_id", "memory_id", "turn_id")),
            ("memory_extractions", ("character_id", "turn_id")),
            ("memory_relations", ("character_id", "from_id", "to_id", "type")),
            ("memory_representatives", ("character_id", "absorbed_id")),
            ("memory_candidate_relations", ("character_id", "turn_id", "from_id", "to_id", "type")),
            ("verbatim_anchors", ("character_id", "anchor_id")),
            ("voice_exemplars", ("character_id", "exemplar_id")),
            ("provision_records", ("provision_id",)),
            ("profile_entries", ("character_id", "entry_id")),
            ("character_profiles", ("character_id",)),
            ("character_states", ("character_id", "session_id", "surface")),
            ("episodes", ("episode_id",)),
            ("episode_spans", ("episode_id", "position")),
            ("context_work", ("operation", "character_id", "session_id", "surface")),
            ("context_notifications", ("character_id", "session_id", "surface")),
        ):
            digest.update(b"table:" + table.encode() + b"\0")
            with connection.cursor(name="deletion_fingerprint") as cursor:
                cursor.execute(
                    sql.SQL("SELECT * FROM {} ORDER BY {}").format(
                        sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, keys))
                    )
                )
                while batch := cursor.fetchmany(64):
                    for row in batch:
                        encoded = json.dumps(
                            row, ensure_ascii=False, default=str, sort_keys=True
                        ).encode()
                        digest.update(len(encoded).to_bytes(8, "big"))
                        digest.update(encoded)
            digest.update(b"end-table\0")
        for material in materials:
            encoded = json.dumps(
                (
                    material.turn.turn_id,
                    material.actor_is_character,
                    material.session.reports_delivery,
                ),
                ensure_ascii=False,
            ).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return DeletionSnapshot(
            target_turn_ids=targets,
            copies=tuple(copies),
            materials=materials,
            references=(),
            fingerprint=digest.hexdigest(),
            observed_at=now,
        )

    def commit(
        self,
        attempt: DeletionAttempt,
        *,
        replacements: tuple[MemoryReplacement, ...],
        now: datetime,
    ) -> DeletionRequest:
        if attempt.snapshot is None or attempt.token is None:
            raise MemoryConflict()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM deletion_requests WHERE request_id=%s FOR UPDATE",
                (attempt.request.command.request_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "running"
                or row["token"] != attempt.token
                or row["lease_until"] <= now
                or _request(row).command != attempt.request.command
                or row["requested_by"] != attempt.request.requested_by
                or tuple(row["target_turn_ids"]) != attempt.request.target_turn_ids
                or attempt.snapshot.target_turn_ids != attempt.request.target_turn_ids
            ):
                raise MemoryConflict()
            snapshot = self._snapshot(
                connection, attempt.request.target_turn_ids, attempt.snapshot.observed_at
            )
            if snapshot.fingerprint != attempt.snapshot.fingerprint:
                raise MemoryConflict()
            if tuple(r.copy for r in replacements) != snapshot.copies:
                raise MemoryConflict()
            recalculated = set()
            invalidated = set()
            projected = {
                (r.copy.memory.character_id, r.copy.memory.id)
                for r in replacements
                if r.copy.table == "derived_memories"
            }
            for replacement in replacements:
                old, value = replacement.copy.memory, replacement.value
                table = replacement.copy.table
                key = (old.character_id, old.id)
                # Every copy was validated before commit. Only the final projection
                # determines its baseline, even when the former baseline lost support.
                if table == "memory_baselines" and key in projected:
                    continue
                if value is None:
                    if table == "derived_memories":
                        invalidated.add(key)
                    continue
                if table == "derived_memories":
                    _MemoryTransaction(connection, old.character_id).replace_memory(value)
                    base = replacement.copy.temporal_base
                    if base is not None:
                        origin = copy_payload(base)
                        connection.execute(
                            "UPDATE memory_baselines SET temporal_origin=%s "
                            "WHERE character_id=%s AND memory_id=%s",
                            (
                                Jsonb(
                                    {
                                        k: origin[k]
                                        for k in ("valid_to", "superseded_at", "last_reinforced_at")
                                    }
                                ),
                                *key,
                            ),
                        )
                    recalculated.add(key)
                    connection.execute(
                        "DELETE FROM memory_baseline_dependencies "
                        "WHERE character_id=%s AND memory_id=%s",
                        key,
                    )
                    for d in replacement.dependencies:
                        connection.execute(
                            "INSERT INTO memory_baseline_dependencies(character_id,memory_id,"
                            "turn_id,basis,receipt_version,available_end,used_end,deadline) "
                            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                            (
                                *key,
                                d.turn_id,
                                d.basis,
                                d.receipt_version,
                                d.available_end,
                                d.used_end,
                                d.deadline,
                            ),
                        )
                else:
                    connection.execute(
                        sql.SQL(
                            "UPDATE {} SET payload=%s WHERE character_id=%s AND memory_id=%s"
                        ).format(sql.Identifier(table)),
                        (Jsonb(copy_payload(value)), *key),
                    )
                    save_copy_sources(connection, table, value)
            self._normalize_candidate_relations(connection, invalidated)
            removed: list[StoredReference] = []
            provisions = self._remove_references(
                connection,
                attempt.request.target_turn_ids,
                recalculated | invalidated,
                invalidated,
                removed,
            )
            for replacement in replacements:
                if replacement.value is not None:
                    continue
                old = replacement.copy.memory
                table = replacement.copy.table
                if table == "memory_baselines" and (old.character_id, old.id) in projected:
                    continue
                if table == "derived_memories":
                    connection.execute(
                        "DELETE FROM memory_baselines WHERE character_id=%s AND memory_id=%s",
                        (old.character_id, old.id),
                    )
                connection.execute(
                    sql.SQL("DELETE FROM {} WHERE character_id=%s AND {}=%s").format(
                        sql.Identifier(table),
                        sql.Identifier("id" if table == "derived_memories" else "memory_id"),
                    ),
                    (old.character_id, old.id),
                )
                removed.append(StoredReference(table=table, key=(old.character_id, old.id)))
            self._remove_extractions(connection, attempt.request.target_turn_ids)
            self._withdraw_index(
                connection, attempt.request.target_turn_ids, recalculated | invalidated
            )
            connection.execute(
                "SELECT public.apply_author_deletion(%s,%s)",
                (attempt.request.command.request_id, attempt.token),
            )
            result = CascadeResult(
                deleted_turn_ids=attempt.request.target_turn_ids,
                recalculated_memories=tuple(
                    MemoryRef(character_id=c, memory_id=m) for c, m in sorted(recalculated)
                ),
                invalidated_memories=tuple(
                    MemoryRef(character_id=c, memory_id=m) for c, m in sorted(invalidated)
                ),
                invalidated_provision_ids=tuple(provisions),
                removed_references=tuple(removed),
            )
            row = connection.execute(
                "UPDATE deletion_requests SET status='done',token=NULL,lease_until=NULL,"
                "cascade_result=%s,error=NULL,completed_at=%s WHERE request_id=%s RETURNING *",
                (Jsonb(json_record(asdict(result))), now, attempt.request.command.request_id),
            ).fetchone()
            assert row is not None
            return _request(row)

    def _normalize_candidate_relations(
        self, connection: psycopg.Connection[DictRow], invalidated: set[tuple[str, str]]
    ) -> None:
        for owner in sorted({owner for owner, _ in invalidated}):
            representatives = _MemoryTransaction(connection, owner).representatives()
            targets = [identifier for character, identifier in invalidated if character == owner]
            for relation in connection.execute(
                "SELECT turn_id,from_id,to_id,type FROM memory_candidate_relations "
                "WHERE character_id=%s AND to_id=ANY(%s)",
                (owner, targets),
            ).fetchall():
                original = MemoryRelation(
                    from_id=relation["from_id"], to_id=relation["to_id"], type=relation["type"]
                )
                (normalized,) = normalize_relations((original,), representatives)
                connection.execute(
                    "DELETE FROM memory_candidate_relations WHERE character_id=%s "
                    "AND turn_id=%s AND from_id=%s AND to_id=%s AND type=%s",
                    (owner, relation["turn_id"], original.from_id, original.to_id, original.type),
                )
                connection.execute(
                    "INSERT INTO memory_candidate_relations VALUES(%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (
                        owner,
                        relation["turn_id"],
                        normalized.from_id,
                        normalized.to_id,
                        normalized.type,
                    ),
                )

    def _remove_extractions(
        self, connection: psycopg.Connection[DictRow], targets: tuple[str, ...]
    ) -> None:
        identifiers = list(targets)
        rows = connection.execute(
            "SELECT DISTINCT character_id,extraction_turn_id FROM memory_input_dependencies "
            "WHERE turn_id=ANY(%s)",
            (identifiers,),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE memory_extractions SET token=NULL,signature='',status='retry',"
                "lease_until=NULL WHERE character_id=%s AND turn_id=%s",
                (row["character_id"], row["extraction_turn_id"]),
            )
        connection.execute(
            "DELETE FROM memory_input_dependencies WHERE turn_id=ANY(%s) "
            "OR extraction_turn_id=ANY(%s)",
            (identifiers, identifiers),
        )
        for table in (
            "memory_candidate_relations",
            "memory_candidates",
            "memory_generated",
            "memory_extractions",
            "memory_notifications",
        ):
            connection.execute(
                sql.SQL("DELETE FROM {} WHERE turn_id=ANY(%s)").format(sql.Identifier(table)),
                (identifiers,),
            )

    def _remove_references(
        self,
        connection: psycopg.Connection[DictRow],
        targets: tuple[str, ...],
        affected: set[tuple[str, str]],
        invalidated: set[tuple[str, str]],
        removed: list[StoredReference],
    ) -> list[str]:
        identifiers = list(targets)
        anchors = connection.execute(
            "SELECT character_id,anchor_id FROM verbatim_anchors WHERE turn_id=ANY(%s)",
            (identifiers,),
        ).fetchall()
        voices = connection.execute(
            "SELECT character_id,exemplar_id FROM voice_exemplars WHERE turn_id=ANY(%s)",
            (identifiers,),
        ).fetchall()
        anchor_ids = {(r["character_id"], r["anchor_id"]) for r in anchors}
        voice_ids = {(r["character_id"], r["exemplar_id"]) for r in voices}
        for anchor in anchors:
            key = (anchor["character_id"], "anchor", anchor["anchor_id"])
            documents = connection.execute(
                "DELETE FROM recall_documents WHERE owner_id=%s AND kind=%s AND identifier=%s "
                "RETURNING content_digest",
                key,
            ).fetchall()
            collect_embeddings(
                connection,
                [d["content_digest"] for d in documents if d["content_digest"] is not None],
            )
            removed.append(
                StoredReference(
                    table="verbatim_anchors", key=(anchor["character_id"], anchor["anchor_id"])
                )
            )
        removed.extend(
            StoredReference(table="voice_exemplars", key=(v["character_id"], v["exemplar_id"]))
            for v in voices
        )
        provision_ids = []
        for row in connection.execute("SELECT * FROM provision_records").fetchall():
            p = row["payload"]
            if (
                p["turn_id"] in targets
                or any(s["turn_id"] in targets for s in p["source_spans"])
                or any(
                    (r["owner_id"], r["identifier"])
                    in (
                        affected
                        if r["kind"] == "memory"
                        else anchor_ids
                        if r["kind"] == "anchor"
                        else voice_ids
                    )
                    for r in p["provided_refs"]
                )
            ):
                connection.execute(
                    "DELETE FROM provision_records WHERE provision_id=%s", (row["provision_id"],)
                )
                provision_ids.append(row["provision_id"])
        for row in connection.execute("SELECT * FROM profile_entries").fetchall():
            if row["turn_id"] in targets or (row["owner_id"], row["memory_id"]) in affected:
                connection.execute(
                    "DELETE FROM profile_entries WHERE character_id=%s AND entry_id=%s",
                    (row["character_id"], row["entry_id"]),
                )
                removed.append(
                    StoredReference(
                        table="profile_entries", key=(row["character_id"], row["entry_id"])
                    )
                )
        profiles = {r.key[0] for r in removed if r.table == "profile_entries"}
        for character in profiles:
            connection.execute(
                "UPDATE character_profiles SET version=version+1 WHERE character_id=%s",
                (character,),
            )
        for character, identifier in invalidated:
            connection.execute(
                "DELETE FROM memory_relations WHERE character_id=%s AND (from_id=%s OR to_id=%s)",
                (character, identifier, identifier),
            )
            connection.execute(
                "DELETE FROM memory_candidate_relations WHERE character_id=%s "
                "AND (from_id=%s OR to_id=%s)",
                (character, identifier, identifier),
            )
        for table in ("character_states", "episodes"):
            for row in connection.execute(
                sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
            ).fetchall():
                if any(d["turn_id"] in targets for d in row["dependencies"]["values"]):
                    if table == "episodes":
                        connection.execute(
                            "DELETE FROM episodes WHERE episode_id=%s", (row["episode_id"],)
                        )
                    else:
                        connection.execute(
                            "DELETE FROM character_states WHERE character_id=%s "
                            "AND session_id=%s AND surface=%s",
                            (row["character_id"], row["session_id"], row["surface"]),
                        )
        connection.execute(
            "DELETE FROM episodes e WHERE EXISTS(SELECT 1 FROM episode_spans s "
            "WHERE s.episode_id=e.episode_id AND s.turn_id=ANY(%s))",
            (identifiers,),
        )
        windows = connection.execute(
            "SELECT DISTINCT unnest(perceived_by) AS character_id,session_id,surface FROM turns "
            "WHERE turn_id=ANY(%s)",
            (identifiers,),
        ).fetchall()
        for window in windows:
            key = (window["character_id"], window["session_id"], window["surface"])
            connection.execute(
                "UPDATE context_notifications SET version=version+1 "
                "WHERE character_id=%s AND session_id=%s AND surface=%s",
                key,
            )
            connection.execute(
                "UPDATE context_work SET status='retry',token=NULL,signature=NULL,"
                "completed_signature=NULL,lease_until=NULL,evaluated_version=NULL "
                "WHERE character_id=%s AND session_id=%s AND surface=%s",
                key,
            )
        return sorted(provision_ids)

    def _withdraw_index(
        self,
        connection: psycopg.Connection[DictRow],
        targets: tuple[str, ...],
        affected: set[tuple[str, str]],
    ) -> None:
        rows = connection.execute(
            "SELECT DISTINCT d.* FROM recall_documents d LEFT JOIN recall_document_sources s "
            "USING(owner_id,kind,identifier) WHERE s.turn_id=ANY(%s)",
            (list(targets),),
        ).fetchall()
        for character, identifier in affected:
            rows.extend(
                connection.execute(
                    "SELECT * FROM recall_documents WHERE owner_id=%s AND kind='memory' "
                    "AND identifier=%s",
                    (character, identifier),
                ).fetchall()
            )
        for row in rows:
            key = (row["owner_id"], row["kind"], row["identifier"])
            connection.execute(
                "DELETE FROM recall_documents WHERE owner_id=%s AND kind=%s AND identifier=%s", key
            )
        collect_embeddings(
            connection, [r["content_digest"] for r in rows if r["content_digest"] is not None]
        )
        for character, identifier in affected:
            connection.execute(
                "INSERT INTO recall_documents(owner_id,kind,identifier) "
                "SELECT character_id,'memory',id FROM derived_memories "
                "WHERE character_id=%s AND id=%s ON CONFLICT DO NOTHING",
                (character, identifier),
            )

    def fail(self, attempt: DeletionAttempt, *, error: str, now: datetime) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE deletion_requests SET status='failed',error=%s,completed_at=%s,"
                "token=NULL,lease_until=NULL WHERE request_id=%s AND token=%s AND status='running'",
                (
                    error
                    if error in ("invalid_output", "snapshot_conflict", "dependency_unavailable")
                    else "dependency_unavailable",
                    now,
                    attempt.request.command.request_id,
                    attempt.token,
                ),
            )

    def get(self, request_id: str) -> DeletionRequest | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM deletion_requests WHERE request_id=%s", (request_id,)
            ).fetchone()
            return _request(row) if row is not None else None
