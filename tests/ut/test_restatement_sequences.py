"""Direct contracts of the independent ledger, generator and relation withdrawal selection."""

from collections.abc import Iterable, Set
from dataclasses import replace
from datetime import timedelta
from random import Random
from typing import TypeVar, cast
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg.rows import DictRow

from digital_souls_memory.adapters.postgres_deletion import PostgresDeletionStore
from digital_souls_memory.governance.domain import (
    DeletionAttempt,
    DeletionCommand,
    DeletionRequest,
    DeletionSnapshot,
    MemoryCopy,
    MemoryReplacement,
)
from digital_souls_memory.memory.domain import DerivedMemory, MemoryRelation
from digital_souls_memory.memory.ports import MemoryContribution
from tests.evidence_support import OCCURRED, RECORDED, turn
from tests.restatement_model import Ledger, canonical, live, semantic_attributes
from tests.restatement_sequences import (
    ObservationMismatch,
    Operation,
    _advance_ledger,
    ambiguity_reproduction_sequence,
    compare,
    continue_sequence,
    duplicate_fact_sequence,
    effect_reextraction_sequence,
    event_times,
    extends_reply_deletion_sequence,
    extends_target_retry_deletion_sequence,
    extract,
    fresh_metadata,
    generated_sequence,
    metadata_reextraction_sequence,
    minimal_sequence,
    reextraction_sequence,
    revived_fact_sequence,
    shrink,
    valid_sequence,
    withdrawal_sequences,
)

pytestmark = pytest.mark.ut

T = TypeVar("T")


def test_ledger_preserves_extends_history_after_reply_context_deletion() -> None:
    ledger = Ledger(RECORDED)
    operations = extends_reply_deletion_sequence()
    _advance_ledger(ledger, operations[0])
    _advance_ledger(ledger, operations[1])
    assert ledger.saved["d"].superseded_at == RECORDED
    assert ledger.saved["c"].last_reinforced_at == operations[1].occurred_at
    _advance_ledger(ledger, operations[2])
    assert ledger.saved["d"].source_turn_ids == ("reply-source",)
    assert ledger.saved["d"].superseded_at == RECORDED


def test_ledger_excludes_unapplied_extends_time_after_target_retry_and_deletion() -> None:
    ledger = Ledger(RECORDED)
    operations = extends_target_retry_deletion_sequence()
    for op in operations[:2]:
        _advance_ledger(ledger, op)
    assert ledger.saved["c"].last_reinforced_at == operations[1].occurred_at
    _advance_ledger(ledger, operations[2])
    assert ledger.expected()[1] == set()
    _advance_ledger(ledger, operations[3])
    assert ledger.saved["c"].source_turn_ids == ("extension-source",)
    assert ledger.saved["c"].last_reinforced_at is None


def test_ledger_keeps_rewritten_effect_after_reextraction_and_source_deletion() -> None:
    ledger = Ledger(RECORDED)
    for op in effect_reextraction_sequence():
        _advance_ledger(ledger, op)
    assert ledger.saved["replacement"].source_turn_ids == ("support",)
    assert ledger.saved["c"].source_turn_ids == ("support", "target")
    assert ledger.saved["c"].last_reinforced_at == ledger.turns["support"].occurred_at
    assert ledger.expected()[1] == {
        MemoryRelation(from_id="replacement", to_id="c", type="extends")
    }


def test_ledger_keeps_each_duplicate_candidates_temporal_effect() -> None:
    ledger = Ledger(RECORDED)
    for identifier, object_value in (("x", "water"), ("y", "tea")):
        _advance_ledger(ledger, extract(identifier, identifier, object_value))
    first = extract("a", "duplicates", "coffee").bodies[0]
    second = replace(first, id="b", valid_from=OCCURRED + timedelta(seconds=1))
    third = replace(first, id="c", valid_from=OCCURRED + timedelta(seconds=2))
    _advance_ledger(
        ledger,
        Operation(
            "extract",
            "duplicates",
            (third, second, first),
            (
                MemoryRelation(from_id="b", to_id="x", type="updates"),
                MemoryRelation(from_id="c", to_id="y", type="updates"),
            ),
        ),
    )
    assert ledger.saved["x"].valid_to == second.valid_from
    assert ledger.saved["y"].valid_to == third.valid_from
    assert ledger.saved["a"].valid_from == first.valid_from
    assert {v.id for v in ledger.owners["duplicates"].bodies} == {"a"}
    _advance_ledger(ledger, Operation("retry", "duplicates"))
    assert ledger.saved["x"].valid_to is None
    assert ledger.saved["y"].valid_to is None


