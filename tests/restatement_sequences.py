"""Reproducible state-dependent sequences and prerequisite-preserving shrinking."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from random import Random

from digital_souls_memory.memory.domain import DerivedMemory, MemoryRelation
from tests.evidence_support import OCCURRED, RECORDED, turn
from tests.memory_support import memory
from tests.restatement_model import Ledger, live


class ObservationMismatch(AssertionError):
    def __init__(self, boundary: str, context: str, actual: object, expected: object) -> None:
        self.boundary = boundary
        super().__init__(
            f"{context}\nboundary={boundary}\nactual={actual!r}\nexpected={expected!r}"
        )


class RecalculationNotInvoked(Exception):
    """A replay candidate no longer reaches its requested failure injection."""


def compare(boundary: str, context: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise ObservationMismatch(boundary, context, actual, expected)


@dataclass(frozen=True)
class Operation:
    kind: str
    origin: str
    bodies: tuple[DerivedMemory, ...] = ()
    relations: tuple[MemoryRelation, ...] = ()
    actor: str = "user-a"
    occurred_at: datetime = OCCURRED
    reply_to_turn_id: str | None = None
    recalculation: str = "preserve"


def extract(identifier: str, origin: str, object_value: str) -> Operation:
    return Operation(
        "extract",
        origin,
        (
            replace(
                memory(),
                id=identifier,
                object=object_value,
                source_turn_ids=(origin,),
            ),
        ),
    )


def extends_reply_deletion_sequence() -> tuple[Operation, ...]:
    target = extract("c", "target-source", "water")
    reply = replace(
        extract("d", "reply-source", "coffee"),
        occurred_at=OCCURRED + timedelta(minutes=2),
        reply_to_turn_id=target.origin,
        relations=(MemoryRelation(from_id="d", to_id="c", type="extends"),),
    )
    return (target, reply, Operation("delete", target.origin))


def extends_target_retry_deletion_sequence() -> tuple[Operation, ...]:
    target = extract("c", "target-source", "water")
    extension = replace(
        extract("d", "extension-source", "coffee"),
        occurred_at=OCCURRED + timedelta(minutes=2),
        relations=(MemoryRelation(from_id="d", to_id="c", type="extends"),),
    )
    return (
        target,
        extension,
        Operation("retry", target.origin),
        Operation("delete", target.origin),
    )


def revived_fact_sequence() -> tuple[Operation, ...]:
    x = extract("x", "source-x", "coffee")
    y = replace(
        extract("y", "source-y", "tea"),
        relations=(MemoryRelation(from_id="y", to_id="x", type="updates"),),
    )
    z = extract("z", "source-z", "coffee")
    return (
        x,
        y,
        z,
        extract("before", "before-revival", "coffee"),
        Operation("delete", "source-y"),
        extract("after", "after-revival", "coffee"),
        Operation("delete", "source-z"),
    )


def duplicate_fact_sequence() -> tuple[Operation, ...]:
    first = extract("a", "duplicates", "coffee").bodies[0]
    later = replace(first, id="b")
    summary = replace(
        memory("summary"),
        id="d",
        source_turn_ids=("duplicates",),
        content='b、`b`、{"from_id":"b"}\n```text\nb\n```\n~~~text\nb',
    )
    return (
        Operation(
            "extract",
            "duplicates",
            (later, first, summary),
            (MemoryRelation(from_id="d", to_id="b", type="derives"),),
        ),
        extract("support", "support", "coffee"),
        Operation("delete", "duplicates"),
    )


def coexisting_relations_sequence(
    *, same_target: bool, effects_only: bool = False, temporal: bool = False
) -> tuple[Operation, ...]:
    x = extract("x", "target-x", "water")
    y = extract("y", "target-y", "juice")
    a = extract("a", "duplicates", "coffee").bodies[0]
    b = replace(a, id="b", valid_from=a.valid_from + timedelta(seconds=1))
    c = replace(a, id="c", valid_from=a.valid_from + timedelta(seconds=2))
    proposal = Operation(
        "extract",
        "duplicates",
        (a, b, c) if effects_only or temporal else (a, b),
        (
            MemoryRelation(
                from_id="b" if effects_only or temporal else "a",
                to_id="x",
                type="updates" if temporal else "derives",
            ),
            MemoryRelation(
                from_id="c" if effects_only or temporal else "b",
                to_id="x" if same_target else "y",
                type="updates" if temporal else "derives",
            ),
        ),
    )
    support = extract("support", "support", "coffee")
    return (
        x,
        y,
        proposal,
        support,
        Operation("retry", "duplicates"),
        proposal,
        replace(proposal, relations=proposal.relations[:1]),
        proposal,
        Operation("delete", "support"),
        extract("support-2", "support-2", "coffee"),
        Operation("delete", "duplicates"),
    )


def effect_reextraction_sequence() -> tuple[Operation, ...]:
    root = extract("b", "root", "coffee")
    support = replace(
        extract("a", "support", "coffee"),
        relations=(MemoryRelation(from_id="a", to_id="c", type="extends"),),
    )
    return (
        extract("c", "target", "water"),
        root,
        support,
        replace(root, bodies=(fresh_metadata(root.bodies[0], identifier="replacement"),)),
        Operation("delete", "root"),
    )


def ambiguity_reproduction_sequence() -> tuple[Operation, ...]:
    """The exact 35 operations recorded for seed 175, independent of generation."""
    operations = list(minimal_sequence("extends", origin_first=True, actor="user-b")[:3])

    def new(index: int, object_value: str, tier: str, reply: str | None = None) -> Operation:
        op = extract(f"candidate-{index}", f"origin-{index}", object_value)
        body = op.bodies[0]
        return replace(
            op,
            actor="user-b",
            reply_to_turn_id=reply,
            bodies=(replace(body, epistemics=replace(body.epistemics, trust_tier=tier)),),
        )

    def renamed(index: int, previous: int) -> Operation:
        op = operations[previous]
        body = fresh_metadata(op.bodies[0], identifier=f"candidate-{index}-0")
        return replace(
            op,
            bodies=(body,),
            relations=tuple(replace(r, from_id=body.id) for r in op.relations),
        )

    operations.append(Operation("delete", "turn-c"))
    operations.append(new(4, "synthetic-4", "A", "turn-b"))
    operations.append(Operation("retry", "origin-4"))
    operations.append(replace(renamed(6, 1), relations=()))
    operations.append(renamed(7, 2))
    operations.append(
        replace(
            new(8, "synthetic-8", "D"),
            relations=(
                MemoryRelation(from_id="candidate-8", to_id="candidate-7-0", type="derives"),
            ),
        )
    )
    operations.append(Operation("retry", "origin-4"))
    operations.append(Operation("delete", "turn-1", recalculation="fail"))
    operations.append(renamed(11, 7))
    operations.append(extract("candidate-12", "origin-12", "synthetic-8"))
    operations.append(renamed(13, 12))
    operations.append(Operation("delete", "origin-8", recalculation="unavailable"))
    operations.append(renamed(15, 4))
    operations.append(new(16, "synthetic-4", "A", "turn-b"))
    operations.append(new(17, "synthetic-4", "A"))
    operations.append(
        replace(
            new(18, "synthetic-18", "D"),
            relations=(
                MemoryRelation(from_id="candidate-18", to_id="candidate-7-0", type="extends"),
            ),
        )
    )
    operations.append(renamed(19, 17))
    operations.append(new(20, "coffee", "A"))
    operations.append(Operation("retry", "origin-4"))
    operations.append(renamed(22, 19))
    operations.append(renamed(23, 18))
    operations.append(Operation("delete", "origin-16"))
    operations.append(Operation("retry", "origin-18"))
    operations.append(renamed(26, 20))
    operations.append(new(27, "synthetic-27", "F", "turn-b"))
    operations.append(Operation("retry", "origin-27"))
    operations.append(Operation("retry", "origin-4"))
    operations.append(Operation("retry", "origin-12"))
    operations.append(Operation("delete", "origin-12"))
    operations.append(
        replace(
            new(32, "synthetic-32", "D"),
            relations=(
                MemoryRelation(from_id="candidate-32", to_id="candidate-22-0", type="extends"),
            ),
        )
    )
    operations.append(Operation("retry", "origin-27"))
    operations.append(new(34, "synthetic-4", "D"))
    return tuple(operations)


def minimal_sequence(
    relation_type: str, *, origin_first: bool, actor: str
) -> tuple[Operation, ...]:
    root = extract("a", "turn-1", "coffee")
    related = replace(root, relations=(MemoryRelation(from_id="a", to_id="c", type=relation_type),))
    support = replace(extract("b", "turn-b", "coffee"), actor=actor)
    deleted = ("turn-1", "turn-b") if origin_first else ("turn-b", "turn-1")
    return (
        extract("c", "turn-c", "water"),
        related,
        support,
        Operation("delete", deleted[0]),
        Operation("delete", deleted[1]),
    )


def generated_sequence(seed: int) -> tuple[Operation, ...]:
    rng = Random(seed)
    relation_type = rng.choice(("updates", "contradicts", "derives", "extends"))
    # extends with different SPO is deliberately historical, never a restatement.
    if relation_type == "extends":
        sequence = list(
            event_times(
                (
                    extract("c", "turn-c", "water"),
                    replace(
                        extract("a", "turn-1", "coffee"),
                        relations=(MemoryRelation(from_id="a", to_id="c", type="extends"),),
                    ),
                    Operation("retry", "turn-1"),
                ),
                rng,
            )
        )
    else:
        sequence = list(
            minimal_sequence(
                relation_type,
                origin_first=bool(rng.randrange(2)),
                actor=rng.choice(("user-a", "user-b")),
            )
        )
        support = sequence[2]
        support = replace(
            support,
            bodies=tuple(
                replace(v, epistemics=replace(v.epistemics, trust_tier=rng.choice(("A", "D", "F"))))
                for v in support.bodies
            ),
            reply_to_turn_id="turn-c" if rng.randrange(2) else None,
        )
        sequence[2] = support
        sequence[3:3] = [Operation("retry", "turn-b"), support]
        first_delete = sequence[5]
        if first_delete.origin == "turn-1":
            sequence[5] = replace(
                first_delete, recalculation=rng.choice(("preserve", "unavailable", "fail"))
            )
        survivor = "turn-b" if first_delete.origin == "turn-1" else "turn-1"
        sequence.insert(6, Operation("retry", survivor))
        if rng.randrange(2):
            sequence.insert(3, replace(extract("e", "turn-e", "coffee"), actor="user-b"))
    if seed % 32 == 0:
        sequence = list(continue_sequence(tuple(sequence), rng, steps=rng.randint(30, 50)))
    return event_times(tuple(sequence), rng)


def continue_sequence(
    operations: tuple[Operation, ...], rng: Random, *, steps: int
) -> tuple[Operation, ...]:
    ledger = Ledger(RECORDED)
    current: dict[str, Operation] = {}
    for op in operations:
        if op.kind == "extract":
            current[op.origin] = op
        elif op.kind == "delete":
            current.pop(op.origin)
            current = {
                i: replace(v, reply_to_turn_id=None) if v.reply_to_turn_id == op.origin else v
                for i, v in current.items()
            }
        _advance_ledger(ledger, op)
    sequence = list(operations)
    while len(sequence) < steps:
        index = len(sequence)
        action = (
            rng.choice(("extract", "extract", "retry", "delete", "reextract"))
            if current
            else "extract"
        )
        if action == "extract":
            op = extract(f"candidate-{index}", f"origin-{index}", f"synthetic-{index}")
            active = tuple(v for v in ledger.expected()[0].values() if live(v, RECORDED))
            if active:
                target = rng.choice(active)
                if rng.randrange(3) == 0:
                    op = replace(op, bodies=(replace(op.bodies[0], object=target.object),))
                elif rng.randrange(2):
                    op = replace(
                        op,
                        relations=(
                            MemoryRelation(
                                from_id=op.bodies[0].id,
                                to_id=target.id,
                                type=rng.choice(("extends", "derives")),
                            ),
                        ),
                    )
            op = replace(
                op,
                actor=rng.choice(("user-a", "user-b")),
                reply_to_turn_id=rng.choice(tuple(current))
                if current and rng.randrange(2)
                else None,
                bodies=tuple(
                    replace(
                        v, epistemics=replace(v.epistemics, trust_tier=rng.choice(("A", "D", "F")))
                    )
                    for v in op.bodies
                ),
            )
            current[op.origin] = op
        else:
            origin = rng.choice(tuple(current))
            previous = current[origin]
            if action == "reextract":
                renamed = tuple(
                    fresh_metadata(v, identifier=f"candidate-{index}-{n}")
                    for n, v in enumerate(previous.bodies)
                )
                replacements = {
                    v.id: new.id for v, new in zip(previous.bodies, renamed, strict=True)
                }
                available = {v.id for v in ledger.expected()[0].values() if live(v, RECORDED)}
                op = replace(
                    previous,
                    bodies=renamed,
                    relations=tuple(
                        replace(
                            r,
                            from_id=replacements.get(r.from_id, r.from_id),
                            to_id=replacements.get(r.to_id, r.to_id),
                        )
                        for r in previous.relations
                        if r.to_id in available
                    ),
                )
                current[origin] = op
            elif action == "delete":
                withdrawn = {origin} | {
                    i for i, source in ledger.turns.items() if source.reply_to_turn_id == origin
                }
                available_sources = {
                    v.id: set(ledger.baseline_sources.get(v.id, v.source_turn_ids)) - {origin}
                    for v in (
                        *ledger.baselines.values(),
                        *(v for o in ledger.owners.values() for v in o.bodies),
                    )
                }
                for owner_id, owner in ledger.owners.items():
                    if owner_id not in withdrawn:
                        for identifier, sources in owner.sources.items():
                            available_sources.setdefault(identifier, set()).update(
                                set(sources) - {origin}
                            )
                invokes = any(
                    origin in v.source_turn_ids and available_sources.get(v.id)
                    for v in ledger.expected()[0].values()
                )
                op = Operation(
                    "delete",
                    origin,
                    recalculation=rng.choice(("preserve", "unavailable", "fail"))
                    if invokes
                    else "preserve",
                )
                current.pop(origin)
                current = {
                    i: replace(v, reply_to_turn_id=None) if v.reply_to_turn_id == origin else v
                    for i, v in current.items()
                }
            else:
                op = Operation("retry", origin)
        sequence.append(op)
        _advance_ledger(ledger, op)
    return tuple(sequence)


def _advance_ledger(ledger: Ledger, op: Operation) -> None:
    if op.kind == "extract":
        ledger.turns.setdefault(
            op.origin,
            replace(
                turn(op.origin, actor=op.actor, reply_to_turn_id=op.reply_to_turn_id),
                occurred_at=op.occurred_at,
            ),
        )
        ledger.extract(op.origin, op.bodies, op.relations)
    elif op.kind == "retry":
        ledger.withdraw(op.origin)
    elif op.kind == "delete":
        ledger.delete(
            op.origin, recalculation="preserve" if op.recalculation == "fail" else op.recalculation
        )
    else:
        raise ValueError(op)
    ledger.checkpoint()


def fresh_metadata(value: DerivedMemory, *, identifier: str) -> DerivedMemory:
    return replace(
        value,
        id=identifier,
        recorded_at=value.recorded_at + timedelta(seconds=1),
        derivation=replace(
            value.derivation,
            model="synthetic-reextraction-model",
            prompt_version="synthetic-v2",
            created_at=value.derivation.created_at + timedelta(seconds=1),
        ),
        epistemics=replace(
            value.epistemics,
            confidence_features=replace(value.epistemics.confidence_features, retrieval=0.2),
        ),
    )


def event_times(operations: tuple[Operation, ...], rng: Random) -> tuple[Operation, ...]:
    times = {
        op.origin: OCCURRED + timedelta(seconds=rng.randrange(24 * 3600))
        for op in operations
        if op.kind == "extract"
    }
    return tuple(replace(op, occurred_at=times[op.origin]) for op in operations)


def reextraction_sequence(seed: int) -> tuple[Operation, ...]:
    rng = Random(seed)
    root = extract("a", "turn-1", "coffee")
    support = replace(extract("b", "turn-b", "coffee"), actor=rng.choice(("user-a", "user-b")))
    variant = seed % 4
    if variant in (0, 1):
        support = replace(
            support, relations=(MemoryRelation(from_id="b", to_id="c", type="derives"),)
        )
        replacement = fresh_metadata(root.bodies[0], identifier="z")
        if variant == 1:
            replacement = replace(replacement, object="tea", content="別内容の合成記憶")
        return (
            extract("c", "turn-c", "water"),
            root,
            support,
            replace(root, bodies=(replacement,)),
            Operation("retry", "turn-b"),
        )
    if variant == 2:
        c = extract("c", "turn-c", "water")
        derived = replace(root, relations=(MemoryRelation(from_id="a", to_id="c", type="derives"),))
        invalidator = replace(
            extract("d", "turn-d", "juice"),
            relations=(MemoryRelation(from_id="d", to_id="c", type="contradicts"),),
        )
        return (c, derived, invalidator, replace(invalidator, relations=()))
    summary = replace(root.bodies[0], kind="summary", subject=None, predicate=None, object=None)
    ambiguous = replace(root, bodies=(replace(summary, id="z"), replace(summary, id="zz")))
    dependent = replace(
        extract("dependent", "turn-dependent", "juice"),
        relations=(MemoryRelation(from_id="dependent", to_id="a", type="derives"),),
    )
    return (replace(root, bodies=(summary,)), dependent, ambiguous)


def metadata_reextraction_sequence() -> tuple[Operation, ...]:
    """Equivalent synthetic replacement with fresh extraction metadata."""
    base = minimal_sequence("derives", origin_first=True, actor="user-b")
    root = replace(base[1], relations=())
    support = replace(base[2], relations=(MemoryRelation(from_id="b", to_id="c", type="derives"),))
    renamed = fresh_metadata(root.bodies[0], identifier="z")
    return (base[0], root, support, replace(root, bodies=(renamed,)), Operation("retry", "turn-b"))


def withdrawal_sequences() -> tuple[tuple[Operation, ...], ...]:
    """Small regressions found while exploring seeds 160, 1600 and 1792."""
    base = minimal_sequence("derives", origin_first=True, actor="user-b")
    root = replace(base[1], relations=())
    support = base[2]
    renamed = replace(support, bodies=(fresh_metadata(support.bodies[0], identifier="z"),))
    historical = minimal_sequence("extends", origin_first=True, actor="user-a")
    extended = historical[1]
    fresh = replace(
        extended,
        bodies=(fresh_metadata(extended.bodies[0], identifier="z"),),
        relations=(MemoryRelation(from_id="z", to_id="c", type="extends"),),
    )
    return (
        (root, support, Operation("retry", root.origin), renamed),
        (
            *base[:3],
            extract("e", "turn-e", "coffee"),
            base[3],
            Operation("retry", "turn-b"),
            Operation("delete", "turn-b"),
            Operation("delete", "turn-e"),
        ),
        (
            *historical[:2],
            Operation("retry", "turn-1"),
            Operation("retry", "turn-c"),
            fresh,
            Operation("delete", "turn-c"),
        ),
    )


def valid_sequence(operations: tuple[Operation, ...]) -> bool:
    origins: set[str] = set()
    known: set[str] = set()
    deleted: set[str] = set()
    owners: dict[str, str] = {}
    for op in operations:
        if op.kind == "extract":
            if op.origin in deleted or (
                op.reply_to_turn_id is not None and op.reply_to_turn_id not in origins
            ):
                return False
            if any(r.to_id not in known | {v.id for v in op.bodies} for r in op.relations):
                return False
            if any(v.id in owners and owners[v.id] != op.origin for v in op.bodies):
                return False
            origins.add(op.origin)
            known.update(v.id for v in op.bodies)
            owners.update((v.id, op.origin) for v in op.bodies)
        elif op.origin not in origins:
            return False
        elif op.kind == "delete":
            origins.remove(op.origin)
            deleted.add(op.origin)
        elif op.kind != "retry":
            return False
    return True


def shrink(
    operations: tuple[Operation, ...], fails: Callable[[tuple[Operation, ...]], bool]
) -> tuple[Operation, ...]:
    candidate = operations
    while True:
        for index in range(len(candidate)):
            shorter = candidate[:index] + candidate[index + 1 :]
            if shorter and valid_sequence(shorter) and fails(shorter):
                candidate = shorter
                break
        else:
            return candidate
