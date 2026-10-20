from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from digital_souls_memory.api import create_app
from tests.evidence_support import (
    INSTRUCTION,
    OCCURRED,
    RECORDED,
    TEXT,
    receipt_payload,
    session_payload,
    turn_payload,
)
from tests.it1.evidence_fakes import FakeEvidenceStore

pytestmark = pytest.mark.it1


@pytest.fixture
def store() -> FakeEvidenceStore:
    return FakeEvidenceStore()


@pytest.fixture
def client(store: FakeEvidenceStore) -> Iterator[TestClient]:
    with TestClient(create_app(store=store, clock=lambda: RECORDED)) as result:
        yield result


def register_turn(client: TestClient) -> None:
    assert client.post("/sessions", json=session_payload()).is_success
    assert client.post("/turns", json=turn_payload()).is_success


def assert_dto_error(response: Response, field: str) -> None:
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert isinstance(errors, list) and errors
    for error in errors:
        assert isinstance(error["loc"], list)
        assert error["loc"][:2] == ["body", field]
        assert isinstance(error["type"], str) and error["type"]
        assert isinstance(error["msg"], str) and error["msg"]


@pytest.mark.parametrize("identifier", ["Session_1", "a"])
def test_http_identifiers_support_all_reference_operations(
    client: TestClient, identifier: str
) -> None:
    participant, audience = "char_a", "char_b"
    payload = {
        **session_payload(),
        "session_id": identifier,
        "participants": [participant],
        "surface": "session/1",
    }
    assert client.post("/sessions", json=payload).status_code == 201
    assert client.get(f"/sessions/{identifier}").json()["surface"] == "session/1"
    payload = {
        **turn_payload(),
        "turn_id": "Turn_1",
        "session_id": identifier,
        "audience": [audience],
        "actor": "actor/1",
        "language": "ja/JP",
        "sensitivity": "label/1",
        "classifier_version": "classifier/1",
    }
    assert client.post("/turns", json=payload).status_code == 201
    saved = client.get("/turns/Turn_1").json()
    for field, expected in payload.items():
        if field != "occurred_at":
            assert saved[field] == expected
    report = {**receipt_payload("none", 0), "reported_by": "window/1"}
    assert client.post("/turns/Turn_1/receipts", json=report).status_code == 201
    assert client.get("/turns/Turn_1/receipts").json()[0]["reported_by"] == "window/1"
    for character in (participant, audience):
        values = client.get(f"/characters/{character}/turns").json()
        assert [value["turn_id"] for value in values] == ["Turn_1"]
        assert values[0] == saved


@pytest.mark.parametrize(
    "path,field",
    [
        ("/sessions", "session_id"),
        ("/sessions", "participants"),
        ("/turns", "turn_id"),
        ("/turns", "session_id"),
        ("/turns", "audience"),
    ],
)
@pytest.mark.parametrize("invalid", ["session/1", "", " ", "日本語", "a\n", "a?b", 1])
def test_invalid_http_identifier_is_rejected_before_storage(
    client: TestClient, store: FakeEvidenceStore, path: str, field: str, invalid: object
) -> None:
    register_turn(client)
    payload = session_payload() if path == "/sessions" else turn_payload()
    payload[field] = [invalid] if field in {"participants", "audience"} else invalid
    before = store.writes
    response = client.post(path, json=payload)
    assert_dto_error(response, field)
    assert store.writes == before


def test_session_and_turn_roundtrip_all_attributes(client: TestClient) -> None:
    register_turn(client)
    response = client.get("/sessions/session-1")
    assert response.status_code == 200
    saved_session = response.json()
    for field, expected in session_payload().items():
        if field in {"started_at", "ended_at"}:
            assert datetime.fromisoformat(saved_session[field]) == datetime.fromisoformat(
                str(expected)
            )
        else:
            assert saved_session[field] == expected
    response = client.get("/turns/turn-1")
    assert response.status_code == 200
    saved_turn = response.json()
    for field, expected in turn_payload().items():
        if field == "occurred_at":
            assert datetime.fromisoformat(saved_turn[field]) == OCCURRED
        else:
            assert saved_turn[field] == expected
    assert datetime.fromisoformat(saved_turn["recorded_at"]) == RECORDED
    assert saved_turn["occurred_at"] != saved_turn["recorded_at"]


