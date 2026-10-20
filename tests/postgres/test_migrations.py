import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = pytest.mark.postgres


def test_evidence_revision_is_the_single_successor_of_phase_zero() -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0002"]
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
                command.upgrade(config, "0001")
                connection.execute("CREATE TABLE synthetic_existing (value vector(3))")
                connection.execute("INSERT INTO synthetic_existing VALUES ('[1,2,3]')")
            command.upgrade(config, "head")
            assert connection.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone() == ("0.8.2",)
            assert connection.execute(
                "SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector"
            ).fetchone() == (1.0,)
            assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
                ("0002",)
            ]
            for table in ("sessions", "turns", "delivery_receipts"):
                assert connection.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (table,)
            command.upgrade(config, "head")
            if previous_head:
                assert connection.execute(
                    "SELECT value::text FROM synthetic_existing"
                ).fetchall() == [("[1,2,3]",)]
            command.downgrade(config, "0001")
            assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
                ("0001",)
            ]
            for table in ("sessions", "turns", "delivery_receipts"):
                assert connection.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (None,)
            command.upgrade(config, "head")
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
