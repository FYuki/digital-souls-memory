"""Fast extraction/withdrawal exploration; SQL author deletion is replayed separately."""

from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.governance.domain import AuthorPrincipal, DeletionCommand
from digital_souls_memory.governance.recalculation import MemoryRecalculator
from digital_souls_memory.governance.use_cases import DeletionService
from digital_souls_memory.memory.domain import ExtractionProposal
from digital_souls_memory.memory.ports import MemoryUnavailable
from digital_souls_memory.memory.projection import collect_evidence, projection_input
from digital_souls_memory.recall.worker import IndexWorker
from tests.evidence_support import RECORDED, turn
from tests.it1.test_memory_worker import setup_worker
from tests.recall_fixtures import SPACE
from tests.recall_support import RecordingEmbedding
from tests.restatement_fakes import SequenceDeletionStore, SequenceIndexStore, SequenceRecalculate
from tests.restatement_model import Ledger, canonical, live
from tests.restatement_sequences import (
    ObservationMismatch,
    Operation,
    compare,
    extract,
    generated_sequence,
    metadata_reextraction_sequence,
    minimal_sequence,
    reextraction_sequence,
    shrink,
    withdrawal_sequences,
)

pytestmark = pytest.mark.it1


@pytest.mark.parametrize(
    "operations", withdrawal_sequences(), ids=["absorbed-id", "baseline-sources", "deleted-target"]
)
def test_withdrawal_exploration_regressions(operations: tuple[Operation, ...]) -> None:
    replay(operations)


def replay(operations: tuple[Operation, ...]) -> None:
    h, store, service, worker = setup_worker()
    ledger = Ledger(RECORDED)
    recalculation = SequenceRecalculate(*(v for op in operations for v in op.bodies))
    deletion = DeletionService(
        SequenceDeletionStore(service, store),
        MemoryRecalculator(
            recalculation,
            delivery_timeout=timedelta(minutes=10),
            non_sensitive_labels=frozenset(),
        ),
        lambda: RECORDED,
    )
    index_store = SequenceIndexStore(store)
    index_worker = IndexWorker(
        index_store, RecordingEmbedding(), space=SPACE, clock=lambda: RECORDED
    )
    for index, op in enumerate(operations):
        context = f"step={index} op={op!r} sequence={operations!r}"
        if op.kind == "extract":
            if op.origin not in ledger.turns:
                source = replace(
                    turn(
                        op.origin,
                        actor=op.actor,
                        addressees=("user-a",) if op.actor != "user-a" else (),
                        address_mode="direct" if op.actor != "user-a" else "broadcast",
                        reply_to_turn_id=op.reply_to_turn_id,
                    ),
                    occurred_at=op.occurred_at,
                )
                h.evidence.add_turn(source)
                ledger.turns[op.origin] = source
            elif store.get_extraction("char-b", op.origin) is not None:
                service.requeue(op.origin)
            h.gateway.fail = False
            h.gateway.output = ExtractionProposal(memories=op.bodies, relations=op.relations)
            worker.process("char-b", op.origin)
            state = store.get_extraction("char-b", op.origin)
            assert state is not None and state.status == "done", (context, state)
            ledger.extract(op.origin, op.bodies, op.relations)
        elif op.kind == "retry":
            service.requeue(op.origin)
            h.gateway.fail = True
            worker.process("char-b", op.origin)
            state = store.get_extraction("char-b", op.origin)
            assert state is not None and state.status == "retry", (context, state)
            ledger.withdraw(op.origin)
        elif op.kind == "delete":
            recalculation.outcome = op.recalculation
            if op.recalculation == "fail":
                with pytest.raises(MemoryUnavailable):
                    deletion.execute(
                        DeletionCommand(request_id=f"delete-{index}", target_turn_ids=(op.origin,)),
                        principal=AuthorPrincipal(author_id="synthetic-author"),
                    )
                with store.transaction("char-b") as tx:
                    assert canonical({v.id: v for v in tx.memories()}) == ledger.saved
                    assert set(tx.applied_relations()) == ledger.expected()[1]
                assert h.evidence.get_turn(op.origin) == ledger.turns[op.origin]
                recalculation.outcome = "preserve"
            result = deletion.execute(
                DeletionCommand(request_id=f"delete-{index}", target_turn_ids=(op.origin,)),
                principal=AuthorPrincipal(author_id="synthetic-author"),
            )
            assert result.status == "done", context
            ledger.delete(op.origin, recalculation=recalculation.outcome)
        else:
            raise ValueError(op)
        expected, relations = ledger.checkpoint()
        with store.transaction("char-b") as tx:
            saved = canonical({v.id: v for v in tx.memories()})
            applied = set(tx.applied_relations())
            snapshot = projection_input(tx)
        compare("saved", context, saved, expected)
        compare("relations", context, applied, relations)
        active = {i: v for i, v in expected.items() if live(v, RECORDED)}
        compare("active", context, canonical({v.id: v for v in service.active("char-b")}), active)
        full = collect_evidence(snapshot, "char-b", service.inputs).project(snapshot, RECORDED)
        compare("full projection", context, canonical(full.memories), expected)
        compare("projected relations", context, set(full.relations), relations)
        for work in index_store.claim(space=SPACE, now=RECORDED, limit=20):
            index_worker.process(work)
        compare(
            "index",
            context,
            canonical(
                {
                    i: v.candidate.memory
                    for i, v in index_store.published.items()
                    if v.candidate.memory is not None
                }
            ),
            active,
        )


