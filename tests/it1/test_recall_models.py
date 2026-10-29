import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Literal

import httpx
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from tests.evidence_support import INSTRUCTION

if TYPE_CHECKING:
    from digital_souls_memory.recall.domain import RerankInput
    from digital_souls_memory.recall.inputs import LocalOnly

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("failure", ["none", "configuration", "startup"])
def test_application_closes_owned_model_clients_on_exit_or_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from digital_souls_memory.adapters.recall_models import _LocalModel
    from digital_souls_memory.memory.ports import MemoryUnavailable
    from tests.memory_fakes import EchoGateway

    clients: list[httpx.Client] = []
    initialize = _LocalModel.__init__

    def observe(
        self: _LocalModel,
        model: Mapping[str, object],
        *,
        endpoint: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        initialize(self, model, endpoint=endpoint, transport=transport)
        clients.append(self.client)

    monkeypatch.setattr(_LocalModel, "__init__", observe)
    monkeypatch.delenv("MEMORY_DATABASE_URL", raising=False)
    monkeypatch.setenv("MEMORY_EMBEDDING_ENDPOINT", "http://127.0.0.1:8080")
    monkeypatch.setenv(
        "MEMORY_RERANKER_ENDPOINT",
        "https://cloud.example.invalid" if failure == "configuration" else "http://127.0.0.1:8081",
    )
    if failure == "configuration":
        with pytest.raises(ValueError):
            create_app()
    elif failure == "startup":
        with (
            pytest.raises(MemoryUnavailable),
            TestClient(create_app(extraction_gateway=EchoGateway())),
        ):
            raise AssertionError("Startup must reject the absent storage")
    else:
        with TestClient(create_app()) as client:
            assert client.get("/health").is_success
            assert clients and all(not v.is_closed for v in clients)
    assert clients and all(v.is_closed for v in clients)


def local_input(text: str) -> "LocalOnly":
    from digital_souls_memory.recall.inputs import InputLabel, LocalOnly

    return LocalOnly(texts=(text,), labels=(InputLabel("synthetic-sensitive", "v1"),))


def selected_model() -> dict[str, object]:
    return {
        "id": "bge-m3-bigm",
        "model_alias": "bge-m3",
        "revision": "9eba04c5d75ba5a1595e45de734d36bef4e5cb98",
        "sha256": "aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173",
        "dimensions": 1024,
        "pooling": "cls",
        "quantization": "Q8_0",
        "query_prefix": "",
        "passage_prefix": "",
        "extension": "pg_bigm",
    }


@pytest.mark.parametrize("purpose", ["query", "passage"])
def test_product_embedding_uses_the_selected_model_and_checks_its_vector_space(
    purpose: Literal["query", "passage"],
) -> None:
    from digital_souls_memory.adapters.recall_models import LlamaCppEmbedding

    sent: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "bge-m3",
                "data": [{"object": "embedding", "index": 0, "embedding": [1.0] + [0.0] * 1023}],
            },
        )

    with LlamaCppEmbedding(
        selected_model(), endpoint="http://127.0.0.1:8080", transport=httpx.MockTransport(respond)
    ) as embedding:
        batch = embedding.embed(local_input(INSTRUCTION), purpose=purpose)
        from tests.recall_fixtures import SPACE

        assert batch.space == SPACE
        assert batch.vectors == ((1.0,) + (0.0,) * 1023,)
    assert sent == [{"model": "bge-m3", "input": [INSTRUCTION], "encoding_format": "float"}]


