import json
from copy import deepcopy

import pytest

from digital_souls_memory.evaluation.adapters.fixture import FixtureTokenCounter
from digital_souls_memory.evaluation.models import number, rows

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("text", ["日本語", "English と日本語"])
def test_fixture_counter_records_a_deterministic_character_estimate(text: str) -> None:
    with FixtureTokenCounter() as counter:
        assert counter.count(text) == len(text)
        assert counter.method == "approximate-characters-v1"


class CharacterCounter:
    method = "synthetic-character-count"

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def count(self, text: str) -> int:
        self.inputs.append(text)
        return len(text)


@pytest.mark.parametrize(
    "text", ["あ" * 900 + "青い駅", "English と日本語 " * 75 + "青い駅"], ids=["japanese", "mixed"]
)
def test_long_original_within_token_equivalent_budget_is_returned(
    source_case: dict[str, object], text: str
) -> None:
    from digital_souls_memory.evaluation.context import bounded_results

    source_case["turns"] = [{**rows(source_case["turns"])[0], "text": text}]
    source_case["memories"] = [
        {
            **rows(source_case["memories"])[0],
            "source_spans": [{"turn_id": "t1", "start": 0, "end": len(text)}],
        }
    ]
    span = {
        "turn_id": "t1",
        "start": 0,
        "end": len(text),
        "text": text,
        "actor": "user",
        "language": "ja",
    }
    hits: list[dict[str, object]] = [{"memory_id": "m1", "score": 0.9, "source_spans": [span]}]
    counter = CharacterCounter()
    selected, evidence = bounded_results(
        hits, case=source_case, limit=5, budget=1500, token_counter=counter
    )
    assert len(selected) == 1
    assert rows(selected[0]["source_spans"])[0]["text"] == text
    assert number(evidence["token_count"]) <= 1500
    assert evidence["token_counter"] == counter.method
    counted = json.loads(counter.inputs[-1])
    assert counted[0]["memory_id"] == "m1"
    assert counted[0]["source_spans"][0]["text"] == text
    assert "score" not in counted[0]
    assert len(counter.inputs[-1].encode()) > 1500


