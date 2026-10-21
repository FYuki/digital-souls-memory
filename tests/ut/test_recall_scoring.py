from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

import pytest
from digital_souls_memory.recall.scoring import Weights, composite_score, recency, trust_score

from tests.evidence_support import OCCURRED, RECORDED
from tests.memory_support import memory

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "changes",
    [
        {"semantic": -0.01, "lexical": 0.81},
        {"semantic": float("nan")},
        {"lexical": float("inf")},
        {"importance": -float("inf")},
        {"semantic": 0.54},
        {"recency": 0.100001, "semantic": 0.499999},
    ],
)
def test_invalid_weights_are_rejected(
    changes: dict[str, float], rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    values = dict(
        semantic=0.55, lexical=0.25, recency=0.05, importance=0.05, self_relevance=0.05, trust=0.05
    )
    with rejected():
        Weights(**(values | changes))


@pytest.mark.parametrize("recent", [0.0, 0.1])
def test_boundary_weights_produce_the_six_term_weighted_sum(recent: float) -> None:
    weights = Weights(
        semantic=0.6 - recent,
        lexical=0.2,
        recency=recent,
        importance=0.05,
        self_relevance=0.05,
        trust=0.1,
    )
    assert composite_score(
        weights=weights,
        semantic=0.9,
        lexical=0.3,
        recency=0.2,
        importance=0.7,
        self_relevance=0.4,
        trust=0.6,
    ) == pytest.approx((0.6 - recent) * 0.9 + 0.2 * 0.3 + recent * 0.2 + 0.035 + 0.02 + 0.06)


def test_default_weights_match_the_approved_configuration() -> None:
    weights = Weights()
    for term, expected in (
        ("semantic", 0.55),
        ("lexical", 0.25),
        ("recency", 0.05),
        ("importance", 0.05),
        ("self_relevance", 0.05),
        ("trust", 0.05),
    ):
        values = dict.fromkeys(
            ("semantic", "lexical", "recency", "importance", "self_relevance", "trust"), 0.0
        )
        values[term] = 1.0
        assert composite_score(weights=weights, **values) == pytest.approx(expected)


def test_recency_uses_the_newer_event_or_reinforcement_time_and_not_recording_time() -> None:
    now = RECORDED + timedelta(days=60)
    event = RECORDED - timedelta(days=60)
    reinforced = now - timedelta(days=30)
    assert recency(
        kind="fact", occurred_at=event, last_reinforced_at=reinforced, now=now
    ) == pytest.approx(0.5)
    assert recency(
        kind="fact", occurred_at=reinforced, last_reinforced_at=event, now=now
    ) == pytest.approx(0.5)


@pytest.mark.parametrize("offset,expected", [(30, 0.5), (300, 2**-10), (-30, 1.0)])
def test_recency_handles_old_and_future_events(offset: int, expected: float) -> None:
    assert recency(
        kind="fact",
        occurred_at=RECORDED - timedelta(days=offset),
        last_reinforced_at=None,
        now=RECORDED,
    ) == pytest.approx(expected)


@pytest.mark.parametrize("kind", ["commitment", "verbatim_anchor"])
def test_commitments_and_anchors_do_not_decay(kind: str) -> None:
    assert (
        recency(
            kind=kind,
            occurred_at=OCCURRED,
            last_reinforced_at=None,
            now=OCCURRED + timedelta(days=3000),
        )
        == 1.0
    )


def test_trust_uses_multiple_features_and_the_stored_independent_evidence_count() -> None:
    original = memory().epistemics
    weak = replace(
        original.confidence_features,
        retrieval=0,
        source=0,
        temporal=0,
        corroboration=0,
        contradiction=1,
        self_assessment=1,
    )
    self_only = replace(original, confidence_features=weak)
    supported = replace(
        original,
        confidence_features=replace(weak, source=1, temporal=1, contradiction=0, corroboration=1),
        independent_evidence_count=4,
    )
    assert trust_score(self_only) < trust_score(supported)
    assert trust_score(original) < trust_score(replace(original, independent_evidence_count=4))
    assert original == memory().epistemics