@pytest.mark.parametrize("invalid", ["model", "count", "dimensions", "nonfinite"])
def test_product_embedding_rejects_an_incompatible_response(
    invalid: str, rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    from digital_souls_memory.adapters.recall_models import LlamaCppEmbedding

    def respond(request: httpx.Request) -> httpx.Response:
        vector = [1.0] + [0.0] * (1022 if invalid == "dimensions" else 1023)
        if invalid == "nonfinite":
            vector[0] = float("nan")
        body = {
            "object": "list",
            "model": "another-model" if invalid == "model" else "bge-m3",
            "data": []
            if invalid == "count"
            else [{"object": "embedding", "index": 0, "embedding": vector}],
        }
        return httpx.Response(200, content=json.dumps(body).encode())

    with (
        LlamaCppEmbedding(
            selected_model(),
            endpoint="http://127.0.0.1:8080",
            transport=httpx.MockTransport(respond),
        ) as embedding,
        rejected(),
    ):
        embedding.embed(local_input("合成原文"), purpose="query")


def test_product_tokenizer_counts_token_ids_from_the_complete_supplied_json() -> None:
    from digital_souls_memory.adapters.recall_models import LlamaCppTokenCounter

    text = json.dumps(
        {"provision_id": "synthetic-provision", "context": INSTRUCTION}, ensure_ascii=False
    )
    sent: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tokenize"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"tokens": [7, 5, 9]})

    with LlamaCppTokenCounter(
        selected_model(), endpoint="http://127.0.0.1:8080", transport=httpx.MockTransport(respond)
    ) as counter:
        assert counter.count(local_input(text)) == 3
    assert sent == [{"content": text, "add_special": False}]


@pytest.mark.parametrize("operation", ["embedding", "tokenizer", "reranker"])
def test_sensitive_content_cannot_follow_a_redirect_to_a_cloud_endpoint(
    operation: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    from digital_souls_memory.adapters.recall_models import (
        LlamaCppEmbedding,
        LlamaCppReranker,
        LlamaCppTokenCounter,
    )

    sent: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(307, headers={"location": "https://cloud.example.invalid/collect"})

    transport = httpx.MockTransport(redirect)
    if operation == "embedding":
        with (
            LlamaCppEmbedding(
                selected_model(), endpoint="http://127.0.0.1:8080", transport=transport
            ) as embedding,
            rejected(),
        ):
            embedding.embed(local_input(INSTRUCTION), purpose="query")
    elif operation == "tokenizer":
        with (
            LlamaCppTokenCounter(
                selected_model(), endpoint="http://127.0.0.1:8080", transport=transport
            ) as counter,
            rejected(),
        ):
            counter.count(local_input(INSTRUCTION))
    else:
        with (
            LlamaCppReranker(
                selected_reranker(), endpoint="http://127.0.0.1:8080", transport=transport
            ) as reranker,
            rejected(),
        ):
            reranker.rerank(rerank_input())
    assert len(sent) == 1
    assert [r.url.host for r in sent] == ["127.0.0.1"]


def selected_reranker() -> dict[str, object]:
    return {
        "model_alias": "bge-reranker-v2-m3",
        "revision": "3093af03b1a635e67b084b1d8c03c5f5e020fd05",
        "sha256": "a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3",
        "quantization": "Q8_0",
    }


def rerank_input() -> "RerankInput":
    from digital_souls_memory.recall.domain import CandidateRef, RerankDocument, RerankInput

    return RerankInput(
        query=local_input("日本語"),
        documents=(
            RerankDocument(
                ref=CandidateRef(owner_id="char-b", memory_id="m1", anchor_id=None),
                input=local_input(INSTRUCTION),
            ),
            RerankDocument(
                ref=CandidateRef(owner_id="char-b", memory_id=None, anchor_id="m1"),
                input=local_input("合成アンカー"),
            ),
        ),
    )


def test_reranker_preserves_candidate_reference_when_response_order_changes() -> None:
    from digital_souls_memory.adapters.recall_models import LlamaCppReranker

    value = rerank_input()
    sent: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rerank"
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 3.0},
                    {"index": 0, "relevance_score": -2.0},
                ]
            },
        )

    with LlamaCppReranker(
        selected_reranker(),
        endpoint="http://127.0.0.1:8080",
        transport=httpx.MockTransport(respond),
    ) as reranker:
        result = reranker.rerank(value)
    assert {v.ref: v.relevance for v in result} == {
        value.documents[0].ref: -2.0,
        value.documents[1].ref: 3.0,
    }
    assert sent == [
        {
            "model": "bge-reranker-v2-m3",
            "query": "日本語",
            "documents": [INSTRUCTION, "合成アンカー"],
            "top_n": 2,
        }
    ]


