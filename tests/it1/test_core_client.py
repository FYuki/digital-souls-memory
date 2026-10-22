"""Core calls cross the real HTTP boundary with isolated in-process ports."""

import json
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from digital_souls_memory.client import (
    MemoryClient,
    MemoryHTTPError,
    MemoryResponseError,
    MemoryTimeoutError,
    MemoryTransportError,
)
from digital_souls_memory.client.models import (
    DeletionRequest,
    RecallRequest,
    ReceiptRequest,
    ResidentRequest,
    SessionRequest,
    TurnRequest,
)

from tests.evidence_support import (
    RECORDED,
    TEXT,
    receipt_payload,
    session,
    session_payload,
    turn,
    turn_payload,
)
from tests.it1.test_recall import request as recall_payload
from tests.memory_support import memory

if TYPE_CHECKING:
    from tests.recall_support import RecallHarness

pytestmark = pytest.mark.it1


@pytest.fixture
def core(harness: "RecallHarness") -> Iterator[MemoryClient]:
    def transfer(request: httpx.Request) -> httpx.Response:
        response = harness.client.request(
            request.method, str(request.url), content=request.content, headers=request.headers
        )
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    with MemoryClient(
        base_url="http://testserver", timeout=3.0, transport=httpx.MockTransport(transfer)
    ) as client:
        yield client


def test_session_conflict_does_not_change_declaration(core: MemoryClient) -> None:
    value = core.get_session("session-1")
    assert value.reports_delivery is False
    with pytest.raises(MemoryHTTPError) as failed:
        core.add_session(SessionRequest.model_validate(session_payload()))
    assert failed.value.status_code == 409
    assert core.get_session("session-1").reports_delivery is False


@pytest.mark.parametrize("group", [False, True])
def test_reply_roundtrip_keeps_addressee_reply_and_perception_independent(
    core: MemoryClient, group: bool
) -> None:
    question = turn_payload() | {
        "turn_id": "question",
        "actor": "user-a",
        "perceived_by": ["char-c"],
    }
    core.add_turn(TurnRequest.model_validate(question))
    payload = turn_payload() | {
        "perceived_by": ["char-c", "char-b"] if group else ["char-c"],
        "addressees": ["user-a"],
        "address_mode": "direct",
        "reply_to_turn_id": "question",
    }
    written = core.add_turn(TurnRequest.model_validate(payload))
    restored = core.get_turn(written.turn_id)
    assert restored.text == TEXT
    assert restored.language == "ja"
    assert list(restored.addressees) == ["user-a"]
    assert list(restored.perceived_by) == payload["perceived_by"]
    assert restored.reply_to_turn_id == "question"
    assert restored.recorded_at == RECORDED
    assert restored.occurred_at != restored.recorded_at
    assert {v.turn_id for v in core.list_perceived_turns("char-c")} == {"question", "turn-1"}
    assert {v.turn_id for v in core.list_perceived_turns("char-b")} == (
        {"turn-1"} if group else set()
    )
    assert not core.list_perceived_turns("char-a")


@pytest.mark.parametrize("delivery", [True, False])
def test_thought_roundtrip_is_private_and_receipt_rejection_writes_nothing(
    core: MemoryClient, delivery: bool
) -> None:
    core.add_session(
        SessionRequest.model_validate(
            session_payload() | {"session_id": "thought-session", "reports_delivery": delivery}
        )
    )
    core.add_turn(
        TurnRequest.model_validate(
            turn_payload()
            | {
                "session_id": "thought-session",
                "kind": "thought",
                "perceived_by": ["char-c"],
                "address_mode": "self",
            }
        )
    )
    assert [v.turn_id for v in core.list_perceived_turns("char-c")] == ["turn-1"]
    assert not core.list_perceived_turns("char-b")
    assert not core.list_perceived_turns("char-a")
    with pytest.raises(MemoryHTTPError) as failed:
        core.add_receipt("turn-1", ReceiptRequest.model_validate(receipt_payload("none", 0)))
    assert failed.value.status_code == 422
    assert not core.list_receipts("turn-1")


