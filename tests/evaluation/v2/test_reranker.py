import json
from collections.abc import Callable
from copy import deepcopy
from typing import cast

import httpx
import pytest

from digital_souls_memory.evaluation.adapters.fixture import FixtureReranker
from digital_souls_memory.evaluation.adapters.real import real_reranker_factory
from digital_souls_memory.evaluation.adapters.reranker import LlamaCppReranker

pytestmark = pytest.mark.it1


@pytest.fixture
def definition() -> dict[str, object]:
    return {
        "model_alias": "synthetic-reranker",
        "revision": "synthetic",
        "sha256": "b" * 64,
        "quantization": "synthetic",
        "max_context_length": 8192,
    }


@pytest.mark.parametrize(
    "fault", [None, "duplicate", "missing", "unknown", "nan", "redirect", "http"]
)
def test_http_reranker_validates_complete_finite_results_and_closes(
    definition: dict[str, object],
    fault: str | None,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert request.url.path == "/rerank"
        assert payload == {
            "model": definition["model_alias"],
            "query": "合成query",
            "documents": ["one", "two"],
            "top_n": 2,
        }
        results = [
            {"index": 1, "relevance_score": -3.0},
            {"index": 0, "relevance_score": 2.0},
        ]
        if fault == "duplicate":
            results[1]["index"] = 1
        elif fault == "missing":
            results.pop()
        elif fault == "unknown":
            results[1]["index"] = 2
        elif fault == "nan":
            return httpx.Response(200, content=b'{"results":[{"index":0,"relevance_score":NaN}]}')
        elif fault == "redirect":
            return httpx.Response(307, headers={"location": "http://127.0.0.1:1/elsewhere"})
        elif fault == "http":
            return httpx.Response(500)
        return httpx.Response(200, json={"results": results})

    port = LlamaCppReranker(
        definition, endpoint="http://127.0.0.1:18083", transport=httpx.MockTransport(respond)
    )
    documents: tuple[dict[str, object], ...] = (
        {"memory_id": "m1", "content": "one"},
        {"memory_id": "m2", "content": "two"},
    )
    with port:
        if fault is None:
            assert port.rerank("合成query", documents) == [
                {"memory_id": "m2", "score": -3.0},
                {"memory_id": "m1", "score": 2.0},
            ]
        else:
            with rejected():
                port.rerank("合成query", documents)
    assert port.client.is_closed
    assert len(requests) == port.real_model_calls == 1


@pytest.mark.parametrize(
    "field", ["model_alias", "revision", "sha256", "quantization", "max_context_length"]
)
def test_reranker_preflight_rejects_identity_mismatch_before_constructing_port(
    definition: dict[str, object],
    candidate: dict[str, object],
    field: str,
    monkeypatch: pytest.MonkeyPatch,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    alias = str(definition["model_alias"])
    config: dict[str, object] = {
        "reranker_endpoints": {alias: "http://127.0.0.1:18083"},
        "reranker_identities": {alias: deepcopy(definition)},
    }
    constructed: list[dict[str, object]] = []

    def build(settings: dict[str, object], *, endpoint: str) -> FixtureReranker:
        constructed.append(settings)
        return FixtureReranker({"reranker_scores": {"q": [{"memory_id": "m1", "score": 1.0}]}})

    monkeypatch.setattr("digital_souls_memory.evaluation.adapters.real.LlamaCppReranker", build)
    settings = {**candidate, "reranker": definition}
    factory = real_reranker_factory(config, [settings])
    assert factory is not None
    assert constructed == []
    with factory(settings) as port:
        assert port.rerank("q", ({"memory_id": "m1", "content": "synthetic"},)) == [
            {"memory_id": "m1", "score": 1.0}
        ]
    identities = cast(dict[str, object], config["reranker_identities"])
    identities[alias] = {**definition, field: 512 if field == "max_context_length" else "other"}
    with rejected():
        real_reranker_factory(config, [settings])
    assert constructed == [definition]


def test_disabled_reranker_has_no_factory(candidate: dict[str, object]) -> None:
    assert real_reranker_factory({}, [candidate]) is None


def test_mixed_reranker_configuration_is_rejected(
    candidate: dict[str, object],
    definition: dict[str, object],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    with rejected():
        real_reranker_factory({}, [candidate, {**candidate, "reranker": definition}])
