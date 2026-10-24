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
)
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from digital_souls_memory.memory.ports import (
    MemoryConflict,
    MemoryContribution,
    MemoryTransaction,
    MemoryUnavailable,
    SurfacePage,
    WorkCursor,
    WorkPage,
)
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

    def requeue(self, turn_id: str) -> None:
        with self.lock:
            self.requests[turn_id] = self.requests.get(turn_id, 0) + 1
            for key, state in tuple(self.states.items()):
                if key[1] == turn_id:
                    self.states[key] = replace(state, signature="")

    @contextmanager
    def claim_surface(self, surface: str, now: datetime) -> Iterator[bool]:
        yield True


class FakeMemoryTransaction:
    def __init__(self, store: FakeMemoryStore, character: str) -> None:
        self.store = store
        self.character = character

    def contribution_ids(self, turn_id: str) -> frozenset[str]:
        return frozenset(
            identifier
            for (owner, identifier), value in self.store.history.items()
            if owner == self.character and value.turn_id == turn_id
        ) | frozenset(
            identifier
            for (owner, identifier), dependencies in self.store.baseline_inputs.items()
            if owner == self.character and any(d.turn_id == turn_id for d in dependencies)
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

    def candidates_for_turns(self, turn_ids: frozenset[str]) -> tuple[MemoryContribution, ...]:
        return tuple(value for value in self.candidates() if value.turn_id in turn_ids)

    def projection_neighbors(self, identifiers: frozenset[str]) -> frozenset[str]:
        values = [
            v.memory for (owner, _), v in self.store.history.items() if owner == self.character
        ]
        values.extend(self.baselines())
        facts = {
            (v.subject, v.predicate, v.object)
            for v in values
            if v.id in identifiers and v.kind == "fact"
        }
        neighbors = {
            v.id for v in values if v.kind == "fact" and (v.subject, v.predicate, v.object) in facts
        }
        origins = {v.turn_id for v in self.history(identifiers)}
        neighbors.update(
            v.id
            for (owner, origin), items in self.store.proposals.items()
            if owner == self.character and origin in origins
            for v in items
        )
        relations = [r for owner, r in self.store.relations if owner == self.character]
        relations.extend(
            r
            for (owner, _), items in self.store.proposed.items()
            if owner == self.character
            for r in items
        )
        for relation in relations:
            if relation.from_id in identifiers and (
                relation.type != "derives"
                or (
                    (self.character, relation.to_id) in self.store.values
                    and (self.character, relation.to_id) in self.store.withdrawn
                )
            ):
                neighbors.add(relation.to_id)
            if relation.to_id in identifiers:
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
        self, identifiers: frozenset[str] | None = None
    ) -> tuple[MemoryRelation, ...]:
        return tuple(
            r
            for (owner, turn), items in self.store.proposed.items()
            if owner == self.character and self.store.states[(owner, turn)].status == "done"
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
        self, identifiers: frozenset[str], values: tuple[MemoryRelation, ...]
    ) -> None:
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
