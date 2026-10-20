import math
from typing import cast

import pytest

from digital_souls_memory.evaluation.retrieval import rerank_candidates, select_observations

pytestmark = pytest.mark.ut


def test_rerank_candidate_limit_uses_id_order_for_composite_ties() -> None:
    observations: list[dict[str, object]] = [
        {"memory_id": f"m{i:02d}", "score": 0.1} for i in reversed(range(21))
    ]
    selected = rerank_candidates(observations)
    assert [o["memory_id"] for o in selected] == [f"m{i:02d}" for i in range(20)]
    assert observations[0]["memory_id"] == "m20"


@pytest.mark.parametrize("threshold", [-2.0, 0.0, 2.0])
def test_relevance_threshold_replaces_composite_threshold(
    new_protocol: dict[str, object], threshold: float
) -> None:
    observations: list[dict[str, object]] = [
        {"memory_id": "irrelevant", "score": 0.95, "reranker_score": threshold - 0.01},
        {"memory_id": "relevant", "score": 0.1, "reranker_score": threshold},
    ]
    selected = select_observations(observations, {**new_protocol, "threshold": threshold})
    assert [row["memory_id"] for row in selected] == ["relevant"]
    assert selected[0]["score"] == 0.1
    assert selected[0]["reranker_score"] == threshold


def test_no_match_is_empty_when_every_relevance_is_below_threshold(
    new_protocol: dict[str, object],
) -> None:
    observations: list[dict[str, object]] = [
        {"memory_id": "m1", "score": 0.99, "reranker_score": -3.0}
    ]
    assert select_observations(observations, {**new_protocol, "threshold": 0.0}) == []


def test_relevance_order_overrides_composite_order(new_protocol: dict[str, object]) -> None:
    observations: list[dict[str, object]] = [
        {"memory_id": "high-composite", "score": 0.9, "reranker_score": 1.0},
        {"memory_id": "high-relevance", "score": 0.6, "reranker_score": 5.0},
    ]
    assert [r["memory_id"] for r in select_observations(observations, new_protocol)] == [
        "high-relevance",
        "high-composite",
    ]


def test_disabled_reranker_keeps_inclusive_composite_threshold_and_id_ties(
    new_protocol: dict[str, object],
) -> None:
    observations: list[dict[str, object]] = [
        {"memory_id": identifier, "score": score, "reranker_score": None}
        for identifier, score in (("z", 0.5), ("below", 0.499), ("a", 0.5), ("first", 0.9))
    ]
    assert [r["memory_id"] for r in select_observations(observations, new_protocol)] == [
        "first",
        "a",
        "z",
    ]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True])
def test_nonfinite_or_boolean_relevance_cannot_enter_selection(
    new_protocol: dict[str, object], value: object
) -> None:
    observations: list[dict[str, object]] = [
        {"memory_id": "m1", "score": 0.9, "reranker_score": value}
    ]
    with pytest.raises(ValueError):
        select_observations(observations, new_protocol)


def test_unmeasured_candidates_are_not_selected_as_zero_relevance(
    new_protocol: dict[str, object],
) -> None:
    observations: list[dict[str, object]] = [
        {"memory_id": f"measured{i:02d}", "score": 0.9, "reranker_score": -1.0} for i in range(20)
    ]
    observations.append({"memory_id": "outside", "score": 0.1, "reranker_score": None})
    selected = select_observations(observations, {**new_protocol, "threshold": -2.0})
    assert [row["memory_id"] for row in selected] == [f"measured{i:02d}" for i in range(20)]
    assert cast(float, selected[0]["reranker_score"]) == -1.0
