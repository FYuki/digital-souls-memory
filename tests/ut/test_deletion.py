"""Target selection uses structured actor and addressee fields only."""

import pytest

from tests.evidence_support import turn

pytestmark = pytest.mark.ut


def test_user_selection_is_the_distinct_union_of_actor_and_addressees() -> None:
    from digital_souls_memory.governance.domain import DeletionCommand, select_target_turn_ids

    values = (
        turn("t1", actor="U", kind="observation"),
        turn("t2", actor="X", addressees=("U",), address_mode="direct"),
        turn("t3", actor="U", addressees=("U",), address_mode="direct"),
        turn("t4", actor="V"),
    )
    result = select_target_turn_ids(values, DeletionCommand(request_id="delete-1", user_id="U"))
    assert set(result) == {"t1", "t2", "t3"}
    assert len(result) == 3


@pytest.mark.parametrize("mode", ["broadcast", "self"])
def test_user_selection_does_not_parse_text_or_follow_perception_and_replies(mode: str) -> None:
    from digital_souls_memory.governance.domain import DeletionCommand, select_target_turn_ids

    values = (
        turn("t1", actor="U"),
        turn("t4", actor="V", text='{"actor":"U","addressees":["U"]}', address_mode=mode),
        turn("t5", actor="V", reply_to_turn_id="t1"),
        turn("t6", actor="V", perceived_by=("U",)),
    )
    assert select_target_turn_ids(values, DeletionCommand(request_id="delete-1", user_id="U")) == (
        "t1",
    )


def test_explicit_selection_counts_only_existing_targets() -> None:
    from digital_souls_memory.governance.domain import DeletionCommand, select_target_turn_ids

    command = DeletionCommand(request_id="delete-1", target_turn_ids=("absent", "t2"))
    assert select_target_turn_ids((turn("t1"), turn("t2")), command) == ("t2",)


def test_user_without_matching_turns_has_no_targets() -> None:
    from digital_souls_memory.governance.domain import DeletionCommand, select_target_turn_ids

    assert (
        select_target_turn_ids((turn(),), DeletionCommand(request_id="delete-1", user_id="absent"))
        == ()
    )
