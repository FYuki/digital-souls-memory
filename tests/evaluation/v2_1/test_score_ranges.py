import pytest

from digital_souls_memory.evaluation.models import record
from digital_souls_memory.evaluation.scoring import score_ranges

pytestmark = pytest.mark.ut


def test_ranges_include_rejected_candidates_and_exclude_unmeasured_relevance() -> None:
    results: list[dict[str, object]] = []
    for index, correct in enumerate((-4.0, 2.0)):
        results.append(
            {
                "case_id": f"c{index}",
                "category": "detail",
                "required_ids": ["correct", "unmeasured"],
                "hits": [],
                "candidate_observations": [
                    {"memory_id": "correct", "score": 0.9, "reranker_score": correct},
                    {"memory_id": "unmeasured", "score": 0.1, "reranker_score": None},
                    {"memory_id": "other", "score": 0.6, "reranker_score": -5.0},
                    {"memory_id": "lower", "score": 0.2, "reranker_score": -8.0},
                    *[
                        {"memory_id": f"extra{i}", "score": 0.3, "reranker_score": -9.0}
                        for i in range(17)
                    ],
                ],
            }
        )
    results.append(
        {
            "case_id": "unrelated",
            "category": "unrelated",
            "required_ids": [],
            "candidate_observations": [],
        }
    )
    ranges = score_ranges(results)
    detail = record(record(ranges["detail"])["reranker"])
    correct_range = record(detail["correct"])
    assert correct_range["count"] == 2
    assert correct_range["min"] == -4.0
    assert correct_range["p05"] == pytest.approx(-3.7)
    assert correct_range["p50"] == -1.0
    assert correct_range["p95"] == pytest.approx(1.7)
    assert correct_range["max"] == 2.0
    assert record(detail["highest_incorrect"])["count"] == 2
    assert record(detail["highest_incorrect"])["max"] == -5.0
    assert record(ranges["unrelated"])["reranker"] is None
