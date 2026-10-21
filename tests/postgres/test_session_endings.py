from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import psycopg
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from tests.evidence_support import RECORDED, session_payload

pytestmark = pytest.mark.postgres
PATH = "/sessions/session-1/endings"
ENDING = {"ended_at": RECORDED.isoformat(), "reported_by": "synthetic-window"}


@pytest.fixture
def client(app_database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    monkeypatch.delenv("MEMORY_EMBEDDING_ENDPOINT", raising=False)
    monkeypatch.delenv("MEMORY_RERANKER_ENDPOINT", raising=False)
    with TestClient(create_app(clock=lambda: RECORDED)) as result:
        response = result.post("/sessions", json=session_payload() | {"ended_at": None})
        assert response.status_code == 201
        yield result


def test_ending_is_appended_and_restored_without_updating_the_session_row(
    client: TestClient, app_database_url: str
) -> None:
    with psycopg.connect(app_database_url, connect_timeout=5) as connection:
        before = connection.execute(
            "SELECT * FROM sessions WHERE session_id='session-1'"
        ).fetchone()
    response = client.post(PATH, json=ENDING)
    assert response.status_code == 201
    saved = response.json()
    assert saved["session_id"] == "session-1"
    assert datetime.fromisoformat(saved["ended_at"]) == RECORDED
    assert saved["reported_by"] == "synthetic-window"
    assert datetime.fromisoformat(saved["reported_at"]) == RECORDED
    with psycopg.connect(app_database_url, connect_timeout=5) as connection:
        assert (
            connection.execute("SELECT * FROM sessions WHERE session_id='session-1'").fetchone()
            == before
        )
        assert connection.execute(
            "SELECT session_id,ended_at,reported_by,reported_at FROM session_endings "
            "WHERE session_id='session-1'"
        ).fetchall() == [("session-1", RECORDED, "synthetic-window", RECORDED)]
    with TestClient(create_app(clock=lambda: RECORDED + timedelta(days=1))) as restored:
        assert restored.get(PATH).json() == [saved]
        session = restored.get("/sessions/session-1")
        assert session.is_success
        assert datetime.fromisoformat(session.json()["ended_at"]) == RECORDED


def test_identical_ending_retry_keeps_the_original_report_time_and_one_record(
    client: TestClient,
) -> None:
    first = client.post(PATH, json=ENDING)
    assert first.status_code == 201
    with TestClient(create_app(clock=lambda: RECORDED + timedelta(days=1))) as later:
        retry = later.post(PATH, json=ENDING)
        assert retry.status_code == 200
        assert retry.json() == first.json()
        assert later.get(PATH).json() == [first.json()]


@pytest.mark.parametrize(
    "change",
    [
        {"ended_at": (RECORDED + timedelta(seconds=1)).isoformat()},
        {"reported_by": "different-window"},
    ],
)
def test_conflicting_ending_does_not_replace_a_previous_report(
    client: TestClient, change: dict[str, str]
) -> None:
    first = client.post(PATH, json=ENDING)
    assert first.status_code == 201
    assert client.post(PATH, json=ENDING | change).status_code == 409
    assert client.get(PATH).json() == [first.json()]
    assert datetime.fromisoformat(client.get("/sessions/session-1").json()["ended_at"]) == RECORDED


def test_naive_ending_does_not_write_and_valid_retry_succeeds(
    client: TestClient,
) -> None:
    assert (
        client.post(
            PATH, json=ENDING | {"ended_at": RECORDED.replace(tzinfo=None).isoformat()}
        ).status_code
        == 422
    )
    assert client.get(PATH).json() == []
    assert client.get("/sessions/session-1").json()["ended_at"] is None
    assert client.post(PATH, json=ENDING).status_code == 201


def test_registered_session_end_is_preserved_when_a_report_conflicts(client: TestClient) -> None:
    assert (
        client.post(
            "/sessions", json=session_payload() | {"session_id": "already-ended"}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/sessions/already-ended/endings",
            json=ENDING | {"ended_at": (RECORDED + timedelta(seconds=1)).isoformat()},
        ).status_code
        == 409
    )
    assert (
        datetime.fromisoformat(client.get("/sessions/already-ended").json()["ended_at"]) == RECORDED
    )
    assert client.get("/sessions/already-ended/endings").json() == []


@pytest.mark.parametrize("conflicting", [False, True])
def test_concurrent_first_ending_reports_follow_the_same_duplicate_policy(
    client: TestClient, conflicting: bool
) -> None:
    start = Barrier(2, timeout=5)

    def report(payload: dict[str, str]) -> tuple[int, object]:
        with TestClient(create_app(clock=lambda: RECORDED)) as reporter:
            start.wait()
            response = reporter.post(PATH, json=payload)
            return response.status_code, response.json()

    other = ENDING | ({"reported_by": "another-window"} if conflicting else {})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(report, ENDING)
        second = pool.submit(report, other)
        results = [first.result(timeout=10), second.result(timeout=10)]
    assert sorted(status for status, _ in results) == ([201, 409] if conflicting else [200, 201])
    saved = client.get(PATH)
    assert saved.is_success
    assert saved.json() == [next(body for status, body in results if status == 201)]
    if not conflicting:
        assert results[0][1] == results[1][1]
