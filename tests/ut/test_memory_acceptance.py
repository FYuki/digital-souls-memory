"""Restatement acceptance resolves structured references without changing content."""

from dataclasses import replace

import pytest

from digital_souls_memory.memory.acceptance import accept
from digital_souls_memory.memory.domain import (
    ExtractionProposal,
    MemoryRelation,
    MemoryValidationError,
    RelationEffect,
    SourceContribution,
)
from tests.evidence_support import OCCURRED, RECORDED, turn
from tests.it1.test_memory_worker import setup_worker
from tests.memory_support import memory

pytestmark = pytest.mark.ut


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
    assert len(accepted.effects) == 1


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
            candidate=replace(repeated, id="a"),
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
