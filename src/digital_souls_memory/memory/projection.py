"""Rebuild temporal projections from currently valid extraction contributions."""

from dataclasses import dataclass, replace
from datetime import datetime

from digital_souls_memory.evidence.domain import Turn
from digital_souls_memory.memory.changes import apply_temporal_effect, supersede, update_fact
from digital_souls_memory.memory.domain import (
    DerivedMemory,
    ExtractionState,
    InputDependency,
    MemoryRelation,
    MemoryValidationError,
)
from digital_souls_memory.memory.evidence import independent_evidence_count
from digital_souls_memory.memory.inputs import MemoryInputs, ResolvedInput
from digital_souls_memory.memory.ports import MemoryContribution, MemoryTransaction
from digital_souls_memory.memory.relations import resolve_relations


def _resolve_absorbed_relations(
    relations: tuple[MemoryRelation, ...], representatives: dict[str, str]
) -> tuple[MemoryRelation, ...]:
    return tuple(replace(r, to_id=representatives.get(r.to_id, r.to_id)) for r in relations)


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
    applied: tuple[MemoryRelation, ...]
    baseline_dependencies: dict[str, tuple[InputDependency, ...]]


def projection_input(
    tx: MemoryTransaction, identifiers: frozenset[str] | None = None
) -> ProjectionInput:
    contributions = tx.candidates(identifiers)
    origins = {v.turn_id for v in contributions}
    proposed = tx.proposed_relations(identifiers)
    applied = tx.applied_relations(identifiers)
    history = tx.history(frozenset(r.to_id for r in (*proposed, *applied)))
    current = tx.candidates_for_turns(frozenset(v.turn_id for v in history))
    proposed = resolve_relations(proposed, history, current)
    applied = resolve_relations(applied, history, current)
    reference_ids = (
        frozenset(r.to_id for r in proposed) - identifiers
        if identifiers is not None
        else frozenset()
    )
    references = tx.memories(reference_ids) if reference_ids else ()
    withdrawn = tx.withdrawn_ids(reference_ids) if reference_ids else frozenset()
    baselines = tx.baselines(identifiers)
    return ProjectionInput(
        tx.memories(identifiers),
        baselines,
        contributions,
        tx.extraction_states(frozenset(origins)),
        tx.notification_versions(frozenset(origins)),
        proposed,
        tx.generated_ids(identifiers),
        references,
        withdrawn,
        applied,
        tx.baseline_dependencies(frozenset(v.id for v in (*baselines, *references))),
    )


def source_ids(snapshot: ProjectionInput) -> frozenset[str]:
    return (
        frozenset(
            identifier
            for value in (
                *snapshot.existing,
                *snapshot.baselines,
                *snapshot.references,
                *(c.memory for c in snapshot.contributions),
            )
            for identifier in value.source_turn_ids
        )
        | frozenset(c.turn_id for c in snapshot.contributions)
        | frozenset(
            d.turn_id
            for dependencies in snapshot.baseline_dependencies.values()
            for d in dependencies
        )
    )


def collect_evidence(
    snapshot: ProjectionInput, character_id: str, inputs: MemoryInputs
) -> "ProjectionEvidence":
    identifiers = source_ids(snapshot)
    turns = {
        identifier: value
        for identifier in identifiers
        if (value := inputs.evidence.get_turn(identifier)) is not None
    }
    return ProjectionEvidence(
        turns,
        {
            identifier: inputs.materials.get_text(character_id, identifier)
            for identifier in identifiers
        },
        {c.turn_id: inputs.resolve(character_id, (c.turn_id,)) for c in snapshot.contributions},
    )


