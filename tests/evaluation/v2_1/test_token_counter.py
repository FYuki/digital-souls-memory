import json
from collections.abc import Callable

import httpx
import pytest

pytestmark = pytest.mark.it1


@pytest.mark.parametrize(
    "text,tokens", [("日本語", [10, 11, 12]), ("English と日本語", [3, 5, 8, 13])]
)
def test_llamacpp_counter_uses_tokenize_without_embedding_prefix_and_closes(
    candidate: dict[str, object],
    text: str,
    tokens: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.evaluation.adapters.token_counter import LlamaCppTokenCounter

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/tokenize"
        assert json.loads(request.content)["content"] == text
        return httpx.Response(200, json={"tokens": tokens})

    port = LlamaCppTokenCounter(
        candidate, endpoint="http://127.0.0.1:18080", transport=httpx.MockTransport(respond)
    )
    with port:
        assert port.count(text) == len(tokens)
    assert port.client.is_closed
    assert len(requests) == 1


@pytest.mark.parametrize("fault", ["http", "redirect", "timeout", "missing", "invalid"])
def test_tokenizer_failure_is_propagated_without_approximation(
    candidate: dict[str, object],
    fault: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    from digital_souls_memory.evaluation.adapters.token_counter import LlamaCppTokenCounter

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if fault == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if fault == "http":
            return httpx.Response(500)
        if fault == "redirect":
            return httpx.Response(307, headers={"location": "http://192.0.2.1/tokenize"})
        return httpx.Response(200, json={} if fault == "missing" else {"tokens": [True, "token"]})

    port = LlamaCppTokenCounter(
        candidate, endpoint="http://127.0.0.1:18080", transport=httpx.MockTransport(respond)
    )
    with port, rejected():
        port.count("合成の原文")
    assert port.client.is_closed
    assert len(requests) == 1


@pytest.mark.parametrize("endpoint", ["http://192.0.2.1:18080", "https://example.invalid"])
def test_token_counter_rejects_non_loopback_endpoints(
    candidate: dict[str, object],
    endpoint: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    from digital_souls_memory.evaluation.adapters.token_counter import LlamaCppTokenCounter

    with rejected():
        LlamaCppTokenCounter(candidate, endpoint=endpoint)


@pytest.mark.parametrize("mismatch", [False, True])
def test_real_counter_uses_embedding_identity_and_endpoint_before_transport(
    candidate: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    mismatch: bool,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    from digital_souls_memory.evaluation.adapters.real import real_token_counter_factory
    from digital_souls_memory.evaluation.adapters.token_counter import LlamaCppTokenCounter

    alias = str(candidate["model_alias"])
    config: dict[str, object] = {
        "endpoints": {alias: "http://127.0.0.1:18080"},
        "model_identities": {alias: {**candidate, "sha256": "b" * 64} if mismatch else candidate},
    }
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == "http://127.0.0.1:18080/tokenize"
        return httpx.Response(200, json={"tokens": [1, 2]})

    def build(settings: dict[str, object], *, endpoint: str) -> LlamaCppTokenCounter:
        return LlamaCppTokenCounter(
            settings, endpoint=endpoint, transport=httpx.MockTransport(respond)
        )

    monkeypatch.setattr("digital_souls_memory.evaluation.adapters.real.LlamaCppTokenCounter", build)
    if mismatch:
        with rejected():
            real_token_counter_factory(config, [candidate])
        assert requests == []
    else:
        factory = real_token_counter_factory(config, [candidate])
        with factory(candidate) as port:
            assert port.count("合成本文") == 2
        assert len(requests) == 1