def test_false_session_rejects_receipt_without_appending(core: MemoryClient) -> None:
    core.add_turn(TurnRequest.model_validate(turn_payload()))
    with pytest.raises(MemoryHTTPError) as failed:
        core.add_receipt("turn-1", ReceiptRequest.model_validate(receipt_payload("partial", 3)))
    assert failed.value.status_code == 422
    assert not core.list_receipts("turn-1")
    assert core.get_turn("turn-1").text == TEXT


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_recall_returns_original_spans_and_committed_provision(
    core: MemoryClient, harness: "RecallHarness", form: str
) -> None:
    harness.seed(turn(), replace(memory(), content="合成の誤訳"))
    response = core.recall("char-b", RecallRequest.model_validate(recall_payload(form=form)))
    assert [(v.turn_id, v.text, v.actor, v.language) for v in response.source_spans] == [
        ("turn-1", TEXT, "char-c", "ja")
    ]
    for span in response.source_spans:
        assert span.text == TEXT[span.start : span.end]
    assert response.context == (TEXT if form == "assembled" else None)
    harness.assert_record_matches(response.model_dump(mode="json"))


def test_resident_then_turn_save_distinct_records(
    core: MemoryClient, harness: "RecallHarness"
) -> None:
    harness.next_id = "prov-resident-1"
    resident = core.resident("char-b", ResidentRequest(session_id="session-1"))
    harness.next_id = "prov-turn-1"
    recalled = core.recall("char-b", RecallRequest.model_validate(recall_payload()))
    assert (resident.provision_id, recalled.provision_id) == ("prov-resident-1", "prov-turn-1")
    assert set(harness.provisions.records) == {resident.provision_id, recalled.provision_id}
    for response, kind in ((resident, "resident"), (recalled, "turn")):
        record = harness.provisions.get(response.provision_id)
        assert record is not None
        assert record.kind == kind
        assert record.provided_at == harness.now
        assert record.temperament_version == response.temperament_version
        assert record.personality_version == response.personality_version
    harness.assert_record_matches(recalled.model_dump(mode="json"))


def test_turn_provision_collision_preserves_existing_resident(
    core: MemoryClient, harness: "RecallHarness"
) -> None:
    harness.next_id = "prov-existing"
    core.resident("char-b", ResidentRequest(session_id="session-1"))
    before = harness.provisions.records.copy()
    with pytest.raises(MemoryHTTPError) as failed:
        core.recall("char-b", RecallRequest.model_validate(recall_payload()))
    assert failed.value.status_code == 409
    assert harness.provisions.records == before


@pytest.mark.parametrize("failure", ["insert", "commit"])
def test_failed_provision_is_visible_and_explicit_retry_succeeds(
    core: MemoryClient, harness: "RecallHarness", failure: str
) -> None:
    harness.provisions.fail_at = failure
    with pytest.raises(MemoryHTTPError) as failed:
        core.recall("char-b", RecallRequest.model_validate(recall_payload()))
    assert failed.value.status_code == 503
    assert harness.provisions.records == {}
    harness.provisions.fail_at = None
    response = core.recall("char-b", RecallRequest.model_validate(recall_payload()))
    harness.assert_record_matches(response.model_dump(mode="json"))
    assert len(harness.provisions.records) == 1


def test_same_client_follows_timeout_then_receipt_corrections(
    core: MemoryClient, harness: "RecallHarness"
) -> None:
    harness.evidence.sessions["session-1"] = session(reports_delivery=True)
    harness.seed(turn(), memory())
    request = RecallRequest.model_validate(recall_payload())
    for seconds in (0, 599, 600):
        harness.now = RECORDED + timedelta(seconds=seconds)
        response = core.recall("char-b", request)
        assert [v.text for v in response.source_spans] == ([TEXT] if seconds == 600 else [])
    for status, end in (("partial", 3), ("none", 0)):
        core.add_receipt("turn-1", ReceiptRequest.model_validate(receipt_payload(status, end)))
        response = core.recall("char-b", request)
        assert not response.memories
        assert not response.source_spans
        assert core.get_turn("turn-1").text == TEXT


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 503])
@pytest.mark.parametrize(
    "detail", ["Synthetic failure", [{"loc": ["body", "x"], "msg": "invalid", "type": "missing"}]]
)
def test_http_error_keeps_status_and_detail_without_retry(status: int, detail: object) -> None:
    calls = []

    def transfer(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"detail": detail})

    with MemoryClient(
        base_url="http://memory.test", timeout=2.0, transport=httpx.MockTransport(transfer)
    ) as client:
        with pytest.raises(MemoryHTTPError) as failed:
            client.add_session(SessionRequest.model_validate(session_payload()))
    assert failed.value.status_code == status
    assert failed.value.detail == detail
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "connection"])
def test_transport_failures_are_distinct_and_not_retried(failure: str) -> None:
    calls = []

    def transfer(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.extensions["timeout"] == {
            "connect": 2.5,
            "read": 2.5,
            "write": 2.5,
            "pool": 2.5,
        }
        if failure == "timeout":
            raise httpx.ReadTimeout("Synthetic timeout", request=request)
        raise httpx.ConnectError("Synthetic connection failure", request=request)

    expected = MemoryTimeoutError if failure == "timeout" else MemoryTransportError
    with MemoryClient(
        base_url="http://memory.test", timeout=2.5, transport=httpx.MockTransport(transfer)
    ) as client:
        with pytest.raises(expected):
            client.add_session(SessionRequest.model_validate(session_payload()))
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [b"[", b"null", b"{}", b'{"session_id":"s"}'])
def test_invalid_success_response_does_not_become_empty_success(payload: bytes) -> None:
    with MemoryClient(
        base_url="http://memory.test",
        timeout=2.0,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload)),
    ) as client:
        with pytest.raises(MemoryResponseError):
            client.get_session("session-1")


