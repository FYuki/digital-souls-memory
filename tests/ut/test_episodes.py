from collections.abc import Callable

import pytest

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("start,end", [(-1, 1), (0, 0), (2, 1)])
def test_episode_span_rejects_negative_empty_and_reversed_ranges(
    start: int, end: int, rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    from digital_souls_memory.episodes.domain import TurnSpan

    with rejected():
        TurnSpan(turn_id="turn-1", start=start, end=end)


@pytest.mark.parametrize("field", ["topic", "turn_span", "model", "prompt_version", "episodes"])
def test_episode_proposal_requires_topic_source_spans_and_generation_provenance(
    field: str, rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    from digital_souls_memory.episodes.domain import EpisodePart, EpisodeProposal, TurnSpan

    with rejected():
        part = EpisodePart(
            topic=" " if field == "topic" else "合成話題",
            turn_span=() if field == "turn_span" else (TurnSpan(turn_id="turn-1", start=0, end=1),),
        )
        EpisodeProposal(
            model=" " if field == "model" else "synthetic-model",
            prompt_version=" " if field == "prompt_version" else "synthetic-v1",
            episodes=() if field == "episodes" else (part,),
        )
