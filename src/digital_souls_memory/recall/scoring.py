"""Six-term ranking without reinforcement or epistemic promotion."""

from dataclasses import asdict, dataclass
from datetime import datetime
from math import isclose, isfinite

from digital_souls_memory.memory.domain import Epistemics


@dataclass(frozen=True, kw_only=True)
class Weights:
    semantic: float = 0.55
    lexical: float = 0.25
    recency: float = 0.05
    importance: float = 0.05
    self_relevance: float = 0.05
    trust: float = 0.05

    def __post_init__(self) -> None:
        values = tuple(asdict(self).values())
        if (
            any(not isfinite(v) or v < 0 for v in values)
            or not isclose(sum(values), 1, abs_tol=1e-12)
            or self.recency > 0.1
        ):
            raise ValueError("Invalid ranking weights")


def composite_score(
    *,
    weights: Weights,
    semantic: float,
    lexical: float,
    recency: float,
    importance: float,
    self_relevance: float,
    trust: float,
) -> float:
    return (
        weights.semantic * semantic
        + weights.lexical * lexical
        + weights.recency * recency
        + weights.importance * importance
        + weights.self_relevance * self_relevance
        + weights.trust * trust
    )


def recency(
    *, kind: str, occurred_at: datetime, last_reinforced_at: datetime | None, now: datetime
) -> float:
    if kind in ("commitment", "verbatim_anchor"):
        return 1.0
    origin = max(occurred_at, last_reinforced_at) if last_reinforced_at else occurred_at
    return 2 ** (-max(0.0, (now - origin).total_seconds()) / (30 * 86400))


def trust_score(value: Epistemics) -> float:
    return (value.confidence_features.score() + 1 - 1 / (1 + value.independent_evidence_count)) / 2
