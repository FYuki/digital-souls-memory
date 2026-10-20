from copy import deepcopy

import pytest

from digital_souls_memory.evaluation.retrieval import (
    bounded_results,
    lexical_terms,
    rank_results,
    score_components,
    source_spans,
)

pytestmark = pytest.mark.ut


def test_lexical_terms_normalize_only_search_keys() -> None:
    assert lexical_terms("ＡＢＣ abc 青い駅 %_\\ x") == ("ab", "bc", "x", "い駅", "青い")
    assert lexical_terms("%_\\") == ()


@pytest.mark.parametrize("kind,expected", [("Fact", 0.5), ("Commitment", 1), ("VerbatimAnchor", 1)])
def test_components_use_thirty_day_half_life_without_reinforcement(
    case: dict[str, object],
    memory: dict[str, object],
    turn: dict[str, object],
    kind: str,
    expected: float,
) -> None:
    case["as_of"] = "2026-11-18T00:00:00Z"
    memory["kind"] = kind
    before = deepcopy(case)
    values = score_components(case, memory, semantic_distance=0.25, lexical=0.5)
    assert values == {
        "semantic": 0.75,
        "lexical": 0.5,
        "recency": expected,
        "importance": 0.5,
        "self_relevance": 0.5,
        "trust": 0.5,
    }
    assert case == before
    turn["occurred_at"] = "2026-11-18T00:00:00Z"
    assert score_components(case, memory, semantic_distance=2, lexical=0)["recency"] == 1


def test_rank_uses_exact_ties_and_inclusive_threshold(protocol: dict[str, object]) -> None:
    protocol["threshold"] = 0.5
    values = {k: 0.5 for k in dict(protocol["weights"])}  # type: ignore[call-overload]
    observations: list[dict[str, object]] = [
        {"memory_id": i, "components": values} for i in ("z", "a")
    ]
    assert [h["memory_id"] for h in rank_results(observations, protocol)] == ["a", "z"]
    protocol["threshold"] = 0.50001
    assert rank_results(observations, protocol) == []


def test_return_budget_keeps_whole_originals_and_counts_json_bytes(
    case: dict[str, object],
) -> None:
    hit = {"memory_id": "m1", "score": 0.55, "source_spans": source_spans(case, memory_id="m1")}
    selected, evidence = bounded_results([hit], limit=5, budget=1500)
    assert selected == [hit]
    size = int(str(evidence["utf8_bytes"]))
    assert size > len(str(hit["source_spans"]))
    assert bounded_results([hit], limit=5, budget=size)[0] == [hit]
    selected, evidence = bounded_results([hit], limit=5, budget=size - 1)
    assert selected == []
    assert evidence["excluded"] == [{"memory_id": "m1", "reason": "token_budget"}]


def test_return_budget_caps_five_and_can_take_a_smaller_later_memory() -> None:
    hits: list[dict[str, object]] = [{"memory_id": f"m{i}", "source_spans": []} for i in range(7)]
    selected, evidence = bounded_results(hits, limit=5, budget=1500)
    assert selected == hits[:5]
    assert evidence["excluded"] == [
        {"memory_id": "m5", "reason": "top_k"},
        {"memory_id": "m6", "reason": "top_k"},
    ]
    oversized: dict[str, object] = {"memory_id": "large", "source_spans": [{"text": "あ" * 600}]}
    selected, evidence = bounded_results([oversized, hits[0]], limit=5, budget=1500)
    assert selected == hits[:1]
    assert evidence["excluded"] == [{"memory_id": "large", "reason": "token_budget"}]
