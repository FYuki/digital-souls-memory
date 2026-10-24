"""ST initialization and audit follow the migration scripts without a real database."""

import os
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg.sql import Composable

from tests.st import test_functional

pytestmark = pytest.mark.ut

ROOT = Path(__file__).parents[2]
ADMIN_URL = "postgresql://memory_st_admin@synthetic.invalid/memory_core_st"
VERSION_QUERY = "SELECT version_num FROM alembic_version"
EMPTY_QUERY = "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"


@pytest.fixture
def initialize(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    monkeypatch.setenv("MEMORY_CORE_ST_ADMIN_URL", ADMIN_URL)
    # initialize writes this environment variable directly; register its restoration first.
    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql://synthetic.invalid/unused")
    return cast(Callable[[], None], runpy.run_path(str(ROOT / "tools/core_st_db.py"))["initialize"])


@pytest.fixture(params=["repository", "synthetic"])
def script_head(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "synthetic":
        monkeypatch.setattr(ScriptDirectory, "get_current_head", lambda self: "synthetic-next")
        return "synthetic-next"
    head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    if head is None:
        raise RuntimeError("The repository must have a migration head")
    return head


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.version = "0010"
    connection.table_count = 0

    def execute(query: str | Composable) -> MagicMock:
        cursor = MagicMock()
        if query == EMPTY_QUERY:
            cursor.fetchone.return_value = (connection.table_count,)
        elif query == VERSION_QUERY:
            cursor.fetchone.return_value = (connection.version,)
        elif query == ("SELECT pg_has_role('memory_st_app','memory_deletion_executor','MEMBER')"):
            cursor.fetchone.return_value = (False,)
        elif query == (
            "SELECT pg_has_role('memory_st_app','memory_deletion_executor','MEMBER'), "
            "pg_has_role('memory_st_delete','memory_deletion_executor','MEMBER')"
        ):
            cursor.fetchone.return_value = (False, True)
        elif isinstance(query, Composable) or query == (
            "GRANT memory_deletion_executor TO memory_st_delete"
        ):
            cursor.fetchone.return_value = None
        else:
            raise RuntimeError(f"Unexpected synthetic SQL: {query}")
        return cursor

    connection.execute.side_effect = execute
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    return connection


@pytest.fixture
def upgrade(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    migration = MagicMock()
    monkeypatch.setattr(command, "upgrade", migration)
    return migration


def test_initialize_accepts_script_head_and_upgrades_head(
    initialize: Callable[[], None], database: MagicMock, upgrade: MagicMock, script_head: str
) -> None:
    database.version = script_head
    initialize()
    upgrade.assert_called_once()
    assert upgrade.call_args.args[1] == "head"
    assert os.environ["MEMORY_DATABASE_URL"] == ADMIN_URL
    database.execute.assert_any_call(VERSION_QUERY)


@pytest.mark.usefixtures("upgrade", "script_head")
def test_initialize_rejects_old_revision(
    initialize: Callable[[], None],
    database: MagicMock,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    database.version = "0010"
    with rejected():
        initialize()
    database.execute.assert_any_call(VERSION_QUERY)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://memory_st_admin@synthetic.invalid/other_database",
        "postgresql://other_user@synthetic.invalid/memory_core_st",
    ],
)
@pytest.mark.usefixtures("database")
def test_initialize_refuses_non_dedicated_target_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
    initialize: Callable[[], None],
    upgrade: MagicMock,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
    url: str,
) -> None:
    monkeypatch.setenv("MEMORY_CORE_ST_ADMIN_URL", url)
    with rejected():
        initialize()
    cast(MagicMock, psycopg.connect).assert_not_called()
    upgrade.assert_not_called()


def test_initialize_refuses_nonempty_database_before_migration(
    initialize: Callable[[], None],
    database: MagicMock,
    upgrade: MagicMock,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    database.table_count = 1
    with rejected():
        initialize()
    database.execute.assert_called_once_with(EMPTY_QUERY)
    upgrade.assert_not_called()


class HTTPSetupReached(Exception):
    """Stop the real ST function at its HTTP boundary."""


@pytest.fixture
def http_setup(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setenv("MEMORY_CORE_ST_AUDIT_URL", ADMIN_URL)
    setup = MagicMock(side_effect=HTTPSetupReached)
    monkeypatch.setattr(test_functional, "new_client", setup)
    return setup


def test_functional_audit_accepts_script_head_before_http(
    database: MagicMock, script_head: str, http_setup: MagicMock
) -> None:
    database.version = script_head
    with pytest.raises(HTTPSetupReached):
        test_functional.test_phase_one_http_lifecycle()
    http_setup.assert_called_once_with()


@pytest.mark.usefixtures("script_head")
def test_functional_audit_rejects_old_revision_before_http(
    database: MagicMock,
    http_setup: MagicMock,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    database.version = "0010"
    with rejected():
        test_functional.test_phase_one_http_lifecycle()
    database.execute.assert_any_call(VERSION_QUERY)
    http_setup.assert_not_called()