@pytest.mark.parametrize("field", ["sensitivity", "classifier_version"])
@pytest.mark.parametrize("invalid", ["missing", None, "", " \t\n", 1, False, [], {}])
def test_invalid_label_returns_422_without_writing(
    client: TestClient, store: FakeEvidenceStore, field: str, invalid: object
) -> None:
    assert client.post("/sessions", json=session_payload()).is_success
    before = store.writes
    payload = turn_payload()
    if invalid == "missing":
        del payload[field]
    else:
        payload[field] = invalid
    assert_dto_error(client.post("/turns", json=payload), field)
    assert store.writes == before
    assert store.turns == {}


@pytest.mark.parametrize(
    "path,field",
    [("/sessions", field) for field in ("session_id", "started_at", "surface", "participants")]
    + [
        ("/turns", field)
        for field in (
            "turn_id",
            "session_id",
            "surface",
            "actor",
            "kind",
            "text",
            "language",
            "register",
            "occurred_at",
        )
    ]
    + [
        ("/turns/turn-1/receipts", field)
        for field in (
            "status",
            "spoken_until",
            "reported_by",
            "reported_at",
        )
    ],
)
def test_required_field_missing_does_not_write(
    client: TestClient, store: FakeEvidenceStore, path: str, field: str
) -> None:
    register_turn(client)
    payload = (
        session_payload()
        if path == "/sessions"
        else turn_payload()
        if path == "/turns"
        else receipt_payload("none", 0)
    )
    del payload[field]
    before = store.writes
    assert_dto_error(client.post(path, json=payload), field)
    assert store.writes == before


def test_optional_session_end_and_turn_audience(client: TestClient) -> None:
    payload = session_payload()
    del payload["ended_at"]
    assert client.post("/sessions", json=payload).is_success
    payload = turn_payload()
    del payload["audience"]
    assert client.post("/turns", json=payload).is_success
    assert client.get("/sessions/session-1").json()["ended_at"] is None
    assert client.get("/turns/turn-1").json()["audience"] == []


@pytest.mark.parametrize(
    "actor,kind,surface",
    [
        ("char-a", "utterance", "chat"),
        ("char-b", "utterance", "ai-chat"),
        ("synthetic-viewer", "utterance", "stream"),
        ("char-a", "observation", "diary"),
    ],
)
def test_all_utterances_and_observations_are_registered(
    client: TestClient, actor: str, kind: str, surface: str
) -> None:
    assert client.post("/sessions", json=session_payload()).is_success
    payload = {**turn_payload(), "actor": actor, "kind": kind, "surface": surface}
    assert client.post("/turns", json=payload).is_success
    saved = client.get("/turns/turn-1").json()
    assert (saved["actor"], saved["kind"], saved["surface"]) == (actor, kind, surface)
    assert saved["text"] == TEXT


def test_character_query_excludes_actor_only_and_unrelated_turns(client: TestClient) -> None:
    register_turn(client)
    assert client.post(
        "/sessions", json={**session_payload(), "session_id": "private", "participants": ["char-z"]}
    ).is_success
    assert client.post(
        "/turns",
        json={**turn_payload(), "turn_id": "private", "session_id": "private", "audience": []},
    ).is_success
    for character, expected in [
        ("char-a", {"turn-1"}),
        ("char-b", {"turn-1"}),
        ("char-c", set()),
        ("nobody", set()),
    ]:
        response = client.get(f"/characters/{character}/turns")
        assert response.status_code == 200
        assert {value["turn_id"] for value in response.json()} == expected


@pytest.mark.parametrize(
    "participants,audience,actor,expected",
    [
        (["char-a"], [], "char-c", True),
        (["char-z"], [], "char-c", False),
        (["char-z"], ["char-a"], "char-c", True),
        (["char-z"], [], "char-a", False),
    ],
)
def test_perception_sources_are_independent_in_http(
    client: TestClient, participants: list[str], audience: list[str], actor: str, expected: bool
) -> None:
    assert client.post(
        "/sessions", json={**session_payload(), "participants": participants}
    ).is_success
    assert client.post(
        "/turns", json={**turn_payload(), "audience": audience, "actor": actor}
    ).is_success
    assert [value["turn_id"] for value in client.get("/characters/char-a/turns").json()] == (
        ["turn-1"] if expected else []
    )


