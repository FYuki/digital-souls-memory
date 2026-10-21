import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app

pytestmark = pytest.mark.postgres


def test_recall_revision_is_the_single_successor_of_memory() -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0007"]
    recall = scripts.get_revision("0007")
    assert recall is not None
    assert recall.down_revision == "0006"
    memory = scripts.get_revision("0006")
    assert memory is not None
    assert memory.down_revision == "0005"
    voice = scripts.get_revision("0005")
    assert voice is not None
    assert voice.down_revision == "0004"
    perception = scripts.get_revision("0004")
    assert perception is not None
    assert perception.down_revision == "0003"
    identity = scripts.get_revision("0003")
    assert identity is not None
    assert identity.down_revision == "0002"
    revision = scripts.get_revision("0002")
    assert revision is not None
    assert revision.down_revision == "0001"


@pytest.mark.parametrize("previous_head", [False, True], ids=["empty", "previous-head"])
def test_upgrade_and_downgrade_on_empty_database(
    monkeypatch: pytest.MonkeyPatch, previous_head: bool
) -> None:
    # Required, dedicated synthetic DB. Never fall back to the runtime DB URL.
    database_url = os.environ["MEMORY_TEST_DATABASE_URL"]
    monkeypatch.setenv("MEMORY_DATABASE_URL", database_url)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as connection:
        with TestClient(create_app()) as client:
            assert client.get("/health").status_code == 200
        assert (
            connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
            == []
        )
        assert (
            connection.execute(
                "SELECT extname FROM pg_extension WHERE extname = 'vector'"
            ).fetchall()
            == []
        )

        try:
            if previous_head:
                command.upgrade(config, "0002")
                connection.execute("CREATE TABLE synthetic_existing (value vector(3))")
                connection.execute("INSERT INTO synthetic_existing VALUES ('[1,2,3]')")
                connection.execute(
                    "INSERT INTO sessions VALUES "
                    "('synthetic-session', '2026-01-01T00:00:00Z', NULL, "
                    "'synthetic-chat', ARRAY['char-a'])"
                )
                connection.execute(
                    "INSERT INTO turns VALUES ('synthetic-turn', 'synthetic-session', "
                    "'synthetic-chat', 'char-a', 'observation', '合成原文', ARRAY[]::text[], "
                    "'ja', '{}'::jsonb, 'synthetic', 'v1', "
                    "'2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')"
                )
                connection.execute(
                    "INSERT INTO delivery_receipts "
                    "(turn_id, status, spoken_until, reported_by, reported_at) "
                    "VALUES ('synthetic-turn', 'full', 4, 'synthetic-window', "
                    "'2026-01-02T00:00:00Z')"
                )
            evidence_before = (
                {
                    table: connection.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ("sessions", "turns", "delivery_receipts")
                }
                if previous_head
                else {}
            )
            command.upgrade(config, "0003")
            assert connection.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone() == ("0.8.2",)
            assert connection.execute(
                "SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector"
            ).fetchone() == (1.0,)
            assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
                ("0003",)
            ]
            for table in ("sessions", "turns", "delivery_receipts"):
                assert connection.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (table,)
            command.upgrade(config, "0003")
            for table in ("core_identities", "temperaments", "personality_profiles"):
                assert connection.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (table,)
            if previous_head:
                assert connection.execute(
                    "SELECT value::text FROM synthetic_existing"
                ).fetchall() == [("[1,2,3]",)]
            for table, rows in evidence_before.items():
                assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
            command.downgrade(config, "0002")
            assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
                ("0002",)
            ]
            for table in ("core_identities", "temperaments", "personality_profiles"):
                assert connection.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (None,)
            for table, rows in evidence_before.items():
                assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
            command.upgrade(config, "0003")
            assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
                ("0003",)
            ]
            for table, rows in evidence_before.items():
                assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
            command.downgrade(config, "0001")
            assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
                ("0001",)
            ]
            for table in ("sessions", "turns", "delivery_receipts"):
                assert connection.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (None,)
            command.upgrade(config, "0003")
            if previous_head:
                assert connection.execute(
                    "SELECT value::text FROM synthetic_existing"
                ).fetchall() == [("[1,2,3]",)]
        finally:
            connection.execute("DROP TABLE IF EXISTS synthetic_existing")
            command.downgrade(config, "base")
            try:
                assert (
                    connection.execute(
                        "SELECT extname FROM pg_extension WHERE extname = 'vector'"
                    ).fetchall()
                    == []
                )
                assert (
                    connection.execute("SELECT version_num FROM alembic_version").fetchall() == []
                )
            finally:
                connection.execute("DROP TABLE alembic_version")
