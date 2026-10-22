"""Rebuild temporal projections from currently valid extraction contributions."""

from dataclasses import dataclass, replace

from digital_souls_memory.evidence.domain import Turn
from digital_souls_memory.evidence.use_cases import Clock
from digital_souls_memory.memory.changes import reinforce, supersede, update_fact
from digital_souls_memory.memory.domain import (
    DerivedMemory,
    ExtractionState,
    MemoryRelation,
    MemoryValidationError,
)
from digital_souls_memory.memory.evidence import independent_evidence_count
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.ports import MemoryContribution, MemoryStore, MemoryTransaction


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


@dataclass(frozen=True)
class ProjectionInput:
    existing: tuple[DerivedMemory, ...]
    baselines: tuple[DerivedMemory, ...]
    contributions: tuple[MemoryContribution, ...]
    states: dict[str, ExtractionState | None]
    versions: dict[str, int]
    proposed: tuple[MemoryRelation, ...]
    generated: frozenset[str]
    references: tuple[DerivedMemory, ...]
    withdrawn_references: frozenset[str]


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        inputs: MemoryInputs,
        clock: Clock,
    ) -> None:
        self.store = store
        self.inputs = inputs
        self.evidence = inputs.evidence
        self.materials = inputs.materials
        self.clock = clock

    def active(self, character_id: str) -> tuple[DerivedMemory, ...]:
        with self.store.transaction(character_id) as tx:
            snapshot = self._snapshot(tx)
        result, _ = self._project(snapshot, character_id)
        return tuple(v for v in result.values() if v.superseded_at is None and v.valid_to is None)

    def affected(self, tx: MemoryTransaction, turn_id: str) -> frozenset[str]:
        identifiers = tx.contribution_ids(turn_id)
        while True:
            expanded = (
                identifiers | tx.projection_neighbors(identifiers) if identifiers else identifiers
            )
            if expanded == identifiers:
                return identifiers
            identifiers = expanded

    def _snapshot(
        self, tx: MemoryTransaction, identifiers: frozenset[str] | None = None
    ) -> ProjectionInput:
        contributions = tx.candidates(identifiers)
        origins = {v.turn_id for v in contributions}
        proposed = tx.proposed_relations(identifiers)
        reference_ids = (
            frozenset(r.to_id for r in proposed) - identifiers
            if identifiers is not None
            else frozenset()
        )
        references = tx.memories(reference_ids) if reference_ids else ()
        withdrawn = tx.withdrawn_ids(reference_ids) if reference_ids else frozenset()
        return ProjectionInput(
            tx.memories(identifiers),
            tx.baselines(identifiers),
            contributions,
            {identifier: tx.get_extraction(identifier) for identifier in origins},
            {identifier: tx.notification_version(identifier) for identifier in origins},
            proposed,
            tx.generated_ids(identifiers),
            references,
            withdrawn,
        )

    def _source_turns(self, identifiers: tuple[str, ...]) -> tuple[Turn, ...]:
        turns = tuple(self.evidence.get_turn(identifier) for identifier in identifiers)
        if any(v is None for v in turns):
            raise MemoryValidationError("Missing original")
        return tuple(v for v in turns if v is not None)

    def _recount(self, value: DerivedMemory) -> DerivedMemory:
        return replace(
            value,
            epistemics=replace(
                value.epistemics,
                independent_evidence_count=independent_evidence_count(
                    self._source_turns(value.source_turn_ids)
                ),
            ),
        )

    def _project(
        self, snapshot: ProjectionInput, character_id: str
    ) -> tuple[dict[str, DerivedMemory], list[MemoryRelation]]:
        existing = {v.id: v for v in snapshot.existing}
        generated = snapshot.generated
        result = {
            v.id: self._recount(v)
            for v in snapshot.baselines
            if all(self.materials.get_text(character_id, i) for i in v.source_turn_ids)
        }
        result.update(
            (v.id, v)
            for v in snapshot.references
            if v.id not in snapshot.withdrawn_references
            and all(self.materials.get_text(character_id, i) for i in v.source_turn_ids)
        )
        candidates = tuple(v.memory for v in snapshot.contributions)
        current_ids: set[str] = set()
        for contribution in snapshot.contributions:
            state = snapshot.states[contribution.turn_id]
            resolved = self.inputs.resolve(character_id, (contribution.turn_id,))
            if (
                state is not None
                and state.status == "done"
                and state.signature == resolved.signature(snapshot.versions[contribution.turn_id])
            ):
                current_ids.add(contribution.memory.id)
        proposed = snapshot.proposed
        known = (
            set(result)
            | {v.id for v in candidates}
            | generated
            | {v.id for v in snapshot.baselines}
            | {v.id for v in snapshot.references}
        )
        if any(r.to_id not in known for r in proposed):
            raise MemoryValidationError("Relation target is absent or outside active evidence")
        relations: list[MemoryRelation] = []
        for candidate in _dependency_order(candidates, proposed):
            if candidate.id not in current_ids:
                continue
            if any(
                not self.materials.get_text(candidate.character_id, i)
                for i in candidate.source_turn_ids
            ):
                continue
            candidate = self._recount(candidate)
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
                    value = reinforce(
                        value,
                        source,
                        source_turns=self._source_turns(
                            tuple(dict.fromkeys((*value.source_turn_ids, source_id)))
                        ),
                        relation_type="same_fact",
                    )
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
        for identifier, value in existing.items():
            if identifier in generated and identifier not in result:
                result[identifier] = supersede(value, at=self.clock())
        for identifier, value in tuple(result.items()):
            previous = existing.get(identifier)
            if previous is not None and previous.superseded_at is not None and value.superseded_at:
                result[identifier] = replace(value, superseded_at=previous.superseded_at)
        for reference in snapshot.references:
            result.pop(reference.id, None)
        return result, relations

    def rebuild(self, tx: MemoryTransaction, identifiers: frozenset[str] | None = None) -> None:
        if identifiers == frozenset():
            return
        snapshot = self._snapshot(tx, identifiers)
        owners = {v.character_id for v in snapshot.existing} | {
            v.memory.character_id for v in snapshot.contributions
        }
        if not owners:
            return
        (character_id,) = owners
        result, relations = self._project(snapshot, character_id)
        existing = {v.id: v for v in snapshot.existing}
        withdrawn = tx.withdrawn_ids(frozenset(result))
        for identifier, value in result.items():
            if identifier in existing:
                if value != existing[identifier] or identifier in withdrawn:
                    tx.replace_projection(value)
            else:
                tx.add_generated(value)
        tx.sync_projection_relations(snapshot.generated, tuple(relations))
