from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.memory.domain import DeliveryBasis, ExtractionState
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.use_cases import MemoryService
from tests.evidence_support import RECORDED, session, turn
from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities
from tests.memory_fakes import FakeMemoryStore
from tests.memory_support import memory

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("origin", ["baseline", "candidate"])
def test_projection_recounts_support_and_read_does_not_write(origin: str) -> None:
    evidence = FakeEvidenceStore()
    identities = registered_identities()
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(actor="user-a"))
    materials = EvidenceMaterials(
        evidence,
        IdentityCharacters(lambda: identities),
        lambda: RECORDED,
        timedelta(minutes=10),
    )
    inputs = MemoryInputs(
        evidence,
        materials,
        conversation_surfaces=frozenset({"synthetic-chat"}),
        comment_surfaces=frozenset(),
    )
    store = FakeMemoryStore(evidence)
    service = MemoryService(store, inputs, lambda: RECORDED)
    value = replace(memory(), epistemics=replace(memory().epistemics, independent_evidence_count=8))
    with store.transaction("char-b") as tx:
        if origin == "baseline":
            tx.add_memory(value)
        else:
            resolved = inputs.resolve("char-b", ("turn-1",))
            decision = resolved.turns[0].decision
            assert decision is not None
            tx.save_extraction(
                ExtractionState(
                    character_id="char-b",
                    turn_id="turn-1",
                    delivery_basis=DeliveryBasis(decision.basis),
                    signature=resolved.signature(tx.notification_version("turn-1")),
                    status="done",
                    token=None,
                    lease_until=None,
                    deadline=decision.deadline,
                    error=None,
                    used_text=decision.text,
                    receipt_version=decision.receipt_version,
                )
            )
            tx.set_candidates("turn-1", (value,), dependencies=resolved.dependencies())
        service.rebuild(tx)
    saved = store.get_memory("char-b", value.id)
    assert saved is not None
    assert saved.epistemics.independent_evidence_count == 1
    before = dict(store.values), dict(store.states), list(store.relations)
    assert service.active("char-b") == (saved,)
    assert (store.values, store.states, store.relations) == before
    assert replace(saved.epistemics, independent_evidence_count=8) == value.epistemics
