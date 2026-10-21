"""Persistent pgvector/pg_bigm retrieval and version-checked index publication."""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from uuid import uuid4

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.memory_records import candidate_record, restore_memory
from digital_souls_memory.adapters.postgres_memory import _MemoryTransaction
from digital_souls_memory.adapters.recall_records import (
    current_stamp,
    json_record,
    read_material,
    restore_stamp,
)
from digital_souls_memory.evidence.provision import CandidateRef, ProvisionUnavailable
from digital_souls_memory.recall.domain import (
    DocumentStamp,
    EmbeddingBatch,
    RecallCandidate,
    SourceStamp,
    VectorSpace,
)
from digital_souls_memory.recall.eligibility import memory_allowed
from digital_souls_memory.recall.indexing import IndexInput, IndexWork
from digital_souls_memory.recall.inputs import InputLabel, LocalOnly, ModelInput
from digital_souls_memory.voice.domain import VerbatimAnchor


def _key(ref: CandidateRef) -> tuple[str, str, str]:
    return (
        ref.owner_id,
        "memory" if ref.memory_id is not None else "anchor",
        str(ref.memory_id or ref.anchor_id),
    )


def _vector(batch: EmbeddingBatch) -> str:
    if len(batch.vectors) != 1:
        raise ProvisionUnavailable()
    return "[" + ",".join(str(v) for v in batch.vectors[0]) + "]"


