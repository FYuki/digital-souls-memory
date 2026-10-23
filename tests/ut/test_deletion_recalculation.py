"""Recalculation uses only remaining, perceived and delivered originals."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from importlib import import_module
from typing import TYPE_CHECKING

import pytest

from digital_souls_memory.evidence.materials import ReceiptState
from digital_souls_memory.memory.domain import DerivedMemory
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from digital_souls_memory.recall.domain import CandidateMaterial
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import INSTRUCTION, RECORDED, session, turn
from tests.memory_support import NEW_ID, memory

if TYPE_CHECKING:
    from digital_souls_memory.governance.domain import (
        DeletionSnapshot,
        MemoryCopy,
        MemoryReplacement,
    )

pytestmark = pytest.mark.ut
TIMEOUT = timedelta(minutes=10)
NO_RECEIPTS = ReceiptState(None, 0)


@pytest.fixture(autouse=True)
def require_recalculation_module() -> None:
    # Import failures must not be mistaken for a rejected gateway response.
    import_module("digital_souls_memory.governance.recalculation")


def material(
    *,
    actor: str = "user-a",
    perceived_by: tuple[str, ...] = ("char-b",),
    addressees: tuple[str, ...] = (),
    mode: str = "broadcast",
    text: str = "残った合成根拠",
    label: str = "synthetic-sensitive",
    reports_delivery: bool = False,
    receipt_state: ReceiptState = NO_RECEIPTS,
) -> CandidateMaterial:
    return CandidateMaterial(
        turn=turn(
            "turn-2",
            actor=actor,
            text=text,
            perceived_by=perceived_by,
            addressees=addressees,
            address_mode=mode,
            sensitivity=label,
        ),
        session=session(reports_delivery=reports_delivery),
        actor_is_character=actor.startswith("char-"),
        receipt_state=receipt_state,
    )


def snapshot(copy: MemoryCopy, materials: tuple[CandidateMaterial, ...]) -> DeletionSnapshot:
    from digital_souls_memory.governance.domain import DeletionSnapshot

    return DeletionSnapshot(
        target_turn_ids=("turn-1",),
        copies=(copy,),
        materials=materials,
        references=(),
        fingerprint="synthetic-snapshot",
        observed_at=RECORDED,
    )


def recalculate(
    original: DerivedMemory,
    gateway: RecordingRecalculationGateway | None,
    materials: tuple[CandidateMaterial, ...],
    *,
    now: datetime = RECORDED,
) -> MemoryReplacement:
    from digital_souls_memory.governance.domain import MemoryCopy
    from digital_souls_memory.governance.recalculation import MemoryRecalculator

    copy = MemoryCopy(table="derived_memories", memory=original, origin_turn_id=None)
    return MemoryRecalculator(
        gateway,
        delivery_timeout=TIMEOUT,
        non_sensitive_labels=frozenset({"public"}),
    ).recalculate(copy, snapshot=snapshot(copy, materials), now=now)


def test_remaining_original_replaces_content_and_records_its_used_range() -> None:
    original = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    output = replace(original, content="新しい合成要約", source_turn_ids=("turn-2",))
    gateway = RecordingRecalculationGateway(output)
    source = material(text=INSTRUCTION)

    result = recalculate(original, gateway, (source,))

    assert result.value == output
    assert [(t.turn_id, t.text) for call, _, _ in gateway.calls for t in call.turns] == [
        ("turn-2", INSTRUCTION)
    ]
    assert [(identifier, kind) for _, identifier, kind in gateway.calls] == [
        (original.id, original.kind)
    ]
    (stamp,) = result.dependencies
    assert (stamp.turn_id, stamp.character_id, stamp.used_end, stamp.available_end) == (
        "turn-2",
        "char-b",
        len(INSTRUCTION),
        len(INSTRUCTION),
    )


def test_no_remaining_support_invalidates_without_a_gateway() -> None:
    assert recalculate(memory("summary"), None, ()).value is None


@pytest.mark.parametrize("returns_none", [True, False])
def test_gateway_failure_is_not_converted_to_successful_invalidation(
    returns_none: bool,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    original = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    gateway = RecordingRecalculationGateway(None)
    if returns_none:
        assert recalculate(original, gateway, (material(),)).value is None
    else:
        gateway.failure = RuntimeError("synthetic model failure")
        with rejected():
            recalculate(original, gateway, (material(),))


def test_missing_gateway_cannot_invalidate_a_supported_memory(
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    original = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    with rejected():
        recalculate(original, None, (material(),))


def test_gateway_timeout_is_reported_as_dependency_unavailable() -> None:
    from digital_souls_memory.memory.ports import MemoryUnavailable

    original = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    gateway = RecordingRecalculationGateway(None)
    gateway.failure = TimeoutError("synthetic dependency timeout")
    with pytest.raises(MemoryUnavailable):
        recalculate(original, gateway, (material(),))


@pytest.mark.parametrize(
    "invalid",
    [
        "owner",
        "identifier",
        "kind",
        "deleted_source",
        "unknown_source",
        "sharing",
        "tier",
        "source_type",
    ],
)
def test_invalid_output_is_rejected(
    invalid: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    original = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    output = replace(original, content="再計算済み", source_turn_ids=("turn-2",))
    if invalid == "owner":
        output = replace(output, character_id="char-c")
    elif invalid == "identifier":
        output = replace(output, id=NEW_ID)
    elif invalid == "kind":
        output = replace(output, kind="key")
    elif invalid == "deleted_source":
        output = replace(output, source_turn_ids=("turn-1",))
    elif invalid == "unknown_source":
        output = replace(output, source_turn_ids=("unknown",))
    elif invalid == "sharing":
        output = replace(output, shared_with=("char-c",))
    elif invalid == "tier":
        output = replace(output, epistemics=replace(output.epistemics, trust_tier="A"))
    else:
        output = replace(output, epistemics=replace(output.epistemics, source_type="direct_user"))
    with rejected():
        recalculate(original, RecordingRecalculationGateway(output), (material(),))


@pytest.mark.parametrize(
    "second_actor,reply_to,expected",
    [
        ("user-a", None, 1),
        ("user-b", "turn-2", 1),
        ("user-b", None, 2),
    ],
)
def test_evidence_count_and_confidence_follow_remaining_opportunities(
    second_actor: str,
    reply_to: str | None,
    expected: int,
) -> None:
    original = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2", "turn-3"))
    confidence = replace(original.epistemics.confidence_features, source=0.9)
    output = replace(
        original,
        content="再計算済み",
        source_turn_ids=("turn-2", "turn-3"),
        epistemics=replace(
            original.epistemics, independent_evidence_count=99, confidence_features=confidence
        ),
    )
    first = material()
    second = replace(
        first,
        turn=replace(first.turn, turn_id="turn-3", actor=second_actor, reply_to_turn_id=reply_to),
    )
    result = recalculate(original, RecordingRecalculationGateway(output), (first, second))
    assert result.value is not None
    assert result.value.epistemics.independent_evidence_count == expected
    assert result.value.epistemics.confidence_features == confidence
    assert result.value.epistemics.trust_tier == original.epistemics.trust_tier
    assert result.value.epistemics.source_type == original.epistemics.source_type


@pytest.mark.parametrize("closed", ["valid_to", "superseded_at"])
def test_recalculation_preserves_closed_baseline_temporal_attributes(closed: str) -> None:
    from digital_souls_memory.governance.domain import MemoryCopy
    from digital_souls_memory.governance.recalculation import MemoryRecalculator

    original = replace(
        memory("summary"),
        source_turn_ids=("turn-1", "turn-2"),
        last_reinforced_at=RECORDED - timedelta(hours=1),
    )
    original = (
        replace(original, valid_to=RECORDED)
        if closed == "valid_to"
        else replace(original, superseded_at=RECORDED)
    )
    output = replace(
        original,
        source_turn_ids=("turn-2",),
        content="残存原文による履歴",
        valid_to=None,
        superseded_at=None,
        last_reinforced_at=RECORDED,
    )
    copy = MemoryCopy(table="memory_baselines", memory=original)
    result = MemoryRecalculator(
        RecordingRecalculationGateway(output),
        delivery_timeout=TIMEOUT,
        non_sensitive_labels=frozenset({"public"}),
    ).recalculate(copy, snapshot=snapshot(copy, (material(),)), now=RECORDED)
    assert result.value is not None
    assert result.value.content == output.content
    assert result.value.source_turn_ids == ("turn-2",)
    assert result.value.valid_to == original.valid_to
    assert result.value.superseded_at == original.superseded_at
    assert result.value.last_reinforced_at == original.last_reinforced_at


@pytest.mark.parametrize(
    "actor,perceived_by,addressees,mode,available",
    [
        ("char-c", ("char-b", "char-c"), (), "broadcast", True),
        ("char-c", ("char-c",), (), "broadcast", False),
        ("user-a", (), (), "broadcast", False),
        ("char-b", ("char-b",), (), "self", True),
        ("char-c", ("char-b", "char-c"), ("char-b",), "direct", True),
        ("char-c", ("char-c",), ("user-a",), "direct", False),
        ("char-c", ("char-c",), ("char-b",), "direct", False),
        ("char-b", ("char-b",), ("user-a",), "direct", True),
    ],
)
def test_perception_cannot_be_inferred_from_addressees_or_session(
    actor: str,
    perceived_by: tuple[str, ...],
    addressees: tuple[str, ...],
    mode: str,
    available: bool,
) -> None:
    original = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    output = replace(original, source_turn_ids=("turn-2",), content="残存原文の再計算")
    gateway = RecordingRecalculationGateway(output)
    source = material(actor=actor, perceived_by=perceived_by, addressees=addressees, mode=mode)
    result = recalculate(original, gateway, (source,))
    if available:
        assert result.value == output
        assert [(t.turn_id, t.text) for call, _, _ in gateway.calls for t in call.turns] == [
            ("turn-2", source.turn.text)
        ]
    else:
        assert result.value is None
        assert gateway.calls == []
    assert source.turn.perceived_by == perceived_by


@pytest.mark.parametrize(
    "end,elapsed,actor,expected",
    [
        (6, 0, "char-c", "配達済み本文"),
        (3, 0, "char-c", "配達済"),
        (0, 0, "char-c", None),
        (None, 0, "char-c", None),
        (None, 10, "char-c", "配達済み本文"),
        (0, 0, "char-b", "配達済み本文"),
    ],
)
def test_delivery_limits_gateway_text_and_persisted_dependencies(
    end: int | None,
    elapsed: int,
    actor: str,
    expected: str | None,
) -> None:
    original = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    output = replace(original, source_turn_ids=("turn-2",), content="配達範囲からの要約")
    source = material(
        actor=actor,
        perceived_by=("char-b", "char-c"),
        text="配達済み本文",
        reports_delivery=True,
        receipt_state=ReceiptState(end, int(end is not None)),
    )
    gateway = RecordingRecalculationGateway(output)
    result = recalculate(original, gateway, (source,), now=RECORDED + timedelta(minutes=elapsed))
    if expected is None:
        assert result.value is None
        assert gateway.calls == []
    else:
        assert result.value == output
        assert [t.text for call, _, _ in gateway.calls for t in call.turns] == [expected]
        (stamp,) = result.dependencies
        assert stamp.used_end == stamp.available_end == len(expected)
        assert stamp.deadline == RECORDED + TIMEOUT


@pytest.mark.parametrize("label,local", [("public", False), ("secret", True), ("unknown", True)])
def test_gateway_route_uses_remaining_source_sensitivity(label: str, local: bool) -> None:
    original = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    output = replace(original, source_turn_ids=("turn-2",), content="再計算済み")
    gateway = RecordingRecalculationGateway(output)
    recalculate(original, gateway, (material(label=label),))
    assert len(gateway.calls) == 1
    assert isinstance(gateway.calls[0][0], LocalOnly if local else CloudInput)
    assert bool(gateway.local_calls) == local
    assert bool(gateway.cloud_calls) != local


def test_deleted_commitment_original_cannot_be_replaced_by_another_turn() -> None:
    original = replace(memory("commitment"), source_turn_ids=("turn-1", "turn-2"))
    gateway = RecordingRecalculationGateway(
        replace(original, source_turn_ids=("turn-2",), verbatim_turn_id="turn-2")
    )
    assert recalculate(original, gateway, (material(),)).value is None
    assert gateway.calls == []


def test_surviving_commitment_original_cannot_be_reassigned(
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    original = replace(
        memory("commitment"),
        source_turn_ids=("turn-1", "turn-2", "turn-3"),
        verbatim_turn_id="turn-2",
    )
    first = material(addressees=("char-b",), mode="direct")
    second = replace(first, turn=replace(first.turn, turn_id="turn-3"))
    output = replace(original, source_turn_ids=("turn-2", "turn-3"), verbatim_turn_id="turn-3")
    with rejected():
        recalculate(original, RecordingRecalculationGateway(output), (first, second))


@pytest.mark.parametrize("relation_type", ["updates", "contradicts", "extends"])
@pytest.mark.parametrize("survives", [False, True])
def test_temporal_effects_are_reconstructed_from_baseline_and_surviving_sources(
    relation_type: str, survives: bool
) -> None:
    from digital_souls_memory.governance.domain import MemoryCopy, TemporalEffect
    from digital_souls_memory.governance.recalculation import MemoryRecalculator
    from digital_souls_memory.memory.changes import apply_temporal_effect

    original = replace(memory(), source_turn_ids=("turn-1", "turn-2"))
    remaining = material()
    supporting = replace(
        remaining,
        turn=replace(remaining.turn, turn_id="turn-3", occurred_at=RECORDED + timedelta(hours=1)),
    )
    candidate = replace(
        original, id=NEW_ID, source_turn_ids=("turn-3",), valid_from=supporting.turn.occurred_at
    )
    old = apply_temporal_effect(
        original,
        candidate,
        relation_type=relation_type,
        source_turns=(turn(), remaining.turn, supporting.turn),
        at=RECORDED,
    )
    copy = MemoryCopy(
        table="derived_memories",
        memory=old,
        temporal_base=original,
        temporal_effects=(
            TemporalEffect(candidate=candidate, relation_type=relation_type, applied_at=RECORDED),
        )
        if survives
        else (),
    )
    output = replace(original, source_turn_ids=("turn-2",), content="残存原文の本文")
    gateway = RecordingRecalculationGateway(output)
    result = MemoryRecalculator(
        gateway, delivery_timeout=TIMEOUT, non_sensitive_labels=frozenset()
    ).recalculate(
        copy,
        snapshot=snapshot(copy, (remaining, supporting) if survives else (remaining,)),
        now=RECORDED,
    )
    assert result.value is not None
    expected = old if survives else original
    assert (result.value.valid_to, result.value.superseded_at, result.value.last_reinforced_at) == (
        expected.valid_to,
        expected.superseded_at,
        expected.last_reinforced_at,
    )
    assert result.value.content == output.content
    assert set(result.value.source_turn_ids) == (
        {"turn-2", "turn-3"} if survives and relation_type == "extends" else {"turn-2"}
    )
    assert {d.turn_id for d in result.dependencies} == set(result.value.source_turn_ids)
