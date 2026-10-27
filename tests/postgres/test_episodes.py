from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Event
from typing import cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.abc import Params, Query
from psycopg.rows import DictRow, dict_row

from digital_souls_memory.adapters.postgres_episodes import replace_episodes
from digital_souls_memory.adapters.postgres_resident import PostgresContextStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from digital_souls_memory.resident.domain import ContextKey
from tests.context_gateway_support import EpisodeGateway, eventually
from tests.evidence_support import RECORDED, TEXT, receipt_payload, turn_payload
from tests.postgres.test_context_work_selection import context_worker
from tests.postgres.test_resident_state import register_open_session

pytestmark = pytest.mark.postgres
PATH = "/characters/char-b/sessions/session-1/episodes"
PARAMS = {"surface": "synthetic-chat"}


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)


def episodes(client: TestClient) -> list[dict[str, object]]:
    response = client.get(PATH, params=PARAMS)
    assert response.is_success
    values = response.json()["episodes"]
    assert isinstance(values, list)
    return values


@pytest.mark.parametrize(
    "label,route", [("synthetic-public", "cloud"), ("synthetic-sensitive", "local")]
)
def test_episode_registration_is_nonblocking_and_restores_verbatim_sources_after_reconnect(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    route: str,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    gateway = EpisodeGateway()
    gateway.episode_release.clear()
    app = create_app(
        context_gateway=gateway,
        clock=lambda: RECORDED,
        non_sensitive_labels=frozenset({"synthetic-public"}),
    )
    with TestClient(app) as client:
        try:
            register_open_session(client, reports_delivery=False)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    client.post, "/turns", json=turn_payload() | {"sensitivity": label}
                )
                try:
                    assert future.result(timeout=2).status_code == 201
                    assert gateway.episode_started.wait(2)
                    assert episodes(client) == []
                finally:
                    gateway.episode_release.set()
            eventually(lambda: len(episodes(client)) == 1)
            saved = episodes(client)
            (value,) = saved
            assert value["episode_id"]
            assert value["topic"] == TEXT
            assert (value["model"], value["prompt_version"]) == ("synthetic-model", "synthetic-v1")
            assert datetime.fromisoformat(str(value["created_at"])) == RECORDED
            assert value["turn_span"] == [{"turn_id": "turn-1", "start": 0, "end": len(TEXT)}]
            spans = value["source_spans"]
            assert isinstance(spans, list) and len(spans) == 1
            assert (spans[0]["text"], spans[0]["actor"], spans[0]["language"]) == (
                TEXT,
                "char-c",
                "ja",
            )
            with gateway.lock:
                calls = tuple(gateway.episode_calls)
            assert calls and all(actual == route for actual, _ in calls)
            assert all(
                isinstance(v, CloudInput if route == "cloud" else LocalOnly) for _, v in calls
            )
            for path, params in (
                (PATH, {"surface": "other"}),
                ("/characters/char-z/sessions/session-1/episodes", PARAMS),
            ):
                response = client.get(path, params=params)
                assert response.is_success and response.json()["episodes"] == []
        finally:
            gateway.episode_release.set()
    with TestClient(create_app(clock=lambda: RECORDED)) as restored:
        assert episodes(restored) == saved


