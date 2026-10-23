from collections.abc import Callable

import pytest

from digital_souls_memory.evaluation.adapters.real import real_factories

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("field,value", [("max_context_length", 512), ("quantization", "other")])
def test_additional_model_identity_is_checked_before_constructing_any_port(
    candidate: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    candidate = {
        **candidate,
        "model_alias": "Qwen3-Embedding-0.6B",
        "quantization": "synthetic",
        "max_context_length": 8192,
    }
    constructed: list[dict[str, object]] = []

    def embedding(settings: dict[str, object], *, endpoint: str) -> object:
        constructed.append(settings)
        return object()

    monkeypatch.setattr(
        "digital_souls_memory.evaluation.adapters.real.LlamaCppEmbedding", embedding
    )
    alias = str(candidate["model_alias"])
    config: dict[str, object] = {
        "admin_url": "postgresql://memory_synthetic@127.0.0.1/memory_synthetic",
        "endpoints": {alias: "http://127.0.0.1:18083"},
        "model_identities": {alias: candidate},
    }
    factories = real_factories(config, [candidate])
    assert constructed == []
    factories[0](candidate)
    assert constructed == [candidate]
    constructed.clear()
    config["model_identities"] = {alias: {**candidate, field: value}}
    with rejected():
        real_factories(config, [candidate])
    assert constructed == []
