"""Direct contracts of the independent ledger, generator and relation withdrawal selection."""

from dataclasses import replace
from datetime import timedelta
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg.rows import DictRow

from digital_souls_memory.adapters.postgres_deletion import PostgresDeletionStore
from digital_souls_memory.memory.domain import DerivedMemory, MemoryRelation
from digital_souls_memory.memory.ports import MemoryContribution
from tests.evidence_support import RECORDED, turn
from tests.restatement_model import Ledger, canonical, live, semantic_attributes
from tests.restatement_sequences import (
    ObservationMismatch,
    Operation,
    compare,
    event_times,
    extract,
    fresh_metadata,
    generated_sequence,
    metadata_reextraction_sequence,
    minimal_sequence,
    reextraction_sequence,
    shrink,
    valid_sequence,
    withdrawal_sequences,
)

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "operations", withdrawal_sequences(), ids=["absorbed-id", "baseline-sources", "deleted-target"]
)
def test_withdrawal_regressions_keep_only_supported_values(
    operations: tuple[Operation, ...],
) -> None:
    ledger = Ledger(RECORDED)
    for op in operations:
        if op.kind == "extract":
            ledger.turns.setdefault(op.origin, turn(op.origin, actor=op.actor))
            ledger.extract(op.origin, op.bodies, op.relations)
        elif op.kind == "retry":
            ledger.withdraw(op.origin)
        else:
            ledger.delete(op.origin)
        ledger.checkpoint()
    values, _ = ledger.expected()
    assert "b" not in values
    if operations[-1].origin == "turn-e":
        assert set(values) == {"c"}
    else:
        assert live(values["z"], RECORDED)
        assert not live(values["a"], RECORDED)


def test_fresh_metadata_preserves_meaning_and_changes_extraction_metadata() -> None:
    original = extract("a", "origin", "coffee").bodies[0]
    renamed = fresh_metadata(original, identifier="z")
    assert renamed.id == "z"
    assert semantic_attributes(renamed) == semantic_attributes(original)
    assert renamed.derivation != original.derivation
    assert renamed.recorded_at != original.recorded_at
    assert renamed.epistemics.confidence_features != original.epistemics.confidence_features


def test_fake_transaction_restores_separate_body_sources_and_temporal_origin() -> None:
    from digital_souls_memory.memory.ports import MemoryUnavailable
    from tests.it1.test_memory_extraction import Harness
    from tests.memory_fakes import FakeMemoryStore

    store = FakeMemoryStore(Harness().evidence)
    original = extract("a", "origin", "coffee").bodies[0]
    key = ("char-b", "a")
    with store.transaction("char-b") as tx:
        tx.add_memory(original)
    store.fail_commit = True
    with pytest.raises(MemoryUnavailable), store.transaction("char-b") as tx:
        store.body_sources[key] = ()
        store.temporal_bases[key] = replace(original, last_reinforced_at=RECORDED)
        assert tx.fact_sources(frozenset({"a"})) == {"a": ()}
        assert tx.temporal_origins(frozenset({"a"}))["a"]["last_reinforced_at"] == RECORDED
        assert tx.baselines() == (original,)
    with store.transaction("char-b") as tx:
        assert tx.fact_sources(frozenset({"a"})) == {"a": ("origin",)}
        assert tx.temporal_origins(frozenset({"a"}))["a"]["last_reinforced_at"] is None
        assert tx.baselines() == (original,)


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
    assert all(valid_sequence(sequence) and len(sequence) <= 50 for sequence in sequences)
    assert {r.type for sequence in sequences for op in sequence for r in op.relations} == {
        "updates",
        "contradicts",
        "derives",
        "extends",
    }
    assert {"turn-1", "turn-b"} <= {
        next(op.origin for op in sequence if op.kind == "delete")
        for sequence in sequences
        if any(op.kind == "delete" for op in sequence)
    }
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


