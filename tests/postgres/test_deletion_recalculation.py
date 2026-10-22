"""Deletion retains the final projection as the baseline for subsequent queries."""

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
from digital_souls_memory.memory.domain import DerivedMemory
from digital_souls_memory.memory.extraction import (
    CloudInput,
    ExtractionGateway,
    LocalOnly,
    MemoryExtractor,
)
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.memory.worker import MemoryWorker
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import RECORDED, turn
from tests.memory_fakes import EchoGateway
from tests.memory_support import NEW_ID, memory
from tests.postgres.test_deletion import HEADERS, seed_evidence
from tests.postgres.test_deletion import deletion_url as deletion_url

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


def make_worker(url: str, store: PostgresMemoryStore, gateway: ExtractionGateway) -> MemoryWorker:
    evidence = PostgresEvidenceStore(url)
    inputs = MemoryInputs(
        evidence,
        EvidenceMaterials(
            evidence,
            IdentityCharacters(lambda: PostgresIdentityStore(url)),
            lambda: RECORDED,
            timedelta(minutes=10),
        ),
        conversation_surfaces=frozenset({"synthetic-chat"}),
        comment_surfaces=frozenset(),
    )
    return MemoryWorker(
        MemoryService(store, inputs, lambda: RECORDED),
        MemoryExtractor(inputs=inputs, gateway=gateway, non_sensitive_labels=frozenset()),
    )


def test_reinforced_projection_survives_when_its_original_baseline_loses_all_sources(
    database_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1"))
    evidence.add_turn(turn("turn-2", actor="user-a"))
    store = PostgresMemoryStore(database_url)
    original = memory()
    with store.transaction("char-b") as tx:
        tx.add_memory(original)

    class SameFact(EchoGateway):
        def _extract(self, value: LocalOnly | CloudInput) -> tuple[DerivedMemory, ...]:
            return (replace(original, id=NEW_ID, source_turn_ids=("turn-2",)),)

    worker = make_worker(database_url, store, SameFact())
    worker.process("char-b", "turn-2")
    with store.transaction("char-b") as tx:
        assert tx.baselines() == (original,)
    strengthened = store.get_memory("char-b", original.id)
    assert strengthened is not None and strengthened.source_turn_ids == ("turn-1", "turn-2")
    revised = replace(
        strengthened,
        source_turn_ids=("turn-2",),
        content="残存原文からの再計算",
        epistemics=replace(strengthened.epistemics, independent_evidence_count=1),
    )
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            recalculation_gateway=RecordingRecalculationGateway(revised),
        )
    ) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-strengthened", "target_turn_ids": ["turn-1"]},
        )
        assert response.status_code == 200
        assert response.json()["cascade_result"]["recalculated_memories"] == [
            {"character_id": "char-b", "memory_id": original.id}
        ]
        with store.transaction("char-b") as tx:
            assert tx.baselines() == (revised,)
        assert store.get_memory("char-b", original.id) == revised
        assert worker.service.active("char-b") == (revised,)
        with store.transaction("char-b") as tx:
            worker.service.rebuild(tx)
        assert store.get_memory("char-b", original.id) == revised
        response = client.get("/characters/char-b/memories")
        assert response.status_code == 200
        assert [(v["id"], v["content"], v["source_turn_ids"]) for v in response.json()] == [
            (original.id, revised.content, ["turn-2"])
        ]


@pytest.mark.parametrize("baseline_result", ["none", "different", "invalid"])
def test_final_projection_owns_baseline_but_every_copy_is_validated(
    database_url: str,
    baseline_result: str,
) -> None:
    evidence = seed_evidence(database_url)
    for identifier in ("turn-1", "turn-2"):
        evidence.add_turn(turn(identifier))
    store = PostgresMemoryStore(database_url)
    original = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    with store.transaction("char-b") as tx:
        tx.add_memory(original)
    final = replace(original, content="projectionからの最終結果", source_turn_ids=("turn-2",))

    class Copies(RecordingRecalculationGateway):
        def recalculate_local(
            self, value: LocalOnly, *, memory_id: str, kind: str
        ) -> DerivedMemory | None:
            super().recalculate_local(value, memory_id=memory_id, kind=kind)
            if len(self.calls) == 1:
                return final
            if baseline_result == "none":
                return None
            return replace(
                final,
                content="旧baselineの別結果",
                source_turn_ids=("turn-1",) if baseline_result == "invalid" else ("turn-2",),
            )

    gateway = Copies(final)
    with TestClient(create_app(clock=lambda: RECORDED, recalculation_gateway=gateway)) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "copy-results", "target_turn_ids": ["turn-1"]},
        )
        assert len(gateway.calls) == 2
        if baseline_result == "invalid":
            assert response.status_code == 422
            assert store.get_memory("char-b", original.id) == original
            assert evidence.get_turn("turn-1") is not None
        else:
            assert response.status_code == 200
            assert store.get_memory("char-b", original.id) == final
            with store.transaction("char-b") as tx:
                assert tx.baselines() == (final,)
            assert not any(
                r["table"] == "memory_baselines"
                for r in response.json()["cascade_result"]["removed_references"]
            )
            assert client.get("/characters/char-b/memories").json()[0]["content"] == final.content
