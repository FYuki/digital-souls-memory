import httpx
import pytest

from digital_souls_memory.evaluation.adapters.real import real_factories

pytestmark = pytest.mark.it1


def test_real_factories_validate_all_conditions_before_any_transport(
    candidate: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def embedding(settings: dict[str, object], *, endpoint: str) -> object:
        calls.append({"settings": settings, "endpoint": endpoint})
        return object()

    monkeypatch.setattr(
        "digital_souls_memory.evaluation.adapters.real.LlamaCppEmbedding", embedding
    )
    config: dict[str, object] = {
        "admin_url": "postgresql://memory_synthetic@127.0.0.1/memory_synthetic",
        "endpoints": {"synthetic-e5": "http://127.0.0.1:18083"},
        "model_identities": {"synthetic-e5": candidate},
    }
    factories = real_factories(config, [candidate])
    assert calls == []
    factories[0](candidate)
    assert calls == [{"settings": candidate, "endpoint": "http://127.0.0.1:18083"}]
    config["endpoints"] = {"synthetic-e5": "https://example.org"}
    with pytest.raises(ValueError):
        real_factories(config, [candidate])
    assert len(calls) == 1


def test_e5_http_prefixes_are_preserved_by_standard_factory(
    candidate: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.evaluation.adapters.llamacpp import LlamaCppEmbedding

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"model": "synthetic-e5", "data": [{"index": 0, "embedding": [1, 0]}]}
        )

    def local(settings: dict[str, object], *, endpoint: str) -> LlamaCppEmbedding:
        return LlamaCppEmbedding(
            settings, endpoint=endpoint, transport=httpx.MockTransport(respond)
        )

    monkeypatch.setattr("digital_souls_memory.evaluation.adapters.real.LlamaCppEmbedding", local)
    config: dict[str, object] = {
        "admin_url": "postgresql://memory_synthetic@127.0.0.1/memory_synthetic",
        "endpoints": {"synthetic-e5": "http://127.0.0.1:18083"},
        "model_identities": {"synthetic-e5": candidate},
    }
    factory, _ = real_factories(config, [candidate])
    with factory(candidate) as embedding:
        assert embedding.embed(("青い駅",), purpose="query") == ((1.0, 0.0),)
        embedding.embed(("青い駅",), purpose="passage")
    import json

    assert [json.loads(r.content)["input"] for r in requests] == [
        ["query: 青い駅"],
        ["passage: 青い駅"],
    ]
    config["model_identities"] = {"synthetic-e5": {**candidate, "pooling": "cls"}}
    with pytest.raises(ValueError):
        real_factories(config, [candidate])
    assert len(requests) == 2