def test_ledger_selects_revived_fact_without_moving_earlier_contribution() -> None:
    ledger = Ledger(RECORDED)
    for op in revived_fact_sequence():
        _advance_ledger(ledger, op)
    assert ledger.owners["before-revival"].sources == {"z": ("before-revival",)}
    assert ledger.owners["after-revival"].sources == {"x": ("after-revival",)}
    assert set(ledger.saved) == {"x", "z"}
    assert ledger.saved["x"].source_turn_ids == ("after-revival", "source-x")
    assert ledger.saved["z"].source_turn_ids == ("before-revival",)


def test_ledger_deduplicates_new_facts_and_rewrites_later_reference() -> None:
    ledger = Ledger(RECORDED)
    op = duplicate_fact_sequence()[0]
    _advance_ledger(ledger, op)
    owner = ledger.owners[op.origin]
    assert {v.id for v in owner.bodies} == {"a", "d"}
    assert owner.sources == {"a": ("duplicates",)}
    assert owner.relations == (MemoryRelation(from_id="d", to_id="a", type="derives"),)
    assert ledger.saved["a"].epistemics.independent_evidence_count == 1


def test_ledger_unions_distinct_sources_of_duplicate_restatements() -> None:
    ledger = Ledger(RECORDED)
    _advance_ledger(ledger, extract("root", "root-source", "coffee"))
    for identifier in ("one", "two", "origin"):
        ledger.turns[identifier] = turn(identifier, actor="user-a")
    bodies = tuple(
        extract(i, source, "coffee").bodies[0] for i, source in (("a", "one"), ("b", "two"))
    )
    ledger.extract("origin", bodies, ())
    assert set(ledger.owners["origin"].sources["root"]) == {"one", "two"}


def test_seed_175_fixed_sequence_reaches_and_resolves_multiple_active_facts() -> None:
    ledger = Ledger(RECORDED)
    operations = ambiguity_reproduction_sequence()
    assert len(operations) == 35
    for op in operations[:-1]:
        _advance_ledger(ledger, op)
    matches = {
        v.id for v in ledger.saved.values() if live(v, RECORDED) and v.object == "synthetic-4"
    }
    assert matches == {"candidate-15-0", "candidate-22-0"}
    _advance_ledger(ledger, operations[-1])
    assert ledger.owners["origin-34"].sources == {"candidate-15-0": ("origin-34",)}


def test_continuation_completes_the_previously_ambiguous_seed() -> None:
    prefix = minimal_sequence("extends", origin_first=True, actor="user-b")[:3]
    operations = continue_sequence(prefix, Random(175), steps=40)
    assert len(operations) == 40
    assert valid_sequence(operations)


