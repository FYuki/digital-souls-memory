import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from tests.evidence_support import session, turn
from tests.postgres.migration_evidence import seed_evidence_before_actor_registry

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("previous_head", [False, True], ids=["empty", "0005-with-data"])
def test_memory_revision_repeat_downgrade_reapply_preserves_previous_data(
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
                command.upgrade(config, "0005")
                seed_evidence_before_actor_registry(
                    url,
                    characters=("char-c",),
                    session=session(reports_delivery=False),
                    turns=(turn(kind="observation", perceived_by=("char-c",)),),
                )
                before = {
                    table: conn.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ("sessions", "turns", "core_identities")
                }
            command.upgrade(config, "0006")
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0006",)
            command.upgrade(config, "0006")
            for table, rows in before.items():
                assert conn.execute(f"SELECT * FROM {table}").fetchall() == rows
            command.downgrade(config, "0005")
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0005",)
            for table, rows in before.items():
                assert conn.execute(f"SELECT * FROM {table}").fetchall() == rows
            command.upgrade(config, "0006")
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0006",)
            for table, rows in before.items():
                assert conn.execute(f"SELECT * FROM {table}").fetchall() == rows
        finally:
            # The prior perception revision intentionally refuses populated observations.
            if conn.execute("SELECT to_regclass('public.turns')").fetchone() != (None,):
                command.downgrade(config, "0005")
                conn.execute("TRUNCATE voice_exemplars, verbatim_anchors, delivery_receipts, turns")
            command.downgrade(config, "base")
            assert (
                conn.execute("SELECT extname FROM pg_extension WHERE extname='vector'").fetchall()
                == []
            )
            conn.execute("DROP TABLE IF EXISTS alembic_version")
