from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from digital_souls_memory.api import create_app
from tests.context_gateway_support import EpisodeGateway
from tests.evidence_support import RECORDED, TEXT, session_payload, turn_payload
from tests.postgres.test_resident_state import register_open_session
from tests.recall_support import RecordingTokenCounter

pytestmark = pytest.mark.postgres


def stored_table(connection: psycopg.Connection[tuple[object, ...]], columns: set[str]) -> str:
    rows = connection.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public'"
    ).fetchall()
    tables: dict[str, set[str]] = {}
    for table, column in rows:
        tables.setdefault(str(table), set()).add(str(column))
    matching = [name for name, available in tables.items() if columns <= available]
    assert len(matching) == 1, f"Expected persisted fields {sorted(columns)}"
    return matching[0]


@pytest.mark.parametrize(
    "target,changes",
    [
        ("state", {"session_id": "missing"}),
        ("state", {"expires_at": RECORDED}),
        ("episode-span", {"turn_id": "missing"}),
        ("episode-span", {"end": len(TEXT) + 1}),
        ("episode-span", {"turn_id": "other-session-turn"}),
        ("episode-span", {"turn_id": "other-surface-turn"}),
        ("episode-span", {"turn_id": "unperceived-turn"}),
        ("profile-entry", {"turn_id": "missing"}),
        ("profile-entry", {"end": len(TEXT) + 1}),
    ],
)
def test_database_rejects_invalid_context_references_spans_and_expiry(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    changes: dict[str, object],
) -> None:
    from digital_souls_memory.adapters.postgres_resident import PostgresContextStore
    from digital_souls_memory.resident.domain import ContextKey
    from digital_souls_memory.resident.worker import ContextWorker

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    for name in ("MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    store = PostgresContextStore(app_database_url)
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    worker = ContextWorker(
        store=store,
        gateway=EpisodeGateway(),
        clock=lambda: RECORDED,
        non_sensitive_labels=frozenset(),
        state_ttl=timedelta(minutes=30),
        episode_id_factory=lambda: "episode-1",
    )
    with TestClient(
        create_app(
            clock=lambda: RECORDED, context_store=store, token_counter=RecordingTokenCounter()
        )
    ) as client:
        register_open_session(client, reports_delivery=False)
        assert client.post("/turns", json=turn_payload()).is_success
        assert client.post(
            "/sessions",
            json=session_payload()
            | {
                "session_id": "session-2",
                "ended_at": None,
                "reports_delivery": False,
            },
        ).is_success
        assert client.post(
            "/sessions",
            json=session_payload()
            | {
                "session_id": "session-3",
                "surface": "other",
                "ended_at": None,
                "reports_delivery": False,
            },
        ).is_success
        for extra in (
            {"turn_id": "other-session-turn", "session_id": "session-2"},
            {"turn_id": "other-surface-turn", "session_id": "session-3", "surface": "other"},
            {"turn_id": "unperceived-turn", "perceived_by": ["char-c"]},
        ):
            assert client.post("/turns", json=turn_payload() | extra).is_success
        worker.process_state(key)
        worker.process_episodes(key)
        assert store.get_state(key) is not None and len(store.list_episodes(key)) == 1
        assert client.put(
            "/characters/char-b/profile",
            json={
                "token_limit": 10000,
                "entries": [
                    {
                        "entry_id": "profile-1",
                        "kind": "turn",
                        "turn_id": "turn-1",
                        "start": 0,
                        "end": len(TEXT),
                    }
                ],
            },
        ).is_success
    required = {
        "state": {"character_id", "session_id", "surface", "expires_at", "updated_at"},
        "episode-span": {"episode_id", "turn_id", "start", "end"},
        "profile-entry": {"entry_id", "turn_id", "start", "end"},
    }[target]
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        table = stored_table(connection, required)
        before = connection.execute(
            sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
        ).fetchall()
        assert before
        # Confirm that permitted writes reach this table before testing a bad value.
        field = "expires_at" if target == "state" else "end"
        connection.execute(
            sql.SQL("UPDATE {table} SET {field}={field}").format(
                table=sql.Identifier(table), field=sql.Identifier(field)
            )
        )
        assignments = sql.SQL(", ").join(
            sql.SQL("{}=%s").format(sql.Identifier(name)) for name in changes
        )
        with pytest.raises(psycopg.IntegrityError):
            with connection.transaction():
                connection.execute(
                    sql.SQL("UPDATE {} SET {}").format(sql.Identifier(table), assignments),
                    tuple(changes.values()),
                )
        assert (
            connection.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))).fetchall()
            == before
        )


@pytest.mark.parametrize("invalid", ["duplicate-state", "empty-episode-spans"])
def test_database_rejects_duplicate_state_keys_and_episodes_without_spans(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    from digital_souls_memory.adapters.postgres_resident import PostgresContextStore
    from digital_souls_memory.resident.domain import ContextKey
    from digital_souls_memory.resident.worker import ContextWorker

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    for name in ("MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    store = PostgresContextStore(app_database_url)
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    worker = ContextWorker(
        store=store,
        gateway=EpisodeGateway(),
        clock=lambda: RECORDED,
        non_sensitive_labels=frozenset(),
        state_ttl=timedelta(minutes=30),
        episode_id_factory=lambda: "episode-1",
    )
    with TestClient(create_app(clock=lambda: RECORDED, context_store=store)) as client:
        register_open_session(client, reports_delivery=False)
        assert client.post("/turns", json=turn_payload()).is_success
        worker.process_state(key)
        worker.process_episodes(key)
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        columns = (
            {"character_id", "session_id", "surface", "expires_at", "updated_at"}
            if invalid == "duplicate-state"
            else {"episode_id", "turn_id", "start", "end"}
        )
        table = stored_table(connection, columns)
        before = connection.execute(
            sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
        ).fetchall()
        assert before
        with pytest.raises(psycopg.IntegrityError):
            with connection.transaction():
                if invalid == "duplicate-state":
                    connection.execute(
                        sql.SQL(
                            "INSERT INTO {t} SELECT (jsonb_populate_record("
                            "NULL::{t},to_jsonb(v) || %s::jsonb)).* FROM {t} v"
                        ).format(t=sql.Identifier(table)),
                        (Jsonb({}),),
                    )
                else:
                    connection.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
        assert (
            connection.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))).fetchall()
            == before
        )
