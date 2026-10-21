from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from tests.context_gateway_support import StateGateway
from tests.evidence_support import RECORDED, TEXT, receipt_payload, session_payload, turn_payload
from tests.postgres.test_memory_delivery import eventually

pytestmark = pytest.mark.postgres
STATE = "/characters/char-b/sessions/session-1/state"
PARAMS = {"surface": "synthetic-chat"}


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)


def get_state(client: TestClient) -> dict[str, object] | None:
    response = client.get(STATE, params=PARAMS)
    assert response.is_success
    value = response.json()["state"]
    assert value is None or isinstance(value, dict)
    return value


def register_open_session(client: TestClient, *, reports_delivery: bool) -> None:
    assert client.post(
        "/characters/char-b/core-identities", json={"values": ["誠実"], "prohibitions": []}
    ).is_success
    assert client.post(
        "/characters/char-c/temperaments", json={"description": "合成気質"}
    ).is_success
    assert client.post(
        "/characters/char-z/core-identities", json={"values": ["誠実"], "prohibitions": []}
    ).is_success
    assert (
        client.post(
            "/sessions",
            json=session_payload() | {"ended_at": None, "reports_delivery": reports_delivery},
        ).status_code
        == 201
    )


@pytest.mark.parametrize("route", ["local", "cloud"])
def test_state_registration_returns_before_model_completion_and_isolates_keys(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    gateway = StateGateway()
    gateway.release.clear()
    labels = frozenset({"synthetic-public"})
    app = create_app(context_gateway=gateway, clock=lambda: RECORDED, non_sensitive_labels=labels)
    with TestClient(app) as client:
        try:
            register_open_session(client, reports_delivery=False)
            assert client.post(
                "/sessions",
                json=session_payload() | {"session_id": "next-session", "ended_at": None},
            ).is_success
            with ThreadPoolExecutor(max_workers=1) as pool:
                payload = turn_payload() | {
                    "sensitivity": "synthetic-public" if route == "cloud" else "synthetic-sensitive"
                }
                pending = pool.submit(client.post, "/turns", json=payload)
                try:
                    assert pending.result(timeout=2).status_code == 201
                    assert gateway.started.wait(2)
                    assert get_state(client) is None
                finally:
                    gateway.release.set()
            eventually(lambda: get_state(client) is not None)
            state = get_state(client)
            assert state is not None and state["current_topic"] == TEXT
            for path, params in (
                (STATE, {"surface": "another-surface"}),
                ("/characters/char-b/sessions/next-session/state", PARAMS),
                ("/characters/char-z/sessions/session-1/state", PARAMS),
            ):
                response = client.get(path, params=params)
                assert response.is_success and response.json()["state"] is None
            with gateway.lock:
                calls = tuple(gateway.calls)
            assert calls
            assert all(actual_route == route for actual_route, _ in calls)
            assert all(
                isinstance(value, CloudInput if route == "cloud" else LocalOnly)
                for _, value in calls
            )
            assert all(turn.language == "ja" for _, value in calls for turn in value.turns)
        finally:
            gateway.release.set()


@pytest.mark.parametrize("invalidation", ["expiry", "ending"])
def test_saved_state_expires_or_ends_without_recreating_the_application(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, invalidation: str
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    now = [RECORDED]
    gateway = StateGateway()
    app = create_app(context_gateway=gateway, clock=lambda: now[0], state_ttl=timedelta(seconds=30))
    with TestClient(app) as client:
        try:
            register_open_session(client, reports_delivery=False)
            assert client.post("/turns", json=turn_payload()).status_code == 201
            eventually(lambda: get_state(client) is not None)
            state = get_state(client)
            assert state is not None
            expires = datetime.fromisoformat(str(state["expires_at"]))
            updated = datetime.fromisoformat(str(state["updated_at"]))
            assert expires - updated == timedelta(seconds=30)
            if invalidation == "expiry":
                now[0] = expires - timedelta(microseconds=1)
                assert get_state(client) is not None
                now[0] = expires
            else:
                assert (
                    client.post(
                        "/sessions/session-1/endings",
                        json={"ended_at": now[0].isoformat(), "reported_by": "synthetic-window"},
                    ).status_code
                    == 201
                )
            assert get_state(client) is None
            assert get_state(client) is None
        finally:
            gateway.release.set()


def test_saved_state_is_withdrawn_before_the_corrected_model_finishes(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    gateway = StateGateway()
    now = [RECORDED]
    app = create_app(context_gateway=gateway, clock=lambda: now[0])
    with TestClient(app) as client:
        try:
            register_open_session(client, reports_delivery=True)
            assert client.post("/turns", json=turn_payload()).status_code == 201
            now[0] += timedelta(minutes=10)
            eventually(lambda: get_state(client) is not None)
            state = get_state(client)
            assert state is not None and state["current_topic"] == TEXT
            gateway.release.clear()
            assert client.post(
                "/turns/turn-1/receipts", json=receipt_payload("partial", 3)
            ).is_success
            assert get_state(client) is None
            gateway.release.set()
            eventually(lambda: get_state(client) is not None)
            state = get_state(client)
            assert state is not None and state["current_topic"] == TEXT[:3]
            assert client.post("/turns/turn-1/receipts", json=receipt_payload("none", 0)).is_success
            assert get_state(client) is None
            assert client.post(
                "/turns/turn-1/receipts", json=receipt_payload("full", len(TEXT))
            ).is_success

            def restored_full() -> bool:
                current = get_state(client)
                return current is not None and current["current_topic"] == TEXT

            eventually(restored_full)
        finally:
            gateway.release.set()
