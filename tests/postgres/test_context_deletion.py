from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import DictRow

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_resident import PostgresContextStore
from digital_souls_memory.api import create_app
from digital_souls_memory.resident.domain import ContextKey
from tests.context_gateway_support import EpisodeGateway
from tests.evidence_support import RECORDED, turn
from tests.postgres.test_context_work_selection import context_worker
from tests.postgres.test_deletion import HEADERS
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_resident_state import register_open_session

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


@pytest.mark.parametrize("operation", ["state", "episodes"])
@pytest.mark.parametrize("phase", ["model", "save"])
def test_author_deletion_cannot_be_undone_by_context_completion(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    phase: str,
) -> None:
    store, gateway = PostgresContextStore(database_url), EpisodeGateway()
    worker = context_worker(store, gateway)
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    entered, release = Event(), Event()
    connection = store._connection

    @contextmanager
    def pause_before_save() -> Iterator[psycopg.Connection[DictRow]]:
        with connection() as conn:
            if not entered.is_set() and (gateway.calls or gateway.episode_calls):
                entered.set()
                assert release.wait(5)
            yield conn

    if phase == "save":
        monkeypatch.setattr(store, "_connection", pause_before_save)
    else:
        entered = gateway.started if operation == "state" else gateway.episode_started
        release = gateway.release if operation == "state" else gateway.episode_release
        release.clear()
    with TestClient(create_app(context_store=store, clock=lambda: RECORDED)) as client:
        register_open_session(client, reports_delivery=False)
        PostgresEvidenceStore(database_url).add_turn(turn(actor="user-a"))
        process = worker.process_state if operation == "state" else worker.process_episodes
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(process, key)
            try:
                assert entered.wait(5)
                response = client.post(
                    "/deletion-requests",
                    headers=HEADERS,
                    json={"request_id": "delete-context-input", "target_turn_ids": ["turn-1"]},
                )
                assert response.status_code == 200, response.json()
            finally:
                release.set()
            pending.result(timeout=5)
        assert store.get_state(key) is None and store.list_episodes(key) == ()
        process(key)
        assert store.get_state(key) is None and store.list_episodes(key) == ()
        work = store.get_work("state" if operation == "state" else "episodes", key)
        assert work is not None and work.status == "done"
        assert work.token is None and work.lease_until is None
        assert client.get("/turns/turn-1").status_code == 404
