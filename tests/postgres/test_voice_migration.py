import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from tests.evidence_support import RECORDED, session_payload, turn_payload
from tests.it1.voice_fakes import VoiceGateway, VoiceReviewer

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("previous_head", [False, True], ids=["empty", "0004-with-data"])
def test_voice_revision_upgrade_repeat_downgrade_and_reapply_preserve_existing_data(
    monkeypatch: pytest.MonkeyPatch,
    previous_head: bool,
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
                command.upgrade(config, "0004")
                with TestClient(
                    create_app(
                        clock=lambda: RECORDED,
                        llm_gateway=VoiceGateway(),
                        reviewer=VoiceReviewer(),
                    )
                ) as client:
                    assert client.post(
                        "/characters/char-a/core-identities",
                        json={
                            "values": ["合成価値"],
                            "prohibitions": [],
                        },
                    ).is_success
                    assert client.post("/sessions", json=session_payload()).is_success
                    assert client.post(
                        "/turns",
                        json=turn_payload()
                        | {
                            "actor": "char-a",
                            "perceived_by": ["char-a"],
                            "kind": "thought",
                        },
                    ).is_success
                before = {
                    table: conn.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ("sessions", "turns", "core_identities")
                }
            command.upgrade(config, "head")
            command.upgrade(config, "head")
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0005",)
            for table, rows in before.items():
                assert conn.execute(f"SELECT * FROM {table}").fetchall() == rows
            for table in ("voice_exemplars", "verbatim_anchors"):
                assert conn.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (table,)
            command.downgrade(config, "0004")
            for table in ("voice_exemplars", "verbatim_anchors"):
                assert conn.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (None,)
            for table, rows in before.items():
                assert conn.execute(f"SELECT * FROM {table}").fetchall() == rows
            command.upgrade(config, "head")
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0005",)
            for table, rows in before.items():
                assert conn.execute(f"SELECT * FROM {table}").fetchall() == rows
        finally:
            # 0004 intentionally refuses to discard thought rows on downgrade.
            if conn.execute("SELECT to_regclass('turns')").fetchone() != (None,):
                head = conn.execute("SELECT version_num FROM alembic_version").fetchone()
                if head == ("0005",):
                    command.downgrade(config, "0004")
                conn.execute("TRUNCATE delivery_receipts, turns")
            command.downgrade(config, "base")
            assert (
                conn.execute("SELECT extname FROM pg_extension WHERE extname='vector'").fetchall()
                == []
            )
            conn.execute("DROP TABLE IF EXISTS alembic_version")
