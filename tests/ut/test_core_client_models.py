"""The Core DTO boundary preserves wire values without domain inference."""

import pytest

from digital_souls_memory.client.models import (
    RecallRequest,
    RecallResponse,
    ReceiptRequest,
    SessionRequest,
    SessionResponse,
    TurnRequest,
    TurnResponse,
)
from tests.evidence_support import RECORDED, TEXT, receipt_payload, session_payload, turn_payload

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("delivery", [True, False])
def test_session_preserves_explicit_boolean(delivery: bool) -> None:
    payload = session_payload() | {"reports_delivery": delivery}
    value = SessionRequest.model_validate(payload)
    assert value.model_dump(mode="json")["reports_delivery"] is delivery
    assert SessionResponse.model_validate(payload).reports_delivery is delivery


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", "false", [], {}])
def test_session_does_not_coerce_delivery_declaration(invalid: object) -> None:
    with pytest.raises(ValueError):
        SessionRequest.model_validate(session_payload() | {"reports_delivery": invalid})


def test_session_requires_delivery_declaration() -> None:
    payload = session_payload()
    del payload["reports_delivery"]
    with pytest.raises(ValueError):
        SessionRequest.model_validate(payload)


@pytest.mark.parametrize("field", ["participants", "audience"])
def test_session_rejects_legacy_fields_in_both_directions(field: str) -> None:
    payload = session_payload() | {field: ["char-c"]}
    for model in (SessionRequest, SessionResponse):
        with pytest.raises(ValueError):
            model.model_validate(payload)


@pytest.mark.parametrize(
    "perceived,targets,mode",
    [
        (["char-c"], ["user-a"], "direct"),
        (["char-b", "char-c"], ["user-a"], "direct"),
        (["char-b", "char-c"], [], "self"),
        ([], [], "broadcast"),
    ],
)
def test_turn_keeps_perception_independent_of_addressees(
    perceived: list[str], targets: list[str], mode: str
) -> None:
    payload = turn_payload() | {
        "actor": "user-a" if not perceived else "char-c",
        "perceived_by": perceived,
        "addressees": targets,
        "address_mode": mode,
        "reply_to_turn_id": "earlier-turn",
    }
    value = TurnRequest.model_validate(payload)
    encoded = value.model_dump(mode="json", by_alias=True)
    for field in ("perceived_by", "addressees", "address_mode", "reply_to_turn_id", "register"):
        assert encoded[field] == payload[field]
    assert encoded["text"] == TEXT
    assert encoded["language"] == "ja"
    assert "audience" not in encoded
    assert "participants" not in encoded


@pytest.mark.parametrize("field", ["address_mode", "perceived_by", "addressees", "kind"])
def test_turn_requires_explicit_routing_fields(field: str) -> None:
    payload = turn_payload()
    del payload[field]
    with pytest.raises(ValueError):
        TurnRequest.model_validate(payload)


@pytest.mark.parametrize("field", ["sensitivity", "classifier_version"])
@pytest.mark.parametrize("invalid", [None, "", " \t", 1])
def test_turn_rejects_invalid_classification(field: str, invalid: object) -> None:
    with pytest.raises(ValueError):
        TurnRequest.model_validate(turn_payload() | {field: invalid})


@pytest.mark.parametrize("field", ["sensitivity", "classifier_version"])
def test_turn_requires_classification_in_requests_and_responses(field: str) -> None:
    payload = turn_payload() | {"recorded_at": RECORDED.isoformat()}
    del payload[field]
    with pytest.raises(ValueError):
        TurnResponse.model_validate(payload)
    del payload["recorded_at"]
    with pytest.raises(ValueError):
        TurnRequest.model_validate(payload)


@pytest.mark.parametrize("mode", [None, "", "unknown"])
def test_turn_does_not_default_explicit_invalid_mode(mode: object) -> None:
    with pytest.raises(ValueError):
        TurnRequest.model_validate(turn_payload() | {"address_mode": mode})


@pytest.mark.parametrize("field", ["audience", "participants"])
def test_turn_rejects_legacy_fields_in_both_directions(field: str) -> None:
    payload = turn_payload() | {field: ["char-c"]}
    with pytest.raises(ValueError):
        TurnRequest.model_validate(payload)
    with pytest.raises(ValueError):
        TurnResponse.model_validate(payload | {"recorded_at": RECORDED.isoformat()})


def test_turn_response_preserves_verbatim_text_and_distinct_timestamps() -> None:
    value = TurnResponse.model_validate(turn_payload() | {"recorded_at": RECORDED.isoformat()})
    encoded = value.model_dump(mode="json", by_alias=True)
    assert encoded["text"] == TEXT
    assert encoded["register"] == turn_payload()["register"]
    assert value.recorded_at == RECORDED
    assert value.occurred_at != value.recorded_at


@pytest.mark.parametrize("status,end", [("none", 0), ("partial", 3), ("full", len(TEXT))])
def test_receipt_preserves_unicode_character_offset(status: str, end: int) -> None:
    value = ReceiptRequest.model_validate(receipt_payload(status, end))
    assert value.status == status
    assert value.spoken_until == end


@pytest.mark.parametrize("end", [True, -1, "3", 3.5, None])
def test_receipt_rejects_non_integer_or_negative_offset(end: object) -> None:
    with pytest.raises(ValueError):
        ReceiptRequest.model_validate(receipt_payload("partial", end))


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_recall_accepts_classified_query_or_origin_turn(form: str) -> None:
    query = RecallRequest.model_validate(
        {
            "session_id": "session-1",
            "format": form,
            "query": "日本語",
            "sensitivity": "synthetic-sensitive",
            "classifier_version": "synthetic-v1",
        }
    )
    assert query.query == "日本語"
    origin = RecallRequest.model_validate(
        {"session_id": "session-1", "format": form, "turn_id": "turn-1"}
    )
    assert origin.turn_id == "turn-1"


@pytest.mark.parametrize(
    "selection",
    [{}, {"query": "q"}, {"query": "q", "turn_id": "t"}, {"query": " "}],
)
def test_recall_rejects_missing_ambiguous_or_unclassified_query(
    selection: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        RecallRequest.model_validate({"session_id": "session-1", "format": "materials"} | selection)


def test_recall_response_requires_provision_and_source_collections() -> None:
    payload: dict[str, object] = {
        "provision_id": "provided-1",
        "temperament_version": None,
        "personality_version": None,
        "memories": [],
        "source_spans": [],
        "verbatim_anchors": [],
        "context": None,
    }
    assert RecallResponse.model_validate(payload).provision_id == "provided-1"
    for field in ("provision_id", "source_spans", "memories", "verbatim_anchors"):
        incomplete = {key: value for key, value in payload.items() if key != field}
        with pytest.raises(ValueError):
            RecallResponse.model_validate(incomplete)
