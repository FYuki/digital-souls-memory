"""Surviving contribution aggregation preserves Fact identity and protected fields."""

from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.memory.domain import SourceContribution
from digital_souls_memory.memory.reinforcement import aggregate_sources
from tests.evidence_support import OCCURRED, turn
from tests.memory_support import memory

pytestmark = pytest.mark.ut


def test_aggregation_unions_evidence_and_uses_latest_surviving_event() -> None:
    root = memory()
    earlier = OCCURRED - timedelta(hours=1)
    supporting = replace(turn("support", actor="user-b"), occurred_at=earlier)
    contribution = SourceContribution(
        character_id=root.character_id,
        fact_id=root.id,
        extraction_turn_id="support",
        source_turn_id="support",
        occurred_at=earlier,
    )
    turns = {"turn-1": turn(actor="user-a"), "support": supporting}
    result = aggregate_sources(root, root.source_turn_ids, (contribution, contribution), turns)
    assert result == replace(
        root,
        source_turn_ids=("turn-1", "support"),
        last_reinforced_at=earlier,
        epistemics=replace(root.epistemics, independent_evidence_count=2),
    )
    assert aggregate_sources(root, root.source_turn_ids, (), turns) == root
    assert aggregate_sources(root, (), (), turns) is None
