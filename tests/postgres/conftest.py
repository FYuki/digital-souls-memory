import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url


@pytest.fixture
def database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    url = os.environ["MEMORY_TEST_DATABASE_URL"]
    monkeypatch.setenv("MEMORY_DATABASE_URL", url)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    try:
        command.upgrade(config, "head")
        yield url
    finally:
        # Application tests may select a temporary nonowner role; migrations use the owner.
        monkeypatch.setenv("MEMORY_DATABASE_URL", url)
        # 0014 cannot reconstruct absorption history once contributions exist.
        # Discard synthetic derived data before exercising an empty downgrade.
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as connection:
            if connection.execute("SELECT to_regclass('public.derived_memories')").fetchone() != (
                None,
            ):
                connection.execute("TRUNCATE derived_memories CASCADE")
        # Remove derived references before truncating the append-only evidence tables.
        command.downgrade(config, "0005")
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as connection:
            if connection.execute("SELECT to_regclass('public.turns')").fetchone() != (None,):
                connection.execute(
                    "TRUNCATE voice_exemplars, verbatim_anchors, delivery_receipts, turns"
                )
        command.downgrade(config, "base")
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as connection:
            connection.execute("DROP TABLE IF EXISTS alembic_version")


@pytest.fixture
def app_database_url(database_url: str) -> Iterator[str]:
    role = "evidence_test_" + uuid4().hex
    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                sql.Identifier(role)
            )
        )
        try:
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role))
            )
            # Grant mutation privileges so the selected trigger defense is actually exercised.
            connection.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                ).format(sql.Identifier(role))
            )
            connection.execute(
                sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                    sql.Identifier(role)
                )
            )
            yield make_url(database_url).set(username=role).render_as_string(hide_password=False)
        finally:
            connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
