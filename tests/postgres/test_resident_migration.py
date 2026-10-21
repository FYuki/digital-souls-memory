import os
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.api import create_app
from tests.evidence_support import session, turn

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("previous_head", [False, True], ids=["empty", "0007-with-data"])
def test_resident_revision_roundtrip_preserves_existing_evidence(
    monkeypatch: pytest.MonkeyPatch, previous_head: bool
) -> None:
    url = os.environ["MEMORY_TEST_DATABASE_URL"]
    monkeypatch.setenv("MEMORY_DATABASE_URL", url)
    monkeypatch.delenv("MEMORY_EMBEDDING_ENDPOINT", raising=False)
    monkeypatch.delenv("MEMORY_RERANKER_ENDPOINT", raising=False)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0008"]
    revision = scripts.get_revision("0008")
    assert revision is not None and revision.down_revision == "0007"
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as connection:
        with TestClient(create_app()) as client:
            assert client.get("/health").is_success
        assert (
            connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            ).fetchall()
            == []
        )
        try:
            before: dict[str, list[tuple[object, ...]]] = {}
            if previous_head:
                command.upgrade(config, "0007")
                evidence = PostgresEvidenceStore(url)
                evidence.add_session(replace(session(reports_delivery=False), ended_at=None))
                evidence.add_turn(turn(actor="synthetic-user", perceived_by=()))
                before = {
                    table: connection.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ("sessions", "turns", "delivery_receipts")
                }
            command.upgrade(config, "head")
            command.upgrade(config, "head")
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0008",
            )
            for target in ("0007", "head"):
                if target == "0007":
                    command.downgrade(config, target)
                else:
                    command.upgrade(config, target)
                for table, rows in before.items():
                    assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
                assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                    "0007" if target == "0007" else "0008",
                )
        finally:
            if connection.execute("SELECT to_regclass('public.turns')").fetchone() != (None,):
                command.downgrade(config, "0005")
                connection.execute(
                    "TRUNCATE voice_exemplars, verbatim_anchors, delivery_receipts, turns"
                )
            command.downgrade(config, "base")
            assert (
                connection.execute(
                    "SELECT extname FROM pg_extension WHERE extname='vector'"
                ).fetchall()
                == []
            )
            connection.execute("DROP TABLE IF EXISTS alembic_version")
