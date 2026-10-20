import json
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from digital_souls_memory.api import create_app
from digital_souls_memory.identity.ports import IdentityUnavailable
from tests.evidence_support import (
    INSTRUCTION,
    OCCURRED,
    RECORDED,
    TEXT,
    receipt_payload,
    session_payload,
    turn_payload,
)
from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities

pytestmark = pytest.mark.it1


@pytest.fixture
def store() -> FakeEvidenceStore:
    return FakeEvidenceStore()


@pytest.fixture
def client(store: FakeEvidenceStore) -> Iterator[TestClient]:
    with TestClient(
        create_app(store=store, clock=lambda: RECORDED, identity_store=registered_identities())
    ) as result:
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
    listener = "char_b"
    payload = {
        **session_payload(),
        "session_id": identifier,
        "surface": "session/1",
    }
    assert client.post("/sessions", json=payload).status_code == 201
    assert client.get(f"/sessions/{identifier}").json()["surface"] == "session/1"
    payload = {
        **turn_payload(),
        "turn_id": "Turn_1",
        "session_id": identifier,
        "perceived_by": [listener],
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
    assert client.get("/characters/char_a/turns").json() == []
    values = client.get(f"/characters/{listener}/turns").json()
    assert [value["turn_id"] for value in values] == ["Turn_1"]
    assert values[0] == saved


@pytest.mark.parametrize(
    "path,field",
    [
        ("/sessions", "session_id"),
        ("/turns", "turn_id"),
        ("/turns", "session_id"),
        ("/turns", "perceived_by"),
        ("/turns", "addressees"),
    ],
)
@pytest.mark.parametrize("invalid", ["session/1", "", " ", "日本語", "a\n", "a?b", 1])
def test_invalid_http_identifier_is_rejected_before_storage(
    client: TestClient, store: FakeEvidenceStore, path: str, field: str, invalid: object
) -> None:
    register_turn(client)
    payload = session_payload() if path == "/sessions" else turn_payload()
    payload[field] = [invalid] if field in {"perceived_by", "addressees"} else invalid
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
    [("/sessions", field) for field in ("session_id", "started_at", "surface", "reports_delivery")]
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
            "perceived_by",
            "addressees",
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


def test_optional_session_end_and_reply(client: TestClient) -> None:
    payload = session_payload()
    del payload["ended_at"]
    assert client.post("/sessions", json=payload).status_code == 201
    payload = turn_payload()
    del payload["reply_to_turn_id"]
    assert client.post("/turns", json=payload).status_code == 201
    assert client.get("/sessions/session-1").json()["ended_at"] is None
    assert client.get("/turns/turn-1").json()["reply_to_turn_id"] is None


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
    payload = {
        **turn_payload(),
        "actor": actor,
        "kind": kind,
        "surface": surface,
        "perceived_by": ["char-a", "char-b"],
    }
    assert client.post("/turns", json=payload).is_success
    saved = client.get("/turns/turn-1").json()
    assert (saved["actor"], saved["kind"], saved["surface"]) == (actor, kind, surface)
    assert saved["text"] == TEXT


def test_each_turn_keeps_its_perception_after_reply_and_hearsay(client: TestClient) -> None:
    assert client.post("/sessions", json=session_payload()).status_code == 201
    for identifier, perceived, targets, reply, kind in [
        ("whisper", ["char-c"], ["viewer"], None, "utterance"),
        ("reply", ["char-b", "char-c"], ["char-a"], "whisper", "utterance"),
        ("hearsay", ["char-a", "char-c"], [], "whisper", "observation"),
    ]:
        assert (
            client.post(
                "/turns",
                json=turn_payload()
                | {
                    "turn_id": identifier,
                    "perceived_by": perceived,
                    "addressees": targets,
                    "address_mode": "direct" if targets else "broadcast",
                    "reply_to_turn_id": reply,
                    "kind": kind,
                },
            ).status_code
            == 201
        )
    for character, expected in [
        ("char-a", ["hearsay"]),
        ("char-b", ["reply"]),
        ("char-c", ["hearsay", "reply", "whisper"]),
        ("char-z", []),
    ]:
        assert [
            v["turn_id"] for v in client.get(f"/characters/{character}/turns").json()
        ] == expected
    assert client.get("/turns/whisper").json()["perceived_by"] == ["char-c"]


@pytest.mark.parametrize(
    "first_occurred,first_recorded,second_occurred,second_recorded,expected",
    [
        (1, 0, 0, 1, ["turn-2", "turn-1"]),
        (0, 1, 0, 0, ["turn-2", "turn-1"]),
        (0, 0, 0, 0, ["turn-1", "turn-2"]),
    ],
    ids=["occurred-time", "recorded-time", "identifier"],
)
def test_character_query_orders_turns_across_sessions_and_surfaces(
    store: FakeEvidenceStore,
    first_occurred: int,
    first_recorded: int,
    second_occurred: int,
    second_recorded: int,
    expected: list[str],
) -> None:
    times = iter(
        [
            RECORDED + timedelta(seconds=second_recorded),
            RECORDED + timedelta(seconds=first_recorded),
        ]
    )
    with TestClient(
        create_app(store=store, clock=lambda: next(times), identity_store=registered_identities())
    ) as client:
        for identifier, surface in [("session-1", "chat"), ("session-2", "diary")]:
            assert client.post(
                "/sessions",
                json={**session_payload(), "session_id": identifier, "surface": surface},
            ).is_success
        for identifier, context, surface, offset in [
            ("turn-2", "session-2", "diary", second_occurred),
            ("turn-1", "session-1", "chat", first_occurred),
        ]:
            assert client.post(
                "/turns",
                json={
                    **turn_payload(),
                    "turn_id": identifier,
                    "session_id": context,
                    "surface": surface,
                    "occurred_at": (OCCURRED + timedelta(seconds=offset)).isoformat(),
                },
            ).is_success
        response = client.get("/characters/char-b/turns")
        assert response.status_code == 200
        assert [value["turn_id"] for value in response.json()] == expected


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
    assert [value["turn_id"] for value in client.get("/characters/char-c/turns").json()] == [
        "turn-1"
    ]
    assert client.get("/characters/char-a/turns").json() == []


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


@pytest.mark.parametrize("value", [True, False])
def test_delivery_declaration_roundtrips(
    client: TestClient, store: FakeEvidenceStore, value: bool
) -> None:
    response = client.post("/sessions", json=session_payload() | {"reports_delivery": value})
    assert response.status_code == 201
    assert response.json()["reports_delivery"] is value
    assert client.get("/sessions/session-1").json()["reports_delivery"] is value
    assert store.sessions["session-1"].reports_delivery is value


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_invalid_delivery_declaration_does_not_write(
    client: TestClient, store: FakeEvidenceStore, value: object
) -> None:
    assert (
        client.post("/sessions", json=session_payload() | {"reports_delivery": value}).status_code
        == 422
    )
    assert store.writes == 0


@pytest.mark.parametrize("path,key", [("/sessions", "participants"), ("/turns", "audience")])
def test_top_level_old_fields_including_escaped_keys_are_rejected(
    client: TestClient, store: FakeEvidenceStore, path: str, key: str
) -> None:
    assert client.post("/sessions", json=session_payload()).status_code == 201
    payload = (
        (session_payload() | {"session_id": "other"}) if path == "/sessions" else turn_payload()
    )
    raw = (
        json.dumps(payload)[:-1]
        + ', "'
        + (r"\u0061udience" if key == "audience" else key)
        + '" : [] }'
    )
    before = store.writes
    assert (
        client.post(path, content=raw, headers={"Content-Type": "application/json"}).status_code
        == 422
    )
    assert store.writes == before


def test_old_field_words_in_text_and_register_are_preserved(client: TestClient) -> None:
    assert client.post("/sessions", json=session_payload()).status_code == 201
    payload = turn_payload() | {
        "text": '{"audience":[]}',
        "register": {"participants": "引用", "audience": "語句"},
    }
    assert client.post("/turns", json=payload).status_code == 201
    saved = client.get("/turns/turn-1").json()
    assert saved["text"] == payload["text"]
    assert saved["register"] == payload["register"]
    assert "audience" not in saved
    assert "participants" not in client.get("/sessions/session-1").json()


@pytest.mark.parametrize("field", ["perceived_by", "addressees"])
@pytest.mark.parametrize(
    "value", ["missing", None, "char-b", 3, {}, [3], [""], [" \t"], ["char-b", "char-b"]]
)
def test_invalid_required_arrays_do_not_write(
    client: TestClient, store: FakeEvidenceStore, field: str, value: object
) -> None:
    assert client.post("/sessions", json=session_payload()).status_code == 201
    payload = turn_payload()
    if value == "missing":
        del payload[field]
    else:
        payload[field] = value
    before = store.writes
    assert client.post("/turns", json=payload).status_code == 422
    assert store.writes == before


@pytest.mark.parametrize("actor", ["char-c", "viewer", "char-unregistered"])
@pytest.mark.parametrize("kind", ["utterance", "observation"])
@pytest.mark.parametrize(
    "mode",
    ["missing", None, "direct", "broadcast", "self", "unknown", "", "Broadcast", 0, False, [], {}],
)
@pytest.mark.parametrize("targets", [[], ["viewer", "char-a"]])
def test_actor_specific_address_mode_acceptance(
    client: TestClient,
    store: FakeEvidenceStore,
    actor: str,
    kind: str,
    mode: object,
    targets: list[str],
) -> None:
    assert client.post("/sessions", json=session_payload()).status_code == 201
    payload = turn_payload() | {"actor": actor, "kind": kind, "addressees": targets}
    if mode == "missing":
        del payload["address_mode"]
    else:
        payload["address_mode"] = mode
    before = store.writes
    resolved = "broadcast" if mode in ("missing", None) and actor != "char-c" else mode
    valid = (resolved == "direct" and bool(targets)) or (
        resolved in ("broadcast", "self") and not targets
    )
    response = client.post("/turns", json=payload)
    assert response.status_code == (201 if valid else 422)
    assert store.writes == before + int(valid)
    if valid:
        assert response.json()["address_mode"] == resolved
        assert response.json()["addressees"] == targets
        assert store.turns["turn-1"].address_mode == resolved
        assert client.get("/turns/turn-1").json() == response.json()


@pytest.mark.parametrize(
    "resource,payload",
    [
        ("core-identities", {"values": ["誠実"], "prohibitions": []}),
        ("temperaments", {"description": "合成気質"}),
    ],
)
@pytest.mark.parametrize("mode", ["missing", None])
def test_either_identity_registration_requires_character_mode(
    client: TestClient,
    store: FakeEvidenceStore,
    resource: str,
    payload: dict[str, object],
    mode: object,
) -> None:
    assert (
        client.post(
            f"/characters/viewer-looking/{resource}",
            json=payload,
        ).status_code
        == 201
    )
    assert client.post("/sessions", json=session_payload()).status_code == 201
    value = turn_payload() | {"actor": "viewer-looking", "perceived_by": ["viewer-looking"]}
    if mode == "missing":
        del value["address_mode"]
    else:
        value["address_mode"] = mode
    before = store.writes
    assert client.post("/turns", json=value).status_code == 422
    assert store.writes == before
    assert client.post("/turns", json=value | {"address_mode": "broadcast"}).status_code == 201


@pytest.mark.parametrize(
    "actor,perceived,kind",
    [
        ("viewer", ["viewer"], "utterance"),
        ("char-c", [], "utterance"),
        ("char-c", ["char-b"], "observation"),
        ("char-c", ["char-c", "unregistered"], "utterance"),
        ("viewer", [], "thought"),
        ("char-c", [], "thought"),
        ("char-c", ["char-b"], "thought"),
        ("char-c", ["char-c", "char-b"], "thought"),
        ("char-c", ["char-c"], "unknown"),
    ],
)
def test_registration_and_thought_constraints_reject_without_writing(
    client: TestClient, store: FakeEvidenceStore, actor: str, perceived: list[str], kind: str
) -> None:
    assert client.post("/sessions", json=session_payload()).status_code == 201
    before = store.writes
    assert (
        client.post(
            "/turns",
            json=turn_payload() | {"actor": actor, "kind": kind, "perceived_by": perceived},
        ).status_code
        == 422
    )
    assert store.writes == before


@pytest.mark.parametrize("mode", ["missing", None])
def test_user_thought_is_not_enabled_by_mode_default(
    client: TestClient, store: FakeEvidenceStore, mode: object
) -> None:
    assert client.post("/sessions", json=session_payload()).status_code == 201
    payload = turn_payload() | {"actor": "viewer", "kind": "thought", "perceived_by": []}
    if mode == "missing":
        del payload["address_mode"]
    else:
        payload["address_mode"] = mode
    before = store.writes
    assert client.post("/turns", json=payload).status_code == 422
    assert store.writes == before


@pytest.mark.parametrize(
    "actor,perceived,mode,kind",
    [
        ("char-c", ["char-c", "char-b"], "self", "utterance"),
        ("viewer", ["char-c", "char-b"], "self", "utterance"),
        ("viewer", [], "self", "utterance"),
        ("char-c", ["char-c"], "broadcast", "thought"),
    ],
)
def test_self_and_thought_have_explicit_perception(
    client: TestClient, actor: str, perceived: list[str], mode: str, kind: str
) -> None:
    assert client.post("/sessions", json=session_payload()).status_code == 201
    assert (
        client.post(
            "/turns",
            json=turn_payload()
            | {"actor": actor, "perceived_by": perceived, "address_mode": mode, "kind": kind},
        ).status_code
        == 201
    )
    for character in ("char-a", "char-b", "char-c", "char-z"):
        assert [v["turn_id"] for v in client.get(f"/characters/{character}/turns").json()] == (
            ["turn-1"] if character in perceived else []
        )


@pytest.mark.parametrize("reference", ["missing", ["turn-1", "turn-2"], "", 3, {}])
def test_invalid_reply_is_rejected_without_writing(
    client: TestClient, store: FakeEvidenceStore, reference: object
) -> None:
    register_turn(client)
    before = store.writes
    assert (
        client.post(
            "/turns", json=turn_payload() | {"turn_id": "reply", "reply_to_turn_id": reference}
        ).status_code
        == 422
    )
    assert store.writes == before


@pytest.mark.parametrize(
    "delivery,kind",
    [(False, "utterance"), (False, "observation"), (False, "thought"), (True, "thought")],
)
def test_ineligible_receipts_do_not_write_or_change_text(
    client: TestClient, store: FakeEvidenceStore, delivery: bool, kind: str
) -> None:
    assert (
        client.post(
            "/sessions", json=session_payload() | {"reports_delivery": delivery}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/turns", json=turn_payload() | {"kind": kind, "perceived_by": ["char-c"]}
        ).status_code
        == 201
    )
    before = store.writes
    assert client.post("/turns/turn-1/receipts", json=receipt_payload("none", 0)).status_code == 422
    assert store.writes == before
    assert client.get("/turns/turn-1/receipts").json() == []
    assert client.get("/turns/turn-1").json()["text"] == TEXT


def test_openapi_exposes_optional_input_but_resolved_response_mode(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    for name in ("SessionRequest", "SessionResponse"):
        assert "participants" not in schemas[name]["properties"]
        assert "reports_delivery" in schemas[name]["required"]
        assert schemas[name]["properties"]["reports_delivery"]["type"] == "boolean"
    request = schemas["TurnRequest"]
    response = schemas["TurnResponse"]
    assert "address_mode" not in request["required"]
    variants = request["properties"]["address_mode"]["anyOf"]
    assert any(v.get("type") == "null" for v in variants)
    assert any(set(v.get("enum", [])) == {"direct", "broadcast", "self"} for v in variants)
    assert "address_mode" in response["required"]
    assert response["properties"]["address_mode"]["type"] == "string"
    assert set(response["properties"]["address_mode"]["enum"]) == {"direct", "broadcast", "self"}
    for schema in (request, response):
        assert "audience" not in schema["properties"]
        assert {"perceived_by", "addressees"} <= set(schema["required"])
        assert set(schema["properties"]["kind"]["enum"]) == {"utterance", "observation", "thought"}


@pytest.mark.parametrize("targets", [["char-b"], ["viewer"], ["char-a", "viewer"]])
def test_direct_addressing_uses_only_declared_perception(
    client: TestClient, targets: list[str]
) -> None:
    assert client.post("/sessions", json=session_payload()).status_code == 201
    assert (
        client.post(
            "/turns", json=turn_payload() | {"addressees": targets, "address_mode": "direct"}
        ).status_code
        == 201
    )
    assert client.get("/characters/char-a/turns").json() == []
    assert client.get("/characters/char-z/turns").json() == []
    for character in ("char-b", "char-c"):
        assert [v["turn_id"] for v in client.get(f"/characters/{character}/turns").json()] == [
            "turn-1"
        ]


def test_identity_lookup_failure_is_not_treated_as_user_and_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = FakeEvidenceStore()
    identities = registered_identities()
    with TestClient(
        create_app(store=evidence, identity_store=identities, clock=lambda: RECORDED)
    ) as client:
        assert client.post("/sessions", json=session_payload()).status_code == 201
        payload = turn_payload() | {"actor": "viewer", "perceived_by": []}
        del payload["address_mode"]
        before = evidence.writes

        def unavailable(character_id: str, version: int | None = None) -> None:
            raise IdentityUnavailable()

        with monkeypatch.context() as patch:
            patch.setattr(identities, "get_core", unavailable)
            patch.setattr(identities, "get_temperament", unavailable)
            assert client.post("/turns", json=payload).status_code == 503
        assert evidence.writes == before
        response = client.post("/turns", json=payload)
        assert response.status_code == 201
        assert response.json()["address_mode"] == "broadcast"
