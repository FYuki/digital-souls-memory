import json
import os
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from tests.evidence_support import session, turn
from tests.memory_support import OLD_ID, memory

pytestmark = pytest.mark.postgres


def seed_memory_at_0006(url: str) -> dict[str, object]:
    """Freeze the old row contract independently of the current product serializer."""
    value = replace(memory(), subject="char-c")
    row: dict[str, object] = {
        "character_id": value.character_id,
        "id": value.id,
        "kind": value.kind,
        "content": value.content,
        "model": value.derivation.model,
        "prompt_version": value.derivation.prompt_version,
        "created_at": value.derivation.created_at,
        "valid_from": value.valid_from,
        "valid_to": value.valid_to,
        "recorded_at": value.recorded_at,
        "superseded_at": value.superseded_at,
        "last_reinforced_at": value.last_reinforced_at,
        "epistemics": json.loads(json.dumps(asdict(value.epistemics))),
        "identity_links": json.loads(json.dumps(asdict(value.identity_links))),
        "subject": value.subject,
        "predicate": value.predicate,
        "object": value.object,
        "shared_with": list(value.shared_with),
        "counterpart": value.counterpart,
        "affection": value.affection,
        "trust": value.trust,
        "status": value.status,
        "deadline": value.deadline,
        "verbatim_turn_id": value.verbatim_turn_id,
    }
    payload = {
        key: at.isoformat() if isinstance(at, datetime) else at for key, at in row.items()
    } | {"source_turn_ids": list(value.source_turn_ids)}
    with psycopg.connect(url, connect_timeout=5) as connection:
        connection.execute(
            sql.SQL("INSERT INTO derived_memories ({}) VALUES ({})").format(
                sql.SQL(",").join(map(sql.Identifier, row)),
                sql.SQL(",").join(sql.Placeholder() for _ in row),
            ),
            tuple(Jsonb(v) if isinstance(v, dict) else v for v in row.values()),
        )
        connection.execute(
            "INSERT INTO memory_sources(character_id,memory_id,turn_id,position) "
            "VALUES (%s,%s,'turn-1',0)",
            (value.character_id, value.id),
        )
        connection.execute(
            "INSERT INTO memory_baselines(character_id,memory_id,payload) VALUES (%s,%s,%s)",
            (value.character_id, value.id, Jsonb(payload)),
        )
    return row


@pytest.mark.parametrize("previous_head", [False, True], ids=["empty", "0006-with-data"])
def test_recall_revision_preserves_evidence_and_memory_through_repeat_and_reapply(
    monkeypatch: pytest.MonkeyPatch, previous_head: bool
) -> None:
    url = os.environ["MEMORY_TEST_DATABASE_URL"]
    monkeypatch.setenv("MEMORY_DATABASE_URL", url)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        with TestClient(create_app()) as client:
            assert client.get("/health").is_success
        assert (
            conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
            == []
        )
        try:
            before: dict[str, list[tuple[object, ...]]] = {}
            if previous_head:
                command.upgrade(config, "0006")
                with TestClient(create_app(identity_store=PostgresIdentityStore(url))) as client:
                    for identifier in ("char-b", "char-c"):
                        assert client.post(
                            f"/characters/{identifier}/core-identities",
                            json={"values": ["合成価値"], "prohibitions": []},
                        ).is_success
                evidence = PostgresEvidenceStore(url)
                evidence.add_session(session(reports_delivery=False))
                evidence.add_turn(turn())
                old_row = seed_memory_at_0006(url)
                before = {
                    table: conn.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ("sessions", "turns", "core_identities", "memory_sources")
                }
            command.upgrade(config, "head")
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0007",)
            if previous_head:
                restored = PostgresMemoryStore(url).get_memory("char-b", OLD_ID)
                assert restored == replace(memory(), subject="char-c")
            assert {
                row[0]
                for row in conn.execute(
                    "SELECT extname FROM pg_extension WHERE extname IN ('vector','pg_bigm')"
                )
            } == {"vector", "pg_bigm"}
            assert conn.execute("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector").fetchone() == (
                1.0,
            )
            assert conn.execute("SELECT show_bigm('合成検索') IS NOT NULL").fetchone() == (True,)
            command.upgrade(config, "head")
            for target in ("0006", "head"):
                if target == "0006":
                    command.downgrade(config, target)
                else:
                    command.upgrade(config, target)
                assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
                    "0006" if target == "0006" else "0007",
                )
                for table, rows in before.items():
                    assert conn.execute(f"SELECT * FROM {table}").fetchall() == rows
                if previous_head:
                    with psycopg.connect(url, row_factory=dict_row, connect_timeout=5) as read:
                        row = read.execute(
                            sql.SQL("SELECT {} FROM derived_memories WHERE id=%s").format(
                                sql.SQL(",").join(map(sql.Identifier, old_row))
                            ),
                            (OLD_ID,),
                        ).fetchone()
                        assert row == old_row
                    if target == "head":
                        assert PostgresMemoryStore(url).get_memory("char-b", OLD_ID) == replace(
                            memory(), subject="char-c"
                        )
                        with PostgresMemoryStore(url).transaction("char-b") as tx:
                            assert tx.baselines() == (replace(memory(), subject="char-c"),)
        finally:
            if conn.execute("SELECT to_regclass('public.turns')").fetchone() != (None,):
                command.downgrade(config, "0005")
                conn.execute("TRUNCATE voice_exemplars, verbatim_anchors, delivery_receipts, turns")
            command.downgrade(config, "base")
            assert (
                conn.execute("SELECT extname FROM pg_extension WHERE extname='vector'").fetchall()
                == []
            )
            conn.execute("DROP TABLE IF EXISTS alembic_version")
