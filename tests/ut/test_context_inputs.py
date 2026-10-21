from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.evidence.materials import ReceiptState
from digital_souls_memory.recall.domain import CandidateMaterial
from tests.evidence_support import RECORDED, TEXT, session, turn

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "actor,kind,perceived,mode,targets,visible",
    [
        ("viewer", "utterance", (), "broadcast", (), False),
        ("viewer", "utterance", ("char-b",), "broadcast", (), True),
        ("char-b", "utterance", ("char-b",), "direct", ("viewer",), True),
        ("char-c", "utterance", ("char-b", "char-c"), "direct", ("char-b",), True),
        ("char-c", "utterance", ("char-c",), "direct", ("viewer",), False),
        ("char-c", "utterance", ("char-c",), "direct", ("char-b",), False),
        ("char-c", "utterance", ("char-b", "char-c"), "self", (), True),
        ("char-b", "thought", ("char-b",), "broadcast", (), True),
        ("char-c", "thought", ("char-c",), "broadcast", (), False),
    ],
)
def test_window_resolves_explicit_perception_without_inheriting_reply_or_addressee(
    actor: str,
    kind: str,
    perceived: tuple[str, ...],
    mode: str,
    targets: tuple[str, ...],
    visible: bool,
) -> None:
    from digital_souls_memory.resident.domain import ContextKey
    from digital_souls_memory.resident.inputs import resolve_window

    owner = replace(session(reports_delivery=False), ended_at=None)
    source = turn(
        actor=actor, kind=kind, perceived_by=perceived, address_mode=mode, addressees=targets
    )
    control = turn(
        "control",
        actor="char-b",
        perceived_by=("char-b",),
        reply_to_turn_id=source.turn_id,
        text="control",
    )
    materials = tuple(
        CandidateMaterial(
            turn=value,
            session=owner,
            actor_is_character=value.actor != "viewer",
            receipt_state=ReceiptState(None, 0),
        )
        for value in (source, control)
    )
    result = resolve_window(
        key=ContextKey(character_id="char-b", session_id=owner.session_id, surface=owner.surface),
        session=owner,
        materials=materials,
        notification_version=1,
        now=RECORDED,
        delivery_timeout=timedelta(minutes=10),
    )
    expected = {"turn-1", "control"} if visible else {"control"}
    assert {value.material.turn.turn_id for value in result.turns} == expected
    assert {value.turn_id for value in result.snapshot.dependencies} == expected
    assert all(value.decision.text == value.material.turn.text for value in result.turns)


@pytest.mark.parametrize(
    "reports,actor,spoken,count,elapsed,expected,basis",
    [
        (False, "char-c", None, 0, 0, TEXT, "declared_full"),
        (True, "char-b", 0, 1, 0, TEXT, "original"),
        (True, "char-c", None, 0, 599, None, "pending"),
        (True, "char-c", None, 0, 600, TEXT, "assumed_full"),
        (True, "char-c", 3, 1, 0, TEXT[:3], "receipt_confirmed"),
        (True, "char-c", 0, 1, 600, "", "receipt_confirmed"),
    ],
)
def test_window_applies_delivery_declaration_receipt_and_exact_timeout(
    reports: bool,
    actor: str,
    spoken: int | None,
    count: int,
    elapsed: int,
    expected: str | None,
    basis: str,
) -> None:
    from digital_souls_memory.resident.domain import ContextKey
    from digital_souls_memory.resident.inputs import resolve_window

    owner = replace(session(reports_delivery=reports), ended_at=None)
    source = turn(actor=actor)
    material = CandidateMaterial(
        turn=source,
        session=owner,
        actor_is_character=True,
        receipt_state=ReceiptState(spoken, count),
    )
    result = resolve_window(
        key=ContextKey(character_id="char-b", session_id=owner.session_id, surface=owner.surface),
        session=owner,
        materials=(material,),
        notification_version=1,
        now=RECORDED + timedelta(seconds=elapsed),
        delivery_timeout=timedelta(minutes=10),
    )
    if expected:
        (resolved,) = result.turns
        assert resolved.decision.text == expected
        assert resolved.decision.basis == basis
        (stamp,) = result.snapshot.dependencies
        assert (stamp.available_end, stamp.used_end, stamp.receipt_version) == (
            len(expected),
            len(expected),
            count if basis == "receipt_confirmed" else 0,
        )
    else:
        assert result.turns == ()
    assert result.snapshot.next_due_at == (
        RECORDED + timedelta(minutes=10) if basis == "pending" else None
    )
    assert material.turn == source and source.text == TEXT