class PostgresRetrievalStore:
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

    def claim(self, *, space: VectorSpace, now: datetime, limit: int) -> tuple[IndexWork, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM recall_documents WHERE (pending OR model IS DISTINCT FROM %s "
                "OR revision IS DISTINCT FROM %s OR digest IS DISTINCT FROM %s OR "
                "dimensions IS DISTINCT FROM %s) "
                "AND (not_before IS NULL OR not_before<=%s) AND (lease_until IS NULL OR "
                "lease_until<=%s) "
                "ORDER BY owner_id,kind,identifier LIMIT %s FOR UPDATE SKIP LOCKED",
                (space.model, space.revision, space.digest, space.dimensions, now, now, limit),
            ).fetchall()
            work: list[IndexWork] = []
            for row in rows:
                token = uuid4().hex
                connection.execute(
                    "UPDATE recall_documents SET claim_token=%s,lease_until=%s "
                    "WHERE owner_id=%s AND kind=%s AND identifier=%s",
                    (
                        token,
                        now + timedelta(minutes=5),
                        row["owner_id"],
                        row["kind"],
                        row["identifier"],
                    ),
                )
                ref = CandidateRef(
                    owner_id=row["owner_id"],
                    memory_id=row["identifier"] if row["kind"] == "memory" else None,
                    anchor_id=row["identifier"] if row["kind"] == "anchor" else None,
                )
                work.append(IndexWork(ref=ref, version=row["version"], token=token, space=space))
            return tuple(work)

    def load(self, work: IndexWork, *, now: datetime) -> IndexInput | None:
        with self._connection() as connection:
            return self._load(connection, work, now)

    def _load(
        self, connection: psycopg.Connection[DictRow], work: IndexWork, now: datetime
    ) -> IndexInput | None:
        row = connection.execute(
            "SELECT * FROM recall_documents WHERE owner_id=%s AND kind=%s AND identifier=%s",
            _key(work.ref),
        ).fetchone()
        if row is None or row["version"] != work.version or row["claim_token"] != work.token:
            return None
        memory = None
        anchor = None
        saved: dict[str, SourceStamp] = {}
        if work.ref.memory_id is not None:
            row = connection.execute(
                "SELECT * FROM derived_memories WHERE character_id=%s AND id=%s",
                (work.ref.owner_id, work.ref.memory_id),
            ).fetchone()
            if row is None or not row["search_eligible"]:
                return None
            memory = _MemoryTransaction(connection, work.ref.owner_id)._restore(row)
            if not memory_allowed(memory, character_id=work.ref.owner_id, now=now):
                return None
            # Relation effects and reinforcement retain the extraction inputs that caused them.
            inputs = connection.execute(
                "WITH RECURSIVE relevant(id) AS (SELECT %s::text UNION SELECT r.from_id "
                "FROM memory_relations r JOIN relevant v ON r.to_id=v.id WHERE r.character_id=%s) "
                "SELECT DISTINCT d.* FROM relevant v JOIN memory_candidates c ON c.memory_id=v.id "
                "AND c.character_id=%s JOIN memory_input_dependencies d ON "
                "d.character_id=c.character_id "
                "AND d.extraction_turn_id=c.turn_id",
                (memory.id, memory.character_id, memory.character_id),
            ).fetchall()
            for item in inputs:
                previous = saved.get(item["turn_id"])
                if previous is not None and previous.used_end >= item["used_end"]:
                    continue
                saved[item["turn_id"]] = SourceStamp(
                    turn_id=item["turn_id"],
                    character_id=work.ref.owner_id,
                    basis=item["basis"],
                    receipt_version=item["receipt_version"],
                    available_end=item["available_end"],
                    used_end=item["used_end"],
                    deadline=item["deadline"],
                )
            source_ids = memory.source_turn_ids
            search_text = memory.content
        else:
            row = connection.execute(
                "SELECT * FROM verbatim_anchors WHERE character_id=%s AND anchor_id=%s",
                (work.ref.owner_id, work.ref.anchor_id),
            ).fetchone()
            if row is None:
                return None
            anchor = VerbatimAnchor(**row)
            source_ids = (anchor.turn_id,)
            search_text = ""
        dependencies: list[SourceStamp] = []
        labels: list[InputLabel] = []
        source_texts: list[str] = []
        times: list[datetime] = []
        for turn_id in dict.fromkeys((*source_ids, *saved)):
            material = read_material(connection, turn_id)
            if material is None:
                return None
            old = saved.get(turn_id)
            stamp = current_stamp(
                connection,
                turn_id=turn_id,
                character_id=work.ref.owner_id,
                used_end=old.used_end if old else len(material.turn.text) if memory else None,
                now=now,
                timeout=self.timeout,
            )
            if stamp is None or stamp.used_end > stamp.available_end or stamp.available_end == 0:
                return None
            dependencies.append(stamp)
            labels.append(InputLabel(material.turn.sensitivity, material.turn.classifier_version))
            source_texts.append(material.turn.text[: stamp.used_end])
            if turn_id in source_ids:
                times.append(material.turn.occurred_at)
        if anchor is not None:
            search_text = source_texts[0]
        digest_input = (search_text, source_texts, [(v.turn_id, v.used_end) for v in dependencies])
        digest = hashlib.sha256(json.dumps(digest_input, ensure_ascii=False).encode()).hexdigest()
        candidate = RecallCandidate(
            ref=work.ref,
            memory=memory,
            anchor=anchor,
            search_text=search_text,
            semantic=0,
            lexical=0,
            importance=0.5,
            occurred_at=max(times),
            document=DocumentStamp(
                ref=work.ref, version=work.version, content_digest=digest, space=work.space
            ),
            dependencies=tuple(dependencies),
            labels=tuple(labels),
        )
        return IndexInput(
            candidate=candidate, input=LocalOnly(texts=(search_text,), labels=tuple(labels))
        )

    def cached(self, value: IndexInput) -> EmbeddingBatch | None:
        space = value.candidate.document.space
        with self._connection() as connection:
            row = connection.execute(
                "SELECT embedding::text AS vector FROM recall_embeddings WHERE content_digest=%s "
                "AND model=%s AND revision=%s AND digest=%s AND dimensions=%s",
                (
                    value.candidate.document.content_digest,
                    space.model,
                    space.revision,
                    space.digest,
                    space.dimensions,
                ),
            ).fetchone()
            if row is None:
                return None
            return EmbeddingBatch(space=space, vectors=(tuple(json.loads(row["vector"])),))

    def publish(
        self, work: IndexWork, *, input: IndexInput, embedding: EmbeddingBatch, now: datetime
    ) -> bool:
        if embedding.space != work.space or len(embedding.vectors) != 1:
            raise ProvisionUnavailable()
        with self._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('recall-state',0))")
            connection.execute(
                "SELECT turn_id FROM memory_notifications WHERE turn_id=ANY(%s) ORDER BY "
                "turn_id FOR UPDATE",
                (sorted(v.turn_id for v in input.candidate.dependencies),),
            ).fetchall()
            current = self._load(connection, work, now)
            if current != input:
                return False
            candidate = input.candidate
            space = work.space
            if candidate.memory is not None:
                payload = candidate_record(candidate.memory)
            else:
                assert candidate.anchor is not None
                payload = json_record(asdict(candidate.anchor))
            connection.execute(
                "INSERT INTO "
                "recall_embeddings(content_digest,model,revision,digest,dimensions,embedding) "
                "VALUES(%s,%s,%s,%s,%s,%s::vector) ON CONFLICT DO NOTHING",
                (
                    candidate.document.content_digest,
                    space.model,
                    space.revision,
                    space.digest,
                    space.dimensions,
                    _vector(embedding),
                ),
            )
            connection.execute(
                "UPDATE recall_documents SET "
                "ready=TRUE,pending=FALSE,not_before=NULL,claim_token=NULL,"
                "lease_until=NULL,search_text=%s,content_digest=%s,model=%s,revision=%s,digest=%s,dimensions=%s,"
                "embedding=%s::vector,payload=%s,dependencies=%s,labels=%s "
                "WHERE owner_id=%s AND kind=%s AND identifier=%s",
                (
                    candidate.search_text,
                    candidate.document.content_digest,
                    space.model,
                    space.revision,
                    space.digest,
                    space.dimensions,
                    _vector(embedding),
                    Jsonb(payload),
                    Jsonb(json_record({"values": [asdict(v) for v in candidate.dependencies]})),
                    Jsonb([asdict(v) for v in candidate.labels]),
                    *_key(work.ref),
                ),
            )
            connection.execute(
                "DELETE FROM recall_document_sources WHERE owner_id=%s AND kind=%s AND "
                "identifier=%s",
                _key(work.ref),
            )
            for stamp in candidate.dependencies:
                connection.execute(
                    "INSERT INTO recall_document_sources(owner_id,kind,identifier,turn_id) "
                    "VALUES(%s,%s,%s,%s)",
                    (*_key(work.ref), stamp.turn_id),
                )
            return True

    def retry(self, work: IndexWork, *, not_before: datetime) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE recall_documents SET claim_token=NULL,lease_until=NULL,not_before=%s "
                "WHERE owner_id=%s AND kind=%s AND identifier=%s AND claim_token=%s",
                (not_before, *_key(work.ref), work.token),
            )

    def _restore(self, row: DictRow) -> RecallCandidate:
        ref = CandidateRef(
            owner_id=row["owner_id"],
            memory_id=row["identifier"] if row["kind"] == "memory" else None,
            anchor_id=row["identifier"] if row["kind"] == "anchor" else None,
        )
        memory = (
            restore_memory(row["payload"], tuple(row["payload"]["source_turn_ids"]))
            if ref.memory_id
            else None
        )
        anchor = None
        if ref.anchor_id:
            payload = row["payload"]
            anchor = VerbatimAnchor(
                **(payload | {"registered_at": datetime.fromisoformat(payload["registered_at"])})
            )
        return RecallCandidate(
            ref=ref,
            memory=memory,
            anchor=anchor,
            search_text=row["search_text"],
            semantic=float(row["semantic"]),
            lexical=float(row["lexical"]),
            importance=0.5,
            occurred_at=row["occurred_at"],
            document=DocumentStamp(
                ref=ref,
                version=row["version"],
                content_digest=row["content_digest"],
                space=VectorSpace(
                    model=row["model"],
                    revision=row["revision"],
                    digest=row["digest"],
                    dimensions=row["dimensions"],
                ),
            ),
            dependencies=tuple(restore_stamp(v) for v in row["dependencies"]["values"]),
            labels=tuple(InputLabel(**v) for v in row["labels"]),
        )

    def search(
        self,
        *,
        character_id: str,
        query: ModelInput,
        query_vector: EmbeddingBatch,
        now: datetime,
        limit: int,
    ) -> tuple[RecallCandidate, ...]:
        space = query_vector.space
        if space.dimensions != 1024:
            return ()
        vector = _vector(query_vector)
        with self._connection() as connection:
            rows = connection.execute(
                "WITH eligible AS NOT MATERIALIZED (SELECT d.*, (SELECT max(t.occurred_at) FROM "
                "recall_document_sources s JOIN turns t USING(turn_id) WHERE s.owner_id=d.owner_id "
                "AND s.kind=d.kind AND s.identifier=d.identifier "
                "AND (d.kind='anchor' OR EXISTS(SELECT 1 FROM memory_sources ms "
                "WHERE ms.character_id=s.owner_id AND ms.memory_id=s.identifier "
                "AND ms.turn_id=s.turn_id))) occurred_at FROM "
                "recall_documents d "
                "LEFT JOIN derived_memories m ON d.kind='memory' AND "
                "m.character_id=d.owner_id AND m.id=d.identifier "
                "WHERE d.ready AND d.model=%s AND d.revision=%s AND d.digest=%s AND "
                "d.dimensions=%s "
                "AND (d.owner_id=%s OR (m.kind IN ('fact','preference') AND "
                "%s=ANY(m.shared_with))) "
                "AND (d.kind='anchor' OR (m.search_eligible AND m.valid_from<=%s "
                "AND (m.valid_to IS NULL OR %s<m.valid_to) AND (m.superseded_at IS NULL OR "
                "%s<m.superseded_at))) "
                "AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(d.dependencies->'values') dep "
                "JOIN turns t ON t.turn_id=dep->>'turn_id' JOIN sessions sess USING(session_id) "
                "WHERE NOT %s=ANY(t.perceived_by) OR (t.kind='thought' AND t.actor<>%s) "
                "OR (dep->>'used_end')::int > CASE WHEN NOT sess.reports_delivery OR t.actor=%s "
                "OR t.kind<>'utterance' OR NOT EXISTS(SELECT 1 FROM core_identities c "
                "WHERE c.character_id=t.actor) "
                "THEN char_length(t.text) ELSE COALESCE((SELECT spoken_until FROM "
                "delivery_receipts r "
                "WHERE r.turn_id=t.turn_id ORDER BY receipt_id DESC LIMIT 1), "
                "CASE WHEN %s>=t.recorded_at+%s THEN char_length(t.text) ELSE 0 END) END)), "
                "semantic AS (SELECT owner_id,kind,identifier FROM eligible ORDER BY "
                "embedding <=> %s::vector LIMIT %s), "
                "lexical AS (SELECT owner_id,kind,identifier FROM eligible WHERE "
                "search_text LIKE likequery(%s) "
                "ORDER BY bigm_similarity(search_text,%s) DESC LIMIT %s), "
                "candidates AS (SELECT * FROM semantic UNION SELECT * FROM lexical) "
                "SELECT e.*,1-(e.embedding <=> %s::vector) "
                "semantic,bigm_similarity(e.search_text,%s) lexical "
                "FROM eligible e JOIN candidates USING(owner_id,kind,identifier)",
                (
                    space.model,
                    space.revision,
                    space.digest,
                    space.dimensions,
                    character_id,
                    character_id,
                    now,
                    now,
                    now,
                    character_id,
                    character_id,
                    character_id,
                    now,
                    self.timeout,
                    vector,
                    limit,
                    query.texts[0],
                    query.texts[0],
                    limit,
                    vector,
                    query.texts[0],
                ),
            ).fetchall()
            return tuple(self._restore(row) for row in rows)

    def anchors_for(
        self, *, character_id: str, turn_ids: tuple[str, ...], now: datetime
    ) -> tuple[RecallCandidate, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT d.*,t.occurred_at,0.0 AS semantic,0.0 AS lexical "
                "FROM recall_documents d JOIN verbatim_anchors a "
                "ON a.character_id=d.owner_id AND a.anchor_id=d.identifier JOIN turns t "
                "USING(turn_id) "
                "WHERE d.kind='anchor' AND d.ready AND d.owner_id=%s AND a.turn_id=ANY(%s)",
                (character_id, list(turn_ids)),
            ).fetchall()
            return tuple(self._restore(row) for row in rows)
