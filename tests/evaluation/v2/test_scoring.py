from collections.abc import Callable
from copy import deepcopy
from typing import cast

import pytest

from digital_souls_memory.evaluation.models import string
from digital_souls_memory.evaluation.scoring import aggregate_run, evaluate_case

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("excluded", [False, True])
def test_safety_eligibility_cannot_be_hidden_by_empty_results(
    case: dict[str, object],
    memory: dict[str, object],
    v2_gold: dict[str, object],
    v2_protocol: dict[str, object],
    excluded: bool,
) -> None:
    blocked = {**memory, "id": "blocked"}
    if excluded:
        blocked["valid_to"] = case["as_of"]
    case["memories"] = [memory, blocked]
    v2_gold.update(forbidden_ids=["blocked"], safety_forbidden_ids=["blocked"])
    assert evaluate_case(case, v2_gold, [], v2_protocol)["safety_passed"] is excluded


def test_empty_recall_failure_does_not_fail_safety(
    case: dict[str, object], v2_gold: dict[str, object], v2_protocol: dict[str, object]
) -> None:
    v2_gold["required_source_spans"] = [{"turn_id": "t1", "text": "青い駅"}]
    result = evaluate_case(case, v2_gold, [], v2_protocol)
    assert result["safety_passed"] is True
    assert result["quality_passed"] is False
    assert result["recall_at_5"] == 0.0


@pytest.mark.parametrize("position", [1, 5, 6])
def test_real_quality_uses_top_five_membership_instead_of_required_order(
    case: dict[str, object],
    memory: dict[str, object],
    hit: dict[str, object],
    v2_gold: dict[str, object],
    v2_protocol: dict[str, object],
    position: int,
) -> None:
    cast(list[dict[str, object]], case["turns"])[0]["text"] = "原文"
    hit = {
        **hit,
        "source_spans": [
            {**cast(list[dict[str, object]], hit["source_spans"])[0], "text": "原文", "end": 2}
        ],
    }
    case["memories"] = [memory, *[{**memory, "id": f"d{i}"} for i in range(5)]]
    hits = [{**hit, "memory_id": f"d{i}"} for i in range(position - 1)] + [hit]
    v2_gold["required_order"] = ["m1"]
    result = evaluate_case(case, v2_gold, hits, v2_protocol)
    assert result["quality_passed"] is (position <= 5)
    assert result["recall_at_5"] == (1.0 if position <= 5 else 0.0)
    assert result["safety_passed"] is True


@pytest.mark.parametrize("safety", [False, True])
def test_forbidden_ids_have_explicit_safety_or_quality_meaning(
    case: dict[str, object],
    memory: dict[str, object],
    hit: dict[str, object],
    v2_gold: dict[str, object],
    v2_protocol: dict[str, object],
    safety: bool,
) -> None:
    case["memories"] = [memory, {**memory, "id": "m2"}]
    v2_gold["forbidden_ids"] = ["m2"]
    v2_gold["safety_forbidden_ids" if safety else "quality_forbidden_ids"] = ["m2"]
    result = evaluate_case(case, v2_gold, [hit, {**hit, "memory_id": "m2"}], v2_protocol)
    assert result["safety_passed"] is (not safety)
    assert result["passed"] is (not safety)
    assert result["quality_passed"] is True
    assert result["precision"] == 0.5


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", "改変した合成原文"),
        ("start", -1),
        ("end", 10000),
        ("actor", "other"),
        ("language", "en"),
        ("turn_id", "unknown"),
    ],
)
def test_invalid_returned_source_is_a_safety_failure(
    case: dict[str, object],
    hit: dict[str, object],
    v2_gold: dict[str, object],
    v2_protocol: dict[str, object],
    field: str,
    value: object,
) -> None:
    span = {**cast(list[dict[str, object]], hit["source_spans"])[0], field: value}
    result = evaluate_case(case, v2_gold, [{**hit, "source_spans": [span]}], v2_protocol)
    assert result["safety_passed"] is False


