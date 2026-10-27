"""Restatement acceptance resolves structured references without changing content."""

from dataclasses import replace
from datetime import timedelta
from itertools import permutations

import pytest

from digital_souls_memory.memory.acceptance import accept
from digital_souls_memory.memory.domain import (
    DerivedMemory,
    ExtractionProposal,
    MemoryRelation,
    MemoryValidationError,
    RelationEffect,
    SourceContribution,
)
from digital_souls_memory.memory.ports import MemoryConflict
from tests.evidence_support import OCCURRED, RECORDED, turn
from tests.it1.test_memory_worker import setup_worker
from tests.memory_support import memory

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("key", ["valid_from", "recorded_at", "id"])
@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_restatement_selects_earliest_fact_independent_of_lookup_order(
    key: str, order: tuple[int, ...]
) -> None:
    h, store, service, _ = setup_worker()
    h.evidence.add_turn(turn("support", actor="user-a"))
    first = replace(memory(), id="z")
    if key == "valid_from":
        first = replace(first, valid_from=OCCURRED - timedelta(seconds=1))
    elif key == "recorded_at":
        first = replace(first, recorded_at=RECORDED - timedelta(seconds=1))
    else:
        first = replace(first, id="a")
    others: tuple[DerivedMemory, ...] = (
        replace(memory(), id="b"),
        replace(memory(), id="c"),
    )
    if key == "valid_from":
        others = tuple(replace(v, recorded_at=RECORDED - timedelta(seconds=1)) for v in others)
    values = (first, *others)
    candidate = replace(memory(), id="repeat", source_turn_ids=("support",))
    with store.transaction("char-b") as tx:
        for index in order:
            tx.add_memory(values[index])
        accepted = accept(
            tx,
            ExtractionProposal(memories=(candidate,)),
            service.inputs.resolve("char-b", ("support",)),
            RECORDED,
        )
    assert accepted.memories == ()
    assert accepted.sources == (
        SourceContribution(
            character_id="char-b",
            fact_id=first.id,
            extraction_turn_id="support",
            source_turn_id="support",
            occurred_at=OCCURRED,
        ),
    )


@pytest.mark.parametrize("order", [("b", "a", "d"), ("d", "b", "a")])
def test_new_duplicate_fact_rewrites_later_reference_and_preserves_literal_content(
    order: tuple[str, ...],
) -> None:
    h, store, service, _ = setup_worker()
    h.evidence.add_turn(turn("turn-1", actor="user-a"))
    content = 'b、`b`、{"from_id":"b"}\n```text\nb\n```\n~~~text\nb'
    first = replace(memory(), id="a")
    later = replace(first, id="b")
    summary = replace(memory("summary"), id="d", content=content)
    by_id = {v.id: v for v in (first, later, summary)}
    with store.transaction("char-b") as tx:
        accepted = accept(
            tx,
            ExtractionProposal(
                memories=tuple(by_id[i] for i in order),
                relations=(MemoryRelation(from_id="d", to_id="b", type="derives"),),
            ),
            service.inputs.resolve("char-b", ("turn-1",)),
            RECORDED,
        )
    assert accepted.memories == (first, summary)
    assert accepted.sources == (
        SourceContribution(
            character_id="char-b",
            fact_id="a",
            extraction_turn_id="turn-1",
            source_turn_id="turn-1",
            occurred_at=OCCURRED,
        ),
    )
    assert accepted.relations == (MemoryRelation(from_id="d", to_id="a", type="derives"),)
    assert next(v.content for v in accepted.memories if v.id == "d") == content


def test_dependency_precedes_candidate_id_when_choosing_duplicate_fact() -> None:
    h, store, service, _ = setup_worker()
    h.evidence.add_turn(turn("turn-1", actor="user-a"))
    first = replace(memory(), id="a")
    dependency = replace(first, id="z")
    with store.transaction("char-b") as tx:
        accepted = accept(
            tx,
            ExtractionProposal(
                memories=(first, dependency),
                relations=(MemoryRelation(from_id="a", to_id="z", type="derives"),),
            ),
            service.inputs.resolve("char-b", ("turn-1",)),
            RECORDED,
        )
    assert accepted.memories == (dependency,)
    assert {v.fact_id for v in accepted.sources} == {"z"}
    assert accepted.relations == ()
    assert accepted.effects == ()