def test_oversized_source_returns_a_verbatim_window_around_query_with_absolute_offsets(
    source_case: dict[str, object],
) -> None:
    from digital_souls_memory.evaluation.context import bounded_results

    text = "前置き。" + "あ" * 2200 + "橙色のリボン" + "い" * 2200 + "後置き。"
    start, end = 4, len(text) - 4
    source_case["query"] = "橙色のリボン"
    source_case["turns"] = [{**rows(source_case["turns"])[0], "text": text}]
    source_case["memories"] = [
        {
            **rows(source_case["memories"])[0],
            "source_spans": [{"turn_id": "t1", "start": start, "end": end}],
        }
    ]
    hits: list[dict[str, object]] = [
        {
            "memory_id": "m1",
            "score": 0.9,
            "source_spans": [
                {
                    "turn_id": "t1",
                    "start": start,
                    "end": end,
                    "text": text[start:end],
                    "actor": "user",
                    "language": "ja",
                }
            ],
        }
    ]
    before = deepcopy((source_case, hits))
    counter = CharacterCounter()
    selected, evidence = bounded_results(
        hits, case=source_case, limit=5, budget=1500, token_counter=counter
    )
    assert len(selected) == 1
    span = rows(selected[0]["source_spans"])[0]
    left, right = int(str(span["start"])), int(str(span["end"]))
    assert start <= left < right <= end
    assert span["text"] == text[left:right]
    assert "橙色のリボン" in str(span["text"])
    assert span["truncated"] is True
    assert (span["original_start"], span["original_end"]) == (start, end)
    fragments = [{"memory_id": h["memory_id"], "source_spans": h["source_spans"]} for h in selected]
    payload = json.dumps(
        fragments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    assert counter.count(payload) <= 1500
    assert evidence["token_count"] == counter.count(payload)
    assert (source_case, hits) == before


def test_final_json_is_counted_as_a_whole_and_five_result_limit_is_kept(
    source_case: dict[str, object],
) -> None:
    from digital_souls_memory.evaluation.context import bounded_results

    span = {
        "turn_id": "t1",
        "start": 3,
        "end": 7,
        "text": "青い駅。",
        "actor": "user",
        "language": "ja",
    }
    hits: list[dict[str, object]] = [
        {"memory_id": f"m{i}", "source_spans": [deepcopy(span)]} for i in range(7)
    ]
    source_case["memories"] = [
        {**rows(source_case["memories"])[0], "id": f"m{i}"} for i in range(7)
    ]

    class WholeJsonCounter(CharacterCounter):
        def count(self, text: str) -> int:
            self.inputs.append(text)
            return 100 + 200 * len(json.loads(text))

    counter = WholeJsonCounter()
    selected, evidence = bounded_results(
        hits, case=source_case, limit=5, budget=1500, token_counter=counter
    )
    assert [h["memory_id"] for h in selected] == [f"m{i}" for i in range(5)]
    assert evidence["token_count"] == 1100
    assert any(len(json.loads(payload)) == 5 for payload in counter.inputs)


@pytest.mark.parametrize("count", [2, 3], ids=["exact-budget", "cumulative-overflow"])
def test_cumulative_token_budget_excludes_an_individually_fitting_memory(
    source_case: dict[str, object], count: int
) -> None:
    from digital_souls_memory.evaluation.context import bounded_results, context_payload
    from digital_souls_memory.evaluation.retrieval import source_spans

    hits: list[dict[str, object]] = [
        {"memory_id": f"m{i}", "source_spans": source_spans(source_case, memory_id="m1")}
        for i in range(count)
    ]
    source_case["memories"] = [
        {**rows(source_case["memories"])[0], "id": f"m{i}"} for i in range(count)
    ]

    class WholeJsonCounter(CharacterCounter):
        def count(self, text: str) -> int:
            self.inputs.append(text)
            return 100 + 700 * len(json.loads(text))

    counter = WholeJsonCounter()
    assert all(counter.count(context_payload([hit])) == 800 for hit in hits)
    assert counter.count(context_payload(hits)) == (1500 if count == 2 else 2200)
    selected, evidence = bounded_results(
        hits, case=source_case, limit=5, budget=1500, token_counter=counter
    )
    assert [hit["memory_id"] for hit in selected] == ["m0", "m1"]
    assert evidence["excluded"] == (
        [] if count == 2 else [{"memory_id": "m2", "reason": "token_budget"}]
    )
    assert counter.count(context_payload(selected)) == evidence["token_count"] == 1500
    assert evidence["token_counter"] == counter.method


def test_window_finds_query_terms_inside_a_japanese_question(
    source_case: dict[str, object],
) -> None:
    from digital_souls_memory.evaluation.context import bounded_results
    from digital_souls_memory.evaluation.retrieval import source_spans

    text = (
        "日常の記録。" * 500 + "小物入れの目印に橙色のリボンを結びました。" + "日常の記録。" * 500
    )
    source_case["query"] = "小物入れにつけた目印の色は何ですか？"
    source_case["turns"] = [{**rows(source_case["turns"])[0], "text": text}]
    source_case["memories"] = [
        {
            **rows(source_case["memories"])[0],
            "source_spans": [{"turn_id": "t1", "start": 0, "end": len(text)}],
        }
    ]
    hits: list[dict[str, object]] = [
        {
            "memory_id": "m1",
            "source_spans": source_spans(source_case, memory_id="m1"),
        }
    ]
    selected, evidence = bounded_results(
        hits, case=source_case, limit=5, budget=1500, token_counter=CharacterCounter()
    )
    span = rows(selected[0]["source_spans"])[0]
    assert "橙色のリボン" in str(span["text"])
    assert span["truncated"] is True
    assert number(evidence["token_count"]) <= 1500


def test_multiple_sources_share_one_memory_budget(source_case: dict[str, object]) -> None:
    from digital_souls_memory.evaluation.context import bounded_results
    from digital_souls_memory.evaluation.retrieval import source_spans

    text = "あ" * 1600 + "青い駅" + "い" * 1600
    turn = {**rows(source_case["turns"])[0], "text": text}
    source_case["query"] = "青い駅"
    source_case["turns"] = [turn, {**turn, "turn_id": "t2"}]
    source_case["memories"] = [
        {
            **rows(source_case["memories"])[0],
            "source_turn_ids": ["t1", "t2"],
            "source_spans": [
                {"turn_id": i, "start": 10, "end": len(text) - 10} for i in ("t1", "t2")
            ],
        }
    ]
    selected, evidence = bounded_results(
        [{"memory_id": "m1", "source_spans": source_spans(source_case, memory_id="m1")}],
        case=source_case,
        limit=5,
        budget=1500,
        token_counter=CharacterCounter(),
    )
    spans = rows(selected[0]["source_spans"])
    assert [s["turn_id"] for s in spans] == ["t1", "t2"]
    assert all(s["truncated"] is True and "青い駅" in str(s["text"]) for s in spans)
    assert all(s["text"] == text[int(str(s["start"])) : int(str(s["end"]))] for s in spans)
    assert number(evidence["token_count"]) <= 1500
