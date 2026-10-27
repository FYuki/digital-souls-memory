"""Generate migration inputs with e0c82e0 projection and deletion semantics."""

from dataclasses import asdict, replace
from hashlib import sha256

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.memory_records import memory_record
from digital_souls_memory.adapters.postgres_memory import copy_payload
from digital_souls_memory.memory.domain import (
    DeliveryBasis,
    DerivedMemory,
    ExtractionState,
    MemoryRelation,
)
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.ports import MemoryContribution
from tests.evidence_support import RECORDED
from tests.postgres.restatement_0013_projection import (
    ProjectionEvidence,
    ProjectionInput,
    ProjectionResult,
    normalize_relations,
)


def legacy_snapshot(
    inputs: MemoryInputs, candidates: tuple[DerivedMemory, ...]
) -> tuple[ProjectionInput, ProjectionEvidence]:
    contributions = tuple(MemoryContribution(v.source_turn_ids[0], v) for v in candidates)
    resolved = {c.turn_id: inputs.resolve("char-b", (c.turn_id,)) for c in contributions}
    states: dict[str, ExtractionState | None] = {}
    for origin, value in resolved.items():
        decision = value.turns[0].decision
        assert decision is not None and decision.text is not None
        states[origin] = ExtractionState(
            character_id="char-b",
            turn_id=origin,
            delivery_basis=DeliveryBasis(decision.basis),
            signature=value.signature(1),
            status="done",
            token=None,
            lease_until=None,
            deadline=decision.deadline,
            error=None,
            text_hash=sha256(decision.text.encode()).hexdigest(),
            used_end=len(decision.text),
            receipt_version=decision.receipt_version,
        )
    snapshot = ProjectionInput(
        existing=(),
        baselines=(),
        contributions=contributions,
        states=states,
        versions={origin: 1 for origin in resolved},
        proposed=(),
        generated=frozenset(v.id for v in candidates),
        references=(),
        withdrawn_references=frozenset(),
        applied=(),
        baseline_dependencies={},
        representatives={},
        temporal_origins={},
    )
    turns = {
        identifier: source
        for candidate in candidates
        for identifier in candidate.source_turn_ids
        if (source := inputs.evidence.get_turn(identifier)) is not None
    }
    evidence = ProjectionEvidence(
        turns=turns,
        texts={i: inputs.materials.get_text("char-b", i) for i in turns},
        inputs=resolved,
    )
    return snapshot, evidence


def save_legacy_projection(
    url: str, inputs: MemoryInputs, snapshot: ProjectionInput, result: ProjectionResult
) -> None:
    # SQL mirrors e0c82e0's save_projection/add_generated/_baseline contracts.
    # Current adapter writes require 0014 tables and must not run against 0013.
    with psycopg.connect(url) as conn:
        for state in snapshot.states.values():
            assert state is not None
            record = asdict(state)
            conn.execute(
                sql.SQL(
                    "INSERT INTO memory_extractions ({}) VALUES ({}) "
                    "ON CONFLICT(character_id,turn_id) DO NOTHING"
                ).format(
                    sql.SQL(",").join(map(sql.Identifier, record)),
                    sql.SQL(",").join(sql.Placeholder() for _ in record),
                ),
                tuple(record.values()),
            )
        for candidate in snapshot.contributions:
            value = candidate.memory
            for table, sources in (
                ("memory_candidates", "memory_candidate_sources"),
                ("memory_generated", "memory_generated_sources"),
            ):
                conn.execute(
                    sql.SQL(
                        "INSERT INTO {}(character_id,memory_id,turn_id,payload) VALUES(%s,%s,%s,%s)"
                    ).format(sql.Identifier(table)),
                    ("char-b", value.id, candidate.turn_id, Jsonb(copy_payload(value))),
                )
                _sources(conn, sources, value)
            for dependency in inputs.resolve("char-b", (candidate.turn_id,)).dependencies():
                conn.execute(
                    "INSERT INTO memory_input_dependencies(character_id,extraction_turn_id,"
                    "turn_id,basis,receipt_version,available_end,used_end,deadline) "
                    "VALUES('char-b',%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        candidate.turn_id,
                        dependency.turn_id,
                        dependency.basis,
                        dependency.receipt_version,
                        dependency.available_end,
                        dependency.used_end,
                        dependency.deadline,
                    ),
                )
        for value in result.memories.values():
            _projection(conn, value)
        for baseline in snapshot.baselines:
            _baseline(conn, baseline)
        for relation in result.relations:
            conn.execute(
                "INSERT INTO memory_relations VALUES('char-b',%s,%s,%s)",
                (relation.from_id, relation.to_id, relation.type),
            )
        for relation in snapshot.proposed:
            origin = next(
                c.turn_id for c in snapshot.contributions if c.memory.id == relation.from_id
            )
            conn.execute(
                "INSERT INTO memory_candidate_relations VALUES('char-b',%s,%s,%s,%s)",
                (origin, relation.from_id, relation.to_id, relation.type),
            )
        for absorbed, target in result.representatives.items():
            conn.execute(
                "INSERT INTO memory_representatives VALUES('char-b',%s,%s)", (absorbed, target)
            )


