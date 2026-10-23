"""Initialize only an empty, dedicated synthetic ST database."""

import os

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url


def initialize() -> None:
    url = os.environ["MEMORY_CORE_ST_ADMIN_URL"]
    parsed = make_url(url)
    if parsed.database != "memory_core_st" or parsed.username != "memory_st_admin":
        raise ValueError("Only the dedicated synthetic ST database is allowed")
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        if conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        ).fetchone() != (0,):
            raise ValueError("ST database must be empty")
    os.environ["MEMORY_DATABASE_URL"] = url
    command.upgrade(Config("alembic.ini"), "head")
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        for role in ("memory_st_app", "memory_st_delete"):
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD 'synthetic-only' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE"
                ).format(sql.Identifier(role))
            )
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            conn.execute(
                sql.SQL(
                    "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {}"
                ).format(sql.Identifier(role))
            )
            conn.execute(
                sql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                    sql.Identifier(role)
                )
            )
        conn.execute("GRANT memory_deletion_executor TO memory_st_delete")
        assert conn.execute(
            "SELECT pg_has_role('memory_st_app','memory_deletion_executor','MEMBER')"
        ).fetchone() == (False,)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0010",)


if __name__ == "__main__":
    initialize()