@pytest.mark.parametrize("variant", ["metadata", "content", "trust tier", "ambiguous"])
def test_ledger_transfers_only_unambiguous_semantically_equivalent_reextraction(
    variant: str,
) -> None:
    operations = metadata_reextraction_sequence()
    ledger = Ledger(RECORDED)
    for op in operations[:3]:
        ledger.turns[op.origin] = turn(op.origin, actor=op.actor)
        ledger.extract(op.origin, op.bodies, op.relations)
        ledger.checkpoint()
    before, relations = ledger.expected()
    assert before["a"].source_turn_ids == ("turn-1", "turn-b")
    assert relations == {MemoryRelation(from_id="a", to_id="c", type="derives")}
    renamed = operations[3].bodies[0]
    if variant == "content":
        renamed = replace(renamed, content="異なる合成本文")
    elif variant == "trust tier":
        renamed = replace(renamed, epistemics=replace(renamed.epistemics, trust_tier="A"))
    candidates = (renamed, replace(renamed, id="zz")) if variant == "ambiguous" else (renamed,)
    ledger.extract("turn-1", candidates, ())
    after, relations = ledger.checkpoint()
    transferred = variant == "metadata"
    assert after["z"].source_turn_ids == (("turn-1", "turn-b") if transferred else ("turn-1",))
    assert after["z"].epistemics.independent_evidence_count == (2 if transferred else 1)
    assert after["z"].last_reinforced_at == (
        ledger.turns["turn-b"].occurred_at if transferred else None
    )
    assert relations == (
        {MemoryRelation(from_id="z", to_id="c", type="derives")} if transferred else set()
    )
    ledger.withdraw("turn-b")
    retried, relations = ledger.checkpoint()
    assert retried["z"].source_turn_ids == ("turn-1",)
    assert retried["z"].last_reinforced_at is None
    assert retried["z"].epistemics.independent_evidence_count == 1
    assert relations == set()


@pytest.mark.parametrize(
    ("reply_targets", "actors", "sources", "expected"),
    [
        ((None, "one", "two"), ("user-a", "user-b", "user-c"), ("one", "two", "three"), 1),
        ((None, "one", "one"), ("user-c", "user-a", "user-b"), ("two", "three"), 2),
        ((None, "one", None), ("user-a", "user-b", "user-a"), ("one", "two", "three"), 2),
    ],
    ids=["reply-chain", "shared-context-outside-sources", "maximum-compatible-subset"],
)
def test_ledger_counts_only_reply_components_inside_sources(
    reply_targets: tuple[str | None, ...],
    actors: tuple[str, ...],
    sources: tuple[str, ...],
    expected: int,
) -> None:
    ledger = Ledger(RECORDED)
    for identifier, actor, reply in zip(
        ("one", "two", "three"), actors, reply_targets, strict=True
    ):
        ledger.turns[identifier] = turn(identifier, actor=actor, reply_to_turn_id=reply)
    assert ledger.count(sources) == expected


def test_ledger_keeps_saved_baseline_when_last_contribution_is_retried() -> None:
    ledger = Ledger(RECORDED)
    for op in minimal_sequence("derives", origin_first=True, actor="user-b")[:3]:
        ledger.turns[op.origin] = turn(op.origin, actor=op.actor)
        ledger.extract(op.origin, op.bodies, op.relations)
        ledger.checkpoint()
    before, _ = ledger.expected()
    assert live(before["a"], RECORDED)
    ledger.delete("turn-1")
    deleted, _ = ledger.checkpoint()
    assert deleted["a"].source_turn_ids == ("turn-b",)
    assert live(deleted["a"], RECORDED)
    baseline = deleted["a"]
    assert ledger.baselines["a"] == baseline
    assert ledger.baseline_sources["a"] == ()
    ledger.withdraw("turn-b")
    retried, relations = ledger.checkpoint()
    assert retried["a"].superseded_at == RECORDED
    assert not live(retried["a"], RECORDED)
    assert relations == set()
    assert ledger.baselines["a"] == baseline


