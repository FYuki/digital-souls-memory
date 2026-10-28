"""IT2 prerequisites are explicit errors, resolved only when a test executes."""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from digital_souls_memory.recall.settings import EMBEDDING_MODEL, RERANKER_MODEL

pytestmark = pytest.mark.ut

MODEL_MANIFEST_ENV = "MEMORY_IT2_MODEL_MANIFEST"
ENDPOINT_ENVS = ("MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT")


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"embedding": EMBEDDING_MODEL, "reranker": RERANKER_MODEL}))
    monkeypatch.setenv(MODEL_MANIFEST_ENV, str(path))
    monkeypatch.setenv(ENDPOINT_ENVS[0], "http://127.0.0.1:18081")
    monkeypatch.setenv(ENDPOINT_ENVS[1], "http://127.0.0.1:18082")
    return path


def test_approved_model_manifest_resolves_explicit_endpoints(configured: Path) -> None:
    from tests.it2_support import load_model_endpoints

    assert load_model_endpoints() == ("http://127.0.0.1:18081", "http://127.0.0.1:18082")


@pytest.mark.parametrize("missing", [MODEL_MANIFEST_ENV, *ENDPOINT_ENVS])
def test_missing_model_configuration_is_an_error(
    configured: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    from tests.it2_support import load_model_endpoints

    monkeypatch.delenv(missing)
    with rejected() as error:
        load_model_endpoints()
    assert isinstance(error.value, Exception)


@pytest.mark.parametrize("model", ["embedding", "reranker"])
@pytest.mark.parametrize("field", ["model_alias", "sha256"])
def test_unapproved_identity_or_digest_is_rejected(
    configured: Path,
    model: str,
    field: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    from tests.it2_support import load_model_endpoints

    payload = json.loads(configured.read_text())
    payload[model][field] = "unapproved-synthetic-value"
    configured.write_text(json.dumps(payload))
    with rejected() as error:
        load_model_endpoints()
    assert isinstance(error.value, Exception)


def test_selected_it2_without_configuration_errors_instead_of_skipping(tmp_path: Path) -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in (MODEL_MANIFEST_ENV, *ENDPOINT_ENVS, "MEMORY_DATABASE_URL")
    }
    environment["HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "it2", "-q", "tests/it2/test_recall_models.py"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "skipped" not in output.lower()
    assert any(key in output for key in (MODEL_MANIFEST_ENV, *ENDPOINT_ENVS))


def test_it2_collection_does_not_require_settings_or_connect_to_models(tmp_path: Path) -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in (MODEL_MANIFEST_ENV, *ENDPOINT_ENVS)
    }
    environment["HOME"] = str(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-m",
            "it2",
            "-q",
            "tests/it2/test_recall_models.py",
            "tests/postgres/test_recall_it2.py",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