def test_episode_input_correction_withdraws_old_batch_before_replacement_finishes(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    now = [RECORDED]
    gateway = EpisodeGateway()
    with TestClient(create_app(context_gateway=gateway, clock=lambda: now[0])) as client:
        try:
            register_open_session(client, reports_delivery=True)
            assert client.post("/turns", json=turn_payload()).status_code == 201
            assert episodes(client) == []
            now[0] += timedelta(minutes=10)
            eventually(lambda: len(episodes(client)) == 1)
            original = episodes(client)
            gateway.episode_release.clear()
            assert client.post(
                "/turns/turn-1/receipts", json=receipt_payload("partial", 3)
            ).is_success
            assert episodes(client) == []
            gateway.episode_release.set()
            eventually(lambda: len(episodes(client)) == 1)
            corrected = episodes(client)
            assert corrected != original
            spans = corrected[0]["source_spans"]
            assert isinstance(spans, list)
            assert [(v["text"], v["end"]) for v in spans] == [(TEXT[:3], 3)]
            assert client.post("/turns/turn-1/receipts", json=receipt_payload("none", 0)).is_success
            assert episodes(client) == []
            assert client.post(
                "/turns/turn-1/receipts", json=receipt_payload("full", len(TEXT))
            ).is_success

            def full_batch() -> bool:
                values = episodes(client)
                return bool(values and values[0]["topic"] == TEXT)

            eventually(full_batch)
            spans = episodes(client)[0]["source_spans"]
            assert isinstance(spans, list)
            assert [v["text"] for v in spans] == [TEXT]
        finally:
            gateway.episode_release.set()


def test_episode_get_keeps_parent_and_spans_consistent_during_batch_replacement(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    store = PostgresContextStore(app_database_url)
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    with TestClient(create_app(context_store=store, clock=lambda: RECORDED)) as client:
        register_open_session(client, reports_delivery=False)
        assert client.post("/turns", json=turn_payload()).status_code == 201
        context_worker(store, EpisodeGateway()).process_episodes(key)
        (original,) = store.list_episodes(key)
        replacement = replace(original, episode_id="replacement", topic="置換した合成話題")
        parent_read, replaced = Event(), Event()
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
            if isinstance(query, str) and query.startswith("SELECT * FROM episodes "):
                parent_read.set()
                assert replaced.wait(5), "Replacement did not commit"
            return result

        with monkeypatch.context() as patch:
            patch.setattr(psycopg.Cursor, "execute", observe)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(client.get, PATH, params=PARAMS)
                try:
                    assert parent_read.wait(5), "Episode parent query was not reached"
                    with psycopg.connect(app_database_url, row_factory=dict_row) as connection:
                        replace_episodes(connection, key, (replacement,))
                    replaced.set()
                    response = future.result(timeout=5)
                finally:
                    replaced.set()
        assert response.status_code == 200, response.json()
        assert response.json()["episodes"][0]["episode_id"] in (
            original.episode_id,
            replacement.episode_id,
        )
        assert response.json()["episodes"][0]["turn_span"] == [
            {"turn_id": "turn-1", "start": 0, "end": len(TEXT)}
        ]
        assert episodes(client)[0]["episode_id"] == replacement.episode_id


@pytest.mark.parametrize(
    "actor,kind,perceived,mode,targets,visible",
    [
        ("viewer", "utterance", [], "broadcast", [], False),
        ("viewer", "utterance", ["char-b"], "broadcast", [], True),
        ("char-b", "utterance", ["char-b"], "direct", ["viewer"], True),
        ("char-c", "utterance", ["char-b", "char-c"], "direct", ["char-b"], True),
        ("char-c", "utterance", ["char-c"], "direct", ["viewer"], False),
        ("char-c", "utterance", ["char-c"], "direct", ["char-b"], False),
        ("char-b", "thought", ["char-b"], "broadcast", [], True),
        ("char-c", "thought", ["char-c"], "broadcast", [], False),
        ("char-c", "utterance", ["char-b", "char-c"], "self", [], True),
    ],
)
def test_episode_inputs_use_explicit_perception_without_addressee_or_reply_inheritance(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    actor: str,
    kind: str,
    perceived: list[str],
    mode: str,
    targets: list[str],
    visible: bool,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    gateway = EpisodeGateway()
    with TestClient(create_app(context_gateway=gateway, clock=lambda: RECORDED)) as client:
        try:
            register_open_session(client, reports_delivery=False)
            assert client.post(
                "/turns",
                json=turn_payload()
                | {
                    "actor": actor,
                    "kind": kind,
                    "perceived_by": perceived,
                    "address_mode": mode,
                    "addressees": targets,
                },
            ).is_success
            assert client.post(
                "/turns",
                json=turn_payload()
                | {
                    "turn_id": "control",
                    "actor": "char-b",
                    "perceived_by": ["char-b"],
                    "reply_to_turn_id": "turn-1",
                    "text": "control",
                },
            ).is_success
            expected = {"control", "turn-1"} if visible else {"control"}

            def current_batch() -> bool:
                values = episodes(client)
                identifiers: set[str] = set()
                for value in values:
                    spans = value["turn_span"]
                    assert isinstance(spans, list)
                    identifiers.update(span["turn_id"] for span in spans)
                return identifiers == expected

            eventually(current_batch)
            with gateway.lock:
                calls = tuple(gateway.episode_calls)
            for _, value in calls:
                if value.character_id == "char-b":
                    assert {v.turn_id for v in value.turns} <= expected
            assert client.get("/turns/turn-1").json()["text"] == TEXT
        finally:
            gateway.episode_release.set()
