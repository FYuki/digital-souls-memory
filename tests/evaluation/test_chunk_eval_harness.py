"""チャンク比較アダプタの重要な契約。"""

import json
from collections.abc import Callable

import pytest

from tests.chunk_evaluation import (
    chunk_case,
    collapse_hits,
    frozen_tuning_case_ids,
    validate_frozen_suite,
)

pytestmark = pytest.mark.ut


def test_frozen_acceptance_rejects_tuning_case_overlap(
    make_suite: Callable[[], dict[str, object]],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    tuning = {**make_suite(), "split": "tuning"}
    tuning_ids = frozen_tuning_case_ids(json.dumps(tuning).encode())
    acceptance = make_suite()

    with rejected():
        validate_frozen_suite("acceptance", acceptance, tuning_ids)


def test_frozen_acceptance_allows_disjoint_tuning_cases(
    make_suite: Callable[[], dict[str, object]],
    case: dict[str, object],
    gold: dict[str, object],
) -> None:
    tuning = {
        **make_suite(),
        "split": "tuning",
        "cases": [{**case, "id": "other-case"}],
        "gold": [{**gold, "case_id": "other-case"}],
    }
    tuning_ids = frozen_tuning_case_ids(json.dumps(tuning).encode())

    validate_frozen_suite("acceptance", make_suite(), tuning_ids)


def test_frozen_tuning_does_not_reject_its_own_case_ids(
    make_suite: Callable[[], dict[str, object]],
) -> None:
    tuning = {**make_suite(), "split": "tuning"}
    tuning_ids = frozen_tuning_case_ids(json.dumps(tuning).encode())

    validate_frozen_suite("tuning", tuning, tuning_ids)


def test_chunk_text_retains_original_provenance() -> None:
    case = {
        "memories": [
            {
                "id": "m",
                "content": "あ" * 1000,
                "source_turn_ids": ["t"],
                "source_spans": [{"turn_id": "t", "start": 0, "end": 1000}],
            }
        ]
    }
    expanded, mapping = chunk_case(case)
    assert [len(m["content"]) for m in expanded["memories"]] == [512, 512, 104]
    assert mapping["m#1"][1].start == 448
    assert expanded["memories"][1]["source_spans"] == case["memories"][0]["source_spans"]
    assert case["memories"][0]["content"] == "あ" * 1000


def test_collapse_after_rerank_keeps_best_chunk_and_original_id() -> None:
    _, mapping = chunk_case({"memories": [{"id": "m", "content": "あ" * 1000}]})
    hits = [
        {"memory_id": "m#0", "reranker_score": 1.0},
        {"memory_id": "m#1", "reranker_score": 4.0},
    ]
    result = collapse_hits(hits, mapping)
    assert len(result) == 1
    assert result[0]["memory_id"] == "m"
    assert result[0]["matched_chunk"] == {"start": 448, "end": 960}