def test_generated_sequences_continue_after_deletion_with_live_retries() -> None:
    sequences = [generated_sequence(seed) for seed in range(16)]
    assert any(30 <= len(sequence) <= 50 for sequence in sequences)
    witnessed_retry = False
    for sequence in sequences:
        assert valid_sequence(sequence)
        live_origins: set[str] = set()
        deleted_origins: set[str] = set()
        for op in sequence:
            if op.kind == "extract":
                assert op.origin not in deleted_origins
                live_origins.add(op.origin)
            elif op.kind == "delete":
                live_origins.remove(op.origin)
                deleted_origins.add(op.origin)
            elif op.kind == "retry":
                assert op.origin in live_origins
                witnessed_retry |= bool(deleted_origins)
    assert witnessed_retry


def test_generated_sequences_include_different_trust_tiers() -> None:
    tiers = {
        body.epistemics.trust_tier
        for seed in range(128)
        for op in generated_sequence(seed)
        for body in op.bodies
    }
    assert len(tiers) > 1


def test_generated_sequences_include_replies_to_existing_turns() -> None:
    witnessed_reply = False
    for seed in range(128):
        live_origins: set[str] = set()
        for op in generated_sequence(seed):
            if op.kind == "extract":
                if op.reply_to_turn_id is not None:
                    assert op.reply_to_turn_id in live_origins
                    witnessed_reply = True
                live_origins.add(op.origin)
            elif op.kind == "delete":
                live_origins.remove(op.origin)
    assert witnessed_reply


def test_generated_deletions_cover_recalculation_failure_modes() -> None:
    outcomes = {
        op.recalculation
        for seed in range(128)
        for op in generated_sequence(seed)
        if op.kind == "delete"
    }
    assert {"preserve", "unavailable", "fail"} <= outcomes


def test_sequence_validation_and_shrinking_preserve_reply_prerequisites() -> None:
    context = replace(extract("context", "context", "water"), bodies=())
    reply = replace(extract("a", "reply", "coffee"), reply_to_turn_id="context")
    assert not valid_sequence((reply,))
    sequence = (context, extract("unrelated", "unrelated", "tea"), reply)
    minimized = shrink(sequence, lambda ops: reply in ops)
    assert valid_sequence(minimized)
    assert minimized == (context, reply)


def test_generated_reextractions_change_extraction_metadata() -> None:
    changed = False
    for seed in range(32):
        history: dict[str, tuple[DerivedMemory, ...]] = {}
        for op in reextraction_sequence(seed):
            for body in op.bodies:
                for previous in history.get(op.origin, ()):
                    if previous.id != body.id and previous.content == body.content:
                        changed |= (
                            previous.derivation != body.derivation
                            and previous.recorded_at != body.recorded_at
                            and previous.epistemics.confidence_features
                            != body.epistemics.confidence_features
                        )
            if op.kind == "extract":
                history[op.origin] = op.bodies
    assert changed


@pytest.mark.parametrize("historical", [False, True], ids=["current-ids", "historical-ids"])
def test_generated_new_origins_do_not_reuse_other_origins_candidate_ids(historical: bool) -> None:
    for seed in range(32):
        for sequence in (generated_sequence(seed), reextraction_sequence(seed)):
            owners: dict[str, str] = {}
            by_origin: dict[str, set[str]] = {}
            for op in sequence:
                if op.kind == "extract":
                    for body in op.bodies:
                        assert body.id not in owners or owners[body.id] == op.origin
                    if not historical:
                        for identifier in by_origin.get(op.origin, set()):
                            owners.pop(identifier)
                    by_origin[op.origin] = {body.id for body in op.bodies}
                    owners.update((body.id, op.origin) for body in op.bodies)
                elif op.kind == "delete" and not historical:
                    for identifier in by_origin.pop(op.origin):
                        owners.pop(identifier)