def test_ready_candidates_are_accepted_by_id_after_dependencies() -> None:
    h, store, service, _ = setup_worker()
    h.evidence.add_turn(turn("turn-1", actor="user-a"))
    a, b, z = (replace(memory("summary"), id=i) for i in ("a", "b", "z"))
    with store.transaction("char-b") as tx:
        accepted = accept(
            tx,
            ExtractionProposal(
                memories=(a, z, b),
                relations=(MemoryRelation(from_id="a", to_id="z", type="derives"),),
            ),
            service.inputs.resolve("char-b", ("turn-1",)),
            RECORDED,
        )
    assert accepted.memories == (b, z, a)


def test_later_duplicate_owns_its_rewritten_relation_effect() -> None:
    h, store, service, _ = setup_worker()
    h.evidence.add_turn(turn("turn-1", actor="user-a"))
    first = replace(memory(), id="a")
    later = replace(first, id="b", epistemics=replace(first.epistemics, trust_tier="A"))
    with store.transaction("char-b") as tx:
        tx.add_memory(replace(memory(), id="x", object="water"))
        accepted = accept(
            tx,
            ExtractionProposal(
                memories=(later, first),
                relations=(MemoryRelation(from_id="b", to_id="x", type="updates"),),
            ),
            service.inputs.resolve("char-b", ("turn-1",)),
            RECORDED,
        )
    assert accepted.memories == (first,)
    assert accepted.relations == ()
    assert accepted.effects == (
        RelationEffect(
            turn_id="turn-1",
            relation=MemoryRelation(from_id="a", to_id="x", type="updates"),
            candidate=later,
        ),
    )


def test_duplicate_candidate_still_rejects_identifier_owned_by_another_origin() -> None:
    h, store, service, _ = setup_worker()
    for identifier in ("turn-1", "support"):
        h.evidence.add_turn(turn(identifier, actor="user-a"))
    with store.transaction("char-b") as tx:
        tx.add_memory(replace(memory(), id="b", object="tea"))
        with pytest.raises(MemoryConflict):
            accept(
                tx,
                ExtractionProposal(
                    memories=tuple(
                        replace(memory(), id=i, source_turn_ids=("support",)) for i in ("a", "b")
                    )
                ),
                service.inputs.resolve("char-b", ("support",)),
                RECORDED,
            )


def test_restatement_does_not_bypass_candidate_dependency_cycle_rejection() -> None:
    h, store, service, _ = setup_worker()
    for identifier in ("turn-1", "turn-2"):
        h.evidence.add_turn(turn(identifier, actor="user-a"))
    root = replace(memory(), id="a")
    repeated = replace(root, id="b", source_turn_ids=("turn-2",))
    dependent = replace(memory("summary"), id="d", source_turn_ids=("turn-2",))
    with store.transaction("char-b") as tx:
        tx.add_memory(root)
        with pytest.raises(MemoryValidationError):
            accept(
                tx,
                ExtractionProposal(
                    memories=(repeated, dependent),
                    relations=(
                        MemoryRelation(from_id="b", to_id="d", type="derives"),
                        MemoryRelation(from_id="d", to_id="b", type="derives"),
                    ),
                ),
                service.inputs.resolve("char-b", ("turn-2",)),
                RECORDED,
            )


def test_restatements_in_one_extraction_share_unique_sources_and_relations() -> None:
    h, store, service, _ = setup_worker()
    for identifier in ("turn-1", "turn-2"):
        h.evidence.add_turn(turn(identifier, actor="user-a"))
    root = replace(memory(), id="a")
    first = replace(root, id="b", source_turn_ids=("turn-2",))
    second = replace(first, id="c")
    summary = replace(memory("summary"), id="d", source_turn_ids=("turn-2",))
    with store.transaction("char-b") as tx:
        tx.add_memory(root)
        accepted = accept(
            tx,
            ExtractionProposal(
                memories=(first, second, summary),
                relations=(
                    MemoryRelation(from_id="d", to_id="b", type="derives"),
                    MemoryRelation(from_id="d", to_id="c", type="derives"),
                    MemoryRelation(from_id="b", to_id="x", type="derives"),
                    MemoryRelation(from_id="c", to_id="x", type="derives"),
                ),
            ),
            service.inputs.resolve("char-b", ("turn-2",)),
            RECORDED,
        )
    assert len(accepted.sources) == 1
    assert accepted.relations == (MemoryRelation(from_id="d", to_id="a", type="derives"),)
    assert {v.candidate.id for v in accepted.effects} == {"b", "c"}
    assert {v.relation for v in accepted.effects} == {
        MemoryRelation(from_id="a", to_id="x", type="derives")
    }


