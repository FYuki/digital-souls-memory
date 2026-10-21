from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from tests.evidence_support import RECORDED, session_payload
from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities

pytestmark = pytest.mark.it1
PATH = "/sessions/session-1/endings"
ENDING = {"ended_at": RECORDED.isoformat(), "reported_by": "synthetic-window"}


@pytest.fixture
def ending_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, FakeEvidenceStore, list[datetime]]]:
    for name in ("MEMORY_DATABASE_URL", "MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    store = FakeEvidenceStore()
    now = [RECORDED]
    with TestClient(
        create_app(store=store, identity_store=registered_identities(), clock=lambda: now[0])
    ) as client:
        assert (
            client.post("/sessions", json=session_payload() | {"ended_at": None}).status_code == 201
        )
        yield client, store, now


def test_ending_http_appends_once_composes_session_and_retains_original_report_time(
    ending_client: tuple[TestClient, FakeEvidenceStore, list[datetime]],
) -> None:
    client, store, now = ending_client
    original = store.sessions["session-1"]
    assert client.get(PATH).json() == []
    first = client.post(PATH, json=ENDING)
    assert first.status_code == 201
    assert first.json()["session_id"] == "session-1"
    assert first.json()["reported_by"] == "synthetic-window"
    assert datetime.fromisoformat(first.json()["reported_at"]) == RECORDED
    now[0] += timedelta(days=1)
    retry = client.post(PATH, json=ENDING)
    assert retry.status_code == 200 and retry.json() == first.json()
    assert client.get(PATH).json() == [first.json()]
    assert datetime.fromisoformat(client.get("/sessions/session-1").json()["ended_at"]) == RECORDED
    assert store.sessions["session-1"] == original
    assert store.writes == 2


@pytest.mark.parametrize(
    "change",
    [
        {"reported_by": "another-window"},
        {"ended_at": (RECORDED + timedelta(seconds=1)).isoformat()},
    ],
)
def test_ending_http_conflict_preserves_the_first_report(
    ending_client: tuple[TestClient, FakeEvidenceStore, list[datetime]],
    change: dict[str, str],
) -> None:
    client, store, _ = ending_client
    first = client.post(PATH, json=ENDING)
    assert first.status_code == 201
    writes = store.writes
    assert client.post(PATH, json=ENDING | change).status_code == 409
    assert client.get(PATH).json() == [first.json()]
    assert store.writes == writes


@pytest.mark.parametrize(
    "change",
    [
        {"ended_at": "2026-01-02T11:00:00"},
        {"ended_at": None},
        {"reported_by": " "},
        {"reported_at": RECORDED.isoformat()},
    ],
)
def test_ending_http_invalid_input_leaves_session_open_and_valid_retry_succeeds(
    ending_client: tuple[TestClient, FakeEvidenceStore, list[datetime]],
    change: dict[str, object],
) -> None:
    client, store, _ = ending_client
    writes = store.writes
    assert client.post(PATH, json=ENDING | change).status_code == 422
    assert store.writes == writes
    assert client.get(PATH).json() == []
    assert client.get("/sessions/session-1").json()["ended_at"] is None
    assert client.post(PATH, json=ENDING).status_code == 201


def test_ending_http_missing_session_and_preexisting_end_follow_defined_conflict_policy(
    ending_client: tuple[TestClient, FakeEvidenceStore, list[datetime]],
) -> None:
    client, store, _ = ending_client
    assert client.post("/sessions/missing/endings", json=ENDING).status_code == 404
    assert client.get("/sessions/missing/endings").status_code == 404
    assert not store.endings
    assert (
        client.post("/sessions", json=session_payload() | {"session_id": "closed"}).status_code
        == 201
    )
    assert (
        client.post(
            "/sessions/closed/endings",
            json=ENDING | {"ended_at": (RECORDED + timedelta(seconds=1)).isoformat()},
        ).status_code
        == 409
    )
    assert client.get("/sessions/closed/endings").json() == []
    assert datetime.fromisoformat(client.get("/sessions/closed").json()["ended_at"]) == RECORDED
