from copy import deepcopy

import pytest

from digital_souls_memory.evaluation.comparison import comparison_protocol


@pytest.fixture
def candidate(candidate: dict[str, object]) -> dict[str, object]:
    return {**candidate, "quantization": "synthetic", "max_context_length": 8192}


@pytest.fixture
def new_protocol() -> dict[str, object]:
    return {**comparison_protocol(), "comparison_version": "memory-v2.1", "threshold": 0.5}


@pytest.fixture
def new_gold(gold: dict[str, object]) -> dict[str, object]:
    return {**gold, "safety_forbidden_ids": [], "quality_forbidden_ids": []}


@pytest.fixture
def source_case(case: dict[str, object]) -> dict[str, object]:
    result = deepcopy(case)
    turns = result["turns"]
    memories = result["memories"]
    assert isinstance(turns, list) and isinstance(memories, list)
    turns[0]["text"] = "前文。青い駅。後文。"
    memories[0]["source_spans"] = [{"turn_id": "t1", "start": 3, "end": 7}]
    return result


@pytest.fixture
def new_suite(
    source_case: dict[str, object],
    new_gold: dict[str, object],
    new_protocol: dict[str, object],
) -> dict[str, object]:
    return {
        "cases": [source_case],
        "gold": [new_gold],
        "protocol": new_protocol,
        "split": "acceptance",
    }
