"""Exercise the IT2 ingestion/assertion path with a disposable DB and synthetic HTTP models."""

from contextlib import ExitStack

import httpx
import pytest

from digital_souls_memory.adapters.recall_models import (
    LlamaCppEmbedding,
    LlamaCppReranker,
    LlamaCppTokenCounter,
)
from digital_souls_memory.recall.settings import EMBEDDING_MODEL, RERANKER_MODEL
from tests.it2.test_recall_models import UNRELATED
from tests.postgres import test_recall_it2 as scenario

pytestmark = pytest.mark.postgres


def test_it2_recall_fixture_reaches_indexing_and_response_assertions(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def model_response(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        if request.url.path == "/v1/embeddings":
            return httpx.Response(
                200,
                json={
                    "model": EMBEDDING_MODEL["model_alias"],
                    "data": [
                        {"index": i, "embedding": [1.0] + [0.0] * 1023}
                        for i, _ in enumerate(body["input"])
                    ],
                },
            )
        if request.url.path == "/rerank":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "index": i,
                            "relevance_score": -3 if UNRELATED in text else 5,
                        }
                        for i, text in enumerate(body["documents"])
                    ]
                },
            )
        assert request.url.path == "/tokenize"
        return httpx.Response(200, json={"tokens": [1] * 100})

    with ExitStack() as stack:
        embedding = stack.enter_context(
            LlamaCppEmbedding(
                EMBEDDING_MODEL,
                endpoint="http://127.0.0.1:18081",
                transport=httpx.MockTransport(model_response),
            )
        )
        reranker = stack.enter_context(
            LlamaCppReranker(
                RERANKER_MODEL,
                endpoint="http://127.0.0.1:18082",
                transport=httpx.MockTransport(model_response),
            )
        )
        counter = stack.enter_context(
            LlamaCppTokenCounter(
                EMBEDDING_MODEL,
                endpoint="http://127.0.0.1:18081",
                transport=httpx.MockTransport(model_response),
            )
        )
        scenario.test_real_models_recall_preserves_sources_count_budget_and_threshold(
            app_database_url, (embedding, reranker, counter), monkeypatch
        )
