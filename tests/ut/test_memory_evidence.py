from dataclasses import replace
from itertools import permutations

import pytest

from digital_souls_memory.evidence.domain import Turn
from tests.evidence_support import turn

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "sources,expected",
    [
        ((), 1),
        ((turn(actor="user-a"),), 1),
        (
            (
                turn(actor="user-a"),
                turn(turn_id="reply", actor="user-b", reply_to_turn_id="turn-1"),
            ),
            1,
        ),
        ((turn(actor="user-a"), turn(turn_id="second", actor="user-a")), 1),
        ((turn(actor="user-a"), turn(turn_id="second", actor="user-b")), 2),
        (
            (
                turn(actor="user-a"),
                turn(turn_id="second", actor="user-b"),
                turn(turn_id="third", actor="user-a"),
            ),
            2,
        ),
        (
            (
                turn(actor="user-a"),
                turn(turn_id="reply", actor="user-b", reply_to_turn_id="turn-1"),
                turn(turn_id="second", actor="user-a"),
            ),
            2,
        ),
        (
            (
                turn(actor="user-a"),
                turn(turn_id="reply", actor="user-b", reply_to_turn_id="turn-1"),
                turn(turn_id="second", actor="user-c"),
                turn(turn_id="third", actor="user-c"),
            ),
            2,
        ),
        (
            (
                turn(actor="user-a", reply_to_turn_id="context-only"),
                turn(turn_id="second", actor="user-b", reply_to_turn_id="context-only"),
            ),
            2,
        ),
        (
            (
                turn(actor="user-a"),
                turn(turn_id="reply", actor="user-b", reply_to_turn_id="turn-1"),
                turn(turn_id="third", actor="user-c", reply_to_turn_id="reply"),
            ),
            1,
        ),
    ],
    ids=[
        "minimum",
        "single",
        "one-round-trip",
        "same-speaker",
        "independent-speakers",
        "three-opportunities-two-speakers",
        "augmenting-path",
        "matching-not-minimum-counts",
        "context-is-not-evidence",
        "connected-evidence",
    ],
)
def test_independent_evidence_uses_maximum_matching(
    sources: tuple[Turn, ...], expected: int
) -> None:
    from digital_souls_memory.memory.evidence import independent_evidence_count

    assert independent_evidence_count(sources) == expected


def test_matching_is_independent_of_source_order_and_duplicates() -> None:
    from digital_souls_memory.memory.evidence import independent_evidence_count

    sources = (
        turn(actor="user-a"),
        turn(turn_id="reply", actor="user-b", reply_to_turn_id="turn-1"),
        turn(turn_id="second", actor="user-a"),
    )
    for ordered in permutations(sources):
        assert independent_evidence_count((*ordered, replace(ordered[0]))) == 2
