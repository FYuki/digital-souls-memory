"""The functional factory cannot advertise real-model performance."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.client import MemoryClient
from tests.st.app import factory
from tests.st.performance import Manifest, run
from tests.ut.test_core_st_harness import manifest_payload

pytestmark = pytest.mark.it1


def test_functional_configuration_marks_fake_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_CORE_ST_MODE", "functional")
    monkeypatch.delenv("MEMORY_EMBEDDING_ENDPOINT", raising=False)
    monkeypatch.delenv("MEMORY_RERANKER_ENDPOINT", raising=False)
    # No lifespan: this inspects the configuration endpoint without starting DB workers.
    client = TestClient(factory())
    response = client.get("/_st/config")
    assert response.status_code == 200
    assert response.json()["mode"] == "functional"
    assert response.json()["real_models"] is False


def test_unknown_mode_is_rejected_before_app_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_CORE_ST_MODE", "unknown")
    with pytest.raises(ValueError):
        factory()


def test_performance_runner_rejects_a_functional_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MemoryClient(
        base_url="http://synthetic",
        timeout=5,
        transport=httpx.MockTransport(lambda _: pytest.fail("No workload before preflight")),
    )
    monkeypatch.setattr("tests.st.performance.new_client", lambda: client)
    monkeypatch.setenv("MEMORY_CORE_ST_URL", "http://synthetic")

    class Preflight:
        def __enter__(self) -> "Preflight":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, path: str) -> httpx.Response:
            assert path == "/_st/config"
            return httpx.Response(
                200,
                request=httpx.Request("GET", "http://synthetic/_st/config"),
                json={"mode": "functional", "real_models": False},
            )

    monkeypatch.setattr("tests.st.performance.httpx.Client", lambda **kwargs: Preflight())
    output = tmp_path / "performance.json"
    with pytest.raises(ValueError):
        run(Manifest.model_validate(manifest_payload()), output)
    result = json.loads(output.read_text())
    assert result["status"] == "FAIL"
    assert result["runs"] == []
    assert result["error"] == "ValueError"
