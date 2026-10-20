import json
from copy import deepcopy
from pathlib import Path

import pytest

from digital_souls_memory.evaluation.comparison import (
    parameter_grid,
    resolved_protocol,
    tuning_candidates,
)
from digital_souls_memory.evaluation.models import record, rows, validate_suite

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("enabled", [False, True])
def test_tuning_uses_candidate_specific_score_grid(
    candidate: dict[str, object],
    new_protocol: dict[str, object],
    enabled: bool,
) -> None:
    if enabled:
        candidate["reranker"] = {
            "model_alias": "synthetic-reranker",
            "revision": "synthetic",
            "sha256": "b" * 64,
            "quantization": "synthetic",
            "max_context_length": 8192,
        }
    expanded = tuning_candidates([candidate], protocol=new_protocol)
    thresholds = {record(c["parameters"])["threshold"] for c in expanded}
    if enabled:
        assert any(float(str(value)) < 0 for value in thresholds)
        assert any(float(str(value)) > 1 for value in thresholds)
    else:
        assert [c["parameters"] for c in expanded] == parameter_grid()
    for item in expanded:
        resolved = resolved_protocol(new_protocol, item)
        assert resolved["threshold"] == record(item["parameters"])["threshold"]


@pytest.mark.parametrize("split", ["tuning", "acceptance"])
def test_new_suite_preserves_original_inputs_and_gold_and_adds_valid_sources(split: str) -> None:
    root = Path(__file__).resolve().parents[3] / "evals/retrieval"
    old = record(json.loads((root / "memory-v2" / split / "suite.json").read_bytes()))
    new = record(json.loads((root / "memory-v2.1" / split / "suite.json").read_bytes()))
    validate_suite(new, tuning_case_ids=set())
    assert record(new["protocol"])["comparison_version"] == "memory-v2.1"
    assert new["gold"] == old["gold"]
    assert (root / "memory-v2.1" / split / "gold.json").read_bytes() == (
        root / "memory-v2" / split / "gold.json"
    ).read_bytes()
    stripped = deepcopy(new["cases"])
    assert isinstance(stripped, list)
    for case in stripped:
        for memory in case["memories"]:
            memory.pop("source_spans", None)
    assert stripped == old["cases"]


def test_new_suite_cannot_weaken_explicit_safety_classification(
    new_suite: dict[str, object],
) -> None:
    validate_suite(new_suite, tuning_case_ids=set())
    gold = rows(new_suite["gold"])[0]
    del gold["safety_forbidden_ids"]
    new_suite["gold"] = [gold]
    with pytest.raises((ValueError, KeyError)):
        validate_suite(new_suite, tuning_case_ids=set())


def test_new_suite_requires_declared_sources_on_eligible_memories(
    new_suite: dict[str, object],
) -> None:
    case = rows(new_suite["cases"])[0]
    memory = rows(case["memories"])[0]
    del memory["source_spans"]
    case["memories"] = [memory]
    new_suite["cases"] = [case]
    with pytest.raises((ValueError, KeyError)):
        validate_suite(new_suite, tuning_case_ids=set())