def test_reranker_preserves_distinct_chunks_of_one_parent_when_results_are_reversed() -> None:
    from digital_souls_memory.adapters.recall_models import LlamaCppReranker
    from digital_souls_memory.recall.domain import RerankDocument, RerankInput

    ref = rerank_input().documents[0].ref
    value = RerankInput(
        query=local_input("合成質問"),
        documents=tuple(
            RerankDocument(ref=ref, chunk_index=i, input=local_input(text))
            for i, text in enumerate(("先頭の生チャンクＡ", "末尾の生チャンク🙂"))
        ),
    )
    sent: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 7},
                    {"index": 0, "relevance_score": -3},
                ]
            },
        )

    with LlamaCppReranker(
        selected_reranker(),
        endpoint="http://127.0.0.1:8080",
        transport=httpx.MockTransport(respond),
    ) as reranker:
        scores = reranker.rerank(value)

    assert {(v.ref, v.chunk_index): v.relevance for v in scores} == {
        (ref, 0): -3,
        (ref, 1): 7,
    }
    assert sent[0]["documents"] == ["先頭の生チャンクＡ", "末尾の生チャンク🙂"]


@pytest.mark.parametrize(
    "results",
    [
        [{"index": 0, "relevance_score": 1.0}],
        [{"index": 0, "relevance_score": 1.0}, {"index": 0, "relevance_score": 2.0}],
        [{"index": 0, "relevance_score": 1.0}, {"index": 2, "relevance_score": 2.0}],
        [{"index": 0, "relevance_score": 1.0}, {"index": 1, "relevance_score": float("inf")}],
    ],
)
def test_reranker_rejects_missing_duplicate_unknown_or_nonfinite_results(
    results: list[dict[str, object]],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    from digital_souls_memory.adapters.recall_models import LlamaCppReranker

    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=json.dumps({"results": results}).encode())
    )
    with (
        LlamaCppReranker(
            selected_reranker(), endpoint="http://127.0.0.1:8080", transport=transport
        ) as reranker,
        rejected(),
    ):
        reranker.rerank(rerank_input())


@pytest.mark.parametrize("operation", ["embedding", "tokenizer", "reranker"])
def test_model_adapters_reject_a_cloud_endpoint_before_sending_sensitive_text(
    operation: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    from digital_souls_memory.adapters.recall_models import (
        LlamaCppEmbedding,
        LlamaCppReranker,
        LlamaCppTokenCounter,
    )

    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(respond)
    with rejected():
        if operation == "embedding":
            with LlamaCppEmbedding(
                selected_model(), endpoint="https://cloud.example.invalid", transport=transport
            ) as adapter:
                adapter.embed(local_input(INSTRUCTION), purpose="query")
        elif operation == "tokenizer":
            with LlamaCppTokenCounter(
                selected_model(), endpoint="https://cloud.example.invalid", transport=transport
            ) as counter:
                counter.count(local_input(INSTRUCTION))
        else:
            with LlamaCppReranker(
                selected_reranker(), endpoint="https://cloud.example.invalid", transport=transport
            ) as reranker:
                reranker.rerank(rerank_input())
    assert sent == []


@pytest.mark.parametrize("operation", ["embedding", "tokenizer", "reranker"])
def test_sensitive_model_requests_do_not_use_environment_proxy(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.adapters.recall_models import (
        LlamaCppEmbedding,
        LlamaCppReranker,
        LlamaCppTokenCounter,
    )

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.example.invalid:8080")
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.delenv("no_proxy", raising=False)
    proxies: list[object] = []
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path == "/v1/embeddings":
            return httpx.Response(
                200,
                json={
                    "model": "bge-m3",
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": 0, "embedding": [1.0] + [0.0] * 1023},
                    ],
                },
            )
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1]})
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 1},
                    {"index": 1, "relevance_score": 2},
                ]
            },
        )

    class InterceptTransport(httpx.MockTransport):
        def __init__(self, **kwargs: object) -> None:
            proxies.append(kwargs.get("proxy"))
            super().__init__(respond)

    monkeypatch.setattr("httpx._client.HTTPTransport", InterceptTransport)
    if operation == "embedding":
        with LlamaCppEmbedding(selected_model(), endpoint="http://127.0.0.1:8080") as adapter:
            assert adapter.embed(local_input(INSTRUCTION), purpose="query").vectors
    elif operation == "tokenizer":
        with LlamaCppTokenCounter(selected_model(), endpoint="http://127.0.0.1:8080") as counter:
            assert counter.count(local_input(INSTRUCTION)) == 1
    else:
        with LlamaCppReranker(selected_reranker(), endpoint="http://127.0.0.1:8080") as reranker:
            assert len(reranker.rerank(rerank_input())) == 2
    assert proxies and all(proxy is None for proxy in proxies)
    assert len(sent) == 1
    assert sent[0].url.host == "127.0.0.1"
