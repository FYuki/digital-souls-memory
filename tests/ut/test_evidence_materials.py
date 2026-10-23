from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.evidence.domain import DeliveryReceipt
from digital_souls_memory.evidence.materials import available_text, material_decision
from tests.evidence_support import RECORDED, TEXT, receipt, session, turn

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "delivery,actor,kind,reader,receipts,elapsed,expected",
    [
        (False, "char-c", "utterance", "char-b", (), 0, TEXT),
        (True, "viewer", "utterance", "char-b", (), 0, TEXT),
        (True, "char-c", "observation", "char-b", (), 0, TEXT),
        (True, "char-c", "utterance", "char-c", (), 0, TEXT),
        (True, "char-c", "utterance", "char-b", (receipt("full", len(TEXT)),), 0, TEXT),
        (True, "char-c", "utterance", "char-b", (receipt("partial", 9),), 0, TEXT[:9]),
        (True, "char-c", "utterance", "char-b", (receipt("none", 0),), 0, ""),
        (True, "char-c", "utterance", "char-b", (), 599, None),
        (True, "char-c", "utterance", "char-b", (), 600, TEXT),
        (True, "char-c", "utterance", "char-b", (receipt("none", 0),), 3600, ""),
        (True, "char-c", "utterance", "char-b", (receipt("partial", 9),), 3600, TEXT[:9]),
    ],
)
def test_material_text_obeys_delivery_and_timeout(
    delivery: bool,
    actor: str,
    kind: str,
    reader: str,
    receipts: tuple[DeliveryReceipt, ...],
    elapsed: int,
    expected: str | None,
) -> None:
    assert (
        available_text(
            character_id=reader,
            session=session(reports_delivery=delivery),
            turn=turn(actor=actor, kind=kind),
            actor_is_character=actor == "char-c",
            receipts=receipts,
            now=RECORDED + timedelta(seconds=elapsed),
            delivery_timeout=timedelta(minutes=10),
        )
        == expected
    )


@pytest.mark.parametrize("elapsed,expected", [(29, None), (30, TEXT)])
def test_custom_timeout_uses_recorded_time(elapsed: int, expected: str | None) -> None:
    assert (
        available_text(
            character_id="char-b",
            session=session(),
            turn=turn(),
            actor_is_character=True,
            receipts=(),
            now=RECORDED + timedelta(seconds=elapsed),
            delivery_timeout=timedelta(seconds=30),
        )
        == expected
    )


def test_last_appended_receipt_controls_the_whole_range() -> None:
    latest = replace(receipt("partial", 9), reported_at=RECORDED - timedelta(hours=1))
    assert (
        available_text(
            character_id="char-b",
            session=session(),
            turn=turn(),
            actor_is_character=True,
            receipts=(receipt("full", len(TEXT)), latest),
            now=RECORDED,
            delivery_timeout=timedelta(minutes=10),
        )
        == TEXT[:9]
    )


@pytest.mark.parametrize(
    "perceived,reader,kind,mode,targets",
    [
        (("char-c", "char-b"), "char-a", "utterance", "direct", ("char-a",)),
        ((), "char-a", "utterance", "broadcast", ()),
        (("char-c",), "char-b", "thought", "self", ()),
    ],
)
def test_addressing_and_delivery_cannot_grant_perception(
    perceived: tuple[str, ...],
    reader: str,
    kind: str,
    mode: str,
    targets: tuple[str, ...],
) -> None:
    value = turn(
        actor="viewer" if not perceived else "char-c",
        kind=kind,
        perceived_by=perceived,
        address_mode=mode,
        addressees=targets,
    )
    assert (
        available_text(
            character_id=reader,
            session=session(reports_delivery=False),
            turn=value,
            actor_is_character=bool(perceived),
            receipts=(),
            now=RECORDED,
            delivery_timeout=timedelta(minutes=10),
        )
        is None
    )


