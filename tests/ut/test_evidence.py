from collections.abc import Callable
from dataclasses import replace
from typing import cast

import pytest

from digital_souls_memory.evidence.domain import (
    DeliveryReceipt,
    Session,
    Turn,
    can_perceive,
    validate_receipt,
)
from tests.evidence_support import INSTRUCTION, TEXT, receipt, session, turn

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize(
    "entity,field",
    [(session, "surface"), (turn, "text"), (lambda: receipt("none", 0), "reported_by")],
)
def test_evidence_rejects_normal_attribute_changes(
    operation: str, entity: Callable[[], Session | Turn | DeliveryReceipt], field: str
) -> None:
    value = entity()
    before = getattr(value, field)
    with pytest.raises((AttributeError, TypeError)):
        if operation == "update":
            setattr(value, field, "changed")
        else:
            delattr(value, field)
    assert getattr(value, field) == before


def test_original_language_and_register_are_preserved_verbatim() -> None:
    value = turn()
    assert value.text == TEXT
    assert value.language == "ja"
    assert dict(value.register) == {"first_person": "私", "politeness": "敬語", "address": "あなた"}


@pytest.mark.parametrize("field", ["sensitivity", "classifier_version"])
@pytest.mark.parametrize("invalid", [None, "", " \t\n", 7, False, [], {}])
def test_labels_are_required_without_defaulting(field: str, invalid: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        if field == "sensitivity":
            turn(sensitivity=cast(str, invalid))
        else:
            turn(classifier_version=cast(str, invalid))


@pytest.mark.parametrize(
    "character,expected", [("char-a", True), ("char-b", True), ("char-c", False), ("x", False)]
)
def test_perception_is_participants_or_audience(character: str, expected: bool) -> None:
    assert can_perceive(character, session(), turn()) is expected


@pytest.mark.parametrize("actor", ["char-a", "char-b", "synthetic-viewer"])
def test_self_ai_and_viewer_utterances_are_not_excluded(actor: str) -> None:
    value = turn(actor=actor)
    assert value.actor == actor
    assert value.kind == "utterance"
    assert can_perceive("char-a", session(), value)


def test_experience_can_be_recorded_as_observation() -> None:
    assert turn(kind="observation").kind == "observation"


def test_instruction_text_does_not_change_labels_or_identity() -> None:
    value = turn(text=INSTRUCTION)
    assert value.text == INSTRUCTION
    assert value.actor == "char-c"
    assert value.sensitivity == "synthetic-sensitive"
    assert value.classifier_version == "synthetic-v1"


@pytest.mark.parametrize("status,end", [("full", len(TEXT)), ("partial", 1), ("none", 0)])
def test_delivery_range_uses_unicode_codepoints(status: str, end: int) -> None:
    report = receipt(status, end)
    validate_receipt(report, TEXT)
    assert report.spoken_until == end


@pytest.mark.parametrize(
    "status,end",
    [
        ("full", len(TEXT) - 1),
        ("full", len(TEXT.encode())),
        ("partial", 0),
        ("partial", len(TEXT)),
        ("none", 1),
        ("partial", -1),
        ("full", len(TEXT) + 1),
        ("partial", True),
        ("partial", 1.0),
        ("partial", "1"),
        ("unknown", 0),
    ],
)
def test_invalid_delivery_range_is_rejected(status: str, end: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        validate_receipt(receipt(status, cast(int, end)), TEXT)


def test_empty_text_has_no_partial_delivery() -> None:
    with pytest.raises((ValueError, TypeError)):
        validate_receipt(receipt("partial", 1), "")
    validate_receipt(receipt("full", 0), "")
    validate_receipt(receipt("none", 0), "")


def test_mutable_input_and_returned_metadata_cannot_change_evidence() -> None:
    participants = ["char-a"]
    audience = ["char-b"]
    metadata = {"first_person": "私"}
    saved_session = replace(session(), participants=cast(tuple[str, ...], participants))
    saved_turn = replace(turn(), audience=cast(tuple[str, ...], audience), register=metadata)
    participants.clear()
    audience.clear()
    metadata["first_person"] = "changed"
    with pytest.raises(TypeError):
        saved_turn.register["first_person"] = "changed"  # type: ignore[index]
    assert saved_session.participants == ("char-a",)
    assert saved_turn.audience == ("char-b",)
    assert dict(saved_turn.register) == {"first_person": "私"}


@pytest.mark.parametrize("field", ["occurred_at", "recorded_at"])
def test_turn_timestamps_require_timezone(field: str) -> None:
    naive = turn().occurred_at.replace(tzinfo=None)
    with pytest.raises(ValueError):
        if field == "occurred_at":
            replace(turn(), occurred_at=naive)
        else:
            replace(turn(), recorded_at=naive)


def test_perception_does_not_use_an_unrelated_session() -> None:
    assert not can_perceive("char-a", session("other"), turn())
