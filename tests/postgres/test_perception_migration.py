"""Synthetic migration contracts; the legacy ID roles below are explicit fixtures."""

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb
from sqlalchemy.exc import IntegrityError

from tests.evidence_support import OCCURRED, RECORDED, TEXT
from tests.identity_support import aspects

pytestmark = pytest.mark.postgres

# No inference from prefixes: all IDs used by the legacy fixture are classified here.
ID_ROLES = {"char-a": "character", "char-b": "character", "char-c": "character", "viewer": "user"}


@pytest.fixture
def migration_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Config]]:
    url = os.environ["MEMORY_TEST_DATABASE_URL"]
    monkeypatch.setenv("MEMORY_DATABASE_URL", url)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    try:
        command.upgrade(config, "0003")
        yield url, config
    finally:
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
            conn.execute("TRUNCATE delivery_receipts, turns")
        command.downgrade(config, "base")
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
            conn.execute("DROP TABLE alembic_version")


def seed_legacy(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    for identifier, role in ID_ROLES.items():
        if role == "character":
            conn.execute(
                "INSERT INTO core_identities VALUES (%s, %s, %s, 1, %s)",
                (identifier, ["誠実"], [], RECORDED),
            )
    conn.execute("INSERT INTO temperaments VALUES ('char-a', '合成気質', 1, %s)", (RECORDED,))
    conn.execute(
        "INSERT INTO personality_profiles VALUES ('char-a', %s, '合成人格', "
        "'v1', 1, 1, %s, NULL, ARRAY[]::text[], 'author_seed')",
        (Jsonb(aspects()), RECORDED),
    )
    for sid, participants in [("present", ["viewer", "char-a", "char-b"]), ("empty", [])]:
        conn.execute(
            "INSERT INTO sessions VALUES (%s, %s, NULL, 'synthetic', %s)",
            (sid, OCCURRED, participants),
        )
    for tid, sid, actor, audience in [
        ("explicit", "present", "char-c", ["viewer", "char-b"]),
        ("fallback", "present", "char-c", []),
        ("alone", "empty", "char-c", []),
        ("absent", "empty", "viewer", []),
        ("included", "present", "char-b", ["char-b", "viewer", "char-a"]),
    ]:
        conn.execute(
            "INSERT INTO turns VALUES (%s, %s, 'synthetic', %s, "
            "'observation', %s, %s, 'ja', %s, 'synthetic', 'v1', %s, %s)",
            (tid, sid, actor, TEXT, audience, Jsonb({"audience": "引用"}), OCCURRED, RECORDED),
        )
    conn.execute(
        "INSERT INTO delivery_receipts (turn_id, status, spoken_until, "
        "reported_by, reported_at) VALUES ('explicit', 'partial', 1, "
        "'synthetic-window', %s)",
        (RECORDED,),
    )


def snapshot(conn: psycopg.Connection[tuple[object, ...]]) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in (
            "sessions",
            "turns",
            "delivery_receipts",
            "core_identities",
            "temperaments",
            "personality_profiles",
            "alembic_version",
        )
    }


def assert_immutable(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    for table in ("sessions", "turns", "delivery_receipts"):
        for statement in (
            f"UPDATE {table} SET reported_by = 'changed'"
            if table == "delivery_receipts"
            else f"UPDATE {table} SET surface = 'changed'",
            f"DELETE FROM {table}",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_upgrade_backfills_explicit_roles_and_preserves_other_values(
    migration_database: tuple[str, Config],
) -> None:
    url, config = migration_database
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        seed_legacy(conn)
        before = snapshot(conn)
        unchanged = conn.execute(
            "SELECT turn_id, session_id, surface, actor, kind, text, "
            "language, register, sensitivity, classifier_version, "
            "occurred_at, recorded_at FROM turns ORDER BY turn_id"
        ).fetchall()
        command.upgrade(config, "0004")
        command.upgrade(config, "0004")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("0004",)]
        assert conn.execute(
            "SELECT session_id, reports_delivery FROM sessions ORDER BY session_id"
        ).fetchall() == [("empty", True), ("present", True)]
        assert conn.execute(
            "SELECT turn_id, perceived_by, addressees, address_mode, "
            "reply_to_turn_id FROM turns ORDER BY turn_id"
        ).fetchall() == [
            ("absent", [], [], "broadcast", None),
            ("alone", ["char-c"], [], "broadcast", None),
            ("explicit", ["char-b", "char-c"], [], "broadcast", None),
            ("fallback", ["char-a", "char-b", "char-c"], [], "broadcast", None),
            ("included", ["char-b", "char-a"], [], "broadcast", None),
        ]
        assert (
            conn.execute(
                "SELECT turn_id, session_id, surface, actor, kind, text, "
                "language, register, sensitivity, classifier_version, "
                "occurred_at, recorded_at FROM turns ORDER BY turn_id"
            ).fetchall()
            == unchanged
        )
        after = snapshot(conn)
        for table in (
            "delivery_receipts",
            "core_identities",
            "temperaments",
            "personality_profiles",
        ):
            assert after[table] == before[table]
        assert_immutable(conn)


@pytest.mark.parametrize("actor", ["char-a", "unclassified"])
def test_upgrade_excludes_unregistered_ids_without_aborting(
    migration_database: tuple[str, Config],
    actor: str,
) -> None:
    url, config = migration_database
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        conn.execute(
            "INSERT INTO sessions VALUES ('unknown', %s, NULL, 'synthetic', %s)",
            (OCCURRED, ["char-a", "unclassified", "viewer"]),
        )
        for tid, audience in [("explicit", ["unclassified", "char-a"]), ("fallback", [])]:
            conn.execute(
                "INSERT INTO turns VALUES (%s, 'unknown', 'synthetic', %s, "
                "'observation', %s, %s, 'ja', '{}'::jsonb, 'synthetic', 'v1', %s, %s)",
                (tid, actor, TEXT, audience, OCCURRED, RECORDED),
            )
        conn.execute(
            "INSERT INTO delivery_receipts (turn_id, status, spoken_until, "
            "reported_by, reported_at) VALUES ('explicit', 'partial', 1, "
            "'synthetic-window', %s)",
            (RECORDED,),
        )
        command.upgrade(config, "0004")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("0004",)]
        assert conn.execute(
            "SELECT turn_id, perceived_by FROM turns ORDER BY turn_id"
        ).fetchall() == [("explicit", []), ("fallback", [])]
        assert_immutable(conn)