def _sources(
    conn: psycopg.Connection[tuple[object, ...]], table: str, value: DerivedMemory
) -> None:
    conn.execute(
        sql.SQL("DELETE FROM {} WHERE character_id='char-b' AND memory_id=%s").format(
            sql.Identifier(table)
        ),
        (value.id,),
    )
    for position, source in enumerate(value.source_turn_ids):
        conn.execute(
            sql.SQL("INSERT INTO {} VALUES('char-b',%s,%s,%s)").format(sql.Identifier(table)),
            (value.id, source, position),
        )


def _projection(conn: psycopg.Connection[tuple[object, ...]], value: DerivedMemory) -> None:
    record = memory_record(value)
    record["epistemics"] = Jsonb(record["epistemics"])
    record["identity_links"] = Jsonb(record["identity_links"])
    conn.execute(
        sql.SQL(
            "INSERT INTO derived_memories ({}) VALUES ({}) "
            "ON CONFLICT(character_id,id) DO UPDATE SET {}"
        ).format(
            sql.SQL(",").join(map(sql.Identifier, record)),
            sql.SQL(",").join(sql.Placeholder() for _ in record),
            sql.SQL(",").join(
                sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(k), sql.Identifier(k))
                for k in record
            ),
        ),
        tuple(record.values()),
    )
    _sources(conn, "memory_sources", value)
    conn.execute("UPDATE derived_memories SET search_eligible=TRUE WHERE id=%s", (value.id,))


def _baseline(conn: psycopg.Connection[tuple[object, ...]], value: DerivedMemory) -> None:
    payload = copy_payload(value)
    conn.execute(
        "INSERT INTO memory_baselines VALUES('char-b',%s,%s,%s) "
        "ON CONFLICT(character_id,memory_id) DO UPDATE SET "
        "payload=EXCLUDED.payload,temporal_origin=EXCLUDED.temporal_origin",
        (
            value.id,
            Jsonb(payload),
            Jsonb({k: payload[k] for k in ("valid_to", "superseded_at", "last_reinforced_at")}),
        ),
    )
    _sources(conn, "memory_baseline_sources", value)


