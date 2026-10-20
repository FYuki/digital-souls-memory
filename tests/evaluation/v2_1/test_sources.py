from copy import deepcopy

import pytest

from digital_souls_memory.evaluation.adapters.fixture import FixtureTokenCounter
from digital_souls_memory.evaluation.models import rows, validate_case
from digital_souls_memory.evaluation.retrieval import source_spans
from digital_souls_memory.evaluation.scoring import score_case

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "text,start,end", [("前文。青い駅。後文。", 3, 7), ("前\ne\u0301駅\n後", 2, 6)]
)
def test_source_recovery_uses_absolute_character_offsets_without_normalization(
    source_case: dict[str, object], text: str, start: int, end: int
) -> None:
    source_case["turns"] = [{**rows(source_case["turns"])[0], "text": text}]
    source_case["memories"] = [
        {
            **rows(source_case["memories"])[0],
            "source_spans": [{"turn_id": "t1", "start": start, "end": end}],
        }
    ]
    before = deepcopy(source_case)
    spans = source_spans(source_case, memory_id="m1")
    assert [(s["text"], s["start"], s["end"]) for s in spans] == [(text[start:end], start, end)]
    assert spans[0]["actor"] == "user"
    assert spans[0]["language"] == "ja"
    assert source_case == before


@pytest.mark.parametrize("start,end", [(-1, 7), (3, 3), (3, 100), (3.0, 7), (True, 7)])
def test_invalid_declared_source_offsets_are_rejected(
    source_case: dict[str, object], start: object, end: object
) -> None:
    source_case["memories"] = [
        {
            **rows(source_case["memories"])[0],
            "source_spans": [{"turn_id": "t1", "start": start, "end": end}],
        }
    ]
    with pytest.raises(ValueError):
        validate_case(source_case)


def test_same_text_on_a_turn_outside_memory_sources_is_not_valid_evidence(
    source_case: dict[str, object], new_gold: dict[str, object], new_protocol: dict[str, object]
) -> None:
    turn = rows(source_case["turns"])[0]
    source_case["turns"] = [turn, {**turn, "turn_id": "t2"}]
    span = {
        "turn_id": "t2",
        "start": 3,
        "end": 7,
        "text": "青い駅。",
        "actor": "user",
        "language": "ja",
    }
    result = score_case(
        source_case,
        new_gold,
        [{"memory_id": "m1", "score": 0.9, "source_spans": [span]}],
        threshold=0.5,
        protocol=new_protocol,
        token_counter=FixtureTokenCounter(),
    )
    assert result["sources_valid"] is False
    assert result["safety_passed"] is False


@pytest.mark.parametrize("returned_turn", ["t1", "t2"])
def test_required_source_inclusion_is_bound_to_its_turn(
    source_case: dict[str, object],
    new_gold: dict[str, object],
    new_protocol: dict[str, object],
    returned_turn: str,
) -> None:
    turn = {**rows(source_case["turns"])[0], "text": "前文。目的地は青い駅です。後文。"}
    source_case["turns"] = [turn, {**turn, "turn_id": "t2"}]
    memory = rows(source_case["memories"])[0]
    source_case["memories"] = [
        {
            **memory,
            "source_turn_ids": ["t1", "t2"],
            "source_spans": [
                {"turn_id": identifier, "start": 3, "end": 14} for identifier in ("t1", "t2")
            ],
        }
    ]
    span = {
        "turn_id": returned_turn,
        "start": 3,
        "end": 14,
        "text": str(turn["text"])[3:14],
        "actor": "user",
        "language": "ja",
    }
    result = score_case(
        source_case,
        {**new_gold, "required_source_spans": [{"turn_id": "t1", "text": "青い駅"}]},
        [{"memory_id": "m1", "score": 0.9, "source_spans": [span]}],
        threshold=0.5,
        protocol=new_protocol,
        token_counter=FixtureTokenCounter(),
    )
    assert result["safety_passed"] is True
    assert result["quality_passed"] is (returned_turn == "t1")
    assert result["passed"] is (returned_turn == "t1")


def test_missing_required_text_fails_quality_without_failing_source_safety(
    source_case: dict[str, object], new_gold: dict[str, object], new_protocol: dict[str, object]
) -> None:
    span = {"turn_id": "t1", "start": 3, "end": 4, "text": "青", "actor": "user", "language": "ja"}
    result = score_case(
        source_case,
        {**new_gold, "required_source_spans": [{"turn_id": "t1", "text": "青い駅"}]},
        [{"memory_id": "m1", "score": 0.9, "source_spans": [span]}],
        threshold=0.5,
        protocol=new_protocol,
        token_counter=FixtureTokenCounter(),
    )
    assert result["recall_at_5"] == 1.0
    assert result["safety_passed"] is True
    assert result["quality_passed"] is False


@pytest.mark.parametrize("relevance,expected", [(-3.0, False), (2.0, True)])
def test_scoring_checks_relevance_instead_of_composite_when_enabled(
    source_case: dict[str, object],
    new_gold: dict[str, object],
    new_protocol: dict[str, object],
    relevance: float,
    expected: bool,
) -> None:
    span = {
        "turn_id": "t1",
        "start": 3,
        "end": 7,
        "text": "青い駅。",
        "actor": "user",
        "language": "ja",
    }
    result = score_case(
        source_case,
        new_gold,
        [
            {
                "memory_id": "m1",
                "score": 0.1 if expected else 0.9,
                "reranker_score": relevance,
                "source_spans": [span],
            }
        ],
        threshold=0.5,
        protocol=new_protocol,
        token_counter=FixtureTokenCounter(),
    )
    assert result["threshold_passed"] is expected


def test_verbatim_text_outside_declared_source_range_fails_safety(
    source_case: dict[str, object],
    new_gold: dict[str, object],
    new_protocol: dict[str, object],
) -> None:
    span = {
        "turn_id": "t1",
        "start": 0,
        "end": 3,
        "text": "前文。",
        "actor": "user",
        "language": "ja",
    }
    result = score_case(
        source_case,
        new_gold,
        [{"memory_id": "m1", "score": 0.9, "source_spans": [span]}],
        threshold=0.5,
        protocol=new_protocol,
        token_counter=FixtureTokenCounter(),
    )
    assert result["sources_valid"] is False
    assert result["safety_passed"] is False
