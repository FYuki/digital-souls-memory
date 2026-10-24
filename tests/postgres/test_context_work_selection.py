from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.abc import Params, Query
from psycopg.rows import DictRow

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_resident import PostgresContextStore
from digital_souls_memory.api import create_app
from digital_souls_memory.episodes.domain import EpisodePart, EpisodeProposal, TurnSpan
from digital_souls_memory.resident.domain import ContextKey
from digital_souls_memory.resident.worker import ContextWorker
from tests.context_gateway_support import EpisodeGateway
from tests.evidence_support import RECORDED, receipt_payload, turn
from tests.postgres.test_memory_continuous_ingestion import LockMeasurements
from tests.postgres.test_resident_state import register_open_session

pytestmark = pytest.mark.postgres


class TurnReads:
    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.queries: list[str] = []
        self.rows = 0
        execute = cast(Callable[..., psycopg.Cursor[DictRow]], psycopg.Cursor.execute)

        def observe(
            cursor: psycopg.Cursor[DictRow],
            query: Query,
            params: Params | None = None,
            *,
            prepare: bool | None = None,
            binary: bool | None = None,
        ) -> psycopg.Cursor[DictRow]:
            result = execute(cursor, query, params, prepare=prepare, binary=binary)
            if isinstance(query, sql.Composable):
                rendered = query.as_string(cursor.connection)
            elif isinstance(query, bytes):
                rendered = query.decode()
            else:
                rendered = str(query)
            if "select" in rendered.lower() and "from turns" in rendered.lower():
                self.queries.append(rendered)
                self.rows += max(0, cursor.rowcount)
            return result

        patch.setattr(psycopg.Cursor, "execute", observe)


def context_worker(store: PostgresContextStore, gateway: EpisodeGateway) -> ContextWorker:
    return ContextWorker(
        store=store,
        gateway=gateway,
        clock=lambda: RECORDED,
        non_sensitive_labels=frozenset(),
        state_ttl=timedelta(minutes=30),
        episode_id_factory=iter(f"episode-{i}" for i in range(1000)).__next__,
    )