@pytest.mark.parametrize(
    "recall_passes,no_match_passes,expected",
    [
        (9, 7, True),
        (8, 7, False),
        (9, 6, False),
    ],
)
def test_threshold_positive_and_no_match_cases_have_separate_gates(
    case: dict[str, object],
    hit: dict[str, object],
    v2_gold: dict[str, object],
    v2_protocol: dict[str, object],
    recall_passes: int,
    no_match_passes: int,
    expected: bool,
) -> None:
    results = []
    for positive, passes in ((True, recall_passes), (False, no_match_passes)):
        for index in range(10):
            identifier = f"threshold-{positive}-{index}"
            row = {**case, "id": identifier, "category": "threshold"}
            expected_gold = {
                **v2_gold,
                "case_id": identifier,
                "required_ids": ["m1"] if positive else [],
            }
            hits = [hit] if (index < passes) is positive else []
            results.append(evaluate_case(row, expected_gold, hits, v2_protocol))
    # Gate evaluation belongs to each run, never to pooled repetition samples.
    summary = aggregate_run(results, protocol=v2_protocol)
    assert summary["passed"] is expected
    assert summary["safety_passed"] is True
    gates = cast(dict[str, object], summary["quality_gates"]["threshold"])
    assert cast(dict[str, object], gates["recall"])["pass_rate"] == recall_passes / 10
    assert cast(dict[str, object], gates["no_match"])["pass_rate"] == no_match_passes / 10


def test_one_safety_failure_cannot_be_hidden_by_quality_successes(
    case: dict[str, object],
    hit: dict[str, object],
    v2_gold: dict[str, object],
    v2_protocol: dict[str, object],
) -> None:
    results = [
        evaluate_case(
            {**case, "id": f"c{i}"}, {**v2_gold, "case_id": f"c{i}"}, [deepcopy(hit)], v2_protocol
        )
        for i in range(10)
    ]
    bad = {**hit, "source_spans": []}
    results[0] = evaluate_case(case, v2_gold, [bad], v2_protocol)
    summary = aggregate_run(results, protocol=v2_protocol)
    assert summary["safety_passed"] is False
    assert summary["passed"] is False


@pytest.mark.parametrize("reranked", [False, True])
def test_score_ranges_use_all_candidates_and_one_highest_incorrect_per_case(
    case: dict[str, object],
    memory: dict[str, object],
    v2_gold: dict[str, object],
    v2_protocol: dict[str, object],
    observation_factory: Callable[[str, float], dict[str, object]],
    reranked: bool,
) -> None:
    results = []
    for index, correct in enumerate((0.1, 0.9)):
        identifier = f"range-{index}"
        row = {
            **case,
            "id": identifier,
            "memories": [memory, {**memory, "id": "other"}, {**memory, "id": "low"}],
        }
        scored = evaluate_case(row, {**v2_gold, "case_id": identifier}, [], v2_protocol)
        scored["candidate_observations"] = [
            observation_factory("m1", correct),
            observation_factory("other", 0.6),
            observation_factory("low", 0.2),
        ]
        if reranked:
            for observation in cast(list[dict[str, object]], scored["candidate_observations"]):
                observation["reranker_score"] = -4.0 if index == 0 else 2.0
        results.append(scored)
    summary = aggregate_run(results, protocol=v2_protocol)
    ranges = cast(dict[str, object], summary["score_ranges"]["detail"])
    correct_range = cast(dict[str, object], cast(dict[str, object], ranges["composite"])["correct"])
    incorrect_range = cast(
        dict[str, object], cast(dict[str, object], ranges["composite"])["highest_incorrect"]
    )
    assert correct_range["min"] == 0.1
    assert correct_range["max"] == 0.9
    assert correct_range["p05"] == pytest.approx(0.14)
    assert correct_range["p50"] == pytest.approx(0.5)
    assert correct_range["p95"] == pytest.approx(0.86)
    assert incorrect_range["count"] == 2
    assert incorrect_range["min"] == incorrect_range["max"] == 0.6
    if reranked:
        reranker = cast(dict[str, object], cast(dict[str, object], ranges["reranker"])["correct"])
        assert reranker["min"] == -4.0
        assert reranker["max"] == 2.0
        assert reranker["p50"] == -1.0
    else:
        assert ranges["reranker"] is None


