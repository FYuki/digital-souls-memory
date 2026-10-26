"""IO substitutes for generated sequences; no expected ledger values enter these ports."""

from dataclasses import replace
from datetime import datetime

from digital_souls_memory.evidence.materials import aggregate_receipts
from digital_souls_memory.governance.domain import (
    AuthorPrincipal,
    DeletionAttempt,
    DeletionCommand,
    DeletionRequest,
    MemoryCopy,
    MemoryReplacement,
    TemporalEffect,
)
from digital_souls_memory.memory.domain import DerivedMemory, memory_valid_at
from digital_souls_memory.memory.extraction import LocalOnly as MemoryInput
from digital_souls_memory.memory.projection import (
    collect_evidence,
    projection_input,
    save_projection,
)
from digital_souls_memory.memory.relations import resolve_relations
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.recall.domain import CandidateMaterial, EmbeddingBatch, VectorSpace
from digital_souls_memory.recall.indexing import IndexInput, IndexWork
from digital_souls_memory.recall.inputs import LocalOnly
from tests.deletion_support import SharedDeletionStore
from tests.memory_fakes import FakeMemoryStore
from tests.postgres.test_restatement_remediation import Recalculate
from tests.recall_fixtures import candidate_for


class SequenceRecalculate(Recalculate):
    outcome = "preserve"

    def recalculate_local(
        self, value: MemoryInput, *, memory_id: str, kind: str
    ) -> DerivedMemory | None:
        if self.outcome == "fail":
            self.calls.append((value, memory_id, kind))
            raise RuntimeError("Synthetic recalculation failure")
        if self.outcome == "unavailable":
            self.calls.append((value, memory_id, kind))
            return None
        assert self.outcome == "preserve"
        return super().recalculate_local(value, memory_id=memory_id, kind=kind)


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
        withdrawn = targets | {
            origin
            for (owner, origin), dependencies in self.memories.dependencies.items()
            if owner == "char-b" and any(d.turn_id in targets for d in dependencies)
        }
        copies = []
        with self.memories.transaction("char-b") as tx:
            affected_ids = set().union(*(tx.contribution_ids(origin) for origin in withdrawn))
            affected_ids.update(copy.memory.id for copy in attempt.snapshot.copies)
            while True:
                expanded = affected_ids | {
                    r.to_id
                    for r in tx.applied_relations()
                    if r.from_id in affected_ids and r.type != "derives"
                }
                if expanded == affected_ids:
                    break
                affected_ids = expanded
            stored_copies = {
                c.memory.id: c for c in attempt.snapshot.copies if c.table == "derived_memories"
            }
            stored_copies.update(
                {
                    v.id: MemoryCopy(table="derived_memories", memory=v)
                    for v in tx.memories()
                    if v.id in affected_ids and v.id not in stored_copies
                }
            )
            candidates = {v.memory.id: v.memory for v in tx.history(frozenset(stored_copies))}
            candidates.update((v.id, v) for v in tx.baselines())
            current_ids = {
                v.id
                for (owner, _), values in self.memories.proposals.items()
                if owner == "char-b"
                for v in values
            }
            effects = tx.relation_effects()
            for copy in stored_copies.values():
                base = self.memories.temporal_bases.get(("char-b", copy.memory.id))
                if base is None:
                    base = candidates[copy.memory.id]
                    if copy.memory.id not in current_ids:
                        base = replace(base, superseded_at=copy.memory.superseded_at)
                temporal = [
                    TemporalEffect(
                        candidate=e.candidate, relation_type=e.relation.type, applied_at=now
                    )
                    for e in effects
                    if e.turn_id not in withdrawn
                    and e.relation.to_id == copy.memory.id
                    and e.relation.type != "derives"
                ]
                for candidate in tx.candidates():
                    if candidate.turn_id in withdrawn:
                        continue
                    for relation in self.memories.proposed.get(("char-b", candidate.turn_id), ()):
                        history = tx.history(frozenset({relation.from_id, relation.to_id}))
                        current = tx.candidates_for_turns(
                            frozenset(c.turn_id for c in history),
                            history_ids=frozenset(c.memory.id for c in history),
                        )
                        (relation,) = resolve_relations((relation,), history, current)
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
                        body_source_ids=self.memories.body_sources.get(
                            ("char-b", base.id), base.source_turn_ids
                        ),
                        withdrawn_origins=tuple(
                            origin
                            for (owner, origin), dependencies in self.memories.dependencies.items()
                            if owner == "char-b" and any(d.turn_id in targets for d in dependencies)
                        ),
                        reinforcements=tx.source_contributions(frozenset({copy.memory.id})),
                    )
                )
        materials = []
        for identifier in sorted({s for c in copies for s in c.memory.source_turn_ids} - targets):
            source = self.evidence.get_turn(identifier)
            assert source is not None
            session = self.evidence.get_session(source.session_id)
            assert session is not None
            with self.evidence.list_receipts(identifier) as receipts:
                materials.append(
                    CandidateMaterial(
                        turn=source,
                        session=session,
                        actor_is_character=source.actor.startswith("char-"),
                        receipt_state=aggregate_receipts(receipts),
                    )
                )
        return replace(
            attempt,
            snapshot=replace(attempt.snapshot, copies=tuple(copies), materials=tuple(materials)),
        )

    def commit(
        self,
        attempt: DeletionAttempt,
        *,
        replacements: tuple[MemoryReplacement, ...],
        now: datetime,
    ) -> DeletionRequest:
        targets = set(attempt.request.target_turn_ids)
        withdrawn = targets | {
            origin
            for (owner, origin), dependencies in self.memories.dependencies.items()
            if owner == "char-b" and any(d.turn_id in targets for d in dependencies)
        }
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
        removed = {r for o, r in owned if o in withdrawn} - {
            r for o, r in owned if o not in withdrawn
        }
        result = super().commit(attempt, replacements=replacements, now=now)
        invalidated = {r.copy.memory.id for r in replacements if r.value is None}
        for replacement in replacements:
            if replacement.value is not None:
                base = replacement.copy.temporal_base
                assert base is not None
                key = ("char-b", base.id)
                self.memories.body_sources[key] = tuple(
                    s
                    for s in replacement.copy.body_source_ids
                    if s in replacement.value.source_turn_ids and s not in targets
                )
                self.memories.temporal_bases[key] = base
            else:
                key = ("char-b", replacement.copy.memory.id)
                self.memories.body_sources.pop(key, None)
                self.memories.temporal_bases.pop(key, None)
        for mapping in (
            self.memories.proposals,
            self.memories.proposed,
            self.memories.effects,
            self.memories.reinforcements,
            self.memories.states,
            self.memories.dependencies,
        ):
            for key in tuple(mapping):
                if key[1] in withdrawn:
                    del mapping[key]
        self.memories.proposed = {
            key: tuple(
                r for r in relations if r.from_id not in invalidated and r.to_id not in invalidated
            )
            for key, relations in self.memories.proposed.items()
        }
        self.memories.effects = {
            key: tuple(
                e
                for e in effects
                if e.relation.from_id not in invalidated and e.relation.to_id not in invalidated
            )
            for key, effects in self.memories.effects.items()
        }
        self.memories.proposals = {
            key: tuple(v for v in values if v.id not in invalidated)
            for key, values in self.memories.proposals.items()
        }
        self.memories.reinforcements = {
            key: tuple(c for c in values if c.fact_id not in invalidated)
            for key, values in self.memories.reinforcements.items()
        }
        self.memories.relations = [
            (o, r)
            for o, r in self.memories.relations
            if r not in removed and r.from_id not in invalidated and r.to_id not in invalidated
        ]
        self.memories.history = {
            k: v
            for k, v in self.memories.history.items()
            if v.turn_id not in targets and v.memory.id not in invalidated
        }
        self.memories.generated["char-b"] = {v.memory.id for v in self.memories.history.values()}
        with self.memories.transaction("char-b") as tx:
            snapshot = projection_input(tx)
            projected = collect_evidence(snapshot, "char-b", self.service.inputs).project(
                snapshot, now
            )
            save_projection(tx, snapshot, projected)
            for replacement in replacements:
                identifier = replacement.copy.memory.id
                if replacement.value is not None and identifier in projected.memories:
                    self.memories.originals[("char-b", identifier)] = projected.memories[identifier]
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
            if v.id not in self.published
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