@pytest.mark.parametrize("ended", [False, True])
@pytest.mark.parametrize("entry", ["scan", "direct"])
def test_unchanged_completed_context_does_not_read_turns(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    ended: bool,
    entry: str,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    store, gateway = PostgresContextStore(app_database_url), EpisodeGateway()
    worker = context_worker(store, gateway)
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    with TestClient(create_app(context_store=store, clock=lambda: RECORDED)) as client:
        register_open_session(client, reports_delivery=False)
        evidence = PostgresEvidenceStore(app_database_url)
        for number in range(8):
            evidence.add_turn(turn(f"turn-{number}", actor="user-a", perceived_by=("char-b",)))
        if ended:
            assert (
                client.post(
                    "/sessions/session-1/endings",
                    json={
                        "ended_at": RECORDED.isoformat(),
                        "reported_by": "synthetic-window",
                    },
                ).status_code
                == 201
            )
        worker.process_state(key)
        worker.process_episodes(key)
        before = store.get_state(key), store.list_episodes(key)
        for operation in ("state", "episodes"):
            state = store.get_work(operation, key)
            assert state is not None and state.status == "done"
        calls = len(gateway.calls), len(gateway.episode_calls)
        observed_at = RECORDED + timedelta(hours=1)
        worker.clock = lambda: observed_at
        with monkeypatch.context() as patch:
            reads = TurnReads(patch)
            for _ in range(3):
                if entry == "direct":
                    worker.process_state(key)
                    worker.process_episodes(key)
                else:
                    after = None
                    while True:
                        page = store.work_page(after=after, now=observed_at, limit=2)
                        for item in page.items:
                            process = (
                                worker.process_state
                                if item.operation == "state"
                                else worker.process_episodes
                            )
                            process(item.key)
                        after = page.next_after
                        if after is None:
                            break
            assert reads.queries == [], reads.queries
            assert reads.rows == 0
        assert (store.get_state(key), store.list_episodes(key)) == before
        assert (len(gateway.calls), len(gateway.episode_calls)) == calls
        # A notification must still make the same worker process new input.
        evidence.add_turn(turn("new-turn", actor="user-a", perceived_by=("char-b",)))
        worker.process_state(key)
        worker.process_episodes(key)
        assert len(store.list_episodes(key)) == 9


def test_context_history_is_loaded_outside_shared_lock(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    store, gateway = PostgresContextStore(app_database_url), EpisodeGateway()
    # Keep output size fixed so measurements isolate reads of the growing history.
    gateway.proposal = EpisodeProposal(
        model="synthetic-model",
        prompt_version="synthetic-v1",
        episodes=(
            EpisodePart(
                topic="固定した合成の要約",
                turn_span=(TurnSpan(turn_id="history-0", start=0, end=1),),
            ),
        ),
    )
    worker = context_worker(store, gateway)
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    measurements = []
    with TestClient(create_app(context_store=store, clock=lambda: RECORDED)) as client:
        register_open_session(client, reports_delivery=False)
        evidence = PostgresEvidenceStore(app_database_url)
        seeded = 0
        for count in (1, 24):
            for number in range(seeded, count):
                evidence.add_turn(
                    turn(f"history-{number}", actor="user-a", perceived_by=("char-b",))
                )
            seeded = count
            with monkeypatch.context() as patch:
                measured = LockMeasurements(patch)
                worker.process_state(key)
                worker.process_episodes(key)
            assert measured.hold_seconds and not measured.acquired
            measurements.append(measured)
            record_property(
                str(count),
                {
                    "rows_under_lock": measured.rows,
                    "queries_under_lock": measured.queries,
                    "gate_hold_seconds": measured.hold_seconds,
                    "gate_wait_seconds": measured.wait_seconds,
                },
            )
            state = store.get_state(key)
            assert state is not None and len(state.dependencies) == count
            assert len(store.list_episodes(key)) == 1
        assert measurements[1].rows <= measurements[0].rows
        assert measurements[1].queries <= measurements[0].queries


def test_context_work_page_reopens_at_delivery_deadline_and_receipt_change(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    store, gateway = PostgresContextStore(app_database_url), EpisodeGateway()
    now = RECORDED
    worker = context_worker(store, gateway)
    monkeypatch.setattr(worker, "clock", lambda: now)
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    with TestClient(create_app(context_store=store, clock=lambda: now)) as client:
        register_open_session(client, reports_delivery=True)
        evidence = PostgresEvidenceStore(app_database_url)
        evidence.add_turn(replace(turn(), recorded_at=RECORDED))
        worker.process_state(key)
        worker.process_episodes(key)
        assert store.get_state(key) is None and store.list_episodes(key) == ()
        now += timedelta(seconds=599)
        assert not any(
            item.key == key for item in store.work_page(after=None, now=now, limit=10).items
        )
        now += timedelta(seconds=1)
        assert {
            item.operation
            for item in store.work_page(after=None, now=now, limit=10).items
            if item.key == key
        } == {"state", "episodes"}
        worker.process_state(key)
        worker.process_episodes(key)
        assert store.get_state(key) is not None and len(store.list_episodes(key)) == 1
        assert not any(
            item.key == key for item in store.work_page(after=None, now=now, limit=10).items
        )
        assert client.post("/turns/turn-1/receipts", json=receipt_payload("none", 0)).is_success
        assert {
            item.operation
            for item in store.work_page(after=None, now=now, limit=10).items
            if item.key == key
        } == {"state", "episodes"}
        worker.process_state(key)
        worker.process_episodes(key)
        assert store.get_state(key) is None and store.list_episodes(key) == ()


@pytest.mark.parametrize("status", ["running", "retry", "failed"])
def test_context_selection_respects_lease_retry_and_permanent_failure(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    store, gateway = PostgresContextStore(app_database_url), EpisodeGateway()
    worker = context_worker(store, gateway)
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    with TestClient(create_app(context_store=store, clock=lambda: RECORDED)) as client:
        register_open_session(client, reports_delivery=False)
        PostgresEvidenceStore(app_database_url).add_turn(turn(actor="user-a"))
        claim = store.claim(operation="state", key=key, now=RECORDED)
        assert claim is not None
        snapshot = store.load_window(key, now=RECORDED).snapshot
        assert store.begin(claim, snapshot=snapshot, now=RECORDED)
        due = RECORDED + timedelta(seconds=30) if status == "retry" else claim.lease_until
        if status != "running":
            store.fail(
                claim,
                error="invalid_output" if status == "failed" else "dependency_unavailable",
                retry_at=due if status == "retry" else None,
            )
        with monkeypatch.context() as patch:
            reads = TurnReads(patch)
            page = store.work_page(after=None, now=RECORDED, limit=10)
            assert not any(item.key == key and item.operation == "state" for item in page.items)
            worker.process_state(key)
            assert reads.queries == []
        assert gateway.calls == []
        if status != "failed":
            page = store.work_page(after=None, now=due, limit=10)
            assert any(item.key == key and item.operation == "state" for item in page.items)
            worker.clock = lambda: due
            worker.process_state(key)
            assert len(gateway.calls) == 1
            assert not store.begin(claim, snapshot=snapshot, now=due)
        else:
            # A changed input must unblock a permanent output failure.
            PostgresEvidenceStore(app_database_url).add_turn(turn("turn-2", actor="user-a"))
            worker.process_state(key)
            assert len(gateway.calls) == 1


def test_context_rechecks_delivery_timeout_change_without_notification(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    store = PostgresContextStore(app_database_url, delivery_timeout=timedelta(0))
    gateway = EpisodeGateway()
    worker = context_worker(store, gateway)
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    with TestClient(create_app(context_store=store, clock=lambda: RECORDED)) as client:
        register_open_session(client, reports_delivery=True)
        PostgresEvidenceStore(app_database_url).add_turn(turn())
        worker.process_state(key)
        worker.process_episodes(key)
        assert store.get_state(key) is not None and len(store.list_episodes(key)) == 1
        store.timeout = timedelta(minutes=10)
        assert {
            item.operation
            for item in store.work_page(after=None, now=RECORDED, limit=10).items
            if item.key == key
        } == {"state", "episodes"}
        worker.process_state(key)
        worker.process_episodes(key)
        assert store.get_state(key) is None and store.list_episodes(key) == ()
        assert not any(
            item.key == key for item in store.work_page(after=None, now=RECORDED, limit=10).items
        )


def test_context_load_failure_waits_for_retry_before_loading_again(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.evidence.ports import EvidenceUnavailable
    from digital_souls_memory.resident.domain import ResolvedWindow

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    store, gateway = PostgresContextStore(app_database_url), EpisodeGateway()
    worker = context_worker(store, gateway)
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    with TestClient(create_app(context_store=store, clock=lambda: RECORDED)) as client:
        register_open_session(client, reports_delivery=False)
        PostgresEvidenceStore(app_database_url).add_turn(turn(actor="user-a"))
        calls = []

        def unavailable(key: ContextKey, *, now: datetime) -> ResolvedWindow:
            calls.append(key)
            raise EvidenceUnavailable()

        with monkeypatch.context() as patch:
            patch.setattr(store, "load_window", unavailable)
            worker.process_state(key)
            work = store.get_work("state", key)
            assert work is not None and work.status == "retry" and work.retry_at is not None
            retry_at = work.retry_at
            assert not any(
                item.key == key and item.operation == "state"
                for item in store.work_page(after=None, now=RECORDED, limit=10).items
            )
            worker.process_state(key)
            assert calls == [key]
        worker.clock = lambda: retry_at
        worker.process_state(key)
        assert len(gateway.calls) == 1
