from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from digital_souls_memory.api import create_app
from tests.evidence_support import receipt_payload
from tests.identity_support import REGISTERED
from tests.it1.test_voice import register_identity, register_turn
from tests.it1.voice_fakes import VoiceGateway, VoiceReviewer
from tests.voice_support import (
    ANCHORS,
    EXEMPLARS,
    INSTRUCTION,
    TEXT,
    anchor_payload,
    exemplar_payload,
)

pytestmark = pytest.mark.postgres


def make_client(gateway: VoiceGateway, reviewer: VoiceReviewer) -> TestClient:
    return TestClient(create_app(llm_gateway=gateway, reviewer=reviewer, clock=lambda: REGISTERED))


def seed(client: TestClient) -> None:
    register_identity(client)
    register_identity(client, "char-b")
    register_turn(client)
    assert client.post(EXEMPLARS, json={"exemplars": [exemplar_payload()]}).is_success
    assert client.post(ANCHORS, json=anchor_payload()).is_success


def test_default_storage_roundtrip_and_check_survive_new_application(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    gateway, reviewer = VoiceGateway(), VoiceReviewer()
    with make_client(gateway, reviewer) as client:
        seed(client)
        saved = client.get(EXEMPLARS + "/voice-1").json()
        saved_anchor = client.get(ANCHORS + "/anchor-1").json()
        assert saved["text"] == saved_anchor["text"] == TEXT
    with make_client(gateway, reviewer) as restored:
        assert restored.get(EXEMPLARS + "/voice-1").json() == saved
        assert restored.get(ANCHORS + "/anchor-1").json() == saved_anchor
        for key, value in exemplar_payload().items():
            assert saved[key] == value
        for key, value in anchor_payload().items():
            assert saved_anchor[key] == value
        assert restored.post(
            EXEMPLARS,
            json={
                "exemplars": [
                    exemplar_payload() | {"exemplar_id": "instruction", "text": INSTRUCTION}
                ]
            },
        ).is_success
        assert restored.get(EXEMPLARS + "/instruction").json()["text"] == INSTRUCTION
    assert len(gateway.voice_calls) == len(reviewer.voice_calls) == 1


@pytest.mark.parametrize(
    "table,changes",
    [
        ("voice_exemplars", {"exemplar_id": "invalid", "trait_version": 999}),
        ("voice_exemplars", {"exemplar_id": "invalid", "character_id": "char-b"}),
        ("voice_exemplars", {"exemplar_id": "invalid", "approved_by": None}),
        ("voice_exemplars", {"exemplar_id": "invalid", "approved_by": " \t\n"}),
        ("voice_exemplars", {"exemplar_id": "invalid", "approved_by": ""}),
        ("voice_exemplars", {"exemplar_id": "invalid", "source": "unknown"}),
        ("voice_exemplars", {"exemplar_id": "invalid", "source": "turn_id", "turn_id": None}),
        ("voice_exemplars", {"exemplar_id": "invalid", "source": "turn_id", "turn_id": "absent"}),
        ("verbatim_anchors", {"anchor_id": "invalid", "turn_id": "absent"}),
        ("verbatim_anchors", {"anchor_id": "invalid", "kind": "unknown"}),
        ("verbatim_anchors", {"anchor_id": "invalid", "approved_by": None}),
        ("verbatim_anchors", {"anchor_id": "invalid", "approved_by": " \t\n"}),
        ("verbatim_anchors", {"anchor_id": "invalid", "approved_by": ""}),
    ],
)
def test_database_rejects_invalid_references_and_approval_without_domain_validation(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    changes: dict[str, object],
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    with make_client(VoiceGateway(), VoiceReviewer()) as client:
        seed(client)
        assert client.post(
            "/characters/unseeded/core-identities",
            json={
                "values": ["合成価値"],
                "prohibitions": [],
            },
        ).is_success
        assert client.post(
            "/characters/unseeded/temperaments",
            json={
                "description": "合成気質",
            },
        ).is_success
    # The other character exists but has no personality version 1.
    if changes.get("character_id") == "char-b":
        changes = changes | {"character_id": "unseeded"}
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as conn:
        query = sql.SQL(
            "INSERT INTO {table} SELECT (jsonb_populate_record(NULL::{table}, "
            "to_jsonb(v) || %s::jsonb)).* FROM {table} v"
        ).format(table=sql.Identifier(table))
        with pytest.raises(psycopg.Error):
            conn.execute(query, (Jsonb(changes),))
        assert conn.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "table,column", [("voice_exemplars", "text"), ("verbatim_anchors", "reason")]
)
def test_database_nonowner_cannot_rewrite_registered_materials(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    column: str,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    with make_client(VoiceGateway(), VoiceReviewer()) as client:
        seed(client)
        before = (client.get(EXEMPLARS).json(), client.get(ANCHORS).json())
        with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as conn:
            assert conn.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == (False,)
            assert conn.execute(
                "SELECT tableowner <> current_user FROM pg_tables "
                "WHERE schemaname='public' AND tablename=%s",
                (table,),
            ).fetchone() == (True,)
            with pytest.raises(psycopg.Error):
                conn.execute(
                    sql.SQL("UPDATE {} SET {} = %s").format(
                        sql.Identifier(table), sql.Identifier(column)
                    ),
                    ("changed",),
                )
        assert (client.get(EXEMPLARS).json(), client.get(ANCHORS).json()) == before


@pytest.mark.parametrize(
    "actor,perceived,kind,mode,targets,allowed",
    [
        ("char-a", ("char-a",), "utterance", "self", (), True),
        ("char-b", ("char-a", "char-b"), "utterance", "direct", ("char-a",), True),
        ("char-b", ("char-b",), "utterance", "direct", ("char-a",), False),
        ("char-b", ("char-b",), "utterance", "direct", ("viewer",), False),
        ("viewer", (), "utterance", "broadcast", (), False),
        ("char-b", ("char-b",), "thought", "self", (), False),
        ("char-a", ("char-a",), "thought", "self", (), True),
    ],
)
def test_real_storage_voice_operations_respect_perception(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    actor: str,
    perceived: tuple[str, ...],
    kind: str,
    mode: str,
    targets: tuple[str, ...],
    allowed: bool,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    with make_client(VoiceGateway(), VoiceReviewer()) as client:
        register_identity(client)
        register_identity(client, "char-b")
        register_turn(
            client, actor=actor, perceived=perceived, kind=kind, mode=mode, targets=targets
        )
        payload = exemplar_payload() | {"source": "turn_id", "turn_id": "turn-1"}
        for path, body, identifier in (
            (EXEMPLARS, {"exemplars": [payload]}, "voice-1"),
            (ANCHORS, anchor_payload(), "anchor-1"),
        ):
            response = client.post(path, json=body)
            assert response.is_success if allowed else response.is_client_error
            values = client.get(path).json()
            assert len(values) == int(allowed)
            if allowed:
                assert client.get(path + "/" + identifier).json()["text"] == TEXT
                assert client.get(path.replace("char-a", "char-b")).json() == []
                assert client.get(
                    (path + "/" + identifier).replace("char-a", "char-b")
                ).is_client_error


def test_real_storage_receipt_changes_restrict_previously_saved_material(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    now = [REGISTERED]
    with TestClient(
        create_app(
            llm_gateway=VoiceGateway(),
            reviewer=VoiceReviewer(),
            clock=lambda: now[0],
        )
    ) as client:
        register_identity(client)
        register_identity(client, "char-b")
        register_turn(client, delivery=True)
        now[0] += timedelta(minutes=10)
        payload = exemplar_payload() | {"source": "turn_id", "turn_id": "turn-1"}
        assert client.post(EXEMPLARS, json={"exemplars": [payload]}).is_success
        assert client.post(ANCHORS, json=anchor_payload()).is_success
        assert client.get(EXEMPLARS + "/voice-1").json()["text"] == TEXT
        assert client.post("/turns/turn-1/receipts", json=receipt_payload("partial", 12)).is_success
        assert client.get(EXEMPLARS).json() == []
        assert not client.get(EXEMPLARS + "/voice-1").is_success
        assert client.get(ANCHORS + "/anchor-1").json()["text"] == TEXT[:12]
        assert [v["text"] for v in client.get(ANCHORS).json()] == [TEXT[:12]]


def test_parallel_initial_sets_share_one_completed_check(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    gateway, reviewer = VoiceGateway(), VoiceReviewer()
    with make_client(gateway, reviewer) as client:
        register_identity(client)
    barrier = Barrier(2)

    def register(identifier: str) -> None:
        with make_client(gateway, reviewer) as client:
            barrier.wait(timeout=10)
            response = client.post(
                EXEMPLARS, json={"exemplars": [exemplar_payload() | {"exemplar_id": identifier}]}
            )
            assert response.is_success

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(register, identifier) for identifier in ("one", "two")]
        for future in futures:
            future.result(timeout=20)
    with make_client(gateway, reviewer) as client:
        assert {v["exemplar_id"] for v in client.get(EXEMPLARS).json()} == {"one", "two"}
    assert len(gateway.voice_calls) == len(reviewer.voice_calls) == 1
