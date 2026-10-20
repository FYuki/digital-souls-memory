import json
from collections.abc import Callable
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

from digital_souls_memory.evaluation.models import (
    load_suite,
    strings,
    validate_candidate,
    validate_protocol,
    validate_suite,
)
from digital_souls_memory.evaluation.retrieval import eligible_memories

pytestmark = pytest.mark.ut


def test_v2_suite_loads_with_a_pinned_digest(
    tmp_path: Path,
    v2_suite: dict[str, object],
) -> None:
    payload = json.dumps(v2_suite).encode()
    path = tmp_path / "suite.json"
    path.write_bytes(payload)
    assert load_suite(path, expected_sha256=sha256(payload).hexdigest()) == v2_suite


@pytest.mark.parametrize(
    "fault", ["missing-safety", "missing-quality", "overlap", "unknown", "union"]
)
def test_v2_gold_rejects_missing_or_ambiguous_forbidden_classification(
    v2_suite: dict[str, object],
    memory: dict[str, object],
    fault: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    validate_suite(v2_suite, tuning_case_ids=set())
    case = cast(list[dict[str, object]], v2_suite["cases"])[0]
    case["memories"] = [memory, {**memory, "id": "m2"}]
    gold = cast(list[dict[str, object]], v2_suite["gold"])[0]
    gold["forbidden_ids"] = ["m2"]
    gold["safety_forbidden_ids"] = ["m2"]
    if fault == "missing-safety":
        del gold["safety_forbidden_ids"]
    elif fault == "missing-quality":
        del gold["quality_forbidden_ids"]
    elif fault == "overlap":
        gold["quality_forbidden_ids"] = ["m2"]
    elif fault == "unknown":
        gold["safety_forbidden_ids"] = ["unknown"]
    else:
        gold["safety_forbidden_ids"] = []
    with rejected():
        validate_suite(v2_suite, tuning_case_ids=set())


def test_shipped_v2_inputs_preserve_queries_required_sets_and_explicit_safety() -> None:
    root = Path(__file__).resolve().parents[3] / "evals/retrieval"
    safety_categories = {
        "multisource_revocation",
        "deleted_source",
        "deleted_memory",
        "binding_character",
        "update",
        "fact_version",
        "fact_revocation",
    }
    for split in ("tuning", "acceptance"):
        old = cast(dict[str, object], json.loads((root / split / "suite.json").read_text()))
        new = cast(
            dict[str, object], json.loads((root / "memory-v2" / split / "suite.json").read_text())
        )
        validate_suite(new, tuning_case_ids=set())
        assert cast(dict[str, object], new["protocol"])["comparison_version"] == "memory-v2"
        assert new["cases"] == old["cases"]
        old_gold = {g["case_id"]: g for g in cast(list[dict[str, object]], old["gold"])}
        for gold in cast(list[dict[str, object]], new["gold"]):
            original = old_gold[gold["case_id"]]
            assert gold["required_ids"] == original["required_ids"]
            safety = set(strings(gold["safety_forbidden_ids"]))
            quality = set(strings(gold["quality_forbidden_ids"]))
            assert safety | quality == set(strings(original["forbidden_ids"]))
            assert not safety & quality
            case = next(
                c for c in cast(list[dict[str, object]], new["cases"]) if c["id"] == gold["case_id"]
            )
            if case["category"] in safety_categories:
                assert safety == set(strings(original["forbidden_ids"]))
            assert not safety & {m["id"] for m in eligible_memories(case)}


@pytest.mark.parametrize("fault", ["sum", "recency", "seventh"])
def test_v2_preserves_six_component_weight_constraints(
    v2_protocol: dict[str, object],
    fault: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    validate_protocol(v2_protocol)
    weights = deepcopy(cast(dict[str, object], v2_protocol["weights"]))
    if fault == "sum":
        weights["semantic"] = 0.5
    elif fault == "recency":
        weights["semantic"] = 0.4
        weights["recency"] = 0.2
    else:
        weights["reranker"] = 0.0
    with rejected():
        validate_protocol({**v2_protocol, "weights": weights})


@pytest.mark.parametrize("alias", ["Qwen3-Embedding-0.6B", "ruri-v3-310m"])
@pytest.mark.parametrize(
    "field",
    [
        "revision",
        "sha256",
        "pooling",
        "quantization",
        "query_prefix",
        "passage_prefix",
        "max_context_length",
    ],
)
def test_additional_model_cannot_execute_with_unresolved_conditions(
    candidate: dict[str, object],
    alias: str,
    field: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    resolved = {
        **candidate,
        "model_alias": alias,
        "quantization": "synthetic",
        "max_context_length": 8192,
    }
    validate_candidate(resolved)
    unresolved = {**resolved, field: None}
    with rejected():
        validate_candidate(unresolved)
