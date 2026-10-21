from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import DictRow

from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
from digital_souls_memory.adapters.postgres_voice import PostgresVoiceStore
from digital_souls_memory.api import create_app
from digital_souls_memory.voice.ports import VoiceTransaction
from tests.context_gateway_support import eventually
from tests.evidence_support import RECORDED, session_payload, turn_payload
from tests.recall_support import RecordingTokenCounter
from tests.voice_support import anchor_payload

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("crossed", [False, True])
@pytest.mark.parametrize("repeat", [0, 1])
def test_resident_save_and_anchor_registration_complete_in_both_lock_orders(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, crossed: bool, repeat: int
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    for name in ("MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    provisions, voice = (
        PostgresProvisionStore(app_database_url),
        PostgresVoiceStore(app_database_url),
    )
    resident_entered, resident_release, anchor_entered, anchor_release = (Event() for _ in range(4))
    pids: dict[str, int] = {}
    failures: list[str | None] = []
    provision_connection, voice_connection = provisions._connection, voice._connection
    voice_transaction = voice.transaction

    @contextmanager
    def paused_resident() -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with provision_connection() as connection:
                connection.execute("SELECT 1")
                pids["resident"] = connection.info.backend_pid
                resident_entered.set()
                assert resident_release.wait(5)
                yield connection
        except Exception as error:
            cause = error.__cause__
            failures.append(cause.sqlstate if isinstance(cause, psycopg.Error) else None)
            raise

    @contextmanager
    def measured_voice() -> Iterator[psycopg.Connection[DictRow]]:
        try:
            with voice_connection() as connection:
                pids["anchor"] = connection.info.backend_pid
                yield connection
        except Exception as error:
            cause = error.__cause__
            failures.append(cause.sqlstate if isinstance(cause, psycopg.Error) else None)
            raise

    @contextmanager
    def paused_anchor(character_id: str) -> Iterator[VoiceTransaction]:
        with voice_transaction(character_id) as transaction:
            anchor_entered.set()
            assert anchor_release.wait(5)
            yield transaction

    monkeypatch.setattr(provisions, "_connection", paused_resident)
    monkeypatch.setattr(voice, "transaction", paused_anchor)
    app = create_app(
        clock=lambda: RECORDED,
        provision_store=provisions,
        voice_store=voice,
        token_counter=RecordingTokenCounter(),
        provision_id_factory=lambda: "resident-parallel",
    )
    with TestClient(app) as client:
        for identifier in ("char-b", "char-c"):
            assert client.post(
                f"/characters/{identifier}/core-identities",
                json={"values": ["合成"], "prohibitions": []},
            ).is_success
        assert client.post(
            "/sessions", json=session_payload() | {"ended_at": None, "reports_delivery": False}
        ).is_success
        assert client.post("/turns", json=turn_payload()).is_success
        with ThreadPoolExecutor(max_workers=2) as pool:
            resident = pool.submit(
                client.post, "/characters/char-b/resident", json={"session_id": "session-1"}
            )
            try:
                assert resident_entered.wait(5)
                # Only the Anchor request uses this connection instrumentation.
                monkeypatch.setattr(voice, "_connection", measured_voice)
                anchor = pool.submit(
                    client.post, "/characters/char-b/verbatim-anchors", json=anchor_payload()
                )
                assert anchor_entered.wait(5)
                if crossed:
                    resident_release.set()
                    with psycopg.connect(app_database_url, autocommit=True) as observer:

                        def waiting() -> bool:
                            return observer.execute(
                                "SELECT %s=ANY(pg_blocking_pids(%s))",
                                (pids["anchor"], pids["resident"]),
                            ).fetchone() == (True,)

                        eventually(waiting)
                        locks = observer.execute(
                            "SELECT pid,locktype,classid,objid,granted FROM pg_locks "
                            "WHERE pid=ANY(%s) AND locktype='advisory' ORDER BY pid,granted",
                            (list(pids.values()),),
                        ).fetchall()
                        print({"repeat": repeat, "crossed": crossed, "pids": pids, "locks": locks})
                    anchor_release.set()
                else:
                    anchor_release.set()
                    assert anchor.result(timeout=5).is_success
                    resident_release.set()
                results = resident.result(timeout=5), anchor.result(timeout=5)
            finally:
                resident_release.set()
                anchor_release.set()
        monkeypatch.setattr(provisions, "_connection", provision_connection)
        monkeypatch.setattr(voice, "_connection", voice_connection)
        record = provisions.get("resident-parallel")
        saved_anchor = voice.get_anchor("char-b", "anchor-1")
        print(
            {
                "crossed": crossed,
                "http": [r.status_code for r in results],
                "sqlstates": failures,
                "provision_saved": record is not None,
                "anchor_saved": saved_anchor is not None,
            }
        )
        assert [r.status_code for r in results] == [200, 201]
        assert record is not None and saved_anchor is not None
        assert results[0].json()["character_profile"]["entries"] == []
        assert results[0].json()["voice_exemplars"] == []