@pytest.mark.parametrize("exceptional", [False, True])
def test_context_manager_closes_owned_transport_even_after_failure(exceptional: bool) -> None:
    class CallerFailure(Exception):
        pass

    class RecordingTransport(httpx.MockTransport):
        closed = False

        def close(self) -> None:
            self.closed = True
            super().close()

    transport = RecordingTransport(lambda _: httpx.Response(200, json=session_payload()))
    with pytest.raises(CallerFailure) if exceptional else nullcontext():
        with MemoryClient(
            base_url="http://memory.test", timeout=2.0, transport=transport
        ) as client:
            client.get_session("session-1")
            if exceptional:
                raise CallerFailure("Synthetic caller failure")
    assert transport.closed


def test_turn_wire_body_uses_register_alias_without_modifying_text() -> None:
    seen: list[tuple[str, str, dict[str, object]]] = []

    def transfer(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append((request.method, request.url.path, payload))
        return httpx.Response(201, json=payload | {"recorded_at": RECORDED.isoformat()})

    with MemoryClient(
        base_url="http://memory.test", timeout=2.0, transport=httpx.MockTransport(transfer)
    ) as client:
        client.add_turn(
            TurnRequest.model_validate(turn_payload() | {"reply_to_turn_id": "earlier"})
        )
    assert len(seen) == 1
    method, path, payload = seen[0]
    assert (method, path) == ("POST", "/turns")
    expected = turn_payload() | {"reply_to_turn_id": "earlier"}
    for field, value in expected.items():
        if field == "occurred_at":
            assert datetime.fromisoformat(str(payload[field])) == datetime.fromisoformat(str(value))
        else:
            assert payload[field] == value
    assert "speech_register" not in payload
    assert "audience" not in payload
    assert "participants" not in payload


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_client_requires_a_positive_finite_timeout(timeout: float) -> None:
    with pytest.raises(ValueError):
        with MemoryClient(base_url="http://memory.test", timeout=timeout):
            pytest.fail("Invalid timeout must be rejected before opening the client")


def test_interrupted_array_is_never_returned_as_partial_success() -> None:
    class InterruptedBody(httpx.SyncByteStream):
        closed = False

        def __iter__(self) -> Iterator[bytes]:
            yield b"["
            raise httpx.ReadError("Synthetic body interruption")

        def close(self) -> None:
            self.closed = True

    body = InterruptedBody()
    with MemoryClient(
        base_url="http://memory.test",
        timeout=2.0,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body)),
    ) as client:
        with pytest.raises(MemoryTransportError):
            client.list_perceived_turns("char-b")
    assert body.closed


def test_author_credential_is_sent_only_for_deletion_and_not_in_error_text() -> None:
    # Synthetic credentials are public fixture values, never real credentials.
    seen = []
    token = "synthetic-core-deletion-credential"

    def transfer(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/deletion-requests":
            assert json.loads(request.content)["user_id"] == "user-a"
            return httpx.Response(401, json={"detail": "Author required"})
        return httpx.Response(200, json=session_payload())

    with MemoryClient(
        base_url="http://memory.test",
        timeout=2.0,
        author_token=token,
        transport=httpx.MockTransport(transfer),
    ) as client:
        client.get_session("session-1")
        with pytest.raises(MemoryHTTPError) as failed:
            client.delete(DeletionRequest(request_id="delete-1", user_id="user-a"))
        client.get_session("session-1")
    assert seen == [
        ("GET", "/sessions/session-1", None),
        ("POST", "/deletion-requests", f"Bearer {token}"),
        ("GET", "/sessions/session-1", None),
    ]
    assert failed.value.status_code == 401
    assert token not in str(failed.value)
    assert token not in repr(failed.value)