def legacy_context_deletion(
    url: str, inputs: MemoryInputs, revised: DerivedMemory, absorbed: DerivedMemory
) -> None:
    # e0c82e0 postgres_deletion.py:395 saves the final recalculation as baseline;
    # :529 marks the extraction depending on deleted context retry, without
    # retiring its generated history. Reproduce that sequence, including deletion.
    with psycopg.connect(url) as conn:
        for recalculated in (revised, absorbed):
            _projection(conn, recalculated)
            _baseline(conn, recalculated)
            for dependency in inputs.resolve("char-b", recalculated.source_turn_ids).dependencies():
                if dependency.turn_id not in recalculated.source_turn_ids:
                    continue
                conn.execute(
                    "INSERT INTO memory_baseline_dependencies "
                    "VALUES('char-b',%s,%s,%s,%s,%s,%s,%s)",
                    (
                        recalculated.id,
                        dependency.turn_id,
                        dependency.basis,
                        dependency.receipt_version,
                        dependency.available_end,
                        dependency.used_end,
                        dependency.deadline,
                    ),
                )
        # The final payload includes B's surviving extends; the temporal origin
        # saved by the old deletion snapshot is A's unreinforced generated body.
        conn.execute(
            "UPDATE memory_baselines b SET temporal_origin=jsonb_build_object("
            "'valid_to',g.payload->'valid_to','superseded_at',g.payload->'superseded_at',"
            "'last_reinforced_at',g.payload->'last_reinforced_at') "
            "FROM memory_generated g WHERE b.memory_id=%s "
            "AND (b.character_id,b.memory_id)=(g.character_id,g.memory_id)",
            (revised.id,),
        )
        conn.execute(
            "UPDATE memory_extractions SET token=NULL,signature='',status='retry',lease_until=NULL "
            "WHERE (character_id,turn_id) IN "
            "(SELECT character_id,extraction_turn_id FROM memory_input_dependencies "
            "WHERE turn_id='question')"
        )
        conn.execute("DELETE FROM memory_input_dependencies WHERE turn_id='question'")
        conn.execute("DELETE FROM memory_notifications WHERE turn_id='question'")
        conn.execute(
            "INSERT INTO deletion_requests(request_id,command,requested_by,target_turn_ids,"
            "status,token,lease_until,requested_at) "
            "VALUES('legacy-delete','{}','synthetic-author',ARRAY['question'],"
            "'running','synthetic-token',%s,%s)",
            (RECORDED, RECORDED),
        )
        conn.execute("SELECT public.apply_author_deletion('legacy-delete','synthetic-token')")


def project_legacy_chain(
    inputs: MemoryInputs,
    a: DerivedMemory,
    b: DerivedMemory,
    d: DerivedMemory,
    z: DerivedMemory,
) -> tuple[ProjectionInput, ProjectionResult]:
    snapshot, evidence = legacy_snapshot(inputs, (a, b, d))
    snapshot = replace(
        snapshot, proposed=(MemoryRelation(from_id=d.id, to_id=b.id, type="derives"),)
    )
    first = evidence.project(snapshot, RECORDED, select_representatives=True)
    # A separate baseline enters a later rebuild. The old projector normalizes
    # D's reference before selecting A's new representative and leaves B -> A -> Z.
    snapshot = replace(
        snapshot,
        existing=tuple(first.memories.values()),
        baselines=(z,),
        representatives=first.representatives,
        proposed=normalize_relations(snapshot.proposed, first.representatives),
        applied=normalize_relations(first.relations, first.representatives),
        temporal_origins={
            z.id: {"valid_to": None, "superseded_at": None, "last_reinforced_at": None}
        },
    )
    original = inputs.evidence.get_turn(z.source_turn_ids[0])
    assert original is not None
    evidence.turns[original.turn_id] = original
    evidence.texts[original.turn_id] = inputs.materials.get_text("char-b", original.turn_id)
    result = evidence.project(snapshot, RECORDED, select_representatives=True)
    # e0c82e0 projection_input fetches only representatives for the selected
    # identifiers and their immediate relation targets. A rebuild of D sees B -> A
    # but omits A -> Z, and therefore reads the superseded A as its reference.
    partial = replace(
        snapshot,
        existing=(result.memories[d.id],),
        baselines=(),
        contributions=tuple(c for c in snapshot.contributions if c.memory.id == d.id),
        proposed=normalize_relations(
            (MemoryRelation(from_id=d.id, to_id=b.id, type="derives"),), {b.id: a.id}
        ),
        generated=frozenset({d.id}),
        references=(result.memories[a.id],),
        applied=(),
        representatives={b.id: a.id},
        temporal_origins={},
    )
    rejected = evidence.project(partial, RECORDED)
    result = replace(
        result,
        memories=result.memories | rejected.memories,
        relations=tuple(r for r in result.relations if r.from_id != d.id),
    )
    return replace(
        snapshot, proposed=(MemoryRelation(from_id=d.id, to_id=b.id, type="derives"),)
    ), result