def test_receipts_append_without_changing_generated_text(client: TestClient) -> None:
    register_turn(client)
    assert client.get("/turns/turn-1/receipts").json() == []
    expected = []
    for index, (status, end) in enumerate(
        [("none", 0), ("partial", 1), ("full", len(TEXT)), ("partial", 2)]
    ):
        reported_at = RECORDED - timedelta(seconds=index)
        payload = {**receipt_payload(status, end), "reported_at": reported_at.isoformat()}
        assert client.post("/turns/turn-1/receipts", json=payload).is_success
        expected.append((status, end, "synthetic-window", reported_at))
        response = client.get("/turns/turn-1/receipts")
        assert response.status_code == 200
        assert [
            (
                r["status"],
                r["spoken_until"],
                r["reported_by"],
                datetime.fromisoformat(r["reported_at"]),
            )
            for r in response.json()
        ] == expected
        assert client.get("/turns/turn-1").json()["text"] == TEXT


@pytest.mark.parametrize(
    "status,end",
    [
        ("full", len(TEXT) - 1),
        ("partial", 0),
        ("partial", len(TEXT)),
        ("none", 1),
        ("partial", -1),
        ("full", len(TEXT) + 1),
        ("full", len(TEXT.encode())),
        ("partial", True),
        ("partial", 1.0),
        ("partial", "1"),
        ("partial", None),
        ("unknown", 0),
    ],
)
def test_invalid_receipt_returns_422_without_writing(
    client: TestClient, store: FakeEvidenceStore, status: str, end: object
) -> None:
    register_turn(client)
    assert client.post("/turns/turn-1/receipts", json=receipt_payload("none", 0)).is_success
    before = store.writes
    response = client.post("/turns/turn-1/receipts", json=receipt_payload(status, end))
    assert response.status_code == 422
    if type(end) is not int or end < 0 or status == "unknown":
        assert_dto_error(response, "status" if status == "unknown" else "spoken_until")
    else:
        detail = response.json()["detail"]
        assert isinstance(detail, str) and detail
    assert store.writes == before
    values = client.get("/turns/turn-1/receipts").json()
    assert [(value["status"], value["spoken_until"]) for value in values] == [("none", 0)]
    assert client.get("/turns/turn-1").json()["text"] == TEXT


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/sessions/session-1", "/turns/turn-1", "/turns/turn-1/receipts"])
def test_normal_update_and_delete_are_rejected(
    client: TestClient, store: FakeEvidenceStore, method: str, path: str
) -> None:
    register_turn(client)
    assert client.post("/turns/turn-1/receipts", json=receipt_payload("none", 0)).is_success
    before = store.writes
    assert client.request(method, path, json={"text": "changed"}).status_code == 405
    assert store.writes == before
    assert client.get("/turns/turn-1").json()["text"] == TEXT
    assert len(client.get("/turns/turn-1/receipts").json()) == 1


@pytest.mark.parametrize("path", ["/sessions", "/turns"])
def test_duplicate_identifier_is_not_an_upsert(client: TestClient, path: str) -> None:
    register_turn(client)
    payload = session_payload() if path == "/sessions" else turn_payload()
    payload["surface"] = "changed"
    assert client.post(path, json=payload).status_code == 409
    identifier = "session-1" if path == "/sessions" else "turn-1"
    assert client.get(f"{path}/{identifier}").json()["surface"] == "synthetic-chat"


def test_missing_references_do_not_write_and_later_valid_request_succeeds(
    client: TestClient, store: FakeEvidenceStore
) -> None:
    assert client.post("/turns", json=turn_payload()).status_code == 404
    assert client.post("/turns/turn-1/receipts", json=receipt_payload("none", 0)).status_code == 404
    assert store.writes == 0
    register_turn(client)


def test_instruction_text_remains_data(client: TestClient) -> None:
    assert client.post("/sessions", json=session_payload()).is_success
    payload = {**turn_payload(), "text": INSTRUCTION}
    assert client.post("/turns", json=payload).is_success
    saved = client.get("/turns/turn-1").json()
    for field in ("text", "actor", "sensitivity", "classifier_version"):
        assert saved[field] == payload[field]
    assert client.get("/characters/char-c/turns").json() == []


def test_invalid_labels_are_validated_before_default_storage_is_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMORY_DATABASE_URL", raising=False)
    with TestClient(create_app()) as default_client:
        assert default_client.post("/turns", json={}).status_code == 422
        assert default_client.post("/sessions", json=session_payload()).status_code == 503


@pytest.mark.parametrize("path", ["/sessions/missing", "/turns/missing", "/turns/missing/receipts"])
def test_missing_evidence_returns_404(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_invalid_database_configuration_does_not_become_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", "invalid://unreachable")
    with TestClient(create_app()) as default_client:
        assert default_client.get("/characters/char-a/turns").status_code == 503
        assert default_client.get("/health").status_code == 200
