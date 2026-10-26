"""Rebuild temporal projections from currently valid extraction contributions."""

from dataclasses import dataclass, replace
from datetime import datetime
from graphlib import CycleError, TopologicalSorter
from heapq import heappop, heappush

from digital_souls_memory.evidence.domain import Turn
from digital_souls_memory.memory.changes import apply_temporal_effect, supersede, update_fact
from digital_souls_memory.memory.domain import (
    DerivedMemory,
    ExtractionState,
    InputDependency,
    MemoryRelation,
    MemoryValidationError,
    RelationEffect,
    SourceContribution,
)
from digital_souls_memory.memory.evidence import independent_evidence_count
from digital_souls_memory.memory.inputs import MemoryInputs, ResolvedInput
from digital_souls_memory.memory.ports import MemoryContribution, MemoryTransaction
from digital_souls_memory.memory.reinforcement import aggregate_sources
from digital_souls_memory.memory.relations import (
    dependency_order,
    equivalent_ids,
    resolve_relations,
)


@dataclass(frozen=True)
class ProjectionResult:
    memories: dict[str, DerivedMemory]
    relations: tuple[MemoryRelation, ...]
    projected_ids: frozenset[str]


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
    temporal_origins: dict[str, dict[str, datetime | None]]
    reinforcements: tuple[SourceContribution, ...]
    effects: tuple[RelationEffect, ...]
    body_sources: dict[str, tuple[str, ...]]
    owned_relations: tuple[MemoryRelation, ...]
    retired_bodies: dict[str, DerivedMemory]


def projection_input(
    tx: MemoryTransaction, identifiers: frozenset[str] | None = None
) -> ProjectionInput:
    contributions = tx.candidates(identifiers)
    reinforcements = tx.source_contributions(identifiers)
    effects = tx.relation_effects(identifiers)
    origins = (
        {v.turn_id for v in contributions}
        | {v.extraction_turn_id for v in reinforcements}
        | {v.turn_id for v in effects}
    )
    proposed = tx.proposed_relations(identifiers)
    applied = tx.applied_relations(identifiers)
    existing = tx.memories(identifiers)
    history = tx.history(
        frozenset(i for r in (*proposed, *applied) for i in (r.from_id, r.to_id))
        | frozenset(v.id for v in existing)
    )
    current = tx.candidates_for_turns(
        frozenset(v.turn_id for v in history), history_ids=frozenset(v.memory.id for v in history)
    )
    replacements = equivalent_ids(history, current)
    proposed = resolve_relations(proposed, history, current)
    applied = resolve_relations(applied, history, current)
    reference_ids = (
        frozenset(r.to_id for r in (*proposed, *(e.relation for e in effects))) - identifiers
        if identifiers is not None
        else frozenset()
    )
    references = tx.memories(reference_ids) if reference_ids else ()
    withdrawn = tx.withdrawn_ids(reference_ids) if reference_ids else frozenset()
    baselines = tx.baselines(identifiers)
    return ProjectionInput(
        existing,
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
        tx.temporal_origins(frozenset(v.id for v in baselines)),
        reinforcements,
        effects,
        tx.fact_sources(frozenset(v.id for v in baselines)),
        tx.proposed_relations(identifiers, include_inactive=True),
        {
            c.memory.id: c.memory
            for c in history
            if c.memory.id in replacements and c.memory.id not in {v.id for v in baselines}
        },
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
        | frozenset(c.source_turn_id for c in snapshot.reinforcements)
        | extraction_origins(snapshot)
        | frozenset(
            d.turn_id
            for dependencies in snapshot.baseline_dependencies.values()
            for d in dependencies
        )
    )


def extraction_origins(snapshot: ProjectionInput) -> frozenset[str]:
    return (
        frozenset(c.turn_id for c in snapshot.contributions)
        | frozenset(c.extraction_turn_id for c in snapshot.reinforcements)
        | frozenset(e.turn_id for e in snapshot.effects)
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
        {
            origin: inputs.resolve(character_id, (origin,))
            for origin in extraction_origins(snapshot)
        },
    )


