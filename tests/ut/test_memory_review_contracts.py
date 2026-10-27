from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from digital_souls_memory.memory.changes import update_fact
from digital_souls_memory.memory.domain import (
    ExtractionProposal,
    InputDependency,
    MemoryRelation,
    MemoryValidationError,
)
from digital_souls_memory.recall.eligibility import memory_allowed
from tests.evidence_support import OCCURRED, turn
from tests.it1.test_memory_worker import setup_worker
from tests.memory_support import NEW_ID, memory

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("old_source", ["inferred", "reflection"])
@pytest.mark.parametrize("new_source", ["inferred", "reflection", "direct_user", "observed"])
def test_fact_update_rejects_model_trust_promotion(old_source: str, new_source: str) -> None:
    old = replace(memory(), epistemics=replace(memory().epistemics, source_type=old_source))
    candidate = replace(
        old,
        id=NEW_ID,
        object="tea",
        content="紅茶",
        source_turn_ids=("turn-2",),
        valid_from=OCCURRED + timedelta(seconds=1),
        epistemics=replace(old.epistemics, source_type=new_source, trust_tier="A"),
    )
    with pytest.raises(MemoryValidationError):
        update_fact(old, candidate)
    assert old.epistemics.trust_tier == "D" and old.valid_to is None


@pytest.mark.parametrize("tier", ["D", "E", "F"])
def test_fact_update_preserves_same_or_lower_trust(tier: str) -> None:
    old = memory()
    candidate = replace(
        old,
        id=NEW_ID,
        object="tea",
        content="紅茶",
        source_turn_ids=("turn-2",),
        valid_from=OCCURRED + timedelta(seconds=1),
        epistemics=replace(old.epistemics, trust_tier=tier),
    )
    closed, added = update_fact(old, candidate)
    assert closed.valid_to == candidate.valid_from
    assert added == candidate


@pytest.mark.parametrize("end_field", ["valid_to", "superseded_at", "none"])
def test_active_and_recall_follow_time_boundaries_in_same_service(end_field: str) -> None:
    h, store, service, _ = setup_worker()
    h.evidence.add_turn(turn(actor="user-a"))
    start = h.now + timedelta(seconds=1)
    end = start + timedelta(seconds=2)
    value = replace(
        memory(),
        valid_from=start,
        valid_to=end if end_field == "valid_to" else None,
        superseded_at=end if end_field == "superseded_at" else None,
    )
    with store.transaction("char-b") as tx:
        tx.add_memory(value)
    for at, expected in (
        (start - timedelta(microseconds=1), False),
        (start, True),
        (end - timedelta(microseconds=1), True),
        (end, end_field == "none"),
    ):
        h.now = at
        assert memory_allowed(value, character_id="char-b", now=at) is expected
        assert service.active("char-b") == ((value,) if expected else ())


def test_active_evaluates_all_memories_at_one_time(monkeypatch: pytest.MonkeyPatch) -> None:
    h, store, service, _ = setup_worker()
    h.evidence.add_turn(turn(actor="user-a"))
    start = h.now
    first = replace(memory("summary"), valid_from=start)
    second = replace(first, id=NEW_ID, valid_from=start + timedelta(seconds=1))
    with store.transaction("char-b") as tx:
        tx.add_memory(first)
        tx.add_memory(second)
    calls: list[datetime] = []

    def ticking_clock() -> datetime:
        calls.append(start + timedelta(seconds=len(calls)))
        return calls[-1]

    monkeypatch.setattr(service, "clock", ticking_clock)
    assert service.active("char-b") == (first,)
    assert calls == [start]


@pytest.mark.parametrize("used_end", [2, 3])
def test_baseline_range_correction_is_applied_to_reads_and_scoped_rebuild(used_end: int) -> None:
    h, store, service, _ = setup_worker()
    h.evidence.add_turn(turn(actor="user-a", text="OK"))
    value = memory("summary")
    store.baseline_inputs[("char-b", value.id)] = (
        InputDependency(
            turn_id="turn-1",
            basis="original",
            receipt_version=0,
            available_end=3,
            used_end=used_end,
            deadline=h.now,
        ),
    )
    with store.transaction("char-b") as tx:
        tx.add_memory(value)
        affected = service.affected(tx, "turn-1")
        assert value.id in affected
    assert service.active("char-b") == ((value,) if used_end == 2 else ())
    assert store.get_memory("char-b", value.id) == value
    with store.transaction("char-b") as tx:
        service.rebuild(tx, affected)
    saved = store.get_memory("char-b", value.id)
    assert saved is not None and (saved.superseded_at is not None) == (used_end == 3)


@pytest.mark.parametrize("applied", [True, False])
def test_only_applied_update_can_reconstruct_a_closed_lower_tier_baseline(applied: bool) -> None:
    h, store, service, worker = setup_worker()
    h.evidence.add_turn(turn(actor="user-a"))
    original = memory()
    with store.transaction("char-b") as tx:
        tx.add_memory(original)
    h.evidence.add_turn(replace(turn("turn-2", actor="user-a"), occurred_at=h.now))
    candidate = replace(
        original, id=NEW_ID, object="tea", source_turn_ids=("turn-2",), valid_from=h.now
    )
    relation = MemoryRelation(from_id=NEW_ID, to_id=original.id, type="updates")
    h.gateway.output = ExtractionProposal(memories=(candidate,), relations=(relation,))
    worker.process("char-b", "turn-2")
    revised = replace(
        original,
        valid_to=candidate.valid_from,
        epistemics=replace(original.epistemics, trust_tier="E"),
    )
    with store.transaction("char-b") as tx:
        tx.replace_memory(revised)
    if not applied:
        store.relations.clear()
    before = dict(store.values)
    if applied:
        assert service.active("char-b") == (candidate,)
        for identifiers in (None, frozenset({original.id, candidate.id})):
            with store.transaction("char-b") as tx:
                service.rebuild(tx, identifiers)
        assert store.get_memory("char-b", original.id) == revised
    else:
        with pytest.raises(MemoryValidationError):
            service.active("char-b")
        with pytest.raises(MemoryValidationError), store.transaction("char-b") as tx:
            service.rebuild(tx)
        assert store.values == before
