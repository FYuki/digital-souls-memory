from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.evidence.domain import DeliveryReceipt
from digital_souls_memory.evidence.materials import available_text
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
