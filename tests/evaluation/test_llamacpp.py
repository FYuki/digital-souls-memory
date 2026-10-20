import json
import math
from collections.abc import Callable

import httpx
import pytest

from digital_souls_memory.evaluation.adapters.llamacpp import LlamaCppEmbedding

pytestmark = pytest.mark.it1


def test_candidate_prefixes_reach_embedding_request(candidate: dict[str, object]) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "synthetic-e5",
                "data": [{"object": "embedding", "index": 0, "embedding": [1.0, 0.0]}],
            },
        )

    with LlamaCppEmbedding(
        candidate, endpoint="http://127.0.0.1:8080", transport=httpx.MockTransport(respond)
    ) as embedding:
        query = embedding.embed(("いつ？",), purpose="query")
        passage = embedding.embed(("11月3日",), purpose="passage")
    assert [list(vector) for vector in query] == [[1.0, 0.0]]
    assert [list(vector) for vector in passage] == [[1.0, 0.0]]
    assert requests == [
        {"model": "synthetic-e5", "input": ["query: いつ？"], "encoding_format": "float"},
        {"model": "synthetic-e5", "input": ["passage: 11月3日"], "encoding_format": "float"},
    ]


def test_bge_baseline_has_no_prefix_and_checks_1024_dimensions(
    candidate: dict[str, object],
) -> None:
    candidate.update(
        {
            "model_alias": "bge-m3",
            "dimensions": 1024,
            "pooling": "cls",
            "quantization": "Q8_0",
            "query_prefix": "",
            "passage_prefix": "",
            "revision": "9eba04c5d75ba5a1595e45de734d36bef4e5cb98",
            "sha256": "aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173",
        }
    )
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "bge-m3",
                "data": [{"object": "embedding", "index": 0, "embedding": [1.0] * 1024}],
            },
        )

    with LlamaCppEmbedding(
        candidate, endpoint="http://127.0.0.1:8080", transport=httpx.MockTransport(respond)
    ) as embedding:
        result = embedding.embed(("青い駅",), purpose="query")
    assert len(result[0]) == 1024
    assert requests[0]["input"] == ["青い駅"]
    assert requests[0]["model"] == "bge-m3"


def test_response_indices_restore_input_order(candidate: dict[str, object]) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "synthetic-e5",
                "data": [
                    {"object": "embedding", "index": 1, "embedding": [0.0, 1.0]},
                    {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]},
                ],
            },
        )

    with LlamaCppEmbedding(
        candidate, endpoint="http://127.0.0.1:8080", transport=httpx.MockTransport(respond)
    ) as embedding:
        vectors = embedding.embed(("甲", "乙"), purpose="passage")
        assert [list(vector) for vector in vectors] == [[1.0, 0.0], [0.0, 1.0]]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"object": "embedding", "index": 1, "embedding": [1.0, 0.0]}],
        [{"object": "embedding", "index": 0, "embedding": [1.0]}],
        [{"object": "embedding", "index": 0, "embedding": [0.0, 0.0]}],
        [{"object": "embedding", "index": True, "embedding": [1.0, 0.0]}],
        [{"object": "embedding", "index": 0, "embedding": [True, 1.0]}],
    ],
    ids=["count", "index", "dimensions", "zero", "bool-index", "bool-value"],
)
def test_invalid_response_is_not_a_successful_embedding(
    candidate: dict[str, object],
    rows: list[dict[str, object]],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "model": "synthetic-e5", "data": rows})

    with LlamaCppEmbedding(
        candidate, endpoint="http://127.0.0.1:8080", transport=httpx.MockTransport(respond)
    ) as embedding:
        with rejected():
            embedding.embed(("甲",), purpose="query")


def test_duplicate_indices_are_rejected(
    candidate: dict[str, object], rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        row = {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]}
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "synthetic-e5",
                "data": [row, row],
            },
        )

    with LlamaCppEmbedding(
        candidate, endpoint="http://127.0.0.1:8080", transport=httpx.MockTransport(respond)
    ) as embedding:
        with rejected():
            embedding.embed(("甲", "乙"), purpose="passage")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_http_values_are_rejected(
    candidate: dict[str, object],
    value: float,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = {
            "object": "list",
            "model": "synthetic-e5",
            "data": [
                {"object": "embedding", "index": 0, "embedding": [value, 1.0]},
            ],
        }
        # Raw bytes intentionally exercise non-standard JSON values at the HTTP boundary.
        return httpx.Response(200, content=json.dumps(payload).encode())

    with LlamaCppEmbedding(
        candidate, endpoint="http://127.0.0.1:8080", transport=httpx.MockTransport(respond)
    ) as embedding:
        with rejected():
            embedding.embed(("甲",), purpose="query")


@pytest.mark.parametrize("failure", ["timeout", "http", "malformed", "model", "redirect"])
def test_transport_failure_never_falls_back_to_fake_vectors(
    candidate: dict[str, object],
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:8888")
    monkeypatch.setenv("HTTPS_PROXY", "http://192.0.2.1:8888")
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if failure == "http":
            return httpx.Response(503)
        if failure == "malformed":
            return httpx.Response(200, content=b"invalid-json")
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://example.invalid/embeddings"})
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "wrong-model",
                "data": [
                    {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]},
                ],
            },
        )

    with LlamaCppEmbedding(
        candidate, endpoint="http://127.0.0.1:8080", transport=httpx.MockTransport(respond)
    ) as embedding:
        with rejected():
            embedding.embed(("合成の機微情報",), purpose="passage")
    assert len(requests) == 1
    assert httpx.URL(requests[0]).host == "127.0.0.1"
