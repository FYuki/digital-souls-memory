from datetime import timedelta
from typing import Literal

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_resident import PostgresContextStore
from digital_souls_memory.api import create_app
from digital_souls_memory.resident.domain import ContextKey
from digital_souls_memory.resident.worker import ContextWorker
from tests.context_gateway_support import EpisodeGateway
from tests.evidence_support import RECORDED, receipt_payload, session_payload, turn_payload

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("operation", ["state", "episodes"])
@pytest.mark.parametrize("registration", ["core", "temperament", "both", "neither"])
@pytest.mark.parametrize("text", ["char-c", "日本語🙂"])
def test_database_actor_registration_controls_gateway_delivery_and_saved_context(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    operation: Literal["state", "episodes"],
    registration: str,
    text: str,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    for name in ("MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    now = [RECORDED]
    store, gateway = PostgresContextStore(app_database_url), EpisodeGateway()
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    worker = ContextWorker(
        store=store,
        gateway=gateway,
        clock=lambda: now[0],
        non_sensitive_labels=frozenset(),
        state_ttl=timedelta(minutes=30),
        episode_id_factory=iter(f"episode-{i}" for i in range(20)).__next__,
    )
    process = worker.process_state if operation == "state" else worker.process_episodes
    calls = gateway.calls if operation == "state" else gateway.episode_calls
    with TestClient(create_app(clock=lambda: now[0], context_store=store)) as client:
        assert client.post(
            "/characters/char-b/core-identities", json={"values": ["合成"], "prohibitions": []}
        ).is_success
        if registration in ("core", "both"):
            assert client.post(
                "/characters/char-c/core-identities", json={"values": ["合成"], "prohibitions": []}
            ).is_success
        if registration in ("temperament", "both", "neither"):
            assert client.post(
                "/characters/char-c/temperaments", json={"description": "合成気質"}
            ).is_success
        assert client.post("/sessions", json=session_payload() | {"ended_at": None}).is_success
        actor = "viewer" if registration == "neither" else "char-c"
        assert client.post(
            "/turns", json=turn_payload() | {"actor": actor, "text": text}
        ).is_success
        now[0] += timedelta(seconds=599)

        def run(expected: str | None, basis: str | None) -> None:
            before = len(calls)
            process(key)
            work = store.get_work(operation, key)
            assert work is not None and work.status == "done"
            assert work.completed_signature == store.load_window(key, now=now[0]).snapshot.signature
            path = f"/characters/char-b/sessions/session-1/{operation}"
            result = client.get(path, params={"surface": key.surface}).json()[operation]
            if expected is None:
                assert len(calls) == before
                assert result == (None if operation == "state" else [])
            else:
                assert [t.text for _, value in calls[before:] for t in value.turns] == [expected]
                artifact = result if operation == "state" else result[0]
                assert artifact["current_topic" if operation == "state" else "topic"] == expected
                assert artifact["dependencies"][0]["basis"] == basis
                assert artifact["dependencies"][0]["available_end"] == len(expected)
                if operation == "episodes":
                    assert artifact["source_spans"] == [
                        {
                            "turn_id": "turn-1",
                            "start": 0,
                            "end": len(expected),
                            "text": expected,
                            "actor": actor,
                            "language": "ja",
                        }
                    ]

        if registration == "neither":
            run(text, "original")
            return
        run(None, None)
        pending = store.get_work(operation, key)
        assert pending is not None and pending.next_due_at == RECORDED + timedelta(seconds=600)
        # The same source is immediately available to its speaker.
        own = ContextKey(character_id="char-c", session_id=key.session_id, surface=key.surface)
        process(own)
        assert calls[-1][1].character_id == "char-c"
        assert [t.text for t in calls[-1][1].turns] == [text]
        now[0] += timedelta(seconds=1)
        run(text, "assumed_full")
        for status, end in (("partial", 3), ("partial", 2), ("none", 0), ("full", len(text))):
            assert client.post(
                "/turns/turn-1/receipts", json=receipt_payload(status, end)
            ).is_success
            if status != "full":
                path = f"/characters/char-b/sessions/session-1/{operation}"
                assert client.get(path, params={"surface": key.surface}).json()[operation] == (
                    None if operation == "state" else []
                )
            run(text[:end] if end else None, "receipt_confirmed")
