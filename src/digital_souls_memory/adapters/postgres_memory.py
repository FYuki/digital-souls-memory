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
from digital_souls_memory.adapters.postgres_projection_evidence import read_projection_evidence
from digital_souls_memory.memory.domain import (
    DeliveryBasis,
    DerivedMemory,
    ExtractionState,
    InputDependency,
    MemoryRelation,
    MemoryValidationError,
    RelationEffect,
    SourceContribution,
    memory_valid_at,
)
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.ports import (
    MemoryConflict,
    MemoryContribution,
    MemoryTransaction,
    MemoryUnavailable,
    SurfacePage,
    WorkCursor,
    WorkPage,
)
from digital_souls_memory.memory.projection import ProjectionEvidence, ProjectionInput
from digital_souls_memory.memory.relations import equivalent_ids, resolve_relations


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
    connection.execute(
        sql.SQL(
            "INSERT INTO {}(character_id,memory_id,turn_id,position) "
            "SELECT %s,%s,turn_id,position-1 FROM unnest(%s::text[]) "
            "WITH ORDINALITY AS s(turn_id,position)"
        ).format(source_table),
        (value.character_id, value.id, list(value.source_turn_ids)),
    )


class _MemoryTransaction:
    def projection_evidence(
        self, snapshot: ProjectionInput, inputs: MemoryInputs, now: datetime
    ) -> ProjectionEvidence:
        return read_projection_evidence(
            self.connection,
            snapshot,
            self.character_id,
            now,
            delivery_timeout=inputs.materials.delivery_timeout,
            conversation_surfaces=inputs.conversation_surfaces,
            comment_surfaces=inputs.comment_surfaces,
        )

    def __init__(
        self, connection: psycopg.Connection[DictRow], character_id: str, *, read_only: bool = False
    ) -> None:
        self.connection = connection
        self.character_id = character_id
        self.read_only = read_only

    def fact_matches(self, value: DerivedMemory, now: datetime) -> tuple[DerivedMemory, ...]:
        rows = self.connection.execute(
            "SELECT DISTINCT matches.memory_id FROM "
            "(SELECT memory_id,payload FROM memory_generated "
            "WHERE character_id=%s AND payload->>'kind'='fact' "
            "AND md5(payload->>'subject')=md5(%s) AND md5(payload->>'predicate')=md5(%s) "
            "AND md5(payload->>'object')=md5(%s) UNION "
            "SELECT memory_id,payload FROM memory_baselines "
            "WHERE character_id=%s AND payload->>'kind'='fact' "
            "AND md5(payload->>'subject')=md5(%s) AND md5(payload->>'predicate')=md5(%s) "
            "AND md5(payload->>'object')=md5(%s)) matches "
            "JOIN derived_memories m ON m.character_id=%s AND m.id=matches.memory_id "
            "WHERE m.id<>%s AND m.kind='fact' AND m.superseded_at IS NULL "
            "AND m.valid_from<=%s AND (m.valid_to IS NULL OR %s<m.valid_to)",
            (self.character_id, value.subject, value.predicate, value.object) * 2
            + (self.character_id, value.id, now, now),
        ).fetchall()
        return tuple(
            v
            for v in self.memories(frozenset(r["memory_id"] for r in rows))
            if v.id != value.id
            and v.kind == "fact"
            and v.superseded_at is None
            and memory_valid_at(v, now)
            and (v.subject, v.predicate, v.object) == (value.subject, value.predicate, value.object)
        )

    def validate_candidate(self, turn_id: str, value: DerivedMemory) -> None:
        old = self.connection.execute(
            "SELECT turn_id,payload FROM memory_generated WHERE character_id=%s AND memory_id=%s",
            (self.character_id, value.id),
        ).fetchone()
        existing = self.connection.execute(
            "SELECT id FROM derived_memories WHERE character_id=%s AND id=%s",
            (self.character_id, value.id),
        ).fetchone()
        if existing is not None and old is None:
            raise MemoryConflict()
        if old is not None and (
            old["turn_id"] != turn_id
            or restore_memory(
                old["payload"],
                copy_sources(self.connection, "memory_generated", self.character_id, value.id),
            )
            != value
        ):
            raise MemoryConflict()

    def source_contributions(
        self, identifiers: frozenset[str] | None = None
    ) -> tuple[SourceContribution, ...]:
        selected = None if identifiers is None else list(identifiers)
        rows = self.connection.execute(
            "SELECT * FROM memory_source_contributions WHERE character_id=%s "
            "AND (%s::text[] IS NULL OR fact_id=ANY(%s)) "
            "ORDER BY extraction_turn_id,source_turn_id",
            (self.character_id, selected, selected),
        ).fetchall()
        values = tuple(SourceContribution(**r) for r in rows)
        replacements = self._equivalent_ids(frozenset(v.fact_id for v in values))
        return tuple(replace(v, fact_id=replacements.get(v.fact_id, v.fact_id)) for v in values)

    def _equivalent_ids(self, identifiers: frozenset[str]) -> dict[str, str]:
        history = self.history(identifiers)
        current = self.candidates_for_turns(
            frozenset(v.turn_id for v in history),
            history_ids=frozenset(v.memory.id for v in history),
        )
        return equivalent_ids(history, current)

    def set_source_contributions(
        self, turn_id: str, values: tuple[SourceContribution, ...]
    ) -> None:
        self.connection.execute(
            "DELETE FROM memory_source_contributions "
            "WHERE character_id=%s AND extraction_turn_id=%s",
            (self.character_id, turn_id),
        )
        if any(
            v.character_id != self.character_id or v.extraction_turn_id != turn_id for v in values
        ):
            raise MemoryValidationError("Wrong contribution owner")
        self.connection.execute(
            "INSERT INTO memory_source_contributions "
            "SELECT %s,fact_id,%s,source_turn_id,occurred_at "
            "FROM unnest(%s::text[],%s::text[],%s::timestamptz[]) "
            "AS c(fact_id,source_turn_id,occurred_at)",
            (
                self.character_id,
                turn_id,
                [v.fact_id for v in values],
                [v.source_turn_id for v in values],
                [v.occurred_at for v in values],
            ),
        )

    def relation_effects(
        self, identifiers: frozenset[str] | None = None
    ) -> tuple[RelationEffect, ...]:
        return tuple(
            replace(
                effect, relation=relation, candidate=replace(effect.candidate, id=relation.from_id)
            )
            for effect, relation in self._resolved_relation_effects(identifiers)
        )

    def _resolved_relation_effects(
        self, identifiers: frozenset[str] | None
    ) -> tuple[tuple[RelationEffect, MemoryRelation], ...]:
        selected = None if identifiers is None else list(identifiers)
        rows = self.connection.execute(
            "SELECT turn_id,from_id,to_id,type,effect FROM memory_candidate_relations "
            "WHERE character_id=%s AND effect IS NOT NULL "
            "AND (%s::text[] IS NULL OR from_id=ANY(%s)) ORDER BY turn_id,from_id,to_id,type",
            (self.character_id, selected, selected),
        ).fetchall()
        effects = tuple(
            RelationEffect(
                turn_id=r["turn_id"],
                relation=MemoryRelation(from_id=r["from_id"], to_id=r["to_id"], type=r["type"]),
                candidate=restore_memory(r["effect"], tuple(r["effect"]["source_turn_ids"])),
            )
            for r in rows
        )
        if not effects:
            return ()
        history = self.history(
            frozenset(i for e in effects for i in (e.relation.from_id, e.relation.to_id))
        )
        current = self.candidates_for_turns(
            frozenset(c.turn_id for c in history),
            history_ids=frozenset(c.memory.id for c in history),
        )
        resolved = resolve_relations(tuple(e.relation for e in effects), history, current)
        return tuple(zip(effects, resolved, strict=True))

    def set_relation_effects(self, turn_id: str, values: tuple[RelationEffect, ...]) -> None:
        previous = self.connection.execute(
            "DELETE FROM memory_candidate_relations WHERE character_id=%s AND turn_id=%s "
            "AND effect IS NOT NULL RETURNING from_id,to_id,type",
            (self.character_id, turn_id),
        ).fetchall()
        historical = tuple(MemoryRelation(**r) for r in previous)
        history = self.history(frozenset(i for r in historical for i in (r.from_id, r.to_id)))
        current = self.candidates_for_turns(
            frozenset(c.turn_id for c in history),
            history_ids=frozenset(c.memory.id for c in history),
        )
        for relation in resolve_relations(historical, history, current):
            self.connection.execute(
                "DELETE FROM memory_relations WHERE character_id=%s AND from_id=%s "
                "AND to_id=%s AND type=%s AND NOT EXISTS(SELECT 1 "
                "FROM memory_candidate_relations r WHERE r.character_id=%s "
                "AND (r.from_id,r.to_id,r.type)=(%s,%s,%s))",
                (self.character_id, relation.from_id, relation.to_id, relation.type) * 2,
            )
        for value in values:
            r = value.relation
            self.connection.execute(
                "INSERT INTO memory_candidate_relations"
                "(character_id,turn_id,from_id,to_id,type,effect) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    self.character_id,
                    turn_id,
                    r.from_id,
                    r.to_id,
                    r.type,
                    Jsonb(candidate_record(value.candidate)),
                ),
            )

    def contribution_ids(self, turn_id: str) -> frozenset[str]:
        rows = self.connection.execute(
            "SELECT memory_id FROM memory_generated WHERE character_id=%s AND turn_id=%s "
            "UNION SELECT memory_id FROM memory_baseline_dependencies "
            "WHERE character_id=%s AND turn_id=%s "
            "UNION SELECT fact_id FROM memory_source_contributions "
            "WHERE character_id=%s AND extraction_turn_id=%s "
            "UNION SELECT from_id FROM memory_candidate_relations "
            "WHERE character_id=%s AND turn_id=%s",
            (self.character_id, turn_id) * 4,
        ).fetchall()
        return frozenset(r["memory_id"] for r in rows)

    def baseline_dependencies(
        self, identifiers: frozenset[str]
    ) -> dict[str, tuple[InputDependency, ...]]:
        rows = self.connection.execute(
            "SELECT memory_id,turn_id,basis,receipt_version,available_end,used_end,deadline "
            "FROM memory_baseline_dependencies WHERE character_id=%s AND memory_id=ANY(%s)",
            (self.character_id, list(identifiers)),
        ).fetchall()
        result: dict[str, tuple[InputDependency, ...]] = {}
        for row in rows:
            identifier = row.pop("memory_id")
            result[identifier] = (*result.get(identifier, ()), InputDependency(**row))
        return result

    def history(self, identifiers: frozenset[str]) -> tuple[MemoryContribution, ...]:
        rows = self.connection.execute(
            "SELECT turn_id,memory_id,payload FROM memory_generated "
            "WHERE character_id=%s AND memory_id=ANY(%s)",
            (self.character_id, list(identifiers)),
        ).fetchall()
        sources = self._source_map("memory_generated_sources", [r["memory_id"] for r in rows])
        return tuple(
            MemoryContribution(r["turn_id"], restore_memory(r["payload"], sources[r["memory_id"]]))
            for r in rows
        )

    def candidates_for_turns(
        self, turn_ids: frozenset[str], *, history_ids: frozenset[str] | None = None
    ) -> tuple[MemoryContribution, ...]:
        selected = None if history_ids is None else list(history_ids)
        rows = self.connection.execute(
            "SELECT c.memory_id FROM memory_candidates c "
            "WHERE c.character_id=%s AND c.turn_id=ANY(%s) "
            "AND (%s::text[] IS NULL OR EXISTS (SELECT 1 FROM memory_generated h "
            "WHERE h.character_id=c.character_id AND h.turn_id=c.turn_id "
            "AND h.memory_id=ANY(%s) AND (h.memory_id=c.memory_id OR "
            "(h.payload->'kind'=c.payload->'kind' AND h.payload->'content'=c.payload->'content' "
            "AND h.payload->'subject' IS NOT DISTINCT FROM c.payload->'subject' "
            "AND h.payload->'predicate' IS NOT DISTINCT FROM c.payload->'predicate' "
            "AND h.payload->'object' IS NOT DISTINCT FROM c.payload->'object'))))",
            (self.character_id, list(turn_ids), selected, selected),
        ).fetchall()
        return self.candidates(frozenset(r["memory_id"] for r in rows))

    def projection_neighbors(
        self, identifiers: frozenset[str], source_only: frozenset[str]
    ) -> frozenset[str]:
        historical_ids = self.connection.execute(
            "SELECT h.memory_id FROM memory_generated h WHERE h.character_id=%s "
            "AND h.turn_id IN (SELECT turn_id FROM memory_generated "
            "WHERE character_id=%s AND memory_id=ANY(%s)) "
            "AND EXISTS (SELECT 1 FROM memory_generated seed "
            "WHERE seed.character_id=h.character_id AND seed.turn_id=h.turn_id "
            "AND seed.memory_id=ANY(%s) AND (seed.memory_id=h.memory_id OR "
            "(seed.payload->'kind'=h.payload->'kind' "
            "AND seed.payload->'content'=h.payload->'content' "
            "AND seed.payload->'subject' IS NOT DISTINCT FROM h.payload->'subject' "
            "AND seed.payload->'predicate' IS NOT DISTINCT FROM h.payload->'predicate' "
            "AND seed.payload->'object' IS NOT DISTINCT FROM h.payload->'object'))) "
            "AND (h.memory_id=ANY(%s) OR EXISTS (SELECT 1 FROM memory_source_contributions s "
            "WHERE s.character_id=h.character_id AND s.fact_id=h.memory_id) "
            "OR EXISTS (SELECT 1 FROM memory_candidate_relations r "
            "WHERE r.character_id=h.character_id "
            "AND (r.from_id=h.memory_id OR r.to_id=h.memory_id)) "
            "OR EXISTS (SELECT 1 FROM memory_relations r WHERE r.character_id=h.character_id "
            "AND (r.from_id=h.memory_id OR r.to_id=h.memory_id)) "
            "OR EXISTS (SELECT 1 FROM derived_memories m "
            "WHERE m.character_id=h.character_id AND m.id=h.memory_id "
            "AND m.superseded_at IS NULL))",
            (self.character_id, self.character_id, *([list(identifiers)] * 3)),
        ).fetchall()
        history = self.history(frozenset(r["memory_id"] for r in historical_ids))
        current = self.candidates_for_turns(
            frozenset(v.turn_id for v in history),
            history_ids=frozenset(v.memory.id for v in history),
        )
        replacements = equivalent_ids(history, current)
        equivalents = frozenset(
            identifier
            for previous, current_id in replacements.items()
            if previous in identifiers or current_id in identifiers
            for identifier in (previous, current_id)
        )
        source_only = source_only | frozenset(
            previous for previous, current_id in replacements.items() if current_id in source_only
        )
        identifiers = identifiers | equivalents
        rows = self.connection.execute(
            "WITH relations AS (SELECT from_id,to_id,type FROM memory_relations "
            "WHERE character_id=%s UNION SELECT from_id,to_id,type "
            "FROM memory_candidate_relations WHERE character_id=%s) "
            "SELECT to_id AS memory_id FROM relations WHERE from_id=ANY(%s) "
            "AND (type<>'derives' OR EXISTS (SELECT 1 FROM derived_memories m "
            "WHERE m.character_id=%s AND m.id=to_id AND NOT m.search_eligible)) UNION "
            "SELECT from_id FROM relations WHERE to_id=ANY(%s) "
            "AND (type<>'derives' OR NOT to_id=ANY(%s))",
            (
                self.character_id,
                self.character_id,
                list(identifiers),
                self.character_id,
                list(identifiers),
                list(source_only),
            ),
        ).fetchall()
        return equivalents | frozenset(r["memory_id"] for r in rows)

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
            "INSERT INTO memory_baselines(character_id,memory_id,payload,temporal_origin) "
            "VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(character_id,memory_id) DO UPDATE SET payload=EXCLUDED.payload, "
            "temporal_origin=EXCLUDED.temporal_origin",
            (
                self.character_id,
                value.id,
                Jsonb(copy_payload(value)),
                Jsonb(
                    {
                        k: copy_payload(value)[k]
                        for k in ("valid_to", "superseded_at", "last_reinforced_at")
                    }
                ),
            ),
        )
        save_copy_sources(self.connection, "memory_baselines", value)
        self.connection.execute(
            "DELETE FROM memory_fact_sources WHERE character_id=%s AND memory_id=%s",
            (self.character_id, value.id),
        )
        self.connection.execute(
            "INSERT INTO memory_fact_sources SELECT %s,%s,turn_id,position-1 "
            "FROM unnest(%s::text[]) WITH ORDINALITY s(turn_id,position)",
            (self.character_id, value.id, list(value.source_turn_ids)),
        )

    def baselines(self, identifiers: frozenset[str] | None = None) -> tuple[DerivedMemory, ...]:
        selected = None if identifiers is None else list(identifiers)
        rows = self.connection.execute(
            "SELECT memory_id,payload FROM memory_baselines WHERE character_id=%s "
            "AND (%s::text[] IS NULL OR memory_id=ANY(%s))",
            (self.character_id, selected, selected),
        ).fetchall()
        sources = self._source_map("memory_baseline_sources", [r["memory_id"] for r in rows])
        return tuple(restore_memory(r["payload"], sources[r["memory_id"]]) for r in rows)

    def fact_sources(self, identifiers: frozenset[str]) -> dict[str, tuple[str, ...]]:
        return self._source_map("memory_fact_sources", list(identifiers))

    def temporal_origins(
        self, identifiers: frozenset[str]
    ) -> dict[str, dict[str, datetime | None]]:
        rows = self.connection.execute(
            "SELECT memory_id,payload,temporal_origin FROM memory_baselines "
            "WHERE character_id=%s AND memory_id=ANY(%s)",
            (self.character_id, list(identifiers)),
        ).fetchall()
        return {
            row["memory_id"]: {
                field: None if value is None else datetime.fromisoformat(value)
                for field in ("valid_to", "superseded_at", "last_reinforced_at")
                for value in [(row["payload"] | row["temporal_origin"])[field]]
            }
            for row in rows
        }

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
        self.connection.execute(
            "INSERT INTO memory_sources(character_id,memory_id,turn_id,position) "
            "SELECT %s,%s,turn_id,position-1 FROM unnest(%s::text[]) "
            "WITH ORDINALITY AS s(turn_id,position)",
            (self.character_id, value.id, list(value.source_turn_ids)),
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

    def applied_relations(
        self, identifiers: frozenset[str] | None = None
    ) -> tuple[MemoryRelation, ...]:
        selected = None if identifiers is None else list(identifiers)
        rows = self.connection.execute(
            "SELECT from_id,to_id,type FROM memory_relations "
            "WHERE character_id=%s AND (%s::text[] IS NULL OR from_id=ANY(%s))",
            (self.character_id, selected, selected),
        ).fetchall()
        return tuple(MemoryRelation(**r) for r in rows)

    def sync_projection_relations(
        self,
        identifiers: frozenset[str],
        values: tuple[MemoryRelation, ...],
        projected_ids: frozenset[str],
    ) -> None:
        self._sync_source_contributions(identifiers, projected_ids)
        for effect, resolved in self._resolved_relation_effects(identifiers):
            previous = effect.relation
            if previous == resolved:
                continue
            if (previous.from_id != resolved.from_id and resolved.from_id not in projected_ids) or (
                previous.to_id != resolved.to_id and resolved.to_id not in projected_ids
            ):
                continue
            key = (
                self.character_id,
                effect.turn_id,
                previous.from_id,
                previous.to_id,
                previous.type,
            )
            self.connection.execute(
                "INSERT INTO memory_candidate_relations "
                "(character_id,turn_id,from_id,to_id,type,effect) "
                "SELECT character_id,turn_id,%s,%s,type,"
                "effect || jsonb_build_object('id',%s::text) "
                "FROM memory_candidate_relations WHERE character_id=%s AND turn_id=%s "
                "AND from_id=%s AND to_id=%s AND type=%s ON CONFLICT DO NOTHING",
                (resolved.from_id, resolved.to_id, resolved.from_id, *key),
            )
            self.connection.execute(
                "DELETE FROM memory_candidate_relations WHERE character_id=%s AND turn_id=%s "
                "AND from_id=%s AND to_id=%s AND type=%s",
                key,
            )
        existing = self.applied_relations(identifiers)
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

    def _sync_source_contributions(
        self, identifiers: frozenset[str], projected_ids: frozenset[str]
    ) -> None:
        rows = self.connection.execute(
            "SELECT DISTINCT fact_id FROM memory_source_contributions "
            "WHERE character_id=%s AND fact_id=ANY(%s)",
            (self.character_id, list(identifiers)),
        ).fetchall()
        for previous, current in self._equivalent_ids(
            frozenset(r["fact_id"] for r in rows)
        ).items():
            if current not in projected_ids:
                continue
            self.connection.execute(
                "INSERT INTO memory_source_contributions "
                "SELECT character_id,%s,extraction_turn_id,source_turn_id,occurred_at "
                "FROM memory_source_contributions WHERE character_id=%s AND fact_id=%s "
                "ON CONFLICT DO NOTHING",
                (current, self.character_id, previous),
            )
            self.connection.execute(
                "DELETE FROM memory_source_contributions WHERE character_id=%s AND fact_id=%s",
                (self.character_id, previous),
            )

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
        self, identifiers: frozenset[str] | None = None, *, include_inactive: bool = False
    ) -> tuple[MemoryRelation, ...]:
        selected = None if identifiers is None else list(identifiers)
        rows = self.connection.execute(
            "SELECT r.from_id,r.to_id,r.type FROM memory_candidate_relations r "
            "JOIN memory_extractions e USING(character_id,turn_id) "
            "WHERE r.character_id=%s AND (e.status='done' OR %s) AND r.effect IS NULL "
            "AND (%s::text[] IS NULL OR r.from_id=ANY(%s))",
            (self.character_id, include_inactive, selected, selected),
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
                "INSERT INTO memory_fact_sources SELECT %s,%s,turn_id,position-1 "
                "FROM unnest(%s::text[]) WITH ORDINALITY s(turn_id,position) "
                "ON CONFLICT DO NOTHING",
                (self.character_id, value.id, list(value.source_turn_ids)),
            )
            self.connection.execute(
                "INSERT INTO memory_candidates(character_id,turn_id,memory_id,payload) "
                "VALUES(%s,%s,%s,%s)",
                (self.character_id, turn_id, value.id, Jsonb(copy_payload(value))),
            )
            save_copy_sources(self.connection, "memory_candidates", value)

    def extraction_states(self, turn_ids: frozenset[str]) -> dict[str, ExtractionState | None]:
        rows = self.connection.execute(
            "SELECT * FROM memory_extractions WHERE character_id=%s AND turn_id=ANY(%s)",
            (self.character_id, list(turn_ids)),
        ).fetchall()
        states = {row["turn_id"]: _state(row) for row in rows}
        return {identifier: states.get(identifier) for identifier in turn_ids}

    def notification_versions(self, turn_ids: frozenset[str]) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT turn_id,version FROM memory_notifications WHERE turn_id=ANY(%s)"
            + ("" if self.read_only else " FOR UPDATE"),
            (list(turn_ids),),
        ).fetchall()
        versions = {row["turn_id"]: row["version"] for row in rows}
        if versions.keys() != turn_ids:
            raise MemoryValidationError("Notification absent")
        return versions

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

    @contextmanager
    def requeue_transaction(self, turn_id: str) -> Iterator[tuple[MemoryTransaction, ...]]:
        with self._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('recall-state',0))")
            owners = connection.execute(
                "SELECT character_id FROM memory_extractions WHERE turn_id=%s "
                "UNION SELECT character_id FROM memory_source_contributions "
                "WHERE extraction_turn_id=%s ORDER BY character_id",
                (turn_id, turn_id),
            ).fetchall()
            for owner in owners:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("memory:" + owner["character_id"],),
                )
            connection.execute(
                "INSERT INTO memory_notifications(turn_id) VALUES(%s) ON "
                "CONFLICT(turn_id) DO UPDATE SET version=memory_notifications.version+1",
                (turn_id,),
            )
            connection.execute(
                "UPDATE memory_extractions SET signature='',status='retry',"
                "token=NULL,lease_until=NULL "
                "WHERE turn_id=%s",
                (turn_id,),
            )
            yield tuple(_MemoryTransaction(connection, r["character_id"]) for r in owners)

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
