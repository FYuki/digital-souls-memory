"""IO substitutes for generated sequences; no expected ledger values enter these ports."""

from dataclasses import replace
from datetime import datetime

from digital_souls_memory.governance.domain import (
    AuthorPrincipal,
    DeletionAttempt,
    DeletionCommand,
    DeletionRequest,
    MemoryReplacement,
    TemporalEffect,
)
from digital_souls_memory.memory.domain import memory_valid_at
from digital_souls_memory.memory.projection import (
    collect_evidence,
    projection_input,
    save_projection,
)
from digital_souls_memory.memory.relations import resolve_relations
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.recall.domain import EmbeddingBatch, VectorSpace
from digital_souls_memory.recall.indexing import IndexInput, IndexWork
from digital_souls_memory.recall.inputs import LocalOnly
from tests.deletion_support import SharedDeletionStore
from tests.memory_fakes import FakeMemoryStore
from tests.recall_fixtures import candidate_for


class SequenceDeletionStore(SharedDeletionStore):
    """Own extraction rows and baselines as well as the shared projection copies.

    SQL transactions, permissions and bounded projection remain PostgreSQL tests.
    """

    def __init__(self, service: MemoryService, memories: FakeMemoryStore) -> None:
        from tests.recall_support import FakeProvisionStore

        super().__init__(memories.evidence, memories, FakeProvisionStore(lambda _: True))
        self.service = service

    def begin(
        self, command: DeletionCommand, *, principal: AuthorPrincipal, now: datetime
    ) -> DeletionAttempt:
        attempt = super().begin(command, principal=principal, now=now)
        if attempt.snapshot is None:
            return attempt
        targets = set(attempt.request.target_turn_ids)
        copies = []
        with self.memories.transaction("char-b") as tx:
            candidates = {v.memory.id: v.memory for v in tx.candidates()}
            effects = tx.relation_effects()
            for copy in attempt.snapshot.copies:
                if copy.table != "derived_memories":
                    continue
                base = self.memories.originals.get(("char-b", copy.memory.id))
                if base is None:
                    base = candidates[copy.memory.id]
                temporal = [
                    TemporalEffect(
                        candidate=e.candidate, relation_type=e.relation.type, applied_at=now
                    )
                    for e in effects
                    if e.turn_id not in targets
                    and e.relation.to_id == copy.memory.id
                    and e.relation.type != "derives"
                ]
                for candidate in tx.candidates():
                    if candidate.turn_id in targets:
                        continue
                    for relation in self.memories.proposed.get(("char-b", candidate.turn_id), ()):
                        if relation.to_id == copy.memory.id and relation.type != "derives":
                            temporal.append(
                                TemporalEffect(
                                    candidate=candidate.memory,
                                    relation_type=relation.type,
                                    applied_at=now,
                                )
                            )
                copies.append(
                    replace(
                        copy,
                        temporal_base=base,
                        temporal_effects=tuple(temporal),
                        body_source_ids=base.source_turn_ids,
                        reinforcements=tx.source_contributions(frozenset({copy.memory.id})),
                    )
                )
        return replace(attempt, snapshot=replace(attempt.snapshot, copies=tuple(copies)))

    def commit(
        self,
        attempt: DeletionAttempt,
        *,
        replacements: tuple[MemoryReplacement, ...],
        now: datetime,
    ) -> DeletionRequest:
        targets = set(attempt.request.target_turn_ids)
        with self.memories.transaction("char-b") as tx:
            raw = tuple(
                (origin, r)
                for (owner, origin), values in self.memories.proposed.items()
                if owner == "char-b"
                for r in values
            ) + tuple((e.turn_id, e.relation) for e in tx.relation_effects())
            history = tx.history(frozenset(i for _, r in raw for i in (r.from_id, r.to_id)))
            current = tx.candidates_for_turns(
                frozenset(c.turn_id for c in history),
                history_ids=frozenset(c.memory.id for c in history),
            )
            owned = tuple(
                zip(
                    (o for o, _ in raw),
                    resolve_relations(tuple(r for _, r in raw), history, current),
                    strict=True,
                )
            )
        removed = {r for o, r in owned if o in targets} - {r for o, r in owned if o not in targets}
        result = super().commit(attempt, replacements=replacements, now=now)
        for replacement in replacements:
            if replacement.value is not None:
                base = replacement.copy.temporal_base
                assert base is not None
                self.memories.originals[("char-b", base.id)] = replace(
                    base, source_turn_ids=replacement.value.source_turn_ids
                )
        for mapping in (
            self.memories.proposals,
            self.memories.proposed,
            self.memories.effects,
            self.memories.reinforcements,
            self.memories.states,
            self.memories.dependencies,
        ):
            for key in tuple(mapping):
                if key[1] in targets:
                    del mapping[key]
        self.memories.relations = [(o, r) for o, r in self.memories.relations if r not in removed]
        self.memories.history = {
            k: v for k, v in self.memories.history.items() if v.turn_id not in targets
        }
        self.memories.generated["char-b"] = {v.memory.id for v in self.memories.history.values()}
        with self.memories.transaction("char-b") as tx:
            snapshot = projection_input(tx)
            projected = collect_evidence(snapshot, "char-b", self.service.inputs).project(
                snapshot, now
            )
            save_projection(tx, snapshot, projected)
        return result


class SequenceIndexStore:
    def __init__(self, memories: FakeMemoryStore) -> None:
        self.memories = memories
        self.published: dict[str, IndexInput] = {}

    def claim(self, *, space: VectorSpace, now: datetime, limit: int) -> tuple[IndexWork, ...]:
        current = {v.id: v for v in self.memories.values.values() if memory_valid_at(v, now)}
        self.published = {
            i: v for i, v in self.published.items() if v.candidate.memory == current.get(i)
        }
        return tuple(
            IndexWork(
                ref=candidate_for(
                    v, sources=tuple(self.memories.evidence.turns[i] for i in v.source_turn_ids)
                ).ref,
                version=1,
                token="synthetic-index",
                space=space,
            )
            for v in current.values()
        )[:limit]

    def load(self, work: IndexWork, *, now: datetime) -> IndexInput | None:
        assert work.ref.memory_id is not None
        value = self.memories.get_memory(work.ref.owner_id, work.ref.memory_id)
        if value is None or not memory_valid_at(value, now):
            return None
        candidate = candidate_for(
            value, sources=tuple(self.memories.evidence.turns[i] for i in value.source_turn_ids)
        )
        return IndexInput(
            candidate=candidate, input=LocalOnly(texts=(value.content,), labels=candidate.labels)
        )

    def cached(self, value: IndexInput) -> EmbeddingBatch | None:
        return None

    def publish(
        self, work: IndexWork, *, input: IndexInput, embedding: EmbeddingBatch, now: datetime
    ) -> bool:
        assert embedding.space == work.space and input == self.load(work, now=now)
        assert work.ref.memory_id is not None
        self.published[work.ref.memory_id] = input
        return True

    def retry(self, work: IndexWork, *, not_before: datetime) -> None:
        raise AssertionError((work, not_before))