def test_thought_is_available_only_to_its_actor() -> None:
    assert (
        available_text(
            character_id="char-c",
            session=session(),
            turn=turn(kind="thought", perceived_by=("char-c",), address_mode="self"),
            actor_is_character=True,
            receipts=(),
            now=RECORDED,
            delivery_timeout=timedelta(minutes=10),
        )
        == TEXT
    )


@pytest.mark.parametrize(
    "declared,elapsed,receipts,basis",
    [
        (False, 0, (), "declared_full"),
        (True, 599, (), "pending"),
        (True, 600, (), "assumed_full"),
        (True, 0, (receipt("partial", 9),), "receipt_confirmed"),
    ],
)
def test_delivery_decision_retains_basis_deadline_and_receipt_version(
    declared: bool,
    elapsed: int,
    receipts: tuple[DeliveryReceipt, ...],
    basis: str,
) -> None:
    value = material_decision(
        character_id="char-b",
        session=session(reports_delivery=declared),
        turn=turn(),
        actor_is_character=True,
        receipts=receipts,
        now=RECORDED + timedelta(seconds=elapsed),
        delivery_timeout=timedelta(minutes=10),
    )
    assert value.basis == basis
    assert value.deadline == RECORDED + timedelta(minutes=10)
    assert value.receipt_version == len(receipts)


@pytest.mark.parametrize("delivery", [False, True])
def test_speaker_full_text_is_never_recorded_as_receipt_confirmation(delivery: bool) -> None:
    value = material_decision(
        character_id="char-c",
        session=session(reports_delivery=delivery),
        turn=turn(),
        actor_is_character=True,
        receipts=(receipt("none", 0),),
        now=RECORDED,
        delivery_timeout=timedelta(minutes=10),
    )
    assert value.text == TEXT
    assert value.basis == ("original" if delivery else "declared_full")
    assert value.receipt_version == 0


def test_receipt_count_and_latest_appended_range_include_repeated_corrections() -> None:
    history = (
        receipt("none", 0),
        receipt("partial", 3),
        receipt("full", 6),
        replace(receipt("partial", 3), reported_at=RECORDED - timedelta(hours=1)),
    )
    value = material_decision(
        character_id="char-b",
        session=session(),
        turn=turn(text="日本語の原文"),
        actor_is_character=True,
        receipts=iter(history),
        now=RECORDED,
        delivery_timeout=timedelta(minutes=10),
    )
    assert (value.text, value.basis, value.receipt_version) == ("日本語", "receipt_confirmed", 4)


@pytest.mark.parametrize(
    "delivery,actor,kind,reader,expected,basis",
    [
        (False, "char-c", "utterance", "char-b", TEXT, "declared_full"),
        (True, "viewer", "utterance", "char-b", TEXT, "original"),
        (True, "char-c", "observation", "char-b", TEXT, "original"),
        (True, "char-c", "utterance", "char-c", TEXT, "original"),
        (True, "char-c", "thought", "char-c", TEXT, "original"),
        (True, "char-c", "utterance", "char-a", None, "inaccessible"),
    ],
)
def test_early_material_decisions_do_not_consume_receipts(
    delivery: bool,
    actor: str,
    kind: str,
    reader: str,
    expected: str | None,
    basis: str,
) -> None:
    def unreadable() -> Iterator[DeliveryReceipt]:
        raise AssertionError("This decision does not require receipts")
        yield

    value = material_decision(
        character_id=reader,
        session=session(reports_delivery=delivery),
        turn=turn(
            actor=actor,
            kind=kind,
            perceived_by=(actor,) if kind == "thought" else ("char-b", "char-c"),
        ),
        actor_is_character=actor == "char-c",
        receipts=unreadable(),
        now=RECORDED,
        delivery_timeout=timedelta(minutes=10),
    )
    assert (value.text, value.basis, value.receipt_version) == (expected, basis, 0)
