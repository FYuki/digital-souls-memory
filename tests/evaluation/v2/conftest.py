from collections.abc import Callable
from copy import deepcopy
from typing import cast

import pytest

from digital_souls_memory.evaluation.comparison import comparison_protocol


@pytest.fixture
def candidate(candidate: dict[str, object]) -> dict[str, object]:
    return {**candidate, "quantization": "synthetic", "max_context_length": 8192}


@pytest.fixture
def v2_protocol() -> dict[str, object]:
    return {**comparison_protocol(), "comparison_version": "memory-v2"}


@pytest.fixture
def v2_gold(gold: dict[str, object]) -> dict[str, object]:
    return {**gold, "safety_forbidden_ids": [], "quality_forbidden_ids": []}


@pytest.fixture
def v2_suite(
    make_suite: Callable[[], dict[str, object]],
    v2_protocol: dict[str, object],
    v2_gold: dict[str, object],
) -> dict[str, object]:
    return {**deepcopy(make_suite()), "protocol": v2_protocol, "gold": [v2_gold]}


@pytest.fixture
def observation_factory(
    v2_protocol: dict[str, object],
) -> Callable[[str, float], dict[str, object]]:
    def build(identifier: str, value: float) -> dict[str, object]:
        return {
            "memory_id": identifier,
            "cosine": value,
            "components": {key: value for key in cast(dict[str, object], v2_protocol["weights"])},
            "score": value,
            "reranker_score": None,
        }

    return build


@pytest.fixture
def v2_fixture(v2_suite: dict[str, object]) -> dict[str, object]:
    case = cast(list[dict[str, object]], v2_suite["cases"])[0]
    memory = cast(list[dict[str, object]], case["memories"])[0]
    return {
        "vectors": {
            "passage": {str(memory["content"]): [1.0, 0.0]},
            "query": {str(case["query"]): [1.0, 0.0]},
        },
        "observations": {
            str(case["id"]): [
                {
                    "memory_id": memory["id"],
                    "cosine": 0.55,
                    "components": {
                        key: 0.55
                        for key in cast(
                            dict[str, object],
                            cast(dict[str, object], v2_suite["protocol"])["weights"],
                        )
                    },
                    "score": 0.55,
                    "reranker_score": None,
                }
            ]
        },
    }