@pytest.mark.parametrize("seed", range(128))
def test_generated_extraction_and_retry_match_independent_ledger(seed: int) -> None:
    operations = generated_sequence(seed)
    try:
        replay(operations)
    except ObservationMismatch as original:
        boundary = original.boundary

        def fails(candidate: tuple[Operation, ...]) -> bool:
            try:
                replay(candidate)
            except ObservationMismatch as error:
                return error.boundary == boundary
            return False

        minimized = shrink(operations, fails)
        pytest.fail(f"seed={seed} minimized={minimized!r}\n{original}", pytrace=True)


@pytest.mark.parametrize("seed", range(32))
def test_generated_reextraction_and_dependency_revival_match_ledger(seed: int) -> None:
    replay(reextraction_sequence(seed))


@pytest.mark.parametrize("relation_type", ["derives", "updates", "contradicts"])
@pytest.mark.parametrize("actor", ["user-a", "user-b"])
def test_retry_after_original_source_deletion_withdraws_last_support(
    relation_type: str, actor: str
) -> None:
    base = minimal_sequence(relation_type, origin_first=True, actor=actor)
    replay((*base[:4], Operation("retry", "turn-b")))


def test_metadata_reextraction_preserves_support_and_relation_until_retry() -> None:
    replay(metadata_reextraction_sequence())


@pytest.mark.parametrize("include_context", [False, True])
def test_reply_evidence_count_matches_ledger(include_context: bool) -> None:
    first = extract("a", "one", "coffee")
    second = replace(extract("b", "two", "coffee"), actor="user-b", reply_to_turn_id="one")
    third = replace(
        extract("d", "three", "coffee"),
        actor="user-a",
        reply_to_turn_id="two" if include_context else "one",
    )
    context = first if include_context else replace(first, bodies=())
    replay((context, second, third))


@pytest.mark.parametrize("outcome", ["unavailable", "fail"])
@pytest.mark.parametrize("relation_type", ["derives", "updates", "contradicts"])
def test_deletion_recalculation_failure_and_resend_match_ledger(
    outcome: str, relation_type: str
) -> None:
    base = minimal_sequence(relation_type, origin_first=True, actor="user-b")
    replay((*base[:3], replace(base[3], recalculation=outcome)))


def test_deleting_reply_context_withdraws_support_and_rewritten_relation() -> None:
    base = minimal_sequence("derives", origin_first=True, actor="user-b")
    support = replace(
        base[2],
        reply_to_turn_id="question",
        relations=(replace(base[1].relations[0], from_id="b"),),
    )
    replay(
        (
            base[0],
            replace(base[1], relations=()),
            Operation("extract", "question"),
            support,
            Operation("delete", "question"),
        )
    )
