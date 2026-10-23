from collections.abc import Callable
from dataclasses import replace
from typing import cast

import pytest

from digital_souls_memory.evidence import domain
from digital_souls_memory.evidence.domain import (
    DeliveryReceipt,
    Session,
    Turn,
    can_perceive,
    validate_receipt,
)
from digital_souls_memory.evidence.use_cases import EvidenceService
from tests.evidence_support import INSTRUCTION, RECORDED, TEXT, receipt, session, turn
from tests.it1.evidence_fakes import FakeEvidenceStore

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize(
    "entity,field",
    [
        (session, "surface"),
        (session, "reports_delivery"),
        (turn, "text"),
        (turn, "perceived_by"),
        (turn, "addressees"),
        (turn, "address_mode"),
        (turn, "reply_to_turn_id"),
        (lambda: receipt("none", 0), "reported_by"),
    ],
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
    "character,expected", [("char-a", False), ("char-b", True), ("char-c", True), ("x", False)]
)
def test_perception_uses_only_explicit_perceived_by(character: str, expected: bool) -> None:
    assert can_perceive(character, session(), turn()) is expected


@pytest.mark.parametrize("actor", ["char-a", "synthetic-viewer"])
def test_empty_perception_does_not_grant_actor_an_exception(actor: str) -> None:
    value = turn(actor=actor, perceived_by=())
    assert not can_perceive(actor, session(), value)


@pytest.mark.parametrize("mode,targets", [("direct", ("char-a",)), ("broadcast", ()), ("self", ())])
def test_addressing_does_not_expand_perception(mode: str, targets: tuple[str, ...]) -> None:
    value = turn(address_mode=mode, addressees=targets)
    assert can_perceive("char-b", session(), value)
    assert not can_perceive("char-a", session(), value)


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
    perceived = ["char-b", "char-c"]
    targets = ["viewer", "char-a"]
    metadata = {"first_person": "私"}
    saved_turn = replace(
        turn(address_mode="direct", addressees=("viewer", "char-a")),
        perceived_by=cast(tuple[str, ...], perceived),
        addressees=cast(tuple[str, ...], targets),
        register=metadata,
    )
    perceived.clear()
    targets.clear()
    metadata["first_person"] = "changed"
    with pytest.raises(TypeError):
        saved_turn.register["first_person"] = "changed"  # type: ignore[index]
    assert saved_turn.perceived_by == ("char-b", "char-c")
    assert saved_turn.addressees == ("viewer", "char-a")
    assert dict(saved_turn.register) == {"first_person": "私"}


@pytest.mark.parametrize("field", ["occurred_at", "recorded_at"])
def test_turn_timestamps_require_timezone(field: str) -> None:
    naive = turn().occurred_at.replace(tzinfo=None)
    with pytest.raises(ValueError):
        if field == "occurred_at":
            replace(turn(), occurred_at=naive)
        else:
            replace(turn(), recorded_at=naive)


@pytest.mark.parametrize("character", ["char-a", "char-b", "char-c"])
def test_perception_does_not_use_an_unrelated_session(character: str) -> None:
    assert not can_perceive(character, session("other"), turn())


@pytest.mark.parametrize("value", [True, False])
def test_session_preserves_delivery_declaration(value: bool) -> None:
    assert session(reports_delivery=value).reports_delivery is value


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_session_rejects_non_boolean_delivery_declaration(value: object) -> None:
    with pytest.raises(ValueError):
        session(reports_delivery=cast(bool, value))


@pytest.mark.parametrize("field", ["perceived_by", "addressees"])
@pytest.mark.parametrize(
    "value", [None, "char-b", 1, {}, (7,), ("",), (" \t",), ("char-b", "char-b")]
)
def test_turn_rejects_invalid_identifier_collections(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        if field == "perceived_by":
            turn(perceived_by=cast(tuple[str, ...], value))
        else:
            turn(addressees=cast(tuple[str, ...], value))


@pytest.mark.parametrize(
    "mode,targets",
    [
        ("direct", ()),
        ("broadcast", ("viewer",)),
        ("self", ("viewer",)),
        ("unknown", ()),
        ("", ()),
        (None, ()),
    ],
)
def test_turn_rejects_invalid_resolved_addressing(mode: str, targets: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        turn(address_mode=mode, addressees=targets)


@pytest.mark.parametrize("perceived", [(), ("char-b",), ("char-c", "char-b")])
def test_thought_rejects_other_than_exact_actor_perception(perceived: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        turn(kind="thought", perceived_by=perceived)


def test_thought_is_preserved_and_visible_only_to_its_actor() -> None:
    value = turn(kind="thought", perceived_by=("char-c",))
    assert value.kind == "thought"
    assert can_perceive("char-c", session(), value)
    assert not can_perceive("char-b", session(), value)


@pytest.mark.parametrize(
    "delivery,kind", [(False, "utterance"), (True, "thought"), (False, "thought")]
)
def test_receipt_eligibility_is_checked_before_storage(delivery: bool, kind: str) -> None:
    store = FakeEvidenceStore()
    store.add_session(session(reports_delivery=delivery))
    store.add_turn(turn(kind=kind, perceived_by=("char-c",)))
    before = store.writes
    with pytest.raises(ValueError):
        EvidenceService(store, lambda: RECORDED).add_receipt(receipt("none", 0))
    assert store.writes == before
    assert store.receipts == []


@pytest.mark.parametrize(
    "is_character,mode,targets,expected",
    [
        (False, None, (), "broadcast"),
        (False, "broadcast", (), "broadcast"),
        (True, "broadcast", (), "broadcast"),
        (False, "self", (), "self"),
        (True, "self", (), "self"),
        (False, "direct", ("viewer", "char-a"), "direct"),
        (True, "direct", ("viewer", "char-a"), "direct"),
    ],
)
def test_mode_resolution_preserves_explicit_modes_and_defaults_only_users(
    is_character: bool, mode: str | None, targets: tuple[str, ...], expected: str
) -> None:
    assert (
        domain.resolve_address_mode(
            address_mode=mode, actor_is_character=is_character, addressees=targets
        )
        == expected
    )


@pytest.mark.parametrize(
    "is_character,mode,targets",
    [
        (True, None, ()),
        (True, None, ("viewer",)),
        (False, None, ("viewer",)),
        (True, "direct", ()),
        (False, "direct", ()),
        (True, "broadcast", ("viewer",)),
        (False, "broadcast", ("viewer",)),
        (True, "self", ("viewer",)),
        (False, "self", ("viewer",)),
        (False, "", ()),
        (True, "unknown", ()),
        (False, "unknown", ()),
        (False, False, ()),
        (True, 0, ()),
    ],
)
def test_mode_resolution_rejects_invalid_inputs_without_defaulting(
    is_character: bool, mode: object, targets: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        domain.resolve_address_mode(
            address_mode=cast(str | None, mode), actor_is_character=is_character, addressees=targets
        )
