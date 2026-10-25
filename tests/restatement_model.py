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
    history: dict[str, tuple[DerivedMemory, ...]] = field(default_factory=dict)
    saved: dict[str, DerivedMemory] = field(default_factory=dict)

    def count(self, sources: tuple[str, ...]) -> int:
        # Enumerate compatible subsets rather than use the product matching algorithm.
        pairs = [(self.turns[i].reply_to_turn_id or i, self.turns[i].actor) for i in sources]
        return max(
            (
                len(group)
                for n in range(len(pairs) + 1)
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
            equivalent = [v.id for v in accepted if replace(old, id=v.id) == v]
            if old.id not in {v.id for v in accepted} and len(equivalent) == 1:
                replacements[old.id] = equivalent[0]
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
        self.history[origin] = bodies

    def delete(self, origin: str) -> None:
        # Recalculation's synthetic gateway preserves content and chooses surviving sources.
        before, _ = self.expected()
        self.withdraw(origin)
        self.turns.pop(origin)
        for identifier, body in before.items():
            if origin not in body.source_turn_ids:
                continue
            remaining = tuple(i for i in body.source_turn_ids if i != origin)
            if remaining:
                self.baselines[identifier] = replace(
                    body,
                    source_turn_ids=remaining,
                    valid_to=None,
                    superseded_at=None,
                    last_reinforced_at=None,
                )
                self.baseline_sources[identifier] = tuple(
                    sorted(
                        {
                            s
                            for owner in self.owners.values()
                            for v in owner.bodies
                            if v.id == identifier
                            for s in v.source_turn_ids
                            if s != origin
                        }
                    )
                )
            else:
                self.baselines.pop(identifier, None)
                self.baseline_sources.pop(identifier, None)
                self.saved.pop(identifier, None)

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
                last_reinforced_at=max(events[i]) if events[i] else v.last_reinforced_at,
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
