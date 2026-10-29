from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Event
from typing import cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
from digital_souls_memory.adapters.postgres_voice import PostgresVoiceStore
from digital_souls_memory.api import create_app
from tests.context_gateway_support import eventually
from tests.evidence_support import RECORDED, TEXT, receipt_payload, session_payload, turn_payload
from tests.identity_support import seed_payload
from tests.it1.identity_fakes import FakeLLMGateway, FakeReviewer
from tests.it1.test_recall import request
from tests.memory_support import memory
from tests.recall_support import (
    RecordingEmbedding,
    RecordingReranker,
    RecordingRetriever,
    RecordingTokenCounter,
)
from tests.voice_support import exemplar

pytestmark = pytest.mark.postgres
ROOT = "/characters/char-b"
RESIDENT = ROOT + "/resident"
PROFILE = ROOT + "/profile"
REQUEST = {"session_id": "session-1"}


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("initial_identity", [False, True])
def test_resident_and_recall_concurrent_selection_survives_reconnect_and_later_versions(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    initial_identity: bool,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    now = [RECORDED]

    def application() -> TestClient:
        return TestClient(
            create_app(
                clock=lambda: now[0],
                llm_gateway=FakeLLMGateway(),
                reviewer=FakeReviewer(),
                retriever=RecordingRetriever(
                    PostgresEvidenceStore(app_database_url),
                    IdentityCharacters(lambda: PostgresIdentityStore(app_database_url)),
                ),
                embedding=RecordingEmbedding(),
                reranker=RecordingReranker(),
                token_counter=RecordingTokenCounter(),
            )
        )

    with application() as client:
        assert client.post(
            ROOT + "/core-identities", json={"values": ["誠実"], "prohibitions": []}
        ).is_success
        if initial_identity:
            assert client.post(ROOT + "/temperaments", json={"description": "初期気質"}).is_success
            assert client.post(
                ROOT + "/personality-profiles/author-seed", json=seed_payload()
            ).is_success
        assert client.post(
            "/sessions",
            json=session_payload() | {"started_at": now[0].isoformat(), "ended_at": None},
        ).is_success
        start = Barrier(2, timeout=5)

        def obtain(path: str, body: Mapping[str, object]) -> dict[str, object]:
            start.wait()
            response = client.post(path, json=body)
            assert response.is_success
            return cast(dict[str, object], response.json())

        with ThreadPoolExecutor(max_workers=2) as pool:
            resident = pool.submit(obtain, RESIDENT, REQUEST)
            recall = pool.submit(obtain, ROOT + "/recall", request())
            results = [resident.result(timeout=10), recall.result(timeout=10)]
        expected = 1 if initial_identity else None
        assert [(v["temperament_version"], v["personality_version"]) for v in results] == [
            (expected, expected),
            (expected, expected),
        ]
        now[0] += timedelta(days=1)
        assert client.post(ROOT + "/temperaments", json={"description": "後日気質"}).is_success
    with application() as restored:
        for path, body in ((RESIDENT, REQUEST), (ROOT + "/recall", request())):
            response = restored.post(path, json=body)
            assert response.is_success
            assert (
                response.json()["temperament_version"],
                response.json()["personality_version"],
            ) == (expected, expected)
        assert restored.post(
            "/sessions",
            json=session_payload()
            | {"session_id": "next-session", "started_at": now[0].isoformat(), "ended_at": None},
        ).is_success
        response = restored.post(RESIDENT, json={"session_id": "next-session"})
        assert response.is_success
        assert response.json()["temperament_version"] == (2 if initial_identity else 1)
        assert response.json()["personality_version"] == expected
        record = PostgresProvisionStore(app_database_url).get(response.json()["provision_id"])
        assert record is not None and record.kind == "resident"
        assert (record.temperament_version, record.personality_version) == (
            response.json()["temperament_version"],
            expected,
        )


@pytest.mark.parametrize("change", ["profile", "receipt", "voice-source"])
def test_resident_save_rechecks_profile_and_source_changes_inside_its_transaction(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    provisions = PostgresProvisionStore(app_database_url)
    original_connection = provisions._connection
    entered, release = Event(), Event()
    armed = False
    next_id = ["resident-control"]

    @contextmanager
    def pause_before_validation() -> Iterator[psycopg.Connection[DictRow]]:
        nonlocal armed
        with original_connection() as connection:
            if armed:
                armed = False
                connection.execute("SELECT 1")
                assert connection.info.transaction_status == TransactionStatus.INTRANS
                entered.set()
                assert release.wait(5), "Test did not release the resident save transaction"
            yield connection

    monkeypatch.setattr(provisions, "_connection", pause_before_validation)
    app = create_app(
        clock=lambda: RECORDED + timedelta(minutes=10),
        provision_store=provisions,
        provision_id_factory=lambda: next_id[0],
        token_counter=RecordingTokenCounter(),
        llm_gateway=FakeLLMGateway(),
        reviewer=FakeReviewer(),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        for identifier in ("char-b", "char-c"):
            resource = "temperaments" if identifier == "char-c" else "core-identities"
            assert client.post(
                f"/characters/{identifier}/{resource}",
                json={"description": "合成気質"}
                if identifier == "char-c"
                else {"values": ["誠実"], "prohibitions": []},
            ).is_success
        assert client.post(
            "/sessions",
            json=session_payload()
            | {
                "ended_at": None,
                "started_at": (RECORDED + timedelta(minutes=10)).isoformat(),
            },
        ).is_success
        assert client.post("/turns", json=turn_payload()).is_success
        assert client.post(
            "/turns/turn-1/receipts", json=receipt_payload("full", len(TEXT))
        ).is_success
        entry = {
            "entry_id": "profile-1",
            "kind": "turn",
            "turn_id": "turn-1",
            "start": 0,
            "end": len(TEXT),
        }
        if change == "voice-source":
            assert client.post(ROOT + "/temperaments", json={"description": "合成気質"}).is_success
            assert client.post(
                ROOT + "/personality-profiles/author-seed", json=seed_payload()
            ).is_success
            with PostgresVoiceStore(app_database_url).transaction("char-b") as tx:
                tx.add_exemplar(
                    replace(
                        exemplar(),
                        character_id="char-b",
                        source="turn_id",
                        turn_id="turn-1",
                        text=TEXT,
                    )
                )
        else:
            assert client.put(PROFILE, json={"token_limit": 10000, "entries": [entry]}).is_success
        initial = client.post(RESIDENT, json=REQUEST)
        assert initial.is_success
        if change == "voice-source":
            assert initial.json()["character_profile"]["entries"] == []
            assert [v["text"] for v in initial.json()["voice_exemplars"]] == [TEXT]
        else:
            assert initial.json()["character_profile"]["entries"]
        original = provisions.get("resident-control")
        assert original is not None
        next_id[0] = "resident-in-flight"
        armed = True
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, RESIDENT, json=REQUEST)
            try:
                assert entered.wait(5)
                if change == "profile":
                    assert client.put(
                        PROFILE, json={"token_limit": 10000, "entries": []}
                    ).is_success
                else:
                    assert client.post(
                        "/turns/turn-1/receipts", json=receipt_payload("none", 0)
                    ).is_success
            finally:
                release.set()
            result = future.result(timeout=5)
        assert result.status_code == 503
        assert provisions.get("resident-in-flight") is None
        assert provisions.get("resident-control") == original
        next_id[0] = "resident-after-change"
        result = client.post(RESIDENT, json=REQUEST)
        assert result.is_success and result.json()["character_profile"]["entries"] == []
        record = provisions.get("resident-after-change")
        assert record is not None and record.kind == "resident"
        assert record.source_spans == ()
        if change == "voice-source":
            assert result.json()["voice_exemplars"] == []
            assert record.voice_exemplar_ids == ()


def test_profile_sources_and_resident_record_roundtrip_without_rewriting_original_turns(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    with TestClient(
        create_app(clock=lambda: RECORDED, token_counter=RecordingTokenCounter())
    ) as client:
        for identifier in ("char-b", "char-c"):
            resource = "temperaments" if identifier == "char-c" else "core-identities"
            assert client.post(
                f"/characters/{identifier}/{resource}",
                json={"description": "合成気質"}
                if identifier == "char-c"
                else {"values": ["誠実"], "prohibitions": []},
            ).is_success
        assert client.post(
            "/sessions", json=session_payload() | {"reports_delivery": False, "ended_at": None}
        ).is_success
        assert client.post("/turns", json=turn_payload()).is_success
        entry = {
            "entry_id": "profile-1",
            "kind": "turn",
            "turn_id": "turn-1",
            "start": 0,
            "end": len(TEXT),
        }
        assert client.put(PROFILE, json={"token_limit": 10000, "entries": [entry]}).is_success
        before = client.post(RESIDENT, json=REQUEST)
        assert before.is_success
        provided = before.json()
        record = PostgresProvisionStore(app_database_url).get(provided["provision_id"])
        assert record is not None and record.kind == "resident"
        assert record.profile_entry_ids == ("profile-1",)
        assert [
            (s.turn_id, s.start, s.end, s.text, s.actor, s.language) for s in record.source_spans
        ] == [("turn-1", 0, len(TEXT), TEXT, "char-c", "ja")]
        for invalid in (entry | {"turn_id": "missing"}, entry | {"end": len(TEXT) + 1}):
            assert (
                client.put(
                    PROFILE,
                    json={"token_limit": 10000, "entries": [entry, invalid | {"entry_id": "bad"}]},
                ).status_code
                == 422
            )
            current = client.post(RESIDENT, json=REQUEST)
            assert current.is_success
            assert current.json()["character_profile"] == provided["character_profile"]
    with TestClient(
        create_app(clock=lambda: RECORDED, token_counter=RecordingTokenCounter())
    ) as restored:
        response = restored.post(RESIDENT, json=REQUEST)
        assert response.is_success
        assert response.json()["character_profile"] == provided["character_profile"]
        assert restored.get("/turns/turn-1").json()["text"] == TEXT
        assert PostgresProvisionStore(app_database_url).get(provided["provision_id"]) == record


@pytest.mark.parametrize("text", ["日本語🙂", "日本語🙂" * 200], ids=["short", "long"])
def test_resident_reading_saved_memory_does_not_reinforce_or_change_its_evidence(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    value = replace(memory(), content=text, last_reinforced_at=RECORDED - timedelta(days=2))
    store = PostgresMemoryStore(app_database_url)
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            embedding=RecordingEmbedding(),
            token_counter=RecordingTokenCounter(),
        )
    ) as client:
        for identifier in ("char-b", "char-c"):
            resource = "temperaments" if identifier == "char-c" else "core-identities"
            assert client.post(
                f"/characters/{identifier}/{resource}",
                json={"description": "合成気質"}
                if identifier == "char-c"
                else {"values": ["誠実"], "prohibitions": []},
            ).is_success
        assert client.post("/sessions", json=session_payload() | {"ended_at": None}).is_success
        assert client.post("/turns", json=turn_payload() | {"text": text}).is_success
        assert client.post(
            "/turns/turn-1/receipts", json=receipt_payload("full", len(text))
        ).is_success
        with store.transaction("char-b") as transaction:
            transaction.add_memory(value)
        before = store.get_memory("char-b", value.id)
        assert before is not None
        ref = {
            "entry_id": "profile-memory",
            "kind": "memory",
            "owner_id": "char-b",
            "memory_id": value.id,
        }
        eventually(
            lambda: client.put(PROFILE, json={"token_limit": 10000, "entries": [ref]}).is_success
        )
        for _ in range(2):
            response = client.post(RESIDENT, json=REQUEST)
            assert response.is_success
            assert [v["text"] for v in response.json()["character_profile"]["entries"]] == [text]
            record = PostgresProvisionStore(app_database_url).get(response.json()["provision_id"])
            assert record is not None and record.memory_ids == (value.id,)
        after = store.get_memory("char-b", value.id)
        assert after is not None
        assert after == before
        assert after.last_reinforced_at == value.last_reinforced_at
        assert after.source_turn_ids == value.source_turn_ids
        assert (
            after.epistemics.independent_evidence_count
            == value.epistemics.independent_evidence_count
        )
        from digital_souls_memory.adapters.postgres_resident import PostgresProfileStore

        configured = PostgresProfileStore(app_database_url).get("char-b")
        assert client.post("/turns/turn-1/receipts", json=receipt_payload("partial", 3)).is_success
        following = client.post(RESIDENT, json=REQUEST)
        assert following.is_success and following.json()["character_profile"]["entries"] == []
        assert PostgresProfileStore(app_database_url).get("char-b") == configured
        assert record is not None
        assert PostgresProvisionStore(app_database_url).get(record.provision_id) == record


def test_temperament_only_source_allows_partial_profile_and_full_voice_with_matching_records(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    text = "日本語🙂"
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            token_counter=RecordingTokenCounter(),
            llm_gateway=FakeLLMGateway(),
            reviewer=FakeReviewer(),
        )
    ) as client:
        assert client.post(
            ROOT + "/core-identities", json={"values": ["合成"], "prohibitions": []}
        ).is_success
        for identifier in ("char-b", "char-c"):
            assert client.post(
                f"/characters/{identifier}/temperaments", json={"description": "合成"}
            ).is_success
        assert client.post(
            ROOT + "/personality-profiles/author-seed", json=seed_payload()
        ).is_success
        assert client.post(
            "/sessions",
            json=session_payload() | {"ended_at": None, "started_at": RECORDED.isoformat()},
        ).is_success
        assert client.post("/turns", json=turn_payload() | {"text": text}).is_success
        assert client.post("/turns/turn-1/receipts", json=receipt_payload("full", 4)).is_success
        with PostgresVoiceStore(app_database_url).transaction("char-b") as tx:
            tx.add_exemplar(
                replace(
                    exemplar(), character_id="char-b", source="turn_id", turn_id="turn-1", text=text
                )
            )
        full = client.post(RESIDENT, json=REQUEST)
        assert full.is_success
        assert [v["text"] for v in full.json()["voice_exemplars"]] == [text]
        provisions = PostgresProvisionStore(app_database_url)
        original = provisions.get(full.json()["provision_id"])
        assert original is not None and original.voice_exemplar_ids == ("voice-1",)
        assert client.post("/turns/turn-1/receipts", json=receipt_payload("partial", 3)).is_success
        entry = {"entry_id": "profile-1", "kind": "turn", "turn_id": "turn-1", "start": 0, "end": 3}
        profile = client.put(PROFILE, json={"token_limit": 10000, "entries": [entry]})
        assert profile.is_success
        assert profile.json()["entries"][0]["text"] == "日本語"
        assert (
            client.put(
                PROFILE, json={"token_limit": 10000, "entries": [entry | {"end": 4}]}
            ).status_code
            == 422
        )
        partial = client.post(RESIDENT, json=REQUEST)
        assert partial.is_success
        assert partial.json()["character_profile"] == profile.json()
        assert partial.json()["voice_exemplars"] == []
        record = provisions.get(partial.json()["provision_id"])
        assert record is not None and record.voice_exemplar_ids == ()
        assert record.profile_entry_ids == ("profile-1",)
        assert [(v.text, v.end) for v in record.source_spans] == [("日本語", 3)]
        assert provisions.get(original.provision_id) == original
