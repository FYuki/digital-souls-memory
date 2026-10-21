from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from threading import Event
from typing import TYPE_CHECKING, Literal

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow

from digital_souls_memory.api import create_app
from tests.context_gateway_support import EpisodeGateway
from tests.evidence_support import RECORDED, TEXT, receipt_payload, turn_payload
from tests.postgres.test_resident_state import register_open_session

if TYPE_CHECKING:
    from digital_souls_memory.episodes.domain import Episode
    from digital_souls_memory.resident.domain import CharacterState, ContextClaim, ContextSnapshot

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("operation", ["state", "episodes"])
@pytest.mark.parametrize("change", ["receipt", "ending"])
def test_context_completion_rechecks_input_inside_the_save_transaction(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    operation: Literal["state", "episodes"],
    change: str,
) -> None:
    from digital_souls_memory.adapters.postgres_resident import PostgresContextStore
    from digital_souls_memory.resident.domain import ContextKey
    from digital_souls_memory.resident.worker import ContextWorker

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    for name in ("MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    now = RECORDED + timedelta(minutes=10)
    store = PostgresContextStore(app_database_url)
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    entered, release = Event(), Event()
    original_connection = store._connection
    original_state = store.complete_state
    original_episodes = store.complete_episodes
    armed = False

    @contextmanager
    def paused_connection() -> Iterator[psycopg.Connection[DictRow]]:
        nonlocal armed
        with original_connection() as connection:
            if armed:
                armed = False
                connection.execute("SELECT 1")
                assert connection.info.transaction_status == TransactionStatus.INTRANS
                entered.set()
                assert release.wait(5), "Test did not release the context save transaction"
            yield connection

    def complete_state(
        claim: "ContextClaim",
        value: "CharacterState | None",
        *,
        snapshot: "ContextSnapshot",
        now: datetime,
    ) -> bool:
        nonlocal armed
        armed = True
        return original_state(claim, value, snapshot=snapshot, now=now)

    # Arm only on completion: claim/begin and model execution must finish first.
    def complete_episodes(
        claim: "ContextClaim",
        values: "tuple[Episode, ...]",
        *,
        snapshot: "ContextSnapshot",
        now: datetime,
    ) -> bool:
        nonlocal armed
        armed = True
        return original_episodes(claim, values, snapshot=snapshot, now=now)

    monkeypatch.setattr(store, "_connection", paused_connection)
    monkeypatch.setattr(store, "complete_state", complete_state)
    monkeypatch.setattr(store, "complete_episodes", complete_episodes)
    gateway = EpisodeGateway()
    ids = iter(("episode-1", "episode-2"))
    worker = ContextWorker(
        store=store,
        gateway=gateway,
        clock=lambda: now,
        non_sensitive_labels=frozenset(),
        state_ttl=timedelta(minutes=30),
        episode_id_factory=lambda: next(ids),
    )
    with TestClient(create_app(clock=lambda: now, context_store=store)) as client:
        register_open_session(client, reports_delivery=True)
        assert client.post("/turns", json=turn_payload()).status_code == 201
        assert client.post(
            "/turns/turn-1/receipts", json=receipt_payload("full", len(TEXT))
        ).is_success
        snapshot = store.load_window(key, now=now).snapshot
        assert snapshot.dependencies
        process = worker.process_state if operation == "state" else worker.process_episodes
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(process, key)
            try:
                assert entered.wait(5)
                if change == "receipt":
                    assert client.post(
                        "/turns/turn-1/receipts", json=receipt_payload("none", 0)
                    ).is_success
                else:
                    assert (
                        client.post(
                            "/sessions/session-1/endings",
                            json={
                                "ended_at": now.isoformat(),
                                "reported_by": "synthetic-window",
                            },
                        ).status_code
                        == 201
                    )
            finally:
                release.set()
            pending.result(timeout=5)
        assert store.get_state(key) is None
        assert store.list_episodes(key) == ()
        work = store.get_work(operation, key)
        assert work is not None and work.status == "retry"
        assert work.completed_signature != snapshot.signature
        assert work.token is None and work.lease_until is None
        assert client.get("/turns/turn-1").json()["text"] == TEXT
        # Retry the same store against the corrected evidence, after removing the test pause.
        monkeypatch.setattr(store, "complete_state", original_state)
        monkeypatch.setattr(store, "complete_episodes", original_episodes)
        if work.retry_at is not None:
            now = work.retry_at
        current = store.load_window(key, now=now).snapshot
        process(key)
        completed = store.get_work(operation, key)
        assert completed is not None and completed.status == "done"
        assert completed.completed_signature == current.signature
        assert store.get_state(key) is None
        if change == "receipt" or operation == "state":
            assert store.list_episodes(key) == ()
        else:
            assert [value.topic for value in store.list_episodes(key)] == [TEXT]


def test_episode_database_id_collision_preserves_old_batch_and_completion_signature(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.adapters.postgres_resident import PostgresContextStore
    from digital_souls_memory.resident.domain import ContextKey
    from digital_souls_memory.resident.worker import ContextWorker

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    for name in ("MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    store, gateway = PostgresContextStore(app_database_url), EpisodeGateway()
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    ids = iter(("episode-old", "episode-new", "episode-old"))
    worker = ContextWorker(
        store=store,
        gateway=gateway,
        clock=lambda: RECORDED,
        non_sensitive_labels=frozenset(),
        state_ttl=timedelta(minutes=30),
        episode_id_factory=lambda: next(ids),
    )
    with TestClient(create_app(clock=lambda: RECORDED, context_store=store)) as client:
        register_open_session(client, reports_delivery=False)
        assert client.post("/turns", json=turn_payload()).status_code == 201
        worker.process_episodes(key)
        before = store.list_episodes(key)
        completed = store.get_work("episodes", key)
        assert len(before) == 1 and completed is not None and completed.status == "done"
        assert client.post("/turns", json=turn_payload() | {"turn_id": "turn-2"}).is_success
        worker.process_episodes(key)
        assert store.list_episodes(key) == before
        work = store.get_work("episodes", key)
        assert work is not None and (work.status, work.error) == ("failed", "invalid_output")
        assert work.completed_signature == completed.completed_signature
        assert work.token is None and work.lease_until is None
    restored = PostgresContextStore(app_database_url)
    assert restored.list_episodes(key) == before
    assert restored.get_work("episodes", key) == work
