"""Client-side nearest-rank aggregation, including every scheduled trial."""

from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil, isfinite


@dataclass(frozen=True, kw_only=True)
class LatencySample:
    elapsed_ms: float
    success: bool
    provision_id: str | None


@dataclass(frozen=True, kw_only=True)
class LatencySummary:
    p50_ms: float | None
    p95_ms: float | None
    required_pass: bool
    p95_target_pass: bool
    p50_target_pass: bool


def summarize_latency(
    samples: Sequence[LatencySample],
    *,
    expected_count: int,
    real_models: bool,
) -> LatencySummary:
    durations = [s.elapsed_ms for s in samples]
    valid = bool(durations) and all(isfinite(v) and v >= 0 for v in durations)
    p50 = p95 = None
    if valid:
        ordered = sorted(durations)
        p50 = ordered[ceil(len(ordered) * 0.5) - 1]
        p95 = ordered[ceil(len(ordered) * 0.95) - 1]
    complete = (
        valid
        and real_models
        and expected_count > 0
        and len(samples) == expected_count
        and all(s.success and bool(s.provision_id) for s in samples)
    )
    return LatencySummary(
        p50_ms=p50,
        p95_ms=p95,
        required_pass=complete and p95 is not None and p95 <= 600,
        p95_target_pass=complete and p95 is not None and p95 <= 300,
        p50_target_pass=complete and p50 is not None and p50 <= 150,
    )
