"""Rebuild temporal projections from currently valid extraction contributions."""

from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.evidence.ports import EvidenceStore
from digital_souls_memory.evidence.use_cases import Clock
from digital_souls_memory.memory.changes import reinforce, supersede, update_fact
from digital_souls_memory.memory.domain import (
    DerivedMemory,
    ExtractionState,
    MemoryRelation,
    MemoryValidationError,
)
from digital_souls_memory.memory.ports import MemoryStore, MemoryTransaction


def _dependency_order(
    candidates: tuple[DerivedMemory, ...], proposed: tuple[MemoryRelation, ...]
) -> tuple[DerivedMemory, ...]:
    by_id = {v.id: v for v in candidates}
    dependencies = {
        v.id: [r.to_id for r in proposed if r.from_id == v.id and r.to_id in by_id]
        for v in candidates
    }
    ordered: list[DerivedMemory] = []
    done: set[str] = set()
    visiting: set[str] = set()
    for candidate in candidates:
        stack = [(candidate.id, False)]
        while stack:
            identifier, expanded = stack.pop()
            if identifier in done:
                continue
            if expanded:
                visiting.remove(identifier)
                done.add(identifier)
                ordered.append(by_id[identifier])
                continue
            if identifier in visiting:
                raise MemoryValidationError("Cyclic memory dependency")
            visiting.add(identifier)
            stack.append((identifier, True))
            stack.extend((target, False) for target in reversed(dependencies[identifier]))
    return tuple(ordered)


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        evidence: EvidenceStore,
        materials: EvidenceMaterials,
        clock: Clock,
    ) -> None:
        self.store = store
        self.evidence = evidence
        self.materials = materials
        self.clock = clock

    def active(self, character_id: str) -> tuple[DerivedMemory, ...]:
        with self.store.transaction(character_id) as tx:
            values = tx.memories()
            states = {s: tx.get_extraction(s) for v in values for s in v.source_turn_ids}
        return tuple(
            v
            for v in values
            if v.superseded_at is None
            and v.valid_to is None
            and all(self._source_current(character_id, s, states[s]) for s in v.source_turn_ids)
        )

    def _source_current(
        self, character_id: str, source_id: str, state: ExtractionState | None
    ) -> bool:
        decision = self.materials.get_decision(character_id, source_id)
        if decision is None or not decision.text:
            return False
        if state is None:
            return True
        return (
            state.status == "done"
            and state.used_text == decision.text
            and state.delivery_basis == decision.basis
            and state.deadline == decision.deadline
        )

    def rebuild(self, tx: MemoryTransaction) -> None:
        existing = {v.id: v for v in tx.memories()}
        generated = tx.generated_ids()
        result = {v.id: v for v in tx.baselines()}
        candidates = tx.candidates()
        proposed = tx.proposed_relations()
        known = set(result) | {v.id for v in candidates} | generated
        if any(r.to_id not in known for r in proposed):
            raise MemoryValidationError("Relation target is absent or outside active evidence")
        relations: list[MemoryRelation] = []
        for candidate in _dependency_order(candidates, proposed):
            if any(
                not self.materials.get_text(candidate.character_id, i)
                for i in candidate.source_turn_ids
            ):
                continue
            explicit = [r for r in proposed if r.from_id == candidate.id]
            if any(
                r.to_id not in result or result[r.to_id].superseded_at is not None for r in explicit
            ):
                continue
            changes = [r for r in explicit if r.type in ("updates", "extends")]
            if len(changes) > 1:
                raise MemoryValidationError("Ambiguous state change")
            targeted = next(iter(changes), None)
            if targeted is not None:
                old = result[targeted.to_id]
                if old.superseded_at is not None or old.valid_to is not None:
                    raise MemoryValidationError("Cannot change inactive memory")
            else:
                old = None
            if not explicit:
                old = next(
                    (
                        v
                        for v in reversed(tuple(result.values()))
                        if v.superseded_at is None
                        and v.valid_to is None
                        and v.kind == candidate.kind == "fact"
                        and (v.subject, v.predicate, v.object)
                        == (candidate.subject, candidate.predicate, candidate.object)
                    ),
                    None,
                )
            if old is not None and (targeted is None or targeted.type == "extends"):
                value = old
                for source_id in candidate.source_turn_ids:
                    source = self.evidence.get_turn(source_id)
                    if source is None:
                        raise MemoryValidationError("Missing original")
                    value = reinforce(value, source, relation_type="same_fact")
                result[old.id] = value
                # Keep the candidate as history, without publishing a duplicate fact.
                result[candidate.id] = supersede(candidate, at=self.clock())
                relations.append(MemoryRelation(from_id=candidate.id, to_id=old.id, type="extends"))
            elif old is not None:
                closed, added = update_fact(old, candidate)
                result[old.id] = closed
                result[added.id] = added
                relations.append(MemoryRelation(from_id=added.id, to_id=old.id, type="updates"))
            else:
                result[candidate.id] = candidate
            for relation in explicit:
                if relation.type in ("derives", "contradicts"):
                    relations.append(relation)
                if relation.type == "contradicts":
                    result[relation.to_id] = supersede(result[relation.to_id], at=self.clock())
        tx.clear_projection_relations()
        for identifier, value in existing.items():
            if identifier in generated and identifier not in result:
                result[identifier] = supersede(value, at=self.clock())
        for identifier, value in result.items():
            if identifier in existing:
                tx.replace_projection(value)
            else:
                tx.add_generated(value)
        for relation in relations:
            tx.add_relation(relation)
