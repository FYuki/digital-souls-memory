import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.postgres


def test_upgrade_and_downgrade_on_empty_database(monkeypatch: pytest.MonkeyPatch) -> None:
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

        command.upgrade(config, "head")
        assert connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone() == ("0.8.2",)
        assert connection.execute("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector").fetchone() == (
            1.0,
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            ("0001",)
        ]

        # A second upgrade is a no-op; downgrade must remove the extension without CASCADE.
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        assert (
            connection.execute(
                "SELECT extname FROM pg_extension WHERE extname = 'vector'"
            ).fetchall()
            == []
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == []
