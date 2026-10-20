import math
from collections.abc import Callable

import pytest

from digital_souls_memory.evaluation.models import validate_vectors
from digital_souls_memory.evaluation.scoring import aggregate_run, percentile, score_case

pytestmark = pytest.mark.ut


def test_correct_result_measures_recall_and_precision(
    case: dict[str, object], gold: dict[str, object], hit: dict[str, object]
) -> None:
    result = score_case(case, gold, [hit], threshold=0.5)
    assert result["passed"] is True
    assert result["recall_at_5"] == 1.0
    assert result["precision"] == 1.0


def test_missing_required_result_is_quality_failure(
    case: dict[str, object], gold: dict[str, object]
) -> None:
    result = score_case(case, gold, [], threshold=0.5)
    assert result["passed"] is False
    assert result["recall_at_5"] == 0.0


def test_forbidden_result_fails_even_when_required_result_is_present(
    case: dict[str, object],
    gold: dict[str, object],
    hit: dict[str, object],
    memory: dict[str, object],
) -> None:
    case["memories"] = [memory, {**memory, "id": "m2"}]
    gold["forbidden_ids"] = ["m2"]
    result = score_case(case, gold, [hit, {**hit, "memory_id": "m2"}], threshold=0.5)
    assert result["passed"] is False
    assert result["recall_at_5"] == 1.0
    assert result["precision"] == 0.5


@pytest.mark.parametrize("returned", [False, True], ids=["empty", "false-positive"])
def test_no_match_is_scored_separately_from_recall(
    case: dict[str, object], gold: dict[str, object], hit: dict[str, object], returned: bool
) -> None:
    gold["required_ids"] = []
    result = score_case(case, gold, [hit] if returned else [], threshold=0.5)
    assert result["passed"] is (not returned)
    assert result["recall_at_5"] is None
    assert result["no_match_correct"] is (not returned)


@pytest.mark.parametrize("ids", [["unknown"], ["m1", "m1"]], ids=["unknown", "duplicate"])
def test_invalid_observation_ids_are_rejected(
    case: dict[str, object],
    gold: dict[str, object],
    hit: dict[str, object],
    ids: list[str],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    with rejected():
        score_case(case, gold, [{**hit, "memory_id": value} for value in ids], threshold=0.5)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True])
def test_non_numeric_or_non_finite_scores_are_rejected(
    case: dict[str, object],
    gold: dict[str, object],
    hit: dict[str, object],
    value: float,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    with rejected():
        score_case(case, gold, [{**hit, "score": value}], threshold=0.5)


def test_sixth_result_cannot_satisfy_recall_at_five(
    case: dict[str, object],
    gold: dict[str, object],
    hit: dict[str, object],
    memory: dict[str, object],
) -> None:
    case["memories"] = [memory] + [{**memory, "id": f"d{i}"} for i in range(5)]
    hits = [{**hit, "memory_id": f"d{i}"} for i in range(5)] + [hit]
    result = score_case(case, gold, hits, threshold=0.5)
    assert result["recall_at_5"] == 0.0
    assert result["passed"] is False


def test_category_aggregation_does_not_average_away_forbidden_hit(
    case: dict[str, object], gold: dict[str, object], hit: dict[str, object]
) -> None:
    passed = score_case(case, gold, [hit], threshold=0.5)
    failed = score_case(
        case, {**gold, "required_ids": [], "forbidden_ids": ["m1"]}, [hit], threshold=0.5
    )
    result = aggregate_run([passed, {**failed, "case_id": "case-b"}])
    assert result["passed"] is False
    assert result["case_count"] == 2
    assert result["categories"]["detail"]["case_count"] == 2


def test_category_metrics_separate_no_match_from_positive_recall(
    case: dict[str, object], gold: dict[str, object], hit: dict[str, object]
) -> None:
    positive = score_case(case, gold, [hit], threshold=0.5)
    no_match = score_case({**case, "id": "case-b"}, {**gold, "required_ids": []}, [], threshold=0.5)
    category = aggregate_run([positive, no_match])["categories"]["detail"]
    assert category["recall_at_5"] == 1.0
    assert category["precision"] == 0.5
    assert category["no_match_correct_rate"] == 1.0


@pytest.mark.parametrize("duplicate", [False, True], ids=["empty", "duplicate"])
def test_empty_or_duplicate_case_run_is_rejected(
    case: dict[str, object],
    gold: dict[str, object],
    hit: dict[str, object],
    duplicate: bool,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    row = score_case(case, gold, [hit], threshold=0.5)
    results = [row, row] if duplicate else []
    with rejected():
        aggregate_run(results)


@pytest.mark.parametrize("quantile,expected", [(0.5, 10.5), (0.95, 19.05)])
def test_percentiles_use_declared_linear_interpolation(quantile: float, expected: float) -> None:
    assert percentile(list(range(20, 0, -1)), quantile, method="linear") == pytest.approx(expected)


@pytest.mark.parametrize("values", [[], [math.nan], [math.inf], [True]])
def test_invalid_latency_samples_are_rejected(
    values: list[float], rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    with rejected():
        percentile(values, 0.95, method="linear")


def test_valid_vectors_preserve_order() -> None:
    vectors = validate_vectors([[1.0, 0.0], [0.0, -2.0]], count=2, dimensions=2)
    assert [list(vector) for vector in vectors] == [[1.0, 0.0], [0.0, -2.0]]


@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [[1.0, 0.0]],
        [[1.0], [0.0, 1.0]],
        [[0.0, 0.0], [0.0, 1.0]],
        [[math.nan, 1.0], [0.0, 1.0]],
        [[math.inf, 1.0], [0.0, 1.0]],
        [[True, 1.0], [0.0, 1.0]],
    ],
    ids=["empty", "count", "dimensions", "zero", "nan", "infinity", "bool"],
)
def test_invalid_vectors_are_rejected(
    vectors: list[list[float]], rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    with rejected():
        validate_vectors(vectors, count=2, dimensions=2)
