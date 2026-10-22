"""Pure aggregation must never turn missing or fake measurements into a performance PASS."""

import pytest

from tests.st.latency import LatencySample, summarize_latency

pytestmark = pytest.mark.ut


def samples(elapsed: list[float]) -> tuple[LatencySample, ...]:
    return tuple(
        LatencySample(elapsed_ms=value, success=True, provision_id=f"provided-{index}")
        for index, value in enumerate(elapsed)
    )


def test_nearest_rank_percentiles_include_slow_samples() -> None:
    result = summarize_latency(samples(list(range(1, 101))), expected_count=100, real_models=True)
    assert result.p50_ms == 50
    assert result.p95_ms == 95
    assert result.required_pass
    assert result.p95_target_pass
    assert result.p50_target_pass


@pytest.mark.parametrize("elapsed,passed", [(599.9, True), (600.0, True), (600.1, False)])
def test_required_p95_has_inclusive_600ms_boundary(elapsed: float, passed: bool) -> None:
    result = summarize_latency(samples([elapsed] * 100), expected_count=100, real_models=True)
    assert result.p95_ms == elapsed
    assert result.required_pass is passed
    assert result.p95_target_pass is False
    assert result.p50_target_pass is False


@pytest.mark.parametrize(
    "elapsed,p50,p95",
    [(150.0, True, True), (150.1, False, True), (300.0, False, True), (300.1, False, False)],
)
def test_targets_are_reported_separately_from_required_limit(
    elapsed: float, p50: bool, p95: bool
) -> None:
    result = summarize_latency(samples([elapsed] * 100), expected_count=100, real_models=True)
    assert result.required_pass
    assert result.p50_target_pass is p50
    assert result.p95_target_pass is p95


def test_samples_over_core_cutoff_are_not_discarded() -> None:
    result = summarize_latency(
        samples([100.0] * 94 + [900.0] * 6), expected_count=100, real_models=True
    )
    assert result.p95_ms == 900
    assert not result.required_pass


@pytest.mark.parametrize("count", [0, 99, 101])
def test_missing_or_extra_measurements_cannot_pass(count: int) -> None:
    result = summarize_latency(samples([1.0] * count), expected_count=100, real_models=True)
    assert not result.required_pass


@pytest.mark.parametrize("elapsed", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_invalid_duration_cannot_pass(elapsed: float) -> None:
    result = summarize_latency(
        samples([1.0] * 99 + [elapsed]), expected_count=100, real_models=True
    )
    assert not result.required_pass


@pytest.mark.parametrize("success,provision_id", [(False, None), (True, None)])
def test_failed_or_uncommitted_trial_cannot_be_dropped(
    success: bool, provision_id: str | None
) -> None:
    values = samples([1.0] * 99) + (
        LatencySample(elapsed_ms=900.0, success=success, provision_id=provision_id),
    )
    result = summarize_latency(values, expected_count=100, real_models=True)
    assert not result.required_pass


def test_fake_functional_measurements_cannot_claim_real_model_performance() -> None:
    result = summarize_latency(samples([1.0] * 100), expected_count=100, real_models=False)
    assert result.p95_ms == 1
    assert not result.required_pass
