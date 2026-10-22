"""Extraction bookkeeping must not become another store of original text."""

from dataclasses import asdict, replace
from hashlib import sha256

import pytest

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.memory.extraction import MemoryExtractor
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.memory.worker import MemoryWorker
from tests.evidence_support import RECORDED, TEXT, receipt, session, turn
from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities
from tests.memory_fakes import EchoGateway, FakeMemoryStore

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "delivery,end", [("full", len(TEXT)), ("partial", 9), ("none", 0), ("pending", None)]
)
def test_extraction_state_retains_delivery_metadata_without_original_copy(
    delivery: str,
    end: int | None,
) -> None:
    from datetime import timedelta

    evidence = FakeEvidenceStore()
    evidence.add_session(session())
    evidence.add_turn(turn())
    if end is not None:
        evidence.add_receipt(receipt(delivery, end))
    identities = registered_identities()
    materials = EvidenceMaterials(
        evidence, IdentityCharacters(lambda: identities), lambda: RECORDED, timedelta(minutes=10)
    )
    inputs = MemoryInputs(
        evidence, materials, conversation_surfaces=frozenset(), comment_surfaces=frozenset()
    )
    store = FakeMemoryStore(evidence)
    gateway = EchoGateway()
    worker = MemoryWorker(
        MemoryService(store, inputs, lambda: RECORDED),
        MemoryExtractor(inputs=inputs, gateway=gateway, non_sensitive_labels=frozenset()),
    )
    worker.process("char-b", "turn-1")
    saved = store.get_extraction("char-b", "turn-1")
    assert saved is not None
    record = asdict(saved)
    assert "used_text" not in record
    expected_hash = None if end is None else sha256(TEXT[:end].encode("utf-8")).hexdigest()
    assert record["text_hash"] == expected_hash
    assert record["used_end"] == end
    assert TEXT not in record.values()
    if end:
        assert TEXT[:end] not in record.values()
    assert saved.receipt_version == (0 if end is None else 1)
    assert saved.delivery_basis == ("pending" if end is None else "receipt_confirmed")
    assert saved.deadline == RECORDED + timedelta(minutes=10)
    assert evidence.get_turn("turn-1") == turn()


def test_input_fingerprint_distinguishes_equal_length_unicode_text() -> None:
    from datetime import timedelta

    evidence = FakeEvidenceStore()
    evidence.add_session(session(reports_delivery=False))
    source = turn(text="合成の青")
    evidence.add_turn(source)
    identities = registered_identities()
    inputs = MemoryInputs(
        evidence,
        EvidenceMaterials(
            evidence,
            IdentityCharacters(lambda: identities),
            lambda: RECORDED,
            timedelta(minutes=10),
        ),
        conversation_surfaces=frozenset(),
        comment_surfaces=frozenset(),
    )
    before = inputs.resolve("char-b", (source.turn_id,)).signature(1)
    # Replacement only builds a counterexample; the production ledger remains append-only.
    evidence.turns[source.turn_id] = replace(source, text="合成の赤")
    assert inputs.resolve("char-b", (source.turn_id,)).signature(1) != before
