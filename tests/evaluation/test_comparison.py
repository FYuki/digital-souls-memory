from copy import deepcopy

import pytest

from digital_souls_memory.evaluation.comparison import (
    comparison_protocol,
    parameter_grid,
    resolved_protocol,
    selected_candidates,
    tuning_candidates,
)
from digital_souls_memory.evaluation.models import rows, validate_protocol

pytestmark = pytest.mark.ut


def test_comparison_grid_and_candidate_settings_are_resolved_once() -> None:
    protocol = comparison_protocol()
    validate_protocol(protocol)
    grid = parameter_grid()
    assert len(grid) == 45
    assert {p["threshold"] for p in grid} == {i / 100 for i in range(20, 91, 5)}
    assert {p["weights"]["semantic"] for p in grid} == {0.55, 0.65, 0.75}  # type: ignore[index]
    before = deepcopy(protocol)
    resolved = resolved_protocol(protocol, {"parameters": grid[-1]})
    assert resolved["threshold"] == 0.9
    assert resolved["weights"]["semantic"] == 0.75  # type: ignore[index]
    assert protocol == before


def test_comparison_rejects_out_of_range_or_altered_rules() -> None:
    protocol = comparison_protocol()
    with pytest.raises(ValueError):
        resolved_protocol(
            protocol, {"parameters": {"threshold": 0.52, "weights": parameter_grid()[0]["weights"]}}
        )
    protocol["token_budget"] = 1499
    with pytest.raises(ValueError):
        validate_protocol(protocol)


def test_selection_uses_only_complete_tuning_and_declared_tie_breaks(
    candidate: dict[str, object],
) -> None:
    expanded = tuning_candidates([candidate])
    report: dict[str, object] = {
        "suite": {"split": "tuning"},
        "candidates": expanded,
        "runs": [
            {
                "candidate_id": c["id"],
                "execution_error": None,
                "case_results": [
                    {
                        "sources_valid": True,
                        "threshold_passed": True,
                        "forbidden_returned": [],
                        "order_passed": True,
                        "recall_at_5": 1.0,
                        "no_match_correct": None,
                        "precision": 1.0,
                    }
                ],
                "categories": {"detail": {"passed_count": 1, "case_count": 1}},
            }
            for c in expanded
            for _ in range(3)
        ],
    }
    selected = selected_candidates(report, [candidate])
    assert selected[0]["parameters"] == parameter_grid()[-1]
    for run in rows(report["runs"]):
        results = run["case_results"]
        assert isinstance(results, list)
        result = results[0]
        assert isinstance(result, dict)
        result["sources_valid"] = False
        if run["candidate_id"] == expanded[-1]["id"]:
            result["threshold_passed"] = False
    # Two failed gates in one case must count separately before quality and parameter ties.
    assert selected_candidates(report, [candidate])[0]["parameters"] == parameter_grid()[29]
    report["suite"] = {"split": "acceptance"}
    with pytest.raises(ValueError):
        selected_candidates(report, [candidate])
    report["suite"] = {"split": "tuning"}
    report["runs"] = []
    with pytest.raises(ValueError):
        selected_candidates(report, [candidate])
