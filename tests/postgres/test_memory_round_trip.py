from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.memory.extraction import MemoryExtractor
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.memory.worker import MemoryWorker
from tests.evidence_support import RECORDED, TEXT, receipt, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import OLD_ID, memory

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("sources", [("turn-1",), ("turn-1", "question")])
def test_round_trip_sources_count_and_context_dependency_survive_adapter_restore(
    app_database_url: str, sources: tuple[str, ...]
) -> None:
    identity = PostgresIdentityStore(app_database_url)
    with TestClient(create_app(identity_store=identity)) as client:
        for identifier in ("char-b", "char-c"):
            assert client.post(
                f"/characters/{identifier}/core-identities",
                json={"values": ["誠実"], "prohibitions": []},
            ).is_success
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_session(session("question-session", reports_delivery=True))
    evidence.add_turn(turn(turn_id="question", session_id="question-session"))
    evidence.add_turn(turn(actor="user-a", reply_to_turn_id="question", text="うん、そうだよ"))
    now = RECORDED + timedelta(minutes=10)
    materials = EvidenceMaterials(
        evidence, IdentityCharacters(lambda: identity), lambda: now, timedelta(minutes=10)
    )
    store = PostgresMemoryStore(app_database_url)
    service = MemoryService(
        store,
        MemoryInputs(
            evidence,
            materials,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            comment_surfaces=frozenset(),
        ),
        lambda: now,
    )
    gateway = RecordingGateway()
    gateway.output = (replace(memory(), source_turn_ids=sources),)
    extractor = MemoryExtractor(
        inputs=service.inputs,
        gateway=gateway,
        non_sensitive_labels=frozenset(),
    )
    worker = MemoryWorker(service, extractor)
    worker.process("char-b", "turn-1")
    assert {v.turn_id: v.text for v in gateway.local_calls[-1].turns} == {
        "turn-1": "うん、そうだよ",
        "question": TEXT,
    }
    restored = PostgresMemoryStore(app_database_url)
    service = MemoryService(
        restored,
        MemoryInputs(
            evidence,
            materials,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            comment_surfaces=frozenset(),
        ),
        lambda: now,
    )
    for _ in range(2):
        with restored.transaction("char-b") as tx:
            service.rebuild(tx)
        (saved,) = service.active("char-b")
        assert saved.source_turn_ids == sources
        assert saved.epistemics == memory().epistemics
        assert restored.get_memory("char-b", OLD_ID) == saved
    evidence.add_receipt(replace(receipt("none", 0), turn_id="question"))
    assert service.active("char-b") == ()
    gateway.fail = True
    worker.process("char-b", "turn-1")
    again = PostgresMemoryStore(app_database_url)
    fresh_service = MemoryService(
        again,
        MemoryInputs(
            evidence,
            materials,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            comment_surfaces=frozenset(),
        ),
        lambda: now,
    )
    assert fresh_service.active("char-b") == ()
    withdrawn = again.get_memory("char-b", OLD_ID)
    assert withdrawn is not None and withdrawn.superseded_at is not None
