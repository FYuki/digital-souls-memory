from collections.abc import Callable
from copy import deepcopy
from typing import cast

import pytest

from digital_souls_memory.evaluation.comparison import (
    parameter_grid,
    selected_candidates,
    tuning_candidates,
)
from digital_souls_memory.evaluation.scoring import aggregate_run, evaluate_case

pytestmark = pytest.mark.ut


@pytest.fixture
def tuning_report(
    candidate: dict[str, object],
    case: dict[str, object],
    hit: dict[str, object],
    v2_gold: dict[str, object],
    v2_suite: dict[str, object],
    v2_protocol: dict[str, object],
) -> dict[str, object]:
    expanded = tuning_candidates([candidate])
    runs = []
    for item in expanded:
        for index in range(1, 4):
            results = []
            for positive in (True, False):
                for i in range(10):
                    identifier = f"{positive}-{i}"
                    row = {
                        **case,
                        "id": identifier,
                        "category": "detail" if positive else "no_match",
                    }
                    expected = {
                        **v2_gold,
                        "case_id": identifier,
                        "required_ids": ["m1"] if positive else [],
                    }
                    results.append(
                        evaluate_case(row, expected, [hit] if positive else [], v2_protocol)
                    )
            runs.append(
                {
                    "candidate_id": item["id"],
                    "run_index": index,
                    "execution_error": None,
                    "case_results": results,
                    **aggregate_run(results, protocol=v2_protocol),
                }
            )
    return {"suite": {**v2_suite, "split": "tuning"}, "candidates": expanded, "runs": runs}


def test_v2_exact_selection_tie_uses_first_fixed_grid_entry(
    candidate: dict[str, object],
    tuning_report: dict[str, object],
) -> None:
    assert selected_candidates(tuning_report, [candidate])[0]["parameters"] == parameter_grid()[0]


def test_any_safety_violation_disqualifies_a_setting(
    candidate: dict[str, object],
    tuning_report: dict[str, object],
    v2_protocol: dict[str, object],
) -> None:
    runs = cast(list[dict[str, object]], tuning_report["runs"])
    for run in runs:
        if (
            run["candidate_id"]
            != cast(list[dict[str, object]], tuning_report["candidates"])[1]["id"]
        ):
            results = cast(list[dict[str, object]], run["case_results"])
            results[0]["safety_passed"] = False
            results[0]["passed"] = False
            run.update(aggregate_run(results, protocol=v2_protocol))
    assert selected_candidates(tuning_report, [candidate])[0]["parameters"] == parameter_grid()[1]


def test_no_safe_setting_is_rejected_instead_of_selecting_least_unsafe(
    candidate: dict[str, object],
    tuning_report: dict[str, object],
    v2_protocol: dict[str, object],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    for run in cast(list[dict[str, object]], tuning_report["runs"]):
        results = cast(list[dict[str, object]], run["case_results"])
        results[0]["safety_passed"] = False
        results[0]["passed"] = False
        run.update(aggregate_run(results, protocol=v2_protocol))
    with rejected():
        selected_candidates(tuning_report, [candidate])


@pytest.mark.parametrize(
    "priority", ["recall-category", "no-match-category", "mean-recall", "worst-repetition"]
)
def test_v2_selection_uses_declared_quality_priority_instead_of_violation_totals(
    candidate: dict[str, object],
    tuning_report: dict[str, object],
    v2_protocol: dict[str, object],
    priority: str,
) -> None:
    # Other grid entries are worse than both contenders without being unsafe.
    identifiers = [c["id"] for c in cast(list[dict[str, object]], tuning_report["candidates"])]
    for run in cast(list[dict[str, object]], tuning_report["runs"]):
        first = run["candidate_id"] == identifiers[0]
        second = run["candidate_id"] == identifiers[1]
        results = deepcopy(cast(list[dict[str, object]], run["case_results"]))
        positives = results[:10]
        negatives = results[10:]
        recall_failures, no_match_failures = (0, 0) if first or second else (5, 5)
        if priority == "recall-category":
            recall_failures, no_match_failures = (1, 3) if first else (2, 0)
        elif priority == "no-match-category":
            no_match_failures = 2 if first else 3
        elif priority == "mean-recall":
            positives[0]["recall_at_5"] = 0.5 if first else 0.0
            recall_failures = 1
        elif first:
            recall_failures = 2 if run["run_index"] == 1 else 0
        elif second:
            recall_failures = 1
        for row in positives[:recall_failures]:
            row.update(quality_passed=False, passed=False)
            if priority != "mean-recall":
                row["recall_at_5"] = 0.0
        for row in negatives[:no_match_failures]:
            row.update(quality_passed=False, passed=False, no_match_correct=False)
        run["case_results"] = results
        run.update(aggregate_run(results, protocol=v2_protocol))
    expected = 1 if priority == "worst-repetition" else 0
    selected = selected_candidates(tuning_report, [candidate])
    assert cast(dict[str, object], selected[0]["parameters"]) == parameter_grid()[expected]
