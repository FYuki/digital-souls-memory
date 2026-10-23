import json
from collections.abc import Callable
from pathlib import Path

import pytest

from digital_souls_memory.evaluation.models import (
    record,
    rows,
    string,
    strings,
    validate_case,
    validate_case_mapping,
)
from digital_souls_memory.evaluation.retrieval import eligible_memories

pytestmark = pytest.mark.ut


def test_inventory_matches_the_pinned_source_snapshot_without_core_runtime() -> None:
    root = Path(__file__).resolve().parents[2] / "evals" / "retrieval"
    snapshot = json.loads((root / "source-inventory.json").read_text())
    provenance = json.loads((root / "provenance.json").read_text())
    assert snapshot["core_commit"] == provenance["core_commit"]
    assert [{"path": f["path"], "sha256": f["sha256"]} for f in snapshot["files"]] == provenance[
        "files"
    ]
    for split, path in (
        ("acceptance", "evals/semantic/cases.json"),
        ("tuning", "evals/semantic/tuning/cases.json"),
    ):
        source_ids = next(f["source_ids"] for f in snapshot["files"] if f["path"] == path)
        assert (
            sorted(r["source_case_id"] for r in provenance["cases"] if r["source_split"] == split)
            == source_ids
        )
    assert all(row["remaining_work"] is None for row in provenance["cases"])


def test_rebuilt_threshold_and_exact_tie_cases_use_memory_rules() -> None:
    from digital_souls_memory.evaluation.retrieval import rank_results, source_spans
    from digital_souls_memory.evaluation.scoring import evaluate_case

    root = Path(__file__).resolve().parents[2] / "evals" / "retrieval"
    boundaries = rows(json.loads((root / "boundary-fixtures.json").read_text()))
    assert len(boundaries) == 15
    for boundary in boundaries:
        suite = record(json.loads((root / string(boundary["split"]) / "suite.json").read_text()))
        case = next(c for c in rows(suite["cases"]) if c["id"] == boundary["case_id"])
        gold = next(g for g in rows(suite["gold"]) if g["case_id"] == case["id"])
        protocol = {**record(suite["protocol"]), "threshold": boundary["threshold"]}
        ranked = rank_results(rows(boundary["observations"]), protocol)
        assert [h["memory_id"] for h in ranked] == boundary["expected_order"]
        hits = [
            {**h, "source_spans": source_spans(case, memory_id=string(h["memory_id"]))}
            for h in ranked
        ]
        # Original spans may exceed the return budget; exclusion must remain a quality failure.
        scored = evaluate_case(case, gold, hits, protocol)
        assert scored["threshold_passed"] is True
        assert scored["passed"] is (len(rows(scored["hits"])) == len(ranked))
        for value, returned in ((0.49, False), (0.50, True), (0.51, True)):
            observed: list[dict[str, object]] = [
                {
                    "memory_id": "boundary",
                    "components": {key: value for key in record(protocol["weights"])},
                }
            ]
            assert bool(rank_results(observed, protocol)) is returned


def test_ported_assets_and_inventory_cover_both_source_splits() -> None:
    root = Path(__file__).resolve().parents[2] / "evals" / "retrieval"
    provenance = json.loads((root / "provenance.json").read_text())
    assert provenance["core_commit"] == "b062a49a32d261d0ce472938c40711c6cb41d7c0"
    inventory = rows(provenance["cases"])
    for split, count in (("acceptance", 62), ("tuning", 89)):
        source = [row for row in inventory if row["source_split"] == split]
        assert len(source) == count
        assert len({row["source_case_id"] for row in source}) == count
        cases = rows(json.loads((root / split / "cases.json").read_text()))
        gold = rows(json.loads((root / split / "gold.json").read_text()))
        by_id = {string(row["case_id"]): row for row in gold}
        assert set(by_id) == {case["id"] for case in cases}
        assert {
            row["target_case_id"] for row in source if row["target_case_id"] is not None
        } == set(by_id)
        for case in cases:
            validate_case(case)
            expected = by_id[string(case["id"])]
            known = {memory["id"] for memory in rows(case["memories"])}
            eligible = {memory["id"] for memory in eligible_memories(case)}
            assert set(strings(expected["required_ids"])) <= eligible
            assert set(strings(expected["forbidden_ids"])) <= known
        for row in source:
            assert string(row["reason"])
            if row["target_case_id"] is None:
                if row.get("replacement_test") is not None:
                    path, test = string(row["replacement_test"]).split("::")
                    assert f"def {test}(" in (root.parents[1] / path).read_text()
                else:
                    assert string(row["remaining_work"])


@pytest.fixture
def mapping() -> list[dict[str, object]]:
    return [
        {
            "source_split": "acceptance",
            "source_case_id": "source-a",
            "target_case_id": "case-a",
            "reason": "Memoryの原文区間で検証",
            "replacement_test": None,
        },
        {
            "source_split": "tuning",
            "source_case_id": "source-b",
            "target_case_id": None,
            "reason": "回答生成のライフサイクルは検索比較の対象外",
            "replacement_test": (
                "tests/evaluation/test_domain.py::test_missing_source_is_not_an_eligible_memory"
            ),
        },
    ]


def test_every_source_case_has_a_migration_or_explained_replacement(
    mapping: list[dict[str, object]],
) -> None:
    validate_case_mapping(
        mapping, source_ids={"acceptance:source-a", "tuning:source-b"}, target_ids={"case-a"}
    )


@pytest.mark.parametrize("fault", ["missing", "duplicate", "reason", "replacement", "target"])
def test_case_mapping_cannot_silently_discard_or_misidentify_cases(
    mapping: list[dict[str, object]],
    fault: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    if fault == "missing":
        mapping.pop()
    elif fault == "duplicate":
        mapping.append(mapping[0])
    elif fault == "reason":
        mapping[1]["reason"] = ""
    elif fault == "replacement":
        mapping[1]["replacement_test"] = None
    else:
        mapping[0]["target_case_id"] = "unknown-case"
    with rejected():
        validate_case_mapping(
            mapping, source_ids={"acceptance:source-a", "tuning:source-b"}, target_ids={"case-a"}
        )