@pytest.mark.parametrize("relation_type", ["derives", "updates", "contradicts", "extends"])
def test_acceptance_records_source_event_and_owns_rewritten_relation(relation_type: str) -> None:
    h, store, service, _ = setup_worker()
    h.evidence.add_turn(turn("turn-1", actor="user-a"))
    h.evidence.add_turn(turn("turn-2", actor="user-a"))
    existing = replace(memory(), id="a")
    repeated = replace(existing, id="b", source_turn_ids=("turn-2",))
    relation = MemoryRelation(from_id="b", to_id="x", type=relation_type)
    with store.transaction("char-b") as tx:
        tx.add_memory(existing)
        resolved = service.inputs.resolve("char-b", ("turn-2",))
        accepted = accept(
            tx, ExtractionProposal(memories=(repeated,), relations=(relation,)), resolved, RECORDED
        )
    assert accepted.memories == ()
    assert accepted.relations == ()
    assert accepted.sources == (
        SourceContribution(
            character_id="char-b",
            fact_id="a",
            extraction_turn_id="turn-2",
            source_turn_id="turn-2",
            occurred_at=OCCURRED,
        ),
    )
    assert accepted.effects == (
        RelationEffect(
            turn_id="turn-2",
            relation=replace(relation, from_id="a"),
            candidate=repeated,
        ),
    )


def test_acceptance_preserves_text_and_removes_relation_to_same_fact() -> None:
    h, store, service, _ = setup_worker()
    for identifier in ("turn-1", "turn-2"):
        h.evidence.add_turn(turn(identifier, actor="user-a"))
    existing = replace(memory(), id="a")
    repeated = replace(existing, id="b", source_turn_ids=("turn-2",))
    summary = replace(memory("summary"), id="d", content="b", source_turn_ids=("turn-2",))
    with store.transaction("char-b") as tx:
        tx.add_memory(existing)
        accepted = accept(
            tx,
            ExtractionProposal(
                memories=(repeated, summary),
                relations=(
                    MemoryRelation(from_id="d", to_id="b", type="derives"),
                    MemoryRelation(from_id="b", to_id="a", type="extends"),
                ),
            ),
            service.inputs.resolve("char-b", ("turn-2",)),
            RECORDED,
        )
    assert accepted.memories == (summary,)
    assert accepted.relations == (MemoryRelation(from_id="d", to_id="a", type="derives"),)
    assert accepted.effects == ()


def test_rewritten_effect_resolves_equivalent_target_reextraction_and_can_be_withdrawn() -> None:
    h, store, service, worker = setup_worker()
    old = replace(memory(), id="x", source_turn_ids=("turn-x",))
    current = replace(memory(), id="a", object="tea", valid_from=RECORDED)
    for candidate in (old, current):
        source = candidate.source_turn_ids[0]
        h.evidence.add_turn(turn(source, actor="user-a"))
        h.gateway.output = (candidate,)
        worker.process("char-b", source)
    h.evidence.add_turn(turn("turn-2", actor="user-a"))
    h.gateway.output = ExtractionProposal(
        memories=(replace(current, id="b", source_turn_ids=("turn-2",)),),
        relations=(MemoryRelation(from_id="b", to_id="x", type="updates"),),
    )
    worker.process("char-b", "turn-2")
    service.requeue("turn-x")
    h.gateway.output = (replace(old, id="replacement"),)
    worker.process("char-b", "turn-x")
    replacement = store.get_memory("char-b", "replacement")
    assert replacement is not None and replacement.valid_to == RECORDED
    with store.list_relations("char-b", "a") as relations:
        assert tuple(relations) == (
            MemoryRelation(from_id="a", to_id="replacement", type="updates"),
        )
    service.requeue("turn-2")
    replacement = store.get_memory("char-b", "replacement")
    assert replacement == replace(old, id="replacement")
    with store.list_relations("char-b", "a") as relations:
        assert tuple(relations) == ()
