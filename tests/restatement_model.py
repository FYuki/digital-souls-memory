"""Small owner ledger for synthetic ADR 0023/0024 sequences.

Only domain value types are shared with the application. Acceptance, temporal
effects, evidence counting and withdrawal are calculated here independently.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from itertools import combinations

from digital_souls_memory.evidence.domain import Turn
from digital_souls_memory.memory.domain import DerivedMemory, MemoryRelation


def canonical(values: dict[str, DerivedMemory]) -> dict[str, DerivedMemory]:
    return {
        k: replace(v, source_turn_ids=tuple(sorted(v.source_turn_ids))) for k, v in values.items()
    }


def live(value: DerivedMemory, now: datetime) -> bool:
    return (
        value.superseded_at is None
        and value.valid_from <= now
        and (value.valid_to is None or now < value.valid_to)
    )


def semantic_attributes(value: DerivedMemory) -> tuple[object, ...]:
    return (
        value.character_id,
        value.kind,
        value.content,
        value.source_turn_ids,
        value.valid_from,
        value.valid_to,
        value.superseded_at,
        value.last_reinforced_at,
        value.epistemics.source_type,
        value.epistemics.trust_tier,
        value.epistemics.independent_evidence_count,
        value.epistemics.contradiction_ids,
        value.identity_links,
        value.subject,
        value.predicate,
        value.object,
        value.shared_with,
        value.counterpart,
        value.affection,
        value.trust,
        value.status,
        value.deadline,
        value.verbatim_turn_id,
    )


@dataclass
class Owner:
    bodies: tuple[DerivedMemory, ...]
    sources: dict[str, tuple[str, ...]]
    relations: tuple[MemoryRelation, ...]
    effects: dict[str, DerivedMemory]


@dataclass
class Ledger:
    now: datetime
    turns: dict[str, Turn] = field(default_factory=dict)
    owners: dict[str, Owner] = field(default_factory=dict)
    baselines: dict[str, DerivedMemory] = field(default_factory=dict)
    baseline_sources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    baseline_temporal: dict[str, DerivedMemory] = field(default_factory=dict)
    history: dict[str, tuple[DerivedMemory, ...]] = field(default_factory=dict)
    candidates: dict[str, tuple[DerivedMemory, ...]] = field(default_factory=dict)
    saved: dict[str, DerivedMemory] = field(default_factory=dict)

    def count(self, sources: tuple[str, ...]) -> int:
        # Enumerate compatible subsets rather than use the product matching algorithm.
        neighbors: dict[str, set[str]] = {i: set() for i in sources}
        for identifier in sources:
            reply = self.turns[identifier].reply_to_turn_id
            if reply in neighbors:
                neighbors[identifier].add(reply)
                neighbors[reply].add(identifier)
        components: dict[str, str] = {}
        for identifier in sources:
            pending = [identifier]
            while pending:
                current = pending.pop()
                if current in components:
                    continue
                components[current] = identifier
                pending.extend(neighbors[current] - components.keys())
        pairs = set((components[i], self.turns[i].actor) for i in sources)
        limit = min(len(set(components.values())), len({p[1] for p in pairs}))
        return max(
            (
                len(group)
                for n in range(limit + 1)
                for group in combinations(pairs, n)
                if len({p[0] for p in group}) == len(group)
                and len({p[1] for p in group}) == len(group)
            ),
            default=0,
        )

    def withdraw(self, origin: str) -> None:
        self.owners.pop(origin, None)

    def extract(
        self, origin: str, bodies: tuple[DerivedMemory, ...], relations: tuple[MemoryRelation, ...]
    ) -> None:
        self.withdraw(origin)
        before, _ = self.expected()
        accepted, sources, effects, rewrites = [], {}, {}, {}
        for body in bodies:
            matches = [
                v
                for v in before.values()
                if body.kind == v.kind == "fact"
                and v.id != body.id
                and live(v, self.now)
                and (v.subject, v.predicate, v.object)
                == (body.subject, body.predicate, body.object)
            ]
            if len(matches) > 1:
                raise ValueError("Sequence has an unspecified acceptance tie")
            if matches:
                target = matches[0].id
                rewrites[body.id] = target
                sources[target] = body.source_turn_ids
                effects[target] = replace(body, id=target)
            else:
                accepted.append(body)
        rewritten = tuple(
            replace(
                r, from_id=rewrites.get(r.from_id, r.from_id), to_id=rewrites.get(r.to_id, r.to_id)
            )
            for r in relations
            if rewrites.get(r.from_id, r.from_id) != rewrites.get(r.to_id, r.to_id)
        )
        self.owners[origin] = Owner(tuple(accepted), sources, rewritten, effects)
        previous = self.history.get(origin, ())
        replacements: dict[str, str] = {}
        for old in previous:
            equivalent = [
                v.id for v in accepted if semantic_attributes(old) == semantic_attributes(v)
            ]
            if old.id not in {v.id for v in accepted} and len(equivalent) == 1:
                replacements[old.id] = equivalent[0]
                if old.id in self.saved:
                    self.saved[old.id] = old
        for owner in self.owners.values():
            owner.sources = {replacements.get(i, i): s for i, s in owner.sources.items()}
            owner.relations = tuple(
                replace(
                    r,
                    from_id=replacements.get(r.from_id, r.from_id),
                    to_id=replacements.get(r.to_id, r.to_id),
                )
                for r in owner.relations
            )
            owner.effects = {
                replacements.get(i, i): replace(v, id=replacements.get(i, i))
                for i, v in owner.effects.items()
            }
        self.history[origin] = tuple({v.id: v for v in (*previous, *bodies)}.values())
        self.candidates[origin] = tuple(accepted)

    def delete(self, origin: str, *, recalculation: str = "preserve") -> None:
        # Recalculation's synthetic gateway preserves content and chooses surviving sources.
        before, applied = self.expected()
        temporal = (
            {v.id: v for values in self.history.values() for v in values}
            | {v.id: v for owner in self.owners.values() for v in owner.bodies}
            | self.baseline_temporal
        )
        body_sources = (
            {v.id: v.source_turn_ids for values in self.history.values() for v in values}
            | {v.id: v.source_turn_ids for owner in self.owners.values() for v in owner.bodies}
            | self.baseline_sources
        )
        dependents = {i for i, t in self.turns.items() if t.reply_to_turn_id == origin}
        affected = {
            identifier
            for owner_id, owner in self.owners.items()
            if owner_id in dependents | {origin}
            for identifier in (
                *owner.sources,
                *(r.to_id for r in owner.relations if r.type != "derives"),
            )
        }
        affected.update(
            identifier
            for identifier, baseline in self.baselines.items()
            if origin in baseline.source_turn_ids
        )
        generated = {
            v.id
            for owner_id, values in self.history.items()
            if owner_id in dependents | {origin}
            for v in values
        }
        affected.update(generated)
        while True:
            expanded = affected | {
                r.to_id for r in applied if r.from_id in affected and r.type != "derives"
            }
            if expanded == affected:
                break
            affected = expanded
        self.withdraw(origin)
        for dependent in dependents:
            self.withdraw(dependent)
        self.turns.pop(origin)
        self.turns = {
            i: replace(t, reply_to_turn_id=None) if i in dependents else t
            for i, t in self.turns.items()
        }
        for identifier, body in before.items():
            if origin not in body.source_turn_ids and identifier not in affected:
                continue
            remaining = tuple(i for i in body.source_turn_ids if i != origin)
            if remaining and recalculation != "unavailable":
                base = temporal[identifier]
                if identifier not in self.baselines and not any(
                    v.id == identifier for values in self.candidates.values() for v in values
                ):
                    base = replace(base, superseded_at=body.superseded_at)
                self.baselines[identifier] = replace(
                    body,
                    source_turn_ids=remaining,
                    valid_to=base.valid_to,
                    superseded_at=base.superseded_at,
                    last_reinforced_at=base.last_reinforced_at,
                    epistemics=replace(
                        body.epistemics, independent_evidence_count=self.count(remaining)
                    ),
                )
                self.baseline_sources[identifier] = tuple(
                    s for s in body_sources[identifier] if s != origin
                )
                self.baseline_temporal[identifier] = base
            else:
                self.baselines.pop(identifier, None)
                self.baseline_sources.pop(identifier, None)
                self.baseline_temporal.pop(identifier, None)
                self.saved.pop(identifier, None)
                for owner in self.owners.values():
                    owner.bodies = tuple(v for v in owner.bodies if v.id != identifier)
                    owner.sources.pop(identifier, None)
                    owner.effects.pop(identifier, None)
                    owner.relations = tuple(
                        r for r in owner.relations if identifier not in (r.from_id, r.to_id)
                    )
                self.candidates = {
                    o: tuple(v for v in values if v.id != identifier)
                    for o, values in self.candidates.items()
                }
                self.history = {
                    o: tuple(v for v in values if v.id != identifier)
                    for o, values in self.history.items()
                }
        projected, _ = self.expected()
        for identifier in self.baselines:
            if (
                origin in before[identifier].source_turn_ids or identifier in affected
            ) and identifier in projected:
                self.baselines[identifier] = projected[identifier]

    def expected(self) -> tuple[dict[str, DerivedMemory], set[MemoryRelation]]:
        bodies = dict(self.baselines)
        for owner in self.owners.values():
            bodies.update((v.id, v) for v in owner.bodies)
        support: dict[str, set[str]] = {
            i: set(self.baseline_sources[i] if i in self.baselines else v.source_turn_ids)
            for i, v in bodies.items()
        }
        events: dict[str, list[datetime]] = {i: [] for i in bodies}
        for owner in self.owners.values():
            for identifier, sources in owner.sources.items():
                if identifier in bodies:
                    support[identifier].update(sources)
                    events[identifier].extend(self.turns[s].occurred_at for s in sources)
        values = {
            i: replace(
                v,
                source_turn_ids=tuple(sorted(support[i])),
                valid_to=self.baseline_temporal[i].valid_to if i in self.baselines else v.valid_to,
                superseded_at=self.baseline_temporal[i].superseded_at
                if i in self.baselines
                else v.superseded_at,
                last_reinforced_at=max(events[i])
                if events[i]
                else (
                    self.baseline_temporal[i].last_reinforced_at
                    if i in self.baselines
                    else v.last_reinforced_at
                ),
                epistemics=replace(
                    v.epistemics, independent_evidence_count=self.count(tuple(support[i]))
                ),
            )
            for i, v in bodies.items()
            if support[i]
        }
        relations: set[MemoryRelation] = set()
        # The generated graphs are acyclic; visit targets before their dependents.
        owned = [(o, r) for o in self.owners.values() for r in o.relations]
        pending = set(values)
        while pending:
            ready = sorted(
                i
                for i in pending
                if not any(r.from_id == i and r.to_id in pending for _, r in owned)
            )
            if not ready:
                raise ValueError("Sequence has a dependency cycle")
            for identifier in ready:
                pending.remove(identifier)
                explicit = [(o, r) for o, r in owned if r.from_id == identifier]
                if any(
                    r.to_id not in values or values[r.to_id].superseded_at is not None
                    for _, r in explicit
                ):
                    values.pop(identifier, None)
                    continue
                for owner, relation in explicit:
                    source = owner.effects.get(identifier, bodies[identifier])
                    target = values[relation.to_id]
                    if relation.type == "updates":
                        values[target.id] = replace(target, valid_to=source.valid_from)
                    elif relation.type == "contradicts":
                        values[target.id] = replace(target, superseded_at=self.now)
                    elif relation.type == "extends":
                        sources = tuple(
                            sorted(set(target.source_turn_ids) | set(source.source_turn_ids))
                        )
                        values[target.id] = replace(
                            target,
                            source_turn_ids=sources,
                            last_reinforced_at=max(
                                self.turns[s].occurred_at
                                for s in sources
                                if s in source.source_turn_ids
                            ),
                            epistemics=replace(
                                target.epistemics, independent_evidence_count=self.count(sources)
                            ),
                        )
                        if identifier not in owner.effects:
                            values[identifier] = replace(values[identifier], superseded_at=self.now)
                    relations.add(relation)
        for identifier, old in self.saved.items():
            if identifier not in values:
                values[identifier] = replace(old, superseded_at=self.now)
        return canonical(values), relations

    def checkpoint(self) -> tuple[dict[str, DerivedMemory], set[MemoryRelation]]:
        values, relations = self.expected()
        self.saved = values
        return values, relations