@pytest.mark.parametrize("registration", ["core", "temperament", "both"])
def test_upgrade_uses_identity_registration_for_perception_and_actor(
    migration_database: tuple[str, Config], registration: str
) -> None:
    url, config = migration_database
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        for identifier in ("subject-42", "subject-99"):
            if registration in ("core", "both"):
                conn.execute(
                    "INSERT INTO core_identities VALUES (%s, %s, %s, 1, %s)",
                    (identifier, ["誠実"], [], RECORDED),
                )
            if registration in ("temperament", "both"):
                conn.execute(
                    "INSERT INTO temperaments VALUES (%s, '合成気質', 1, %s)",
                    (identifier, RECORDED),
                )
        conn.execute(
            "INSERT INTO sessions VALUES ('registered', %s, NULL, 'synthetic', %s)",
            (OCCURRED, ["subject-99", "viewer", "subject-42"]),
        )
        for tid, actor, audience in [
            ("explicit", "subject-42", ["viewer", "subject-99"]),
            ("fallback", "subject-42", []),
            ("included", "subject-42", ["subject-42", "viewer", "subject-99"]),
            ("user-actor", "viewer", ["subject-99", "viewer", "subject-42"]),
        ]:
            conn.execute(
                "INSERT INTO turns VALUES (%s, 'registered', 'synthetic', %s, "
                "'observation', %s, %s, 'ja', '{}'::jsonb, 'synthetic', 'v1', %s, %s)",
                (tid, actor, TEXT, audience, OCCURRED, RECORDED),
            )
        before = snapshot(conn)
        command.upgrade(config, "0004")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("0004",)]
        assert conn.execute(
            "SELECT turn_id, perceived_by FROM turns ORDER BY turn_id"
        ).fetchall() == [
            ("explicit", ["subject-99", "subject-42"]),
            ("fallback", ["subject-99", "subject-42"]),
            ("included", ["subject-42", "subject-99"]),
            ("user-actor", ["subject-99", "subject-42"]),
        ]
        after = snapshot(conn)
        for table in ("core_identities", "temperaments", "personality_profiles"):
            assert after[table] == before[table]


@pytest.mark.parametrize("actor, expected", [("viewer", []), ("char-c", ["char-c"])])
def test_upgrade_does_not_fall_back_from_nonempty_user_only_audience(
    migration_database: tuple[str, Config], actor: str, expected: list[str]
) -> None:
    url, config = migration_database
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        seed_legacy(conn)
        conn.execute(
            "INSERT INTO turns SELECT (jsonb_populate_record(NULL::turns, "
            "to_jsonb(t) || %s)).* FROM turns t WHERE turn_id = 'explicit'",
            (Jsonb({"turn_id": "user-only", "actor": actor, "audience": ["viewer"]}),),
        )
        command.upgrade(config, "0004")
        assert conn.execute(
            "SELECT perceived_by FROM turns WHERE turn_id = 'user-only'"
        ).fetchone() == (expected,)