@pytest.mark.parametrize(
    "condition",
    [
        "accepted",
        "missing",
        "inaccessible",
        "receipt",
        "range",
        "deadline",
        "basis",
        "retry",
        "dependencies",
    ],
)
def test_deletion_validates_saved_input_without_reclassifying_surface(condition: str) -> None:
    from digital_souls_memory.adapters.postgres_projection_evidence import _accepted_origins
    from digital_souls_memory.memory.projection import collect_evidence, projection_input
    from digital_souls_memory.memory.use_cases import MemoryService
    from digital_souls_memory.memory.worker import MemoryWorker
    from tests.it1.test_memory_extraction import Harness
    from tests.memory_fakes import FakeMemoryStore
    from tests.memory_support import memory

    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn(actor="user-a"))
    store = FakeMemoryStore(h.evidence)
    service = MemoryService(store, h.extractor.inputs, lambda: h.now)
    MemoryWorker(service, h.extractor).process("char-b", "turn-1")
    resolved = h.extractor.inputs.resolve("char-b", ("turn-1",))
    dependencies = {"turn-1": list(resolved.dependencies())}
    values = {v.turn.turn_id: replace(v, eligible=False) for v in resolved.turns}
    with store.transaction("char-b") as tx:
        snapshot = projection_input(tx)
    value = values["turn-1"]
    decision = value.decision
    assert decision is not None and decision.text is not None
    if condition == "missing":
        values.clear()
    elif condition == "inaccessible":
        values["turn-1"] = replace(value, decision=None)
    elif condition == "receipt":
        values["turn-1"] = replace(value, decision=replace(decision, receipt_version=1))
    elif condition == "range":
        values["turn-1"] = replace(value, decision=replace(decision, text=decision.text[:-1]))
    elif condition == "deadline":
        values["turn-1"] = replace(
            value, decision=replace(decision, deadline=decision.deadline + timedelta(seconds=1))
        )
    elif condition == "basis":
        values["turn-1"] = replace(value, decision=replace(decision, basis="pending"))
    elif condition == "retry":
        state = snapshot.states["turn-1"]
        assert state is not None
        snapshot = replace(snapshot, states={"turn-1": replace(state, status="retry")})
    elif condition == "dependencies":
        dependencies.clear()
    valid = _accepted_origins(snapshot, values, dependencies)
    assert valid == (frozenset({"turn-1"}) if condition == "accepted" else frozenset())
    evidence = collect_evidence(snapshot, "char-b", service.inputs)
    inputs = {
        "turn-1": replace(resolved, turns=tuple(replace(v, eligible=False) for v in resolved.turns))
    }
    evidence = replace(evidence, inputs=inputs)
    assert evidence.project(snapshot, h.now).projected_ids == frozenset()
    projected = replace(evidence, validated_origins=valid).project(snapshot, h.now)
    assert projected.projected_ids == (frozenset({memory().id}) if valid else frozenset())


def test_generator_ledger_tracks_retry_reextraction_and_failed_deletion_retry() -> None:
    ledger = Ledger(RECORDED)
    base = minimal_sequence("derives", origin_first=True, actor="user-b")
    for op in base[:3]:
        _advance_ledger(ledger, op)
    source = ledger.turns["turn-b"]
    _advance_ledger(ledger, Operation("retry", "turn-b"))
    assert ledger.saved["a"].source_turn_ids == ("turn-1",)
    _advance_ledger(ledger, replace(base[2], occurred_at=RECORDED))
    assert ledger.turns["turn-b"] == source
    _advance_ledger(ledger, Operation("delete", "turn-1", recalculation="fail"))
    assert "turn-1" not in ledger.turns
    assert ledger.saved["a"].source_turn_ids == ("turn-b",)


def test_ledger_deletion_removes_relations_to_invalidated_historical_ids() -> None:
    ledger = Ledger(RECORDED)
    base = minimal_sequence("extends", origin_first=True, actor="user-b")
    renamed = replace(base[0], bodies=(fresh_metadata(base[0].bodies[0], identifier="renamed"),))
    for op in (*base[:2], renamed):
        _advance_ledger(ledger, op)
    assert ledger.expected()[1] == {MemoryRelation(from_id="a", to_id="renamed", type="extends")}
    _advance_ledger(ledger, Operation("delete", "turn-c"))
    assert ledger.expected()[1] == set()
    assert ledger.saved["a"].superseded_at is None
    assert ledger.saved["renamed"].superseded_at == RECORDED


def test_ledger_withdraws_reply_contribution_when_its_context_is_deleted() -> None:
    ledger = Ledger(RECORDED)
    root = extract("a", "root", "coffee")
    reply = replace(extract("b", "reply", "coffee"), reply_to_turn_id="root")
    for op in (root, reply, Operation("delete", "root")):
        _advance_ledger(ledger, op)
    assert ledger.saved == {}


def test_ledger_extends_keeps_the_latest_surviving_event_time() -> None:
    ledger = Ledger(RECORDED)
    root = extract("c", "root", "water")
    newer = replace(extract("a", "newer", "coffee"), occurred_at=RECORDED)
    older = extract("b", "older", "tea")
    for op in (root, newer, older):
        if op is not root:
            op = replace(
                op, relations=(MemoryRelation(from_id=op.bodies[0].id, to_id="c", type="extends"),)
            )
        _advance_ledger(ledger, op)
    assert ledger.saved["c"].last_reinforced_at == RECORDED


