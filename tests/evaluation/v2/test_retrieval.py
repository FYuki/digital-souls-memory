from collections.abc import Callable
from typing import cast

import pytest

from digital_souls_memory.evaluation.retrieval import composite_score, select_observations

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "z_lexical,expected_ids",
    [(0.75, ["a", "z"]), (1.0, ["z", "a"])],
)
def test_disabled_reranker_selects_by_score_then_id(
    v2_protocol: dict[str, object],
    observation_factory: Callable[[str, float], dict[str, object]],
    z_lexical: float,
    expected_ids: list[str],
) -> None:
    protocol = {**v2_protocol, "threshold": 0.5}
    observations = [observation_factory(identifier, 0.75) for identifier in ("z", "a")]
    cast(dict[str, object], observations[0]["components"])["lexical"] = z_lexical
    for observation in observations:
        observation["score"] = composite_score(observation["components"], protocol["weights"])

    selected = select_observations(observations, protocol)

    assert [observation["memory_id"] for observation in selected] == expected_ids
