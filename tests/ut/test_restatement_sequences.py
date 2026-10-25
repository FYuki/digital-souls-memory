"""Direct contracts of the independent ledger, generator and relation withdrawal selection."""

from dataclasses import replace
from datetime import timedelta
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg.rows import DictRow

from digital_souls_memory.adapters.postgres_deletion import PostgresDeletionStore
from digital_souls_memory.memory.domain import MemoryRelation
from digital_souls_memory.memory.ports import MemoryContribution
from tests.evidence_support import RECORDED, turn
from tests.restatement_model import Ledger, canonical, live
from tests.restatement_sequences import (
    ObservationMismatch,
    Operation,
    compare,
    event_times,
    extract,
    generated_sequence,
    minimal_sequence,
    reextraction_sequence,
    shrink,
    valid_sequence,
)

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("ambiguous", [False, True])
def test_withdrawal_selection_resolves_ids_before_comparing_surviving_owners(
    monkeypatch: pytest.MonkeyPatch, retained: bool, ambiguous: bool
) -> None:
    from digital_souls_memory.adapters import postgres_deletion

    source = extract("old", "origin", "coffee").bodies[0]
    target = extract("target", "target-origin", "water").bodies[0]
    history = (MemoryContribution("origin", source), MemoryContribution("target-origin", target))
    current: tuple[MemoryContribution, ...] = (
        MemoryContribution("origin", replace(source, id="new")),
        MemoryContribution("target-origin", replace(target, id="new-target")),
    )
    if ambiguous:
        current += (MemoryContribution("origin", replace(source, id="another")),)
    tx = MagicMock()
    tx.history.return_value = history
    tx.candidates_for_turns.return_value = current
    monkeypatch.setattr(postgres_deletion, "_MemoryTransaction", lambda *args: tx)
    rows = [{"turn_id": "origin", "from_id": "old", "to_id": "target", "type": "derives"}]
    if retained:
        rows.append(
            {"turn_id": "support", "from_id": "new", "to_id": "new-target", "type": "derives"}
        )
    connection = MagicMock()
    connection.execute.return_value.fetchall.side_effect = [
        [{"character_id": "char-b", "turn_id": "origin"}],
        rows,
    ]
    store = PostgresDeletionStore(
        "postgresql://synthetic",
        delivery_timeout=timedelta(minutes=10),
        conversation_surfaces=frozenset(),
        comment_surfaces=frozenset(),
    )
    removed = MemoryRelation(
        from_id="old" if ambiguous else "new", to_id="new-target", type="derives"
    )
    expected = {} if retained and not ambiguous else {"char-b": frozenset({removed})}
    assert (
        store._withdrawn_relations(cast(psycopg.Connection[DictRow], connection), ("origin",))
        == expected
    )


def test_empty_withdrawal_does_not_read_other_characters() -> None:
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = []
    store = PostgresDeletionStore(
        "postgresql://synthetic",
        delivery_timeout=timedelta(minutes=10),
        conversation_surfaces=frozenset(),
        comment_surfaces=frozenset(),
    )
    assert (
        store._withdrawn_relations(cast(psycopg.Connection[DictRow], connection), ("absent",)) == {}
    )
    assert connection.execute.call_count == 1


def test_seed_generation_covers_vocabulary_and_both_deletion_orders() -> None:
    sequences = [generated_sequence(seed) for seed in range(128)]
    assert all(valid_sequence(sequence) and len(sequence) <= 8 for sequence in sequences)
    assert {r.type for sequence in sequences for op in sequence for r in op.relations} == {
        "updates",
        "contradicts",
        "derives",
        "extends",
    }
    assert {
        next(op.origin for op in sequence if op.kind == "delete")
        for sequence in sequences
        if any(op.kind == "delete" for op in sequence)
    } == {"turn-1", "turn-b"}
    variants = [reextraction_sequence(seed) for seed in range(4)]
    assert all(valid_sequence(sequence) for sequence in variants)
    assert any(len(op.bodies) == 2 for sequence in variants for op in sequence)
    assert {op.kind for sequence in variants for op in sequence} == {"extract", "retry"}
    from random import Random

    timed = event_times((extract("a", "origin", "coffee"), Operation("retry", "origin")), Random(3))
    assert timed[0].occurred_at == timed[1].occurred_at
    assert timed[0].bodies == extract("a", "origin", "coffee").bodies


def test_shrinking_keeps_prerequisites_and_the_observed_failure() -> None:
    operations = generated_sequence(86)
    required = minimal_sequence("derives", origin_first=True, actor="user-a")[:4]

    def fails(sequence: tuple[Operation, ...]) -> bool:
        return any(op.kind == "delete" and op.origin == "turn-1" for op in sequence) and any(
            op.kind == "extract" and op.origin == "turn-b" for op in sequence
        )

    minimized = shrink(operations, fails)
    assert valid_sequence(minimized) and fails(minimized)
    assert len(minimized) == len(required)
    assert not valid_sequence((Operation("delete", "missing"),))
    assert not valid_sequence((required[1],))
    compare("saved", "synthetic", 1, 1)
    with pytest.raises(ObservationMismatch) as error:
        compare("saved", "synthetic", 1, 2)
    assert error.value.boundary == "saved"


def test_ledger_counts_opportunity_and_actor_and_preserves_fact_after_origin_deletion() -> None:
    ledger = Ledger(RECORDED)
    operations = minimal_sequence("derives", origin_first=True, actor="user-b")
    for op in operations[:3]:
        ledger.turns[op.origin] = turn(op.origin, actor=op.actor)
        ledger.extract(op.origin, op.bodies, op.relations)
        ledger.checkpoint()
    before, relations = ledger.expected()
    assert before["a"].epistemics.independent_evidence_count == 2
    assert before["a"].source_turn_ids == ("turn-1", "turn-b")
    assert relations == {MemoryRelation(from_id="a", to_id="c", type="derives")}
    ledger.delete("turn-1")
    after, relations = ledger.checkpoint()
    assert after["a"].source_turn_ids == ("turn-b",)
    assert after["a"].epistemics.independent_evidence_count == 1
    assert after["a"].id == before["a"].id and relations == set()
    assert canonical(after) == after and live(after["a"], RECORDED)
    assert not live(replace(after["a"], valid_to=RECORDED), RECORDED)
    ledger.turns["reply"] = turn("reply", actor="user-b", reply_to_turn_id="turn-b")
    assert ledger.count(("turn-b", "reply")) == 1