def test_ledger_recalculated_saved_history_drops_withdrawn_reinforcement_time() -> None:
    ledger = Ledger(RECORDED)
    for op in (
        extract("a", "root", "coffee"),
        extract("b", "support", "coffee"),
        Operation("retry", "root"),
        Operation("retry", "support"),
        Operation("delete", "root"),
    ):
        _advance_ledger(ledger, op)
    assert ledger.saved["a"].source_turn_ids == ("support",)
    assert ledger.saved["a"].last_reinforced_at is None


def test_ledger_withdraws_last_contribution_before_applying_extends() -> None:
    ledger = Ledger(RECORDED)
    base = minimal_sequence("extends", origin_first=True, actor="user-b")
    support = replace(extract("b", "support", "water"), reply_to_turn_id="turn-1")
    for op in (*base[:2], support, Operation("delete", "turn-c"), Operation("delete", "support")):
        _advance_ledger(ledger, op)
    assert "c" not in ledger.saved
    assert ledger.saved["a"].superseded_at is None


@pytest.mark.parametrize("count", [5_000, 10_000, 20_000])
def test_deletion_collects_withdrawn_ids_with_linear_copy_work(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    from digital_souls_memory.adapters import postgres_deletion

    copied = 0

    class MeasuredFrozenSet(frozenset[str]):
        def __new__(cls, values: Iterable[str] = ()) -> "MeasuredFrozenSet":
            nonlocal copied
            result = super().__new__(cls, values)
            copied += len(result)
            return result

        def __or__(self, other: Set[T]) -> frozenset[str | T]:
            nonlocal copied
            copied += len(self) + len(other)
            result = super().__or__(other)
            # Keep observing subsequent unions, including copies of previous results.
            return MeasuredFrozenSet(cast(Iterable[str], result))

    class Collected(Exception):
        pass

    template = extract("a", "origin", "coffee").bodies[0]
    copies = tuple(
        MemoryCopy(
            table="derived_memories",
            memory=replace(template, id=f"fact-{n}", character_id=f"char-{n % 2}"),
        )
        for n in range(count)
    )
    snapshot = DeletionSnapshot(
        target_turn_ids=("origin",),
        copies=copies,
        materials=(),
        references=(),
        fingerprint="synthetic",
        observed_at=RECORDED,
    )
    request = DeletionRequest(
        command=DeletionCommand(request_id="synthetic", target_turn_ids=("origin",)),
        requested_by="synthetic-author",
        target_turn_ids=("origin",),
        status="running",
        cascade_result=None,
        error=None,
        requested_at=RECORDED,
        completed_at=None,
    )
    attempt = DeletionAttempt(request=request, token="synthetic-token", snapshot=snapshot)
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = {
        "status": "running",
        "token": attempt.token,
        "lease_until": RECORDED + timedelta(minutes=5),
        "requested_by": request.requested_by,
        "target_turn_ids": request.target_turn_ids,
    }
    store = PostgresDeletionStore(
        "postgresql://synthetic",
        delivery_timeout=timedelta(minutes=10),
        conversation_surfaces=frozenset(),
        comment_surfaces=frozenset(),
    )
    connection_context = MagicMock()
    connection_context.__enter__.return_value = connection
    monkeypatch.setattr(store, "_connection", lambda: connection_context)
    monkeypatch.setattr(store, "_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(store, "_withdrawn_relations", lambda *args: {})
    monkeypatch.setattr(postgres_deletion, "_request", lambda row: request)
    monkeypatch.setattr(postgres_deletion, "frozenset", MeasuredFrozenSet, raising=False)
    closures: dict[str, frozenset[str]] = {}

    def transaction(conn: object, owner: str) -> MagicMock:
        tx = MagicMock()
        tx.relation_effects.return_value = ()

        def neighbors(ids: frozenset[str], excluded: frozenset[str]) -> frozenset[str]:
            closures[owner] = ids
            return frozenset()

        tx.projection_neighbors.side_effect = neighbors
        return tx

    monkeypatch.setattr(postgres_deletion, "_MemoryTransaction", transaction)
    monkeypatch.setattr(store, "_remove_references", MagicMock(side_effect=Collected))
    with pytest.raises(Collected):
        store.commit(
            attempt,
            replacements=tuple(MemoryReplacement(copy=c, value=None) for c in copies),
            now=RECORDED,
        )
    assert closures == {
        owner: frozenset(c.memory.id for c in copies if c.memory.character_id == owner)
        for owner in ("char-0", "char-1")
    }
    assert copied <= count * 12, (count, copied)


def test_ledger_deleted_extends_source_never_returns_from_saved_history() -> None:
    ledger = Ledger(RECORDED)
    for op in minimal_sequence("extends", origin_first=True, actor="user-a")[:2]:
        ledger.turns[op.origin] = turn(op.origin, actor=op.actor)
        ledger.extract(op.origin, op.bodies, op.relations)
        ledger.checkpoint()
    assert set(ledger.saved["c"].source_turn_ids) == {"turn-c", "turn-1"}
    ledger.delete("turn-c")
    for values, _ in (ledger.expected(), ledger.checkpoint(), ledger.expected()):
        assert values["c"].source_turn_ids == ("turn-1",)
        assert values["c"].epistemics.independent_evidence_count == 1
        assert values["c"].superseded_at == RECORDED
    assert ledger.saved["c"].source_turn_ids == ("turn-1",)


@pytest.mark.parametrize("outcome", ["unavailable", "fail"])
def test_recalculation_records_invocation_before_none_or_failure(outcome: str) -> None:
    from digital_souls_memory.memory.ports import MemoryUnavailable
    from tests.restatement_fakes import SequenceRecalculate
    from tests.ut.test_deletion_recalculation import material, recalculate

    original = replace(
        extract("c", "turn-1", "water").bodies[0], source_turn_ids=("turn-1", "turn-2")
    )
    gateway = SequenceRecalculate(original)
    gateway.outcome = outcome
    if outcome == "fail":
        with pytest.raises(MemoryUnavailable):
            recalculate(original, gateway, (material(),))
    else:
        assert recalculate(original, gateway, (material(),)).value is None
    assert [(identifier, kind) for _, identifier, kind in gateway.calls] == [("c", "fact")]


@pytest.mark.parametrize("relation_type", ["extends", "derives"])
def test_continuation_generates_relations_to_existing_different_facts(relation_type: str) -> None:
    prefix = (extract("c", "turn-c", "water"),)
    witnessed = False
    for seed in range(32):
        sequence = continue_sequence(prefix, Random(seed), steps=40)
        assert valid_sequence(sequence)
        bodies = {v.id: v for op in sequence for v in op.bodies}
        witnessed |= any(
            r.type == relation_type
            and (bodies[r.from_id].subject, bodies[r.from_id].predicate, bodies[r.from_id].object)
            != (bodies[r.to_id].subject, bodies[r.to_id].predicate, bodies[r.to_id].object)
            for op in sequence[len(prefix) :]
            for r in op.relations
        )
    assert witnessed


@pytest.mark.parametrize("outcome", ["unavailable", "fail"])
def test_continuation_generates_deletion_recalculation_outcomes(outcome: str) -> None:
    prefix = minimal_sequence("derives", origin_first=True, actor="user-b")[:3]
    sequences = [continue_sequence(prefix, Random(seed), steps=40) for seed in range(32)]
    assert all(valid_sequence(sequence) for sequence in sequences)
    assert any(
        op.kind == "delete" and op.recalculation == outcome
        for sequence in sequences
        for op in sequence[len(prefix) :]
    )


def test_validation_accepts_distinct_candidate_ids_from_distinct_origins() -> None:
    assert valid_sequence((extract("a", "turn-1", "coffee"), extract("b", "turn-b", "tea")))


def test_validation_rejects_candidate_id_reused_by_another_origin() -> None:
    assert not valid_sequence((extract("a", "turn-1", "coffee"), extract("a", "turn-b", "tea")))


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


@pytest.mark.parametrize("variant", ["metadata", "content", "trust tier"])
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
    ledger.extract("turn-1", (renamed,), ())
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
