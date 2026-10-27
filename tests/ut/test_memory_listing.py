"""Pure projection inputs and the list reader's dependency failure contract."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory.adapters.postgres_memory_listing import PostgresMemoryListing
from digital_souls_memory.memory.domain import InputDependency, memory_valid_at
from digital_souls_memory.memory.inputs import ResolvedTurn, input_eligible, resolve_input
from digital_souls_memory.memory.ports import MemoryUnavailable
from digital_souls_memory.memory.projection import (
    collect_evidence,
    projection_input,
    source_ids,
)
from tests.evidence_support import RECORDED, turn
from tests.it1.test_memory_worker import setup_worker
from tests.memory_support import memory

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "registered,kind,surface,replied,expected",
    [
        (True, "utterance", "unknown", False, True),
        (False, "observation", "unknown", False, True),
        (False, "utterance", "chat", False, True),
        (False, "utterance", "comment", True, True),
        (False, "utterance", "comment", False, False),
        (False, "utterance", "unknown", True, False),
    ],
)
def test_input_eligibility_preserves_surface_and_comment_reply_rules(
    registered: bool, kind: str, surface: str, replied: bool, expected: bool
) -> None:
    assert (
        input_eligible(
            replace(turn(actor="user-a", kind=kind), surface=surface),
            actor_is_character=registered,
            conversation_surfaces=frozenset({"chat"}),
            comment_surfaces=frozenset({"comment"}),
            has_character_reply=replied,
        )
        is expected
    )


def test_loaded_round_trip_expands_only_the_first_direct_reply_and_keeps_missing_absent() -> None:
    values = {
        t.turn_id: ResolvedTurn(t, None, True)
        for t in (
            turn("origin", reply_to_turn_id="question"),
            turn("question", reply_to_turn_id="ancestor"),
            turn("ancestor"),
        )
    }
    resolved = resolve_input(("origin", "question", "absent"), values)
    assert tuple(v.turn.turn_id for v in resolved.turns) == ("origin", "question")
    assert resolve_input(("absent",), values).turns == ()


@pytest.mark.parametrize("used_end,visible", [(2, True), (3, False)])
def test_loaded_projection_checks_dependency_range_without_fetching_during_evaluation(
    monkeypatch: pytest.MonkeyPatch, used_end: int, visible: bool
) -> None:
    h, store, service, _ = setup_worker()
    h.evidence.add_turn(turn(actor="user-a"))
    h.evidence.add_turn(turn("dependency", actor="user-a", text="OK"))
    value = memory("summary")
    store.baseline_inputs[("char-b", value.id)] = (
        InputDependency(
            turn_id="dependency",
            basis="original",
            receipt_version=0,
            available_end=3,
            used_end=used_end,
            deadline=RECORDED,
        ),
    )
    with store.transaction("char-b") as tx:
        tx.add_memory(value)
        snapshot = projection_input(tx)
    assert source_ids(snapshot) == frozenset({"turn-1", "dependency"})
    loaded = collect_evidence(snapshot, "char-b", service.inputs)
    monkeypatch.setattr(
        h.evidence, "get_turn", MagicMock(side_effect=AssertionError("Unexpected I/O"))
    )
    monkeypatch.setattr(
        h.materials, "get_text", MagicMock(side_effect=AssertionError("Unexpected I/O"))
    )
    result = loaded.project(snapshot, RECORDED)
    assert tuple(v for v in result.memories.values() if memory_valid_at(v, RECORDED)) == (
        (value,) if visible else ()
    )
    assert result.relations == ()
    assert store.get_memory("char-b", value.id) == value


def test_postgres_listing_translates_connect_failure_and_retries_with_a_new_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchall.return_value = []
    connect = MagicMock(side_effect=[psycopg.OperationalError(), connection])
    monkeypatch.setattr(psycopg, "connect", connect)
    listing = PostgresMemoryListing(
        "postgresql://synthetic",
        delivery_timeout=timedelta(minutes=10),
        conversation_surfaces=frozenset(),
        comment_surfaces=frozenset(),
    )
    with pytest.raises(MemoryUnavailable):
        listing.read("char-b", RECORDED)
    snapshot, evidence = listing.read("char-b", RECORDED)
    result = evidence.project(snapshot, RECORDED)
    assert result.memories == {}
    assert result.relations == ()
    connection.__exit__.assert_called_once()
    assert connect.call_count == 2