@dataclass(frozen=True)
class ProjectionEvidence:
    turns: dict[str, Turn]
    texts: dict[str, str | None]
    inputs: dict[str, ResolvedInput]

    def _source_turns(self, identifiers: tuple[str, ...]) -> tuple[Turn, ...]:
        turns = tuple(self.turns.get(identifier) for identifier in identifiers)
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

    def project(
        self, snapshot: ProjectionInput, now: datetime
    ) -> tuple[dict[str, DerivedMemory], list[MemoryRelation]]:
        existing = {v.id: v for v in snapshot.existing}
        generated = snapshot.generated
        invalid_baselines = {
            identifier
            for identifier, dependencies in snapshot.baseline_dependencies.items()
            if any(
                (text := self.texts.get(dependency.turn_id)) is None
                or dependency.used_end > len(text)
                for dependency in dependencies
            )
        }
        result = {
            v.id: self._recount(v)
            for v in snapshot.baselines
            if v.id not in invalid_baselines and all(self.texts.get(i) for i in v.source_turn_ids)
        }
        result.update(
            (v.id, v)
            for v in snapshot.references
            if v.id not in snapshot.withdrawn_references
            and v.id not in invalid_baselines
            and all(self.texts.get(i) for i in v.source_turn_ids)
        )
        candidates = tuple(
            v.memory
            for v in sorted(
                (c for c in snapshot.contributions if c.turn_id in self.turns),
                key=lambda c: (
                    c.memory.valid_from,
                    self.turns[c.turn_id].occurred_at,
                    self.turns[c.turn_id].recorded_at,
                    c.turn_id,
                ),
            )
        )
        current_ids: set[str] = set()
        for contribution in snapshot.contributions:
            state = snapshot.states[contribution.turn_id]
            resolved = self.inputs[contribution.turn_id]
            if (
                state is not None
                and state.status == "done"
                and state.signature == resolved.signature(snapshot.versions[contribution.turn_id])
            ):
                current_ids.add(contribution.memory.id)
        proposed = snapshot.proposed
        known = (
            set(result)
            | {v.memory.id for v in snapshot.contributions}
            | generated
            | {v.id for v in snapshot.baselines}
            | {v.id for v in snapshot.references}
        )
        if any(r.to_id not in known for r in proposed):
            raise MemoryValidationError("Relation target is absent or outside active evidence")
        relations: list[MemoryRelation] = []
        representatives: dict[str, str] = {}
        for candidate in _dependency_order(candidates, proposed):
            if candidate.id not in current_ids:
                continue
            if any(not self.texts.get(i) for i in candidate.source_turn_ids):
                continue
            candidate = self._recount(candidate)
            explicit = _resolve_absorbed_relations(
                tuple(r for r in proposed if r.from_id == candidate.id), representatives
            )
            applied = _resolve_absorbed_relations(snapshot.applied, representatives)
            if any(
                r.to_id not in result
                or (
                    result[r.to_id].superseded_at is not None
                    and not (r.type == "contradicts" and r in applied)
                )
                for r in explicit
            ):
                continue
            changes = [r for r in explicit if r.type in ("updates", "extends")]
            if len(changes) > 1:
                raise MemoryValidationError("Ambiguous state change")
            targeted = next(iter(changes), None)
            if targeted is not None:
                old = result[targeted.to_id]
                if old.superseded_at is not None or (
                    old.valid_to is not None
                    and not (
                        targeted.type == "updates"
                        and targeted in applied
                        and old.valid_to == candidate.valid_from
                    )
                ):
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
                value = apply_temporal_effect(
                    old,
                    candidate,
                    relation_type="extends",
                    source_turns=self._source_turns(
                        tuple(dict.fromkeys((*old.source_turn_ids, *candidate.source_turn_ids)))
                    ),
                    at=now,
                )
                result[old.id] = value
                # Keep the candidate as history, without publishing a duplicate fact.
                result[candidate.id] = supersede(candidate, at=now)
                if old.kind == candidate.kind == "fact" and (
                    old.subject,
                    old.predicate,
                    old.object,
                ) == (candidate.subject, candidate.predicate, candidate.object):
                    representatives[candidate.id] = old.id
                relations.append(MemoryRelation(from_id=candidate.id, to_id=old.id, type="extends"))
            elif old is not None:
                if targeted in applied:
                    closed = apply_temporal_effect(
                        old, candidate, relation_type="updates", source_turns=(), at=now
                    )
                    added = candidate
                else:
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
                    result[relation.to_id] = apply_temporal_effect(
                        result[relation.to_id],
                        candidate,
                        relation_type="contradicts",
                        source_turns=(),
                        at=now,
                    )
        for identifier, value in existing.items():
            if (
                identifier in generated or identifier in invalid_baselines
            ) and identifier not in result:
                result[identifier] = supersede(value, at=now)
        for identifier, value in tuple(result.items()):
            previous = existing.get(identifier)
            if previous is not None and previous.superseded_at is not None and value.superseded_at:
                result[identifier] = replace(
                    value, superseded_at=min(previous.superseded_at, value.superseded_at)
                )
        for reference in snapshot.references:
            result.pop(reference.id, None)
        return result, relations
