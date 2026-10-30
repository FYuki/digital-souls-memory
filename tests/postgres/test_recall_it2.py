"""Disposable PostgreSQL plus real llama.cpp recall; excluded from the postgres CI suite."""

import psycopg
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
from digital_souls_memory.adapters.recall_models import (
    LlamaCppEmbedding,
    LlamaCppReranker,
    LlamaCppTokenCounter,
)
from digital_souls_memory.api import create_app
from digital_souls_memory.evidence.provision import CandidateRef
from tests.evidence_support import RECORDED, session_payload, turn_payload
from tests.it2.test_recall_models import (
    QUERY,
    RELATED,
    UNRELATED,
    model_input,
)
from tests.it2.test_recall_models import real_models as real_models
from tests.memory_fakes import EchoGateway
from tests.st.support import eventually

pytestmark = pytest.mark.it2


@pytest.fixture
def model_database_url(
    request: pytest.FixtureRequest,
    real_models: tuple[LlamaCppEmbedding, LlamaCppReranker, LlamaCppTokenCounter],  # noqa: F811
) -> str:
    # Validate model prerequisites before entering the destructive disposable DB fixture.
    return str(request.getfixturevalue("app_database_url"))


def test_real_models_recall_preserves_sources_count_budget_and_threshold(
    model_database_url: str,
    real_models: tuple[LlamaCppEmbedding, LlamaCppReranker, LlamaCppTokenCounter],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding, reranker, counter = real_models
    monkeypatch.setenv("MEMORY_DATABASE_URL", model_database_url)
    originals = {
        "related-1": RELATED,
        "related-2": "合成の予定です。十一月三日に青い駅で集合する約束を確認しました。",
        "unrelated": UNRELATED,
    }
    app = create_app(
        embedding=embedding,
        reranker=reranker,
        token_counter=counter,
        extraction_gateway=EchoGateway(),
        clock=lambda: RECORDED,
        conversation_surfaces=frozenset({"synthetic-chat"}),
    )
    with TestClient(app) as client:
        assert client.post(
            "/characters/char-b/core-identities",
            json={"values": ["誠実"], "prohibitions": []},
        ).is_success
        assert client.post(
            "/sessions", json=session_payload() | {"reports_delivery": False}
        ).is_success
        for index, (identifier, text) in enumerate(originals.items()):
            assert client.post(
                "/turns",
                json=turn_payload()
                | {
                    "turn_id": identifier,
                    "text": text,
                    "actor": f"synthetic-user-{index}",
                    "perceived_by": ["char-b"],
                },
            ).is_success

        def indexed() -> bool:
            with psycopg.connect(model_database_url, connect_timeout=5) as connection:
                return connection.execute(
                    "SELECT count(*) FROM recall_documents WHERE ready AND owner_id=%s",
                    ("char-b",),
                ).fetchone() == (3,)

        eventually(indexed, timeout=180)
        response = client.post(
            "/characters/char-b/recall",
            json={
                "session_id": "session-1",
                "format": "materials",
                "query": QUERY,
                "sensitivity": "synthetic-sensitive",
                "classifier_version": "synthetic-v1",
            },
        )
        assert response.is_success
        body = response.json()
        assert len(body["memories"]) == 2
        assert {span["turn_id"] for span in body["source_spans"]} == {"related-1", "related-2"}
        for span in body["source_spans"]:
            assert span["text"] == originals[span["turn_id"]][span["start"] : span["end"]]
        assert counter.count(model_input(response.text)) <= 1500
        record = PostgresProvisionStore(model_database_url).get(body["provision_id"])
        assert record is not None
        assert [(s.turn_id, s.start, s.end, s.text) for s in record.source_spans] == [
            (s["turn_id"], s["start"], s["end"], s["text"]) for s in body["source_spans"]
        ]
        store = PostgresMemoryStore(model_database_url)
        rejected_ids: set[str] = set()
        for score in record.scores:
            if not isinstance(score.ref, CandidateRef) or score.ref.memory_id is None:
                continue
            value = store.get_memory("char-b", score.ref.memory_id)
            assert value is not None
            assert score.relevance is not None
            if "unrelated" in value.source_turn_ids:
                assert score.relevance < -2
                rejected_ids.add(value.id)
            else:
                assert score.relevance >= -2
        assert rejected_ids
        assert rejected_ids.isdisjoint(item["id"] for item in body["memories"])
        assert set(record.memory_ids) == {item["id"] for item in body["memories"]}
