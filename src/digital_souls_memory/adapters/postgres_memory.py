"""Explicit SQL for derived projections and restartable extraction work."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.memory_records import (
    candidate_record,
    memory_record,
    restore_memory,
)
from digital_souls_memory.memory.domain import (
    DeliveryBasis,
    DerivedMemory,
    ExtractionState,
    InputDependency,
    MemoryRelation,
    MemoryValidationError,
)
from digital_souls_memory.memory.ports import (
    MemoryConflict,
    MemoryContribution,
    MemoryTransaction,
    MemoryUnavailable,
    SurfacePage,
    WorkCursor,
    WorkPage,
)


def _state(row: DictRow | None) -> ExtractionState | None:
    if row is None:
        return None
    return ExtractionState(**(row | {"delivery_basis": DeliveryBasis(row["delivery_basis"])}))


COPY_SOURCE_TABLES = {
    "memory_candidates": "memory_candidate_sources",
    "memory_generated": "memory_generated_sources",
    "memory_baselines": "memory_baseline_sources",
}


def copy_payload(value: DerivedMemory) -> dict[str, object]:
    record = candidate_record(value)
    del record["source_turn_ids"]
    return record


def copy_sources(
    connection: psycopg.Connection[DictRow], table: str, character_id: str, memory_id: str
) -> tuple[str, ...]:
    rows = connection.execute(
        sql.SQL(
            "SELECT turn_id FROM {} WHERE character_id=%s AND memory_id=%s ORDER BY position"
        ).format(sql.Identifier(COPY_SOURCE_TABLES[table])),
        (character_id, memory_id),
    ).fetchall()
    return tuple(r["turn_id"] for r in rows)


def save_copy_sources(
    connection: psycopg.Connection[DictRow], table: str, value: DerivedMemory
) -> None:
    source_table = sql.Identifier(COPY_SOURCE_TABLES[table])
    connection.execute(
        sql.SQL("DELETE FROM {} WHERE character_id=%s AND memory_id=%s").format(source_table),
        (value.character_id, value.id),
    )
    for position, identifier in enumerate(value.source_turn_ids):
        connection.execute(
            sql.SQL(
                "INSERT INTO {}(character_id,memory_id,turn_id,position) VALUES(%s,%s,%s,%s)"
            ).format(source_table),
            (value.character_id, value.id, identifier, position),
        )


class _MemoryTransaction:
    def __init__(self, connection: psycopg.Connection[DictRow], character_id: str) -> None:
        self.connection = connection
        self.character_id = character_id

    def contribution_ids(self, turn_id: str) -> frozenset[str]:
        rows = self.connection.execute(
            "SELECT memory_id FROM memory_generated WHERE character_id=%s AND turn_id=%s",
            (self.character_id, turn_id),
        ).fetchall()
        return frozenset(r["memory_id"] for r in rows)

    def projection_neighbors(self, identifiers: frozenset[str]) -> frozenset[str]:
        fact_matches = sql.SQL(" UNION ALL ").join(
            sql.SQL(
                "SELECT other.memory_id FROM {} other WHERE other.character_id=%s "
                "AND other.payload->>'kind'='fact' "
                "AND md5(other.payload->>'subject')=md5(seed.payload->>'subject') "
                "AND md5(other.payload->>'predicate')=md5(seed.payload->>'predicate') "
                "AND md5(other.payload->>'object')=md5(seed.payload->>'object') "
                "AND (other.payload->>'subject',other.payload->>'predicate',"
                "other.payload->>'object')="
                "(seed.payload->>'subject',seed.payload->>'predicate',seed.payload->>'object')"
            ).format(sql.Identifier(table))
            for table in ("memory_generated", "memory_baselines")
        )
        rows = self.connection.execute(
            sql.SQL(
                "WITH seeds AS MATERIALIZED ("
                "SELECT payload FROM memory_generated WHERE character_id=%s AND memory_id=ANY(%s) "
                "AND payload->>'kind'='fact' UNION ALL "
                "SELECT payload FROM memory_baselines WHERE character_id=%s AND memory_id=ANY(%s) "
                "AND payload->>'kind'='fact'), "
                "relations AS NOT MATERIALIZED (SELECT from_id,to_id,type FROM memory_relations "
                "WHERE character_id=%s UNION SELECT from_id,to_id,type "
                "FROM memory_candidate_relations WHERE character_id=%s) "
                "SELECT to_id AS memory_id FROM relations WHERE from_id=ANY(%s) "
                "AND (type<>'derives' OR EXISTS (SELECT 1 FROM derived_memories m "
                "WHERE m.character_id=%s AND m.id=to_id AND NOT m.search_eligible)) UNION "
                "SELECT from_id FROM relations WHERE to_id=ANY(%s) UNION "
                "SELECT matched.memory_id FROM seeds seed CROSS JOIN LATERAL "
                "({}) matched"
            ).format(fact_matches),
            (
                self.character_id,
                list(identifiers),
                self.character_id,
                list(identifiers),
                self.character_id,
                self.character_id,
                list(identifiers),
                self.character_id,
                list(identifiers),
                self.character_id,
                self.character_id,
            ),
        ).fetchall()
        return frozenset(r["memory_id"] for r in rows)

    def withdrawn_ids(self, identifiers: frozenset[str]) -> frozenset[str]:
        rows = self.connection.execute(
            "SELECT id FROM derived_memories WHERE character_id=%s AND id=ANY(%s) "
            "AND NOT search_eligible",
            (self.character_id, list(identifiers)),
        ).fetchall()
        return frozenset(r["id"] for r in rows)

    def _source_map(self, table: str, identifiers: list[str]) -> dict[str, tuple[str, ...]]:
        rows = self.connection.execute(
            sql.SQL(
                "SELECT memory_id,turn_id FROM {} WHERE character_id=%s "
                "AND memory_id=ANY(%s) ORDER BY memory_id,position"
            ).format(sql.Identifier(table)),
            (self.character_id, identifiers),
        ).fetchall()
        sources: dict[str, tuple[str, ...]] = {identifier: () for identifier in identifiers}
        for row in rows:
            sources[row["memory_id"]] += (row["turn_id"],)
        return sources

    def _record(self, value: DerivedMemory) -> dict[str, object]:
        replace(value)
        if value.character_id != self.character_id:
            raise MemoryValidationError("Wrong transaction owner")
        record = memory_record(value)
        record["epistemics"] = Jsonb(record["epistemics"])
        record["identity_links"] = Jsonb(record["identity_links"])
        return record

    def add_memory(self, value: DerivedMemory) -> None:
        self._insert_projection(value)
        self._baseline(value)

    def _insert_projection(self, value: DerivedMemory) -> None:
        record = self._record(value)
        self.connection.execute(
            sql.SQL("INSERT INTO derived_memories ({}) VALUES ({})").format(
                sql.SQL(",").join(map(sql.Identifier, record)),
                sql.SQL(",").join(sql.Placeholder() for _ in record),
            ),
            tuple(record.values()),
        )
        self._sources(value)
        self.connection.execute(
            "UPDATE derived_memories SET search_eligible=TRUE WHERE character_id=%s AND id=%s",
            (self.character_id, value.id),
        )

    def replace_memory(self, value: DerivedMemory) -> None:
        self.replace_projection(value)
        self._baseline(value)

    def _baseline(self, value: DerivedMemory) -> None:
        self.connection.execute(
            "INSERT INTO memory_baselines(character_id,memory_id,payload) VALUES(%s,%s,%s) "
            "ON CONFLICT(character_id,memory_id) DO UPDATE SET payload=EXCLUDED.payload",
            (self.character_id, value.id, Jsonb(copy_payload(value))),
        )
        save_copy_sources(self.connection, "memory_baselines", value)

    def baselines(self, identifiers: frozenset[str] | None = None) -> tuple[DerivedMemory, ...]:
        selected = None if identifiers is None else list(identifiers)
        rows = self.connection.execute(
            "SELECT memory_id,payload FROM memory_baselines WHERE character_id=%s "
            "AND (%s::text[] IS NULL OR memory_id=ANY(%s))",
            (self.character_id, selected, selected),
        ).fetchall()
        sources = self._source_map("memory_baseline_sources", [r["memory_id"] for r in rows])
        return tuple(restore_memory(r["payload"], sources[r["memory_id"]]) for r in rows)

    def replace_projection(self, value: DerivedMemory) -> None:
        record = self._record(value)
        del record["character_id"], record["id"]
        result = self.connection.execute(
            sql.SQL("UPDATE derived_memories SET {} WHERE character_id=%s AND id=%s").format(
                sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(k)) for k in record)
            ),
            (*record.values(), self.character_id, value.id),
        )
        if result.rowcount != 1:
            raise MemoryValidationError("Memory absent")
        self.connection.execute(
            "DELETE FROM memory_sources WHERE character_id=%s AND memory_id=%s",
            (self.character_id, value.id),
        )
        self._sources(value)
        self.connection.execute(
            "UPDATE derived_memories SET search_eligible=TRUE WHERE character_id=%s AND id=%s",
            (self.character_id, value.id),
        )

    def _sources(self, value: DerivedMemory) -> None:
        for position, identifier in enumerate(value.source_turn_ids):
            self.connection.execute(
                "INSERT INTO memory_sources(character_id,memory_id,turn_id,position) "
                "VALUES(%s,%s,%s,%s)",
                (self.character_id, value.id, identifier, position),
            )

    def memories(self, identifiers: frozenset[str] | None = None) -> tuple[DerivedMemory, ...]:
        selected = None if identifiers is None else list(identifiers)
        rows = self.connection.execute(
            "SELECT * FROM derived_memories WHERE character_id=%s "
            "AND (%s::text[] IS NULL OR id=ANY(%s)) ORDER BY "
            "valid_from,recorded_at,id",
            (self.character_id, selected, selected),
        ).fetchall()
        sources = self._source_map("memory_sources", [r["id"] for r in rows])
        return tuple(restore_memory(row, sources[row["id"]]) for row in rows)

    def _restore(self, row: DictRow) -> DerivedMemory:
        sources = self.connection.execute(
            "SELECT turn_id FROM memory_sources WHERE character_id=%s AND memory_id=%s "
            "ORDER BY position",
            (self.character_id, row["id"]),
        ).fetchall()
        return restore_memory(row, tuple(r["turn_id"] for r in sources))

    def add_relation(self, value: MemoryRelation) -> None:
        self.connection.execute(
            "INSERT INTO memory_relations(character_id,from_id,to_id,type) VALUES(%s,%s,%s,%s)",
            (self.character_id, value.from_id, value.to_id, value.type),
        )

    def sync_projection_relations(
        self, identifiers: frozenset[str], values: tuple[MemoryRelation, ...]
    ) -> None:
        rows = self.connection.execute(
            "SELECT from_id,to_id,type FROM memory_relations "
            "WHERE character_id=%s AND from_id=ANY(%s)",
            (self.character_id, list(identifiers)),
        ).fetchall()
        existing = tuple(MemoryRelation(**r) for r in rows)
        for relation in existing:
            if relation not in values:
                self.connection.execute(
                    "DELETE FROM memory_relations WHERE character_id=%s "
                    "AND from_id=%s AND to_id=%s AND type=%s",
                    (self.character_id, relation.from_id, relation.to_id, relation.type),
                )
        for relation in values:
            if relation not in existing:
                self.add_relation(relation)

    def candidates(
        self, identifiers: frozenset[str] | None = None
    ) -> tuple[MemoryContribution, ...]:
        selected = None if identifiers is None else list(identifiers)
        rows = self.connection.execute(
            "SELECT c.turn_id,c.memory_id,c.payload FROM memory_candidates c "
            "JOIN memory_extractions e "
            "USING(character_id,turn_id) WHERE c.character_id=%s AND e.status='done' "
            "AND (%s::text[] IS NULL OR c.memory_id=ANY(%s))",
            (self.character_id, selected, selected),
        ).fetchall()
        sources = self._source_map("memory_candidate_sources", [r["memory_id"] for r in rows])
        values = [
            MemoryContribution(
                r["turn_id"],
                restore_memory(r["payload"], sources[r["memory_id"]]),
            )
            for r in rows
        ]
        return tuple(
            sorted(values, key=lambda v: (v.memory.valid_from, v.memory.recorded_at, v.memory.id))
        )

    def generated_ids(self, identifiers: frozenset[str] | None = None) -> frozenset[str]:
        selected = None if identifiers is None else list(identifiers)
        rows = self.connection.execute(
            "SELECT memory_id FROM memory_generated WHERE character_id=%s "
            "AND (%s::text[] IS NULL OR memory_id=ANY(%s))",
            (self.character_id, selected, selected),
        ).fetchall()
        return frozenset(r["memory_id"] for r in rows)

    def add_generated(self, value: DerivedMemory) -> None:
        self._insert_projection(value)

    def proposed_relations(
        self, identifiers: frozenset[str] | None = None
    ) -> tuple[MemoryRelation, ...]:
        selected = None if identifiers is None else list(identifiers)
        rows = self.connection.execute(
            "SELECT r.from_id,r.to_id,r.type FROM memory_candidate_relations r "
            "JOIN memory_extractions e USING(character_id,turn_id) "
            "WHERE r.character_id=%s AND e.status='done' "
            "AND (%s::text[] IS NULL OR r.from_id=ANY(%s))",
            (self.character_id, selected, selected),
        ).fetchall()
        return tuple(MemoryRelation(**r) for r in rows)

    def set_relations(self, turn_id: str, values: tuple[MemoryRelation, ...]) -> None:
        self.connection.execute(
            "DELETE FROM memory_candidate_relations WHERE character_id=%s AND turn_id=%s",
            (self.character_id, turn_id),
        )
        for value in values:
            self.connection.execute(
                "INSERT INTO memory_candidate_relations(character_id,turn_id,from_id,to_id,type) "
                "VALUES(%s,%s,%s,%s,%s)",
                (self.character_id, turn_id, value.from_id, value.to_id, value.type),
            )

    def set_candidates(
        self,
        turn_id: str,
        values: tuple[DerivedMemory, ...],
        *,
        dependencies: tuple[InputDependency, ...],
    ) -> None:
        self.connection.execute(
            "DELETE FROM memory_input_dependencies WHERE character_id=%s AND extraction_turn_id=%s",
            (self.character_id, turn_id),
        )
        for dependency in dependencies:
            self.connection.execute(
                "INSERT INTO memory_input_dependencies(character_id,extraction_turn_id,turn_id,"
                "basis,receipt_version,available_end,used_end,deadline) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    self.character_id,
                    turn_id,
                    dependency.turn_id,
                    dependency.basis,
                    dependency.receipt_version,
                    dependency.available_end,
                    dependency.used_end,
                    dependency.deadline,
                ),
            )
        self.connection.execute(
            "DELETE FROM memory_candidates WHERE character_id=%s AND turn_id=%s",
            (self.character_id, turn_id),
        )
        for value in values:
            old = self.connection.execute(
                "SELECT turn_id,payload FROM memory_generated "
                "WHERE character_id=%s AND memory_id=%s",
                (self.character_id, value.id),
            ).fetchone()
            existing = self.connection.execute(
                "SELECT content,kind FROM derived_memories WHERE character_id=%s AND id=%s",
                (self.character_id, value.id),
            ).fetchone()
            if old is not None and (
                old["turn_id"] != turn_id
                or restore_memory(
                    old["payload"],
                    copy_sources(self.connection, "memory_generated", self.character_id, value.id),
                )
                != value
            ):
                raise MemoryConflict()
            if existing is not None and old is None:
                raise MemoryConflict()
            self.connection.execute(
                "INSERT INTO memory_generated(character_id,memory_id,turn_id,payload) "
                "VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (self.character_id, value.id, turn_id, Jsonb(copy_payload(value))),
            )
            save_copy_sources(self.connection, "memory_generated", value)
            self.connection.execute(
                "INSERT INTO memory_candidates(character_id,turn_id,memory_id,payload) "
                "VALUES(%s,%s,%s,%s)",
                (self.character_id, turn_id, value.id, Jsonb(copy_payload(value))),
            )
            save_copy_sources(self.connection, "memory_candidates", value)

    def get_extraction(self, turn_id: str) -> ExtractionState | None:
        return _state(
            self.connection.execute(
                "SELECT * FROM memory_extractions WHERE character_id=%s AND turn_id=%s",
                (self.character_id, turn_id),
            ).fetchone()
        )

    def save_extraction(self, value: ExtractionState) -> None:
        if value.character_id != self.character_id:
            raise MemoryValidationError("Wrong extraction owner")
        self.connection.execute(
            "INSERT INTO memory_extractions(character_id,turn_id,delivery_basis,signature,"
            "status,token,lease_until,deadline,error,text_hash,used_end,receipt_version)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(character_id,turn_id) DO "
            "UPDATE SET delivery_basis=EXCLUDED.delivery_basis,signature=EXCLUDED.signature,"
            "status=EXCLUDED.status,token=EXCLUDED.token,lease_until=EXCLUDED.lease_until,"
            "deadline=EXCLUDED.deadline,error=EXCLUDED.error,text_hash=EXCLUDED.text_hash,used_end=EXCLUDED.used_end,"
            "receipt_version=EXCLUDED.receipt_version",
            (
                value.character_id,
                value.turn_id,
                value.delivery_basis,
                value.signature,
                value.status,
                value.token,
                value.lease_until,
                value.deadline,
                value.error,
                value.text_hash,
                value.used_end,
                value.receipt_version,
            ),
        )

    def notification_version(self, turn_id: str) -> int:
        row = self.connection.execute(
            "SELECT version FROM memory_notifications WHERE turn_id=%s FOR UPDATE", (turn_id,)
        ).fetchone()
        if row is None:
            raise MemoryValidationError("Notification absent")
        return cast(int, row["version"])


class PostgresMemoryStore:
    def __init__(self, url: str) -> None:
        if not url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise MemoryUnavailable()
        self._url = url.replace("postgresql+psycopg://", "postgresql://", 1)

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with psycopg.connect(self._url, row_factory=dict_row, connect_timeout=5) as connection:
                yield connection
        except psycopg.errors.UniqueViolation as error:
            raise MemoryConflict() from error
        except (psycopg.IntegrityError, psycopg.DataError) as error:
            raise MemoryValidationError("Invalid persisted memory") from error
        except psycopg.Error as error:
            raise MemoryUnavailable() from error

    @contextmanager
    def transaction(self, character_id: str) -> Iterator[MemoryTransaction]:
        with self._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('recall-state',0))")
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("memory:" + character_id,)
            )
            yield _MemoryTransaction(connection, character_id)

    def get_memory(self, character_id: str, identifier: str) -> DerivedMemory | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM derived_memories WHERE character_id=%s AND id=%s",
                (character_id, identifier),
            ).fetchone()
            return _MemoryTransaction(connection, character_id)._restore(row) if row else None

    @contextmanager
    def list_memories(self, character_id: str) -> Iterator[Iterator[DerivedMemory]]:
        with self._connection() as connection:
            yield iter(_MemoryTransaction(connection, character_id).memories())

    @contextmanager
    def list_relations(
        self, character_id: str, identifier: str
    ) -> Iterator[Iterator[MemoryRelation]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT from_id,to_id,type FROM memory_relations WHERE character_id=%s "
                "AND (from_id=%s OR to_id=%s) ORDER BY from_id,to_id,type",
                (character_id, identifier, identifier),
            ).fetchall()
            yield (MemoryRelation(**r) for r in rows)

    def get_extraction(self, character_id: str, turn_id: str) -> ExtractionState | None:
        with self._connection() as connection:
            return _MemoryTransaction(connection, character_id).get_extraction(turn_id)

    def surface_page(self, *, after: str | None, limit: int) -> SurfacePage:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT t.surface FROM memory_notifications n JOIN turns t "
                "USING(turn_id) WHERE (%s::text IS NULL OR t.surface>%s) "
                "ORDER BY t.surface LIMIT %s",
                (after, after, limit),
            ).fetchall()
            surfaces = tuple(r["surface"] for r in rows)
        return SurfacePage(surfaces, surfaces[-1] if len(surfaces) == limit else None)

    def work_page(self, surface: str, *, after: WorkCursor | None, limit: int) -> WorkPage:
        condition = (
            "AND (t.occurred_at,t.recorded_at,t.turn_id,p.character_id)>(%s,%s,%s,%s) "
            if after is not None
            else ""
        )
        key = (
            (after.occurred_at, after.recorded_at, after.turn_id, after.character_id)
            if after
            else ()
        )
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT t.occurred_at,t.recorded_at,t.turn_id,p.character_id FROM "
                "memory_notifications n JOIN turns t USING(turn_id) "
                "CROSS JOIN LATERAL unnest(t.perceived_by) AS p(character_id) "
                "WHERE t.surface=%s "
                + condition
                + "ORDER BY t.occurred_at,t.recorded_at,t.turn_id,p.character_id LIMIT %s",
                (surface, *key, limit),
            ).fetchall()
            items = tuple(WorkCursor(**r) for r in rows)
        return WorkPage(items, items[-1] if len(items) == limit else None)

    def requeue(self, turn_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO memory_notifications(turn_id) VALUES(%s) ON "
                "CONFLICT(turn_id) DO UPDATE SET version=memory_notifications.version+1",
                (turn_id,),
            )
            connection.execute(
                "UPDATE memory_extractions SET signature='' WHERE turn_id=%s", (turn_id,)
            )

    @contextmanager
    def claim_surface(self, surface: str, now: datetime) -> Iterator[bool]:
        token = uuid4().hex
        with self._connection() as connection:
            row = connection.execute(
                "INSERT INTO memory_surfaces(surface,token,lease_until) "
                "VALUES(%s,%s,%s) ON CONFLICT(surface) DO UPDATE SET "
                "token=EXCLUDED.token,lease_until=EXCLUDED.lease_until WHERE "
                "memory_surfaces.lease_until<=%s OR memory_surfaces.token IS NULL "
                "RETURNING token",
                (surface, token, now + timedelta(minutes=5), now),
            ).fetchone()
        try:
            yield row is not None
        finally:
            if row is not None:
                with self._connection() as connection:
                    connection.execute(
                        "UPDATE memory_surfaces SET token=NULL,lease_until=NULL WHERE "
                        "surface=%s AND token=%s",
                        (surface, token),
                    )