@pytest.mark.parametrize("required", [False, True])
def test_absent_score_groups_remain_unmeasured(
    case: dict[str, object],
    v2_gold: dict[str, object],
    v2_protocol: dict[str, object],
    observation_factory: Callable[[str, float], dict[str, object]],
    required: bool,
) -> None:
    gold = {**v2_gold, "required_ids": ["m1"] if required else []}
    result = evaluate_case(case, gold, [], v2_protocol)
    result["candidate_observations"] = [observation_factory("m1", 0.4)]
    summary = aggregate_run([result], protocol=v2_protocol)
    ranges = cast(
        dict[str, object],
        cast(dict[str, object], summary["score_ranges"]["detail"])["composite"],
    )
    assert ranges["highest_incorrect" if required else "correct"] is None


def test_valid_partial_source_does_not_fail_recall_with_all_required_ids(
    case: dict[str, object],
    hit: dict[str, object],
    v2_gold: dict[str, object],
    v2_protocol: dict[str, object],
) -> None:
    v2_gold["required_source_spans"] = [{"turn_id": "t1", "text": "青い駅"}]
    original = cast(list[dict[str, object]], hit["source_spans"])[0]
    partial = {**original, "end": 2, "text": string(original["text"])[:2]}
    result = evaluate_case(case, v2_gold, [{**hit, "source_spans": [partial]}], v2_protocol)
    assert result["safety_passed"] is True
    assert result["quality_passed"] is True
    assert result["passed"] is True


@pytest.mark.parametrize("complete", [False, True])
def test_equivalent_order_requires_all_ids_but_not_their_order(
    case: dict[str, object],
    memory: dict[str, object],
    hit: dict[str, object],
    v2_gold: dict[str, object],
    v2_protocol: dict[str, object],
    complete: bool,
) -> None:
    case.update(category="equivalent_order", memories=[memory, {**memory, "id": "m2"}])
    v2_gold.update(required_ids=["m1", "m2"], required_order=["m1", "m2"])
    hits = [{**hit, "memory_id": "m2"}, hit] if complete else [hit]
    result = evaluate_case(case, v2_gold, hits, v2_protocol)
    assert result["passed"] is complete
    assert result["quality_passed"] is complete
    assert result["safety_passed"] is True
    assert result["recall_at_5"] == (1.0 if complete else 0.5)


def test_false_positive_counts_include_unlisted_ids_only_in_returned_top_five(
    case: dict[str, object],
    memory: dict[str, object],
    hit: dict[str, object],
    v2_gold: dict[str, object],
    v2_protocol: dict[str, object],
) -> None:
    case["memories"] = [{**memory, "id": f"m{i}"} for i in range(1, 7)]
    cast(list[dict[str, object]], case["turns"])[0]["text"] = "原文"
    hit = {
        **hit,
        "source_spans": [
            {**cast(list[dict[str, object]], hit["source_spans"])[0], "text": "原文", "end": 2}
        ],
    }
    v2_gold.update(forbidden_ids=["m2"], quality_forbidden_ids=["m2"])
    hits = [{**hit, "memory_id": f"m{i}"} for i in range(1, 7)]
    first = evaluate_case(case, v2_gold, hits, v2_protocol)
    second = evaluate_case({**case, "id": "case-b"}, v2_gold, [hit], v2_protocol)
    assert first["false_positive_count"] == 4
    assert first["precision"] == 0.2
    assert second["false_positive_count"] == 0
    summary = aggregate_run([first, second], protocol=v2_protocol)
    assert cast(dict[str, object], summary["categories"]["detail"])["false_positive_count"] == 4


@pytest.mark.parametrize("category", ["unrelated", "threshold"])
@pytest.mark.parametrize("returned", [False, True])
def test_no_match_requires_empty_results_even_without_quality_forbidden_ids(
    case: dict[str, object],
    hit: dict[str, object],
    v2_gold: dict[str, object],
    v2_protocol: dict[str, object],
    category: str,
    returned: bool,
) -> None:
    result = evaluate_case(
        {**case, "category": category},
        {**v2_gold, "required_ids": []},
        [hit] if returned else [],
        v2_protocol,
    )
    assert result["passed"] is (not returned)
    assert result["quality_passed"] is (not returned)
    assert result["no_match_correct"] is (not returned)
    assert result["safety_passed"] is True