def migration_schema(
    conn: psycopg.Connection[tuple[object, ...]],
) -> dict[str, list[tuple[object, ...]]]:
    return {
        "columns": conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable FROM "
            "information_schema.columns WHERE table_schema = 'public' "
            "ORDER BY table_name, ordinal_position"
        ).fetchall(),
        "triggers": conn.execute(
            "SELECT c.relname, t.tgname, t.tgenabled FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND NOT t.tgisinternal "
            "ORDER BY c.relname, t.tgname"
        ).fetchall(),
    }


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_failed_backfill_restores_data_schema_and_immutable_triggers(
    migration_database: tuple[str, Config], direction: str
) -> None:
    url, config = migration_database
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        seed_legacy(conn)
        if direction == "downgrade":
            command.upgrade(config, "0004")
        # Existing rows remain valid input; only the migration's UPDATE must fail.
        conn.execute("ALTER TABLE turns ADD CONSTRAINT backfill_failure CHECK (false) NOT VALID")
        try:
            before = snapshot(conn)
            schema_before = migration_schema(conn)
            assert_immutable(conn)
            with pytest.raises(IntegrityError) as failure:
                if direction == "upgrade":
                    command.upgrade(config, "0004")
                else:
                    command.downgrade(config, "0003")
            assert isinstance(failure.value.orig, psycopg.errors.CheckViolation)
            assert failure.value.orig.diag.constraint_name == "backfill_failure"
            assert snapshot(conn) == before
            assert migration_schema(conn) == schema_before
            assert_immutable(conn)
        finally:
            conn.execute("ALTER TABLE turns DROP CONSTRAINT backfill_failure")


def test_downgrade_copies_perception_and_loses_only_declared_fields(
    migration_database: tuple[str, Config],
) -> None:
    url, config = migration_database
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        seed_legacy(conn)
        command.upgrade(config, "0004")
        conn.execute(
            "INSERT INTO sessions (session_id, started_at, surface, "
            "reports_delivery) VALUES ('new', %s, 'synthetic', false)",
            (OCCURRED,),
        )
        conn.execute(
            "INSERT INTO turns SELECT (jsonb_populate_record(NULL::turns, "
            "to_jsonb(t) || %s)).* FROM turns t WHERE turn_id = 'explicit'",
            (
                Jsonb(
                    {
                        "turn_id": "new",
                        "session_id": "new",
                        "perceived_by": ["char-c", "char-a"],
                        "addressees": ["viewer"],
                        "address_mode": "direct",
                        "reply_to_turn_id": "explicit",
                    }
                ),
            ),
        )
        perception = conn.execute(
            "SELECT turn_id, perceived_by FROM turns ORDER BY turn_id"
        ).fetchall()
        command.downgrade(config, "0003")
        assert (
            conn.execute("SELECT turn_id, audience FROM turns ORDER BY turn_id").fetchall()
            == perception
        )
        assert conn.execute("SELECT participants FROM sessions").fetchall() == [([],), ([],), ([],)]
        assert_immutable(conn)
        command.upgrade(config, "0004")
        assert (
            conn.execute("SELECT turn_id, perceived_by FROM turns ORDER BY turn_id").fetchall()
            == perception
        )
        assert conn.execute(
            "SELECT DISTINCT addressees, address_mode, reply_to_turn_id FROM turns"
        ).fetchall() == [([], "broadcast", None)]
        assert conn.execute("SELECT DISTINCT reports_delivery FROM sessions").fetchall() == [
            (True,)
        ]
        assert_immutable(conn)


def test_thought_refuses_downgrade_before_any_schema_or_data_changes(
    migration_database: tuple[str, Config],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    url, config = migration_database
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        seed_legacy(conn)
        command.upgrade(config, "0004")
        conn.execute(
            "INSERT INTO turns SELECT (jsonb_populate_record(NULL::turns, "
            "to_jsonb(t) || %s)).* FROM turns t WHERE turn_id = 'explicit'",
            (Jsonb({"turn_id": "thought", "kind": "thought", "perceived_by": ["char-c"]}),),
        )
        before = snapshot(conn)
        columns = conn.execute(
            "SELECT table_name, column_name, data_type FROM "
            "information_schema.columns WHERE table_schema = 'public' ORDER "
            "BY table_name, ordinal_position"
        ).fetchall()
        with rejected():
            command.downgrade(config, "0003")
        assert snapshot(conn) == before
        assert (
            conn.execute(
                "SELECT table_name, column_name, data_type FROM "
                "information_schema.columns WHERE table_schema = 'public' ORDER "
                "BY table_name, ordinal_position"
            ).fetchall()
            == columns
        )
        assert_immutable(conn)


def test_empty_database_upgrade_is_repeatable(migration_database: tuple[str, Config]) -> None:
    url, config = migration_database
    command.downgrade(config, "base")
    command.upgrade(config, "0004")
    command.upgrade(config, "0004")
    with psycopg.connect(url, connect_timeout=5) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("0004",)]
        assert conn.execute("SELECT count(*) FROM turns").fetchone() == (0,)
