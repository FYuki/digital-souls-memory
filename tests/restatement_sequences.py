"""Reproducible short sequences and precondition-preserving failure shrinking."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from random import Random

from digital_souls_memory.memory.domain import DerivedMemory, MemoryRelation
from tests.evidence_support import OCCURRED
from tests.memory_support import memory


class ObservationMismatch(AssertionError):
    def __init__(self, boundary: str, context: str, actual: object, expected: object) -> None:
        self.boundary = boundary
        super().__init__(
            f"{context}\nboundary={boundary}\nactual={actual!r}\nexpected={expected!r}"
        )


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
        return event_times(
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
    sequence = list(
        minimal_sequence(
            relation_type,
            origin_first=bool(rng.randrange(2)),
            actor=rng.choice(("user-a", "user-b")),
        )
    )
    support = sequence[2]
    sequence[3:3] = [Operation("retry", "turn-b"), support]
    if rng.randrange(2):
        sequence.insert(3, replace(extract("e", "turn-e", "coffee"), actor="user-b"))
    return event_times(tuple(sequence), rng)


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
        replacement = replace(root.bodies[0], id="z")
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


def valid_sequence(operations: tuple[Operation, ...]) -> bool:
    origins: set[str] = set()
    known: set[str] = set()
    for op in operations:
        if op.kind == "extract":
            if any(r.to_id not in known for r in op.relations):
                return False
            origins.add(op.origin)
            known.update(v.id for v in op.bodies)
        elif op.origin not in origins:
            return False
        elif op.kind == "delete":
            origins.remove(op.origin)
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
