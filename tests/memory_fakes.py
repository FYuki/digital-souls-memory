"""Transactional Memory port fake with observable committed state."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from threading import Event, RLock
from uuid import NAMESPACE_URL, uuid5

from digital_souls_memory.memory.domain import (
    DerivedMemory,
    ExtractionProposal,
    ExtractionState,
    InputDependency,
    MemoryRelation,
    RelationEffect,
    SourceContribution,
    memory_valid_at,
)
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
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
from digital_souls_memory.memory.projection import (
    ProjectionEvidence,
    ProjectionInput,
    collect_evidence,
)
from digital_souls_memory.memory.relations import equivalent_ids, resolve_relations
from tests.it1.evidence_fakes import FakeEvidenceStore
from tests.memory_support import memory


class EchoGateway:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.release.set()
        self.fail = False
        self.calls: list[str] = []
        self.inputs: list[LocalOnly | CloudInput] = []

    def extract_local(self, value: LocalOnly) -> tuple[DerivedMemory, ...]:
        return self._extract(value)

    def extract_cloud(self, value: CloudInput) -> tuple[DerivedMemory, ...]:
        return self._extract(value)

    def _extract(self, value: LocalOnly | CloudInput) -> tuple[DerivedMemory, ...]:
        self.calls.append(value.character_id)
        self.inputs.append(value)
        self.started.set()
        if not self.release.wait(5):
            raise RuntimeError("Synthetic blocked model was not released")
        if self.fail:
            raise RuntimeError("Synthetic model failed")
        source_ids = tuple(turn.turn_id for turn in value.turns)
        identifier = str(
            uuid5(
                NAMESPACE_URL,
                f"synthetic:{value.character_id}:{source_ids}:{value.turns[0].text}",
            )
        )
        return (
            replace(
                memory(),
                id=identifier,
                character_id=value.character_id,
                content=value.turns[0].text,
                object=value.turns[0].text,
                valid_from=value.turns[0].occurred_at,
                source_turn_ids=source_ids,
                subject=value.turns[0].actor,
            ),
        )


class ExplicitUpdatesGateway:
    """Synthetic tea assertion explicitly replaces the preceding coffee assertion."""

    def __init__(self) -> None:
        self.echo = EchoGateway()
        self.seeds: dict[str, str] = {}

    def extract_local(self, value: LocalOnly) -> ExtractionProposal:
        return self._proposal(value.character_id, self.echo.extract_local(value))

    def extract_cloud(self, value: CloudInput) -> ExtractionProposal:
        return self._proposal(value.character_id, self.echo.extract_cloud(value))

    def _proposal(self, character: str, values: tuple[DerivedMemory, ...]) -> ExtractionProposal:
        (candidate,) = values
        if candidate.object == "coffee":
            self.seeds[character] = candidate.id
        return ExtractionProposal(
            memories=values,
            relations=(
                MemoryRelation(from_id=candidate.id, to_id=self.seeds[character], type="updates"),
            )
            if candidate.object == "tea"
            else (),
        )


class FakeMemoryStore:
    def __init__(self, evidence: FakeEvidenceStore) -> None:
        self.evidence = evidence
        self.values: dict[tuple[str, str], DerivedMemory] = {}
        self.states: dict[tuple[str, str], ExtractionState] = {}
        self.proposals: dict[tuple[str, str], tuple[DerivedMemory, ...]] = {}
        self.generated: dict[str, set[str]] = {}
        self.relations: list[tuple[str, MemoryRelation]] = []
        self.lock = RLock()
        self.fail_commit = False
        self.requests: dict[str, int] = {}
        self.originals: dict[tuple[str, str], DerivedMemory] = {}
        self.proposed: dict[tuple[str, str], tuple[MemoryRelation, ...]] = {}
        self.dependencies: dict[tuple[str, str], tuple[InputDependency, ...]] = {}
        self.baseline_inputs: dict[tuple[str, str], tuple[InputDependency, ...]] = {}
        self.history: dict[tuple[str, str], MemoryContribution] = {}
        self.withdrawn: set[tuple[str, str]] = set()
        self.reinforcements: dict[tuple[str, str], tuple[SourceContribution, ...]] = {}
        self.effects: dict[tuple[str, str], tuple[RelationEffect, ...]] = {}

    @contextmanager
    def transaction(self, character_id: str) -> Iterator[MemoryTransaction]:
        with self.lock:
            snapshot = (
                dict(self.values),
                dict(self.states),
                dict(self.proposals),
                {k: set(v) for k, v in self.generated.items()},
                list(self.relations),
                dict(self.originals),
                dict(self.proposed),
                dict(self.dependencies),
                dict(self.history),
                set(self.withdrawn),
                dict(self.reinforcements),
                dict(self.effects),
                dict(self.requests),
            )
            try:
                yield FakeMemoryTransaction(self, character_id)
                if self.fail_commit:
                    self.fail_commit = False
                    raise MemoryUnavailable()
            except BaseException:
                (
                    self.values,
                    self.states,
                    self.proposals,
                    self.generated,
                    self.relations,
                    self.originals,
                    self.proposed,
                    self.dependencies,
                    self.history,
                    self.withdrawn,
                    self.reinforcements,
                    self.effects,
                    self.requests,
                ) = snapshot
                raise

    def get_memory(self, character_id: str, identifier: str) -> DerivedMemory | None:
        with self.lock:
            return self.values.get((character_id, identifier))

    @contextmanager
    def list_memories(self, character_id: str) -> Iterator[Iterator[DerivedMemory]]:
        with self.lock:
            values = tuple(v for (owner, _), v in self.values.items() if owner == character_id)
        yield iter(values)

    @contextmanager
    def list_relations(
        self, character_id: str, identifier: str
    ) -> Iterator[Iterator[MemoryRelation]]:
        yield iter(
            r
            for owner, r in self.relations
            if owner == character_id and identifier in (r.from_id, r.to_id)
        )

    def get_extraction(self, character_id: str, turn_id: str) -> ExtractionState | None:
        with self.lock:
            return self.states.get((character_id, turn_id))

    def surface_page(self, *, after: str | None, limit: int) -> SurfacePage:
        surfaces = tuple(
            sorted(
                {
                    t.surface
                    for t in tuple(self.evidence.turns.values())
                    if after is None or t.surface > after
                }
            )
        )[:limit]
        return SurfacePage(surfaces, surfaces[-1] if len(surfaces) == limit else None)

    def work_page(self, surface: str, *, after: WorkCursor | None, limit: int) -> WorkPage:
        items = sorted(
            WorkCursor(t.occurred_at, t.recorded_at, t.turn_id, owner)
            for t in tuple(self.evidence.turns.values())
            if t.surface == surface
            for owner in t.perceived_by
        )
        page = tuple(v for v in items if after is None or v > after)[:limit]
        return WorkPage(page, page[-1] if len(page) == limit else None)

    @contextmanager
    def requeue_transaction(self, turn_id: str) -> Iterator[tuple[MemoryTransaction, ...]]:
        owners = sorted({owner for owner, origin in self.states if origin == turn_id})
        with self.transaction(owners[0] if owners else ""):
            self.requests[turn_id] = self.requests.get(turn_id, 0) + 1
            for key, state in tuple(self.states.items()):
                if key[1] == turn_id:
                    self.states[key] = replace(
                        state, signature="", status="retry", token=None, lease_until=None
                    )
            yield tuple(FakeMemoryTransaction(self, owner) for owner in owners)

    @contextmanager
    def claim_surface(self, surface: str, now: datetime) -> Iterator[bool]:
        yield True


class FakeMemoryTransaction:
    def projection_evidence(
        self, snapshot: ProjectionInput, inputs: MemoryInputs, now: datetime
    ) -> ProjectionEvidence:
        return collect_evidence(snapshot, self.character, inputs)

    def __init__(self, store: FakeMemoryStore, character: str) -> None:
        self.store = store
        self.character = character

    def temporal_origins(
        self, identifiers: frozenset[str]
    ) -> dict[str, dict[str, datetime | None]]:
        return {
            value.id: {
                "valid_to": value.valid_to,
                "superseded_at": value.superseded_at,
                "last_reinforced_at": value.last_reinforced_at,
            }
            for value in self.baselines(identifiers)
        }

    def validate_candidate(self, turn_id: str, value: DerivedMemory) -> None:
        previous = self.store.history.get((self.character, value.id))
        existing = self.store.values.get((self.character, value.id))
        if (existing is not None and previous is None) or (
            previous is not None and (previous.turn_id != turn_id or previous.memory != value)
        ):
            raise MemoryConflict()

    def fact_matches(self, value: DerivedMemory, now: datetime) -> tuple[DerivedMemory, ...]:
        return tuple(
            v
            for v in self.memories()
            if v.id != value.id
            and v.kind == "fact"
            and v.superseded_at is None
            and memory_valid_at(v, now)
            and (v.subject, v.predicate, v.object) == (value.subject, value.predicate, value.object)
        )

    def source_contributions(
        self, identifiers: frozenset[str] | None = None
    ) -> tuple[SourceContribution, ...]:
        values = tuple(
            c
            for (owner, _), values in self.store.reinforcements.items()
            if owner == self.character
            for c in values
            if identifiers is None or c.fact_id in identifiers
        )
        history = self.history(frozenset(c.fact_id for c in values))
        current = (
            self.candidates_for_turns(
                frozenset(c.turn_id for c in history),
                history_ids=frozenset(c.memory.id for c in history),
            )
            if history
            else ()
        )
        replacements = equivalent_ids(history, current)
        return tuple(replace(c, fact_id=replacements.get(c.fact_id, c.fact_id)) for c in values)

    def set_source_contributions(
        self, turn_id: str, values: tuple[SourceContribution, ...]
    ) -> None:
        self.store.reinforcements[(self.character, turn_id)] = values

    def relation_effects(
        self, identifiers: frozenset[str] | None = None
    ) -> tuple[RelationEffect, ...]:
        effects = tuple(
            e
            for (owner, _), values in self.store.effects.items()
            if owner == self.character
            for e in values
            if identifiers is None or e.relation.from_id in identifiers
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
        return tuple(
            replace(e, relation=r, candidate=replace(e.candidate, id=r.from_id))
            for e, r in zip(effects, resolved, strict=True)
        )

    def set_relation_effects(self, turn_id: str, values: tuple[RelationEffect, ...]) -> None:
        old = tuple(e for e in self.relation_effects() if e.turn_id == turn_id)
        self.store.effects[(self.character, turn_id)] = values
        supported = {e.relation for e in self.relation_effects()}
        self.store.relations = [
            (owner, r)
            for owner, r in self.store.relations
            if owner != self.character or r not in {e.relation for e in old} or r in supported
        ]

    def fact_sources(self, identifiers: frozenset[str]) -> dict[str, tuple[str, ...]]:
        return {v.id: v.source_turn_ids for v in self.baselines(identifiers)}

    def contribution_ids(self, turn_id: str) -> frozenset[str]:
        return (
            frozenset(
                identifier
                for (owner, identifier), value in self.store.history.items()
                if owner == self.character and value.turn_id == turn_id
            )
            | frozenset(
                identifier
                for (owner, identifier), dependencies in self.store.baseline_inputs.items()
                if owner == self.character and any(d.turn_id == turn_id for d in dependencies)
            )
            | frozenset(
                c.fact_id for c in self.source_contributions() if c.extraction_turn_id == turn_id
            )
            | frozenset(e.relation.from_id for e in self.relation_effects() if e.turn_id == turn_id)
        )

    def baseline_dependencies(
        self, identifiers: frozenset[str]
    ) -> dict[str, tuple[InputDependency, ...]]:
        return {
            identifier: dependencies
            for (owner, identifier), dependencies in self.store.baseline_inputs.items()
            if owner == self.character and identifier in identifiers
        }

    def history(self, identifiers: frozenset[str]) -> tuple[MemoryContribution, ...]:
        return tuple(
            value
            for (owner, identifier), value in self.store.history.items()
            if owner == self.character and identifier in identifiers
        )

    def candidates_for_turns(
        self, turn_ids: frozenset[str], *, history_ids: frozenset[str] | None = None
    ) -> tuple[MemoryContribution, ...]:
        history = self.history(history_ids) if history_ids is not None else ()
        return tuple(
            value
            for value in self.candidates()
            if value.turn_id in turn_ids
            and (
                history_ids is None
                or any(
                    old.turn_id == value.turn_id
                    and (
                        old.memory.id == value.memory.id
                        or (
                            old.memory.kind,
                            old.memory.content,
                            old.memory.subject,
                            old.memory.predicate,
                            old.memory.object,
                        )
                        == (
                            value.memory.kind,
                            value.memory.content,
                            value.memory.subject,
                            value.memory.predicate,
                            value.memory.object,
                        )
                    )
                    for old in history
                )
            )
        )

    def projection_neighbors(
        self, identifiers: frozenset[str], source_only: frozenset[str]
    ) -> frozenset[str]:
        seeds = self.history(identifiers)
        origins = frozenset(v.turn_id for v in seeds)
        relations = [r for owner, r in self.store.relations if owner == self.character]
        relations.extend(
            r
            for (owner, _), items in self.store.proposed.items()
            if owner == self.character
            for r in items
        )
        relations.extend(
            e.relation
            for (owner, _), items in self.store.effects.items()
            if owner == self.character
            for e in items
        )
        required = (
            identifiers
            | {i for r in relations for i in (r.from_id, r.to_id)}
            | {
                c.fact_id
                for (owner, _), items in self.store.reinforcements.items()
                if owner == self.character
                for c in items
            }
            | {
                v.id
                for (owner, _), v in self.store.values.items()
                if owner == self.character and v.superseded_at is None
            }
        )
        history = tuple(
            v
            for (owner, _), v in self.store.history.items()
            if owner == self.character
            and v.turn_id in origins
            and v.memory.id in required
            and any(
                seed.turn_id == v.turn_id
                and (
                    seed.memory.id == v.memory.id
                    or (
                        seed.memory.kind,
                        seed.memory.content,
                        seed.memory.subject,
                        seed.memory.predicate,
                        seed.memory.object,
                    )
                    == (
                        v.memory.kind,
                        v.memory.content,
                        v.memory.subject,
                        v.memory.predicate,
                        v.memory.object,
                    )
                )
                for seed in seeds
            )
        )
        replacements = equivalent_ids(
            history,
            self.candidates_for_turns(origins, history_ids=frozenset(v.memory.id for v in history)),
        )
        neighbors = {
            identifier
            for previous, current_id in replacements.items()
            if previous in identifiers or current_id in identifiers
            for identifier in (previous, current_id)
        }
        source_only = source_only | frozenset(
            previous for previous, current_id in replacements.items() if current_id in source_only
        )
        identifiers = identifiers | frozenset(neighbors)
        for relation in relations:
            if relation.from_id in identifiers and (
                relation.type != "derives"
                or (
                    (self.character, relation.to_id) in self.store.values
                    and (self.character, relation.to_id) in self.store.withdrawn
                )
            ):
                neighbors.add(relation.to_id)
            if relation.to_id in identifiers and (
                relation.type != "derives" or relation.to_id not in source_only
            ):
                neighbors.add(relation.from_id)
        return frozenset(neighbors)

    def withdrawn_ids(self, identifiers: frozenset[str]) -> frozenset[str]:
        return frozenset(
            identifier
            for owner, identifier in self.store.withdrawn
            if owner == self.character and identifier in identifiers
        )

    def add_memory(self, value: DerivedMemory) -> None:
        self._insert(value)
        self.store.originals[(self.character, value.id)] = value

    def _insert(self, value: DerivedMemory) -> None:
        if (self.character, value.id) in self.store.values:
            raise MemoryConflict()
        self.store.values[(self.character, value.id)] = value

    def replace_memory(self, value: DerivedMemory) -> None:
        self.replace_projection(value)
        self.store.originals[(self.character, value.id)] = value

    def replace_projection(self, value: DerivedMemory) -> None:
        self.store.values[(self.character, value.id)] = value
        self.store.withdrawn.discard((self.character, value.id))

    def add_generated(self, value: DerivedMemory) -> None:
        self._insert(value)

    def baselines(self, identifiers: frozenset[str] | None = None) -> tuple[DerivedMemory, ...]:
        return tuple(
            v
            for (owner, identifier), v in self.store.originals.items()
            if owner == self.character and (identifiers is None or identifier in identifiers)
        )

    def proposed_relations(
        self, identifiers: frozenset[str] | None = None, *, include_inactive: bool = False
    ) -> tuple[MemoryRelation, ...]:
        return tuple(
            r
            for (owner, turn), items in self.store.proposed.items()
            if owner == self.character
            and (include_inactive or self.store.states[(owner, turn)].status == "done")
            for r in items
            if identifiers is None or r.from_id in identifiers
        )

    def set_relations(self, turn_id: str, values: tuple[MemoryRelation, ...]) -> None:
        self.store.proposed[(self.character, turn_id)] = values

    def add_relation(self, value: MemoryRelation) -> None:
        self.store.relations.append((self.character, value))

    def memories(self, identifiers: frozenset[str] | None = None) -> tuple[DerivedMemory, ...]:
        return tuple(
            v
            for (owner, identifier), v in self.store.values.items()
            if owner == self.character and (identifiers is None or identifier in identifiers)
        )

    def candidates(
        self, identifiers: frozenset[str] | None = None
    ) -> tuple[MemoryContribution, ...]:
        values = [
            MemoryContribution(turn, v)
            for (owner, turn), items in self.store.proposals.items()
            if owner == self.character and self.store.states[(owner, turn)].status == "done"
            for v in items
            if identifiers is None or v.id in identifiers
        ]
        return tuple(
            sorted(values, key=lambda v: (v.memory.valid_from, v.memory.recorded_at, v.memory.id))
        )

    def generated_ids(self, identifiers: frozenset[str] | None = None) -> frozenset[str]:
        values = frozenset(self.store.generated.get(self.character, set()))
        return values if identifiers is None else values & identifiers

    def set_candidates(
        self,
        turn_id: str,
        values: tuple[DerivedMemory, ...],
        *,
        dependencies: tuple[InputDependency, ...],
    ) -> None:
        self.store.dependencies[(self.character, turn_id)] = dependencies
        self.store.proposals[(self.character, turn_id)] = values
        self.store.generated.setdefault(self.character, set()).update(v.id for v in values)
        for value in values:
            self.store.history[(self.character, value.id)] = MemoryContribution(turn_id, value)

    def applied_relations(
        self, identifiers: frozenset[str] | None = None
    ) -> tuple[MemoryRelation, ...]:
        return tuple(
            r
            for owner, r in self.store.relations
            if owner == self.character and (identifiers is None or r.from_id in identifiers)
        )

    def sync_projection_relations(
        self,
        identifiers: frozenset[str],
        values: tuple[MemoryRelation, ...],
        projected_ids: frozenset[str],
    ) -> None:
        history = self.history(identifiers)
        current = self.candidates_for_turns(
            frozenset(c.turn_id for c in history),
            history_ids=frozenset(c.memory.id for c in history),
        )
        replacements = {
            previous: replacement
            for previous, replacement in equivalent_ids(history, current).items()
            if replacement in projected_ids
        }
        for key, sources in tuple(self.store.reinforcements.items()):
            if key[0] == self.character:
                self.store.reinforcements[key] = tuple(
                    dict.fromkeys(
                        replace(c, fact_id=replacements.get(c.fact_id, c.fact_id)) for c in sources
                    )
                )
        raw = tuple(
            e
            for (owner, _), effects in self.store.effects.items()
            if owner == self.character
            for e in effects
            if e.relation.from_id in identifiers
        )
        resolved = {
            previous: resolved
            for previous, resolved in zip(raw, self.relation_effects(identifiers), strict=True)
            if (
                previous.relation.from_id == resolved.relation.from_id
                or resolved.relation.from_id in projected_ids
            )
            and (
                previous.relation.to_id == resolved.relation.to_id
                or resolved.relation.to_id in projected_ids
            )
        }
        for (owner, origin), effects in tuple(self.store.effects.items()):
            if owner == self.character:
                self.store.effects[(owner, origin)] = tuple(resolved.get(e, e) for e in effects)
        self.store.relations = [
            (owner, r)
            for owner, r in self.store.relations
            if owner != self.character or r.from_id not in identifiers or r in values
        ]
        for value in values:
            if (self.character, value) not in self.store.relations:
                self.add_relation(value)

    def extraction_states(self, turn_ids: frozenset[str]) -> dict[str, ExtractionState | None]:
        return {identifier: self.get_extraction(identifier) for identifier in turn_ids}

    def notification_versions(self, turn_ids: frozenset[str]) -> dict[str, int]:
        return {identifier: self.notification_version(identifier) for identifier in turn_ids}

    def get_extraction(self, turn_id: str) -> ExtractionState | None:
        return self.store.states.get((self.character, turn_id))

    def save_extraction(self, value: ExtractionState) -> None:
        self.store.states[(self.character, value.turn_id)] = value

    def notification_version(self, turn_id: str) -> int:
        return (
            1
            + sum(r.turn_id == turn_id for r in self.store.evidence.receipts)
            + self.store.requests.get(turn_id, 0)
        )
