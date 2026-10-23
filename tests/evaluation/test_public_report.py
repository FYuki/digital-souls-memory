import json
from collections.abc import Callable
from copy import deepcopy
from typing import cast

import pytest

from digital_souls_memory.evaluation.models import record, rows
from digital_souls_memory.evaluation.report import public_report, validate_public_report
from digital_souls_memory.evaluation.scoring import aggregate_run, evaluate_case

pytestmark = pytest.mark.ut


@pytest.fixture
def internal_report(
    make_suite: Callable[[], dict[str, object]],
    case: dict[str, object],
    gold: dict[str, object],
    hit: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object]:
    result = evaluate_case(case, gold, [hit], record(make_suite()["protocol"]))
    summary = aggregate_run([result])
    return {
        "mode": "fixture",
        "created_at": "2026-10-20T00:00:00Z",
        "suite": make_suite(),
        "candidates": [candidate],
        "quality_passed": None,
        "runs": [
            {
                "candidate_id": candidate["id"],
                "run_index": index,
                "real_model_calls": 0,
                "execution_error": None,
                "quality_passed": None,
                "case_results": [result],
                **summary,
            }
            for index in range(1, 4)
        ],
    }


def test_public_report_retains_verdicts_without_source_or_unknown_fields(
    make_suite: Callable[[], dict[str, object]],
    candidate: dict[str, object],
    internal_report: dict[str, object],
    manifest: dict[str, object],
) -> None:
    report = internal_report
    report["manifest"] = {**manifest, "private_path": "/home/synthetic/secret"}
    report["private_log"] = "/home/synthetic/private.log"
    cast(list[dict[str, object]], report["runs"])[0]["private_log"] = "secret"
    before = deepcopy(report)
    published = public_report(report)
    assert report == before
    assert "suite" not in published
    assert "private_log" not in published
    for internal, public in zip(rows(report["runs"]), rows(published["runs"]), strict=True):
        assert "private_log" not in public
        assert public["quality_passed"] is internal["quality_passed"] is None
        assert public["real_model_calls"] == 0
        assert public["case_count"] == internal["case_count"]
        result = rows(public["case_results"])[0]
        assert result["passed"] is True
        hit = rows(result["hits"])[0]
        assert hit["memory_id"] == "m1"
        span = rows(hit["source_spans"])[0]
        assert set(span) == {"turn_id", "start", "end", "sha256"}
        assert len(str(span["sha256"])) == 64
    assert record(published["manifest"]) == manifest
    inputs = record(published["input_sha256"])
    assert set(inputs) == {"suite", "cases", "gold", "protocol", "candidates"}
    assert all(len(str(value)) == 64 for value in inputs.values())
    assert "/home/synthetic" not in json.dumps(published)
    validate_public_report(published, suite=make_suite(), candidates=[candidate])
    cast(dict[str, object], published["input_sha256"])["suite"] = "0" * 64
    with pytest.raises(ValueError):
        validate_public_report(published, suite=make_suite(), candidates=[candidate])


def test_public_report_rejects_forged_results_before_projection(
    internal_report: dict[str, object],
) -> None:
    report = internal_report
    cast(list[dict[str, object]], report["runs"])[0]["passed"] = False
    with pytest.raises(ValueError):
        public_report(report)
