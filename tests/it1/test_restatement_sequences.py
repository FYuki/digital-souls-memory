"""Fast extraction/withdrawal exploration; SQL author deletion is replayed separately."""

from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.governance.domain import AuthorPrincipal, DeletionCommand
from digital_souls_memory.governance.recalculation import MemoryRecalculator
from digital_souls_memory.governance.use_cases import DeletionService
from digital_souls_memory.memory.domain import ExtractionProposal
from digital_souls_memory.memory.projection import collect_evidence, projection_input
from digital_souls_memory.recall.worker import IndexWorker
from tests.evidence_support import RECORDED, turn
from tests.it1.test_memory_worker import setup_worker
from tests.postgres.test_restatement_remediation import Recalculate
from tests.recall_fixtures import SPACE
from tests.recall_support import RecordingEmbedding
from tests.restatement_fakes import SequenceDeletionStore, SequenceIndexStore
from tests.restatement_model import Ledger, canonical, live
from tests.restatement_sequences import (
    ObservationMismatch,
    Operation,
    compare,
    generated_sequence,
    reextraction_sequence,
    shrink,
)

pytestmark = pytest.mark.it1


def replay(operations: tuple[Operation, ...]) -> None:
    h, store, service, worker = setup_worker()
    ledger = Ledger(RECORDED)
    deletion = DeletionService(
        SequenceDeletionStore(service, store),
        MemoryRecalculator(
            Recalculate(*(v for op in operations for v in op.bodies)),
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
            result = deletion.execute(
                DeletionCommand(request_id=f"delete-{index}", target_turn_ids=(op.origin,)),
                principal=AuthorPrincipal(author_id="synthetic-author"),
            )
            assert result.status == "done", context
            ledger.delete(op.origin)
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