@dataclass(frozen=True)
class ProjectionEvidence:
    turns: dict[str, Turn]
    texts: dict[str, str | None]
    inputs: dict[str, ResolvedInput]
    validated_origins: frozenset[str] | None = None

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

    def project(self, snapshot: ProjectionInput, now: datetime) -> ProjectionResult:
        existing = {v.id: v for v in snapshot.existing}
        generated = snapshot.generated
        current_origins = (
            set(self.validated_origins)
            if self.validated_origins is not None
            else {
                origin
                for origin in extraction_origins(snapshot)
                if (state := snapshot.states[origin]) is not None
                and state.status == "done"
                and state.signature == self.inputs[origin].signature(snapshot.versions[origin])
            }
        )
        migrated_origins = {
            c.extraction_turn_id
            for c in snapshot.reinforcements
            if snapshot.states[c.extraction_turn_id] is None
        }
        current_ids: set[str] = set()
        for contribution in snapshot.contributions:
            if contribution.turn_id in current_origins:
                current_ids.add(contribution.memory.id)
        current_origins |= migrated_origins
        invalid_baselines = {
            identifier
            for identifier, dependencies in snapshot.baseline_dependencies.items()
            if any(
                (text := self.texts.get(dependency.turn_id)) is None
                or dependency.used_end > len(text)
                for dependency in dependencies
            )
        }
        baseline_sources = {
            v.id: tuple(
                dict.fromkeys(
                    (
                        *snapshot.body_sources[v.id],
                        *(
                            c.source_turn_id
                            for c in snapshot.reinforcements
                            if c.fact_id == v.id
                            and c.extraction_turn_id in current_origins
                            and self.texts.get(c.source_turn_id)
                        ),
                    )
                )
            )
            for v in snapshot.baselines
        }
        result = {
            v.id: self._recount(
                replace(
                    v,
                    source_turn_ids=baseline_sources[v.id],
                    valid_to=snapshot.temporal_origins[v.id]["valid_to"],
                    superseded_at=snapshot.temporal_origins[v.id]["superseded_at"],
                    last_reinforced_at=snapshot.temporal_origins[v.id]["last_reinforced_at"],
                )
            )
            for v in snapshot.baselines
            if v.id not in invalid_baselines
            and baseline_sources[v.id]
            and all(self.texts.get(i) for i in baseline_sources[v.id])
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
        relations: list[MemoryRelation] = [
            r
            for r in snapshot.applied
            if r.from_id in {v.id for v in snapshot.baselines}
            and r.from_id not in {c.memory.id for c in snapshot.contributions}
            and r.from_id in result
            and r.to_id in result
            and r not in snapshot.owned_relations
            and not any(e.relation == r for e in snapshot.effects)
        ]
        baseline_ids = {v.id for v in snapshot.baselines}
        for item in _projection_order(candidates, proposed, snapshot.effects):
            if isinstance(item, RelationEffect):
                effect = item
                relation = effect.relation
                if relation.to_id not in known:
                    raise MemoryValidationError(
                        "Relation target is absent or outside active evidence"
                    )
                if (
                    effect.turn_id not in current_origins
                    or relation.from_id not in result
                    or relation.to_id not in result
                    or any(not self.texts.get(i) for i in effect.candidate.source_turn_ids)
                    or (
                        result[relation.to_id].superseded_at is not None
                        and not (relation.type == "contradicts" and relation in snapshot.applied)
                    )
                ):
                    continue
                effect_changes = [
                    e
                    for e in snapshot.effects
                    if e.turn_id == effect.turn_id
                    and e.candidate.id == effect.candidate.id
                    and e.relation.type in ("updates", "extends")
                ]
                if len(effect_changes) > 1:
                    raise MemoryValidationError("Ambiguous state change")
                effect_applied = (*snapshot.applied, *relations)
                if relation.type in ("updates", "extends"):
                    _validate_change_target(
                        result[relation.to_id], effect.candidate, relation, effect_applied
                    )
                if relation.type == "updates" and relation not in effect_applied:
                    update_fact(result[relation.to_id], effect.candidate)
                if relation.type in ("updates", "contradicts", "extends"):
                    result[relation.to_id] = apply_temporal_effect(
                        result[relation.to_id],
                        effect.candidate,
                        relation_type=relation.type,
                        source_turns=self._source_turns(
                            tuple(
                                dict.fromkeys(
                                    (
                                        *result[relation.to_id].source_turn_ids,
                                        *effect.candidate.source_turn_ids,
                                    )
                                )
                            )
                        ),
                        at=now,
                    )
                if relation not in relations:
                    relations.append(relation)
                continue
            candidate = item
            if candidate.id in baseline_ids:
                # Recalculation owns the value; its surviving relations still carry effects.
                if candidate.id not in result:
                    continue
                candidate = result[candidate.id]
            if candidate.id not in current_ids:
                continue
            if any(not self.texts.get(i) for i in candidate.source_turn_ids):
                continue
            candidate = self._recount(candidate)
            explicit = tuple(r for r in proposed if r.from_id == candidate.id)
            applied = snapshot.applied
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
                _validate_change_target(old, candidate, targeted, applied)
            else:
                old = None
            if not explicit:
                if candidate.id in baseline_ids:
                    continue
            if old is not None and targeted is not None and targeted.type == "extends":
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
                result[candidate.id] = supersede(candidate, at=now)
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
        for identifier, value in tuple(result.items()):
            supporting = tuple(
                c
                for c in snapshot.reinforcements
                if c.fact_id == identifier
                and c.extraction_turn_id in current_origins
                and self.texts.get(c.source_turn_id)
            )
            if supporting:
                strengthened = aggregate_sources(
                    value, value.source_turn_ids, supporting, self.turns
                )
                assert strengthened is not None
                result[identifier] = strengthened
        projected_ids = frozenset(result)
        for identifier, value in existing.items():
            if (
                identifier in generated
                or identifier in invalid_baselines
                or identifier in baseline_ids
            ) and identifier not in result:
                result[identifier] = supersede(
                    snapshot.retired_bodies.get(identifier, value), at=now
                )
        for identifier, value in tuple(result.items()):
            previous = existing.get(identifier)
            if previous is not None and previous.superseded_at is not None and value.superseded_at:
                result[identifier] = replace(
                    value, superseded_at=min(previous.superseded_at, value.superseded_at)
                )
        for reference in snapshot.references:
            result.pop(reference.id, None)
        return ProjectionResult(result, tuple(relations), projected_ids)


def _projection_order(
    candidates: tuple[DerivedMemory, ...],
    proposed: tuple[MemoryRelation, ...],
    effects: tuple[RelationEffect, ...],
) -> tuple[DerivedMemory | RelationEffect, ...]:
    ordered = dependency_order(candidates, proposed)
    items: tuple[DerivedMemory | RelationEffect, ...] = (*ordered, *effects)
    candidate_nodes = {v.id: i for i, v in enumerate(ordered)}
    dependencies: dict[int, set[int]] = {i: set() for i in range(len(items))}
    for i, item in enumerate(items):
        relations = (
            (item.relation,)
            if isinstance(item, RelationEffect)
            else tuple(r for r in proposed if r.from_id == item.id)
        )
        for relation in relations:
            if relation.to_id in candidate_nodes:
                dependencies[i].add(candidate_nodes[relation.to_id])
            if not isinstance(item, RelationEffect):
                dependencies[i].update(
                    len(ordered) + j
                    for j, effect in enumerate(effects)
                    if effect.relation.to_id == relation.to_id
                    and effect.relation.from_id != item.id
                )
        if isinstance(item, RelationEffect) and item.relation.from_id in candidate_nodes:
            dependencies[i].add(candidate_nodes[item.relation.from_id])
    try:
        sorter = TopologicalSorter(dependencies)
        sorter.prepare()
    except CycleError as error:
        raise MemoryValidationError("Cyclic memory dependency") from error
    ready: list[int] = []
    for i in sorter.get_ready():
        heappush(ready, i)
    result = []
    while ready:
        i = heappop(ready)
        result.append(items[i])
        sorter.done(i)
        for j in sorter.get_ready():
            heappush(ready, j)
    return tuple(result)


def _validate_change_target(
    old: DerivedMemory,
    candidate: DerivedMemory,
    relation: MemoryRelation,
    applied: tuple[MemoryRelation, ...],
) -> None:
    if old.superseded_at is not None or (
        old.valid_to is not None
        and not (
            relation.type == "updates"
            and relation in applied
            and old.valid_to == candidate.valid_from
        )
    ):
        raise MemoryValidationError("Cannot change inactive memory")


def save_projection(
    tx: MemoryTransaction, snapshot: ProjectionInput, result: ProjectionResult
) -> None:
    existing = {v.id: v for v in snapshot.existing}
    withdrawn = tx.withdrawn_ids(frozenset(result.memories))
    for identifier, value in result.memories.items():
        if identifier in existing:
            if value != existing[identifier] or identifier in withdrawn:
                tx.replace_projection(value)
        else:
            tx.add_generated(value)
    tx.sync_projection_relations(
        frozenset(v.id for v in snapshot.existing) | snapshot.generated,
        result.relations,
        result.projected_ids,
    )
