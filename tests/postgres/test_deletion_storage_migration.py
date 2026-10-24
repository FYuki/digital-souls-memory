"""Upgrade the explicitly supported synthetic 0008 data, never a runtime legacy fallback."""

import os
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.memory_records import candidate_record, memory_record
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import RECORDED, TEXT, session, turn
from tests.memory_support import memory
from tests.postgres.migration_evidence import seed_evidence_before_actor_registry

pytestmark = pytest.mark.postgres


def test_deletion_migration_upgrades_empty_database_and_reapplies_after_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.environ["MEMORY_TEST_DATABASE_URL"]
    monkeypatch.setenv("MEMORY_DATABASE_URL", url)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        try:
            for _ in range(2):
                command.upgrade(config, "0009")
                command.upgrade(config, "0009")
                assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
                    "0009",
                )
                command.downgrade(config, "0008")
                assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
                    "0008",
                )
        finally:
            command.downgrade(config, "base")
            assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == []
            conn.execute("DROP TABLE IF EXISTS alembic_version")


@pytest.mark.parametrize("sources", [("turn-2", "turn-1"), ("turn-2",)])
@pytest.mark.parametrize(
    "content",
    [
        "turn-1という文字列",
        '引用 {"source_turn_ids":["turn-1"]}',
        '```json\n{"nested":{"source_turn_ids":["turn-1"]}}\n```',
        '閉じていないフェンス\n```json\n{"source_turn_ids":["turn-1"]}',
    ],
)
def test_upgrade_restores_payloads_from_direct_sources_and_preserves_literal_content(
    monkeypatch: pytest.MonkeyPatch,
    sources: tuple[str, ...],
    content: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    url = os.environ["MEMORY_TEST_DATABASE_URL"]
    monkeypatch.setenv("MEMORY_DATABASE_URL", url)
    monkeypatch.delenv("MEMORY_EMBEDDING_ENDPOINT", raising=False)
    monkeypatch.delenv("MEMORY_RERANKER_ENDPOINT", raising=False)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    value = replace(memory(), source_turn_ids=sources, content=content)
    payload = candidate_record(value) | {"source_turn_ids": list(sources)}
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        try:
            command.upgrade(config, "0008")
            seed_evidence_before_actor_registry(
                url,
                characters=("char-b", "char-c"),
                session=session(reports_delivery=False),
                turns=tuple(
                    turn(turn_id=i, text="削除する合成原文" if i == "turn-1" else TEXT)
                    for i in ("turn-1", "turn-2")
                ),
            )
            record = memory_record(value)
            for key in ("epistemics", "identity_links"):
                record[key] = Jsonb(record[key])
            with conn.transaction():
                conn.execute(
                    sql.SQL("INSERT INTO derived_memories ({}) VALUES ({})").format(
                        sql.SQL(",").join(map(sql.Identifier, record)),
                        sql.SQL(",").join(sql.Placeholder() for _ in record),
                    ),
                    tuple(record.values()),
                )
                for position, identifier in enumerate(sources):
                    conn.execute(
                        "INSERT INTO memory_sources VALUES (%s,%s,%s,%s)",
                        (value.character_id, value.id, identifier, position),
                    )
                conn.execute(
                    "INSERT INTO memory_extractions(character_id,turn_id,delivery_basis,"
                    "signature,status,deadline,used_text,receipt_version) "
                    "VALUES (%s,'turn-2','declared_full','synthetic','done',%s,%s,0)",
                    (value.character_id, RECORDED, TEXT),
                )
                conn.execute(
                    "INSERT INTO memory_baselines VALUES (%s,%s,%s)",
                    (value.character_id, value.id, Jsonb(payload)),
                )
                for table in ("memory_candidates", "memory_generated"):
                    conn.execute(
                        sql.SQL(
                            "INSERT INTO {}(character_id,memory_id,turn_id,payload) "
                            "VALUES (%s,%s,'turn-2',%s)"
                        ).format(sql.Identifier(table)),
                        (value.character_id, value.id, Jsonb(payload)),
                    )
            evidence_before = conn.execute("SELECT * FROM turns ORDER BY turn_id").fetchall()
            for _ in range(2):
                command.upgrade(config, "0009")
                assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
                    "0009",
                )
                assert (
                    conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='memory_extractions' "
                        "AND column_name='used_text'"
                    ).fetchall()
                    == []
                )
                assert conn.execute(
                    "SELECT text_hash,used_end FROM memory_extractions"
                ).fetchone() == (sha256(TEXT.encode("utf-8")).hexdigest(), len(TEXT))
                restored_state = PostgresMemoryStore(url).get_extraction("char-b", "turn-2")
                assert restored_state is not None
                assert restored_state.text_hash == sha256(TEXT.encode("utf-8")).hexdigest()
                assert restored_state.used_end == len(TEXT)
                for table in ("memory_candidates", "memory_generated", "memory_baselines"):
                    rows = conn.execute(
                        sql.SQL("SELECT payload FROM {}").format(sql.Identifier(table))
                    ).fetchall()
                    assert len(rows) == 1
                    assert rows[0][0]["content"] == value.content
                    assert "source_turn_ids" not in rows[0][0]
                with PostgresMemoryStore(url).transaction(value.character_id) as tx:
                    assert tx.baselines() == (value,)
                    assert tuple(item.memory for item in tx.candidates()) == (value,)
                    # The generated collision check must also restore normalized provenance.
                    tx.set_candidates("turn-2", (value,), dependencies=())
                assert conn.execute("SELECT * FROM turns ORDER BY turn_id").fetchall() == (
                    evidence_before
                )
                conn.execute("UPDATE memory_extractions SET text_hash=repeat('0',64)")
                with rejected():
                    command.downgrade(config, "0008")
                assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
                    "0009",
                )
                assert conn.execute("SELECT text_hash FROM memory_extractions").fetchone() == (
                    "0" * 64,
                )
                conn.execute(
                    "UPDATE memory_extractions SET text_hash=%s",
                    (sha256(TEXT.encode("utf-8")).hexdigest(),),
                )
                command.downgrade(config, "0008")
                for table in ("memory_candidates", "memory_generated", "memory_baselines"):
                    assert conn.execute(
                        sql.SQL("SELECT payload FROM {}").format(sql.Identifier(table))
                    ).fetchone() == (payload,)
                assert conn.execute("SELECT used_text FROM memory_extractions").fetchone() == (
                    TEXT,
                )
            command.upgrade(config, "head")
            monkeypatch.setenv("MEMORY_AUTHOR_ID", "synthetic-author")
            monkeypatch.setenv("MEMORY_AUTHOR_TOKEN", "synthetic-test-credential")
            monkeypatch.setenv("MEMORY_DELETION_DATABASE_URL", url)
            replacement = replace(value, source_turn_ids=("turn-2",), content="残存原文の派生内容")
            gateway = RecordingRecalculationGateway(replacement)
            with TestClient(
                create_app(recalculation_gateway=gateway, clock=lambda: RECORDED)
            ) as client:
                response = client.post(
                    "/deletion-requests",
                    headers={"Authorization": "Bearer synthetic-test-credential"},
                    json={"request_id": "delete-migrated", "target_turn_ids": ["turn-1"]},
                )
                assert response.status_code == 200
                assert response.json()["cascade_result"]["deleted_turn_ids"] == ["turn-1"]
                assert client.get("/turns/turn-1").status_code == 404
            expected = replacement if "turn-1" in sources else value
            assert PostgresMemoryStore(url).get_memory(value.character_id, value.id) == expected
            for table in ("memory_candidates", "memory_generated", "memory_baselines"):
                rows = conn.execute(
                    sql.SQL("SELECT payload FROM {}").format(sql.Identifier(table))
                ).fetchall()
                assert len(rows) == 1
                assert all(row[0]["content"] == expected.content for row in rows)
                assert all("source_turn_ids" not in row[0] for row in rows)
            if "turn-1" in sources:
                assert gateway.calls
                for model_input, identifier, kind in gateway.calls:
                    assert tuple(item.turn_id for item in model_input.turns) == ("turn-2",)
                    assert identifier == value.id
                    assert kind == value.kind
            else:
                assert gateway.calls == []
            command.downgrade(config, "0008")
            assert conn.execute("SELECT turn_id FROM turns ORDER BY turn_id").fetchall() == [
                ("turn-2",)
            ]
            for table in ("memory_candidates", "memory_generated", "memory_baselines"):
                rows = conn.execute(
                    sql.SQL("SELECT payload FROM {}").format(sql.Identifier(table))
                ).fetchall()
                assert all(row[0]["source_turn_ids"] == ["turn-2"] for row in rows)
                assert all(row[0]["content"] == expected.content for row in rows)
            command.upgrade(config, "0009")
            assert PostgresMemoryStore(url).get_memory(value.character_id, value.id) == expected
        finally:
            command.downgrade(config, "0005")
            conn.execute("TRUNCATE voice_exemplars, verbatim_anchors, delivery_receipts, turns")
            command.downgrade(config, "base")
            conn.execute("DROP TABLE IF EXISTS alembic_version")
