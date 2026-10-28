"""Manifest contracts for cold, concurrent and long-input measurements."""

import json
from pathlib import Path

import pytest

from tests.st.performance import Manifest
from tests.ut.test_core_st_harness import manifest_payload

pytestmark = pytest.mark.ut


def cold_condition() -> dict[str, bool]:
    return {
        "restart_memory_service": True,
        "restart_postgres": False,
        "restart_model_servers": False,
        "flush_os_cache": False,
    }


@pytest.mark.parametrize("concurrency", [1, 2, 4, 8])
def test_manifest_accepts_positive_concurrency(concurrency: int) -> None:
    result = Manifest.model_validate(manifest_payload() | {"concurrency": concurrency})
    assert result.concurrency == concurrency


def test_manifest_defaults_to_one_worker() -> None:
    payload = manifest_payload()
    del payload["concurrency"]
    assert Manifest.model_validate(payload).concurrency == 1


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.0, "4", None])
def test_manifest_refuses_nonpositive_or_noninteger_concurrency(concurrency: object) -> None:
    with pytest.raises(ValueError):
        Manifest.model_validate(manifest_payload() | {"concurrency": concurrency})


@pytest.mark.parametrize("indent,sort_keys", [(None, False), (2, True)])
def test_structured_cold_condition_roundtrips_without_losing_reset_scope(
    indent: int | None, sort_keys: bool
) -> None:
    payload = manifest_payload() | {"cold_condition": cold_condition()}
    result = Manifest.model_validate_json(json.dumps(payload, indent=indent, sort_keys=sort_keys))
    assert result.model_dump(mode="json")["cold_condition"] == cold_condition()


@pytest.mark.parametrize(
    "change",
    [
        {"restart_postgres": True},
        {"restart_model_servers": True},
        {"flush_os_cache": True},
        {"unknown_reset": True},
    ],
)
def test_manifest_refuses_unsupported_reset_targets(change: dict[str, bool]) -> None:
    with pytest.raises(ValueError):
        Manifest.model_validate(manifest_payload() | {"cold_condition": cold_condition() | change})


def test_manifest_rejects_truncated_cold_json() -> None:
    payload = json.dumps(manifest_payload() | {"cold_condition": cold_condition()})
    with pytest.raises(ValueError):
        Manifest.model_validate_json(payload[:-1])


@pytest.mark.parametrize("length_delta", [0, 1])
def test_long_japanese_input_keeps_character_count_validation(length_delta: int) -> None:
    text = "これは合成の会話です。青い駅で十一月三日に待ち合わせます。" * 50
    payload = manifest_payload() | {
        "input_text": text,
        "input_length": len(text) + length_delta,
    }
    if length_delta:
        with pytest.raises(ValueError):
            Manifest.model_validate(payload)
    else:
        assert Manifest.model_validate(payload).input_text == text


def test_manifest_templates_include_a_valid_fixed_long_input() -> None:
    templates = Path("docs/reports").glob("*manifest*.json")
    long_inputs = []
    defaults = manifest_payload()
    for path in templates:
        payload = json.loads(path.read_text())
        text = payload.get("input_text", "")
        if not isinstance(text, str) or not 1000 <= len(text) <= 2000:
            continue
        for field in (
            "revision",
            "environment",
            "hardware",
            "db_image",
            "client_image",
            "db_version",
            "cold_condition",
            "period",
        ):
            if payload.get(field) == "":
                payload[field] = defaults[field]
        manifest = Manifest.model_validate(payload)
        assert manifest.input_length == len(text)
        long_inputs.append(manifest.input_text)
    assert long_inputs, "Provide a frozen long-input measurement manifest"
