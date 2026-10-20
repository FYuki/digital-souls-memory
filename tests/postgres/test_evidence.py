from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.api import create_app
from digital_souls_memory.evidence.domain import EvidenceValidationError
from digital_souls_memory.evidence.ports import (
    EvidenceConflict,
    EvidenceNotFound,
    EvidenceUnavailable,
)
from digital_souls_memory.evidence.use_cases import EvidenceService
from tests.evidence_support import (
    INSTRUCTION,
    OCCURRED,
    RECORDED,
    TEXT,
    receipt,
    receipt_payload,
    session,
    session_payload,
    turn,
    turn_payload,
)

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def registered_characters(app_database_url: str) -> None:
    with TestClient(create_app(identity_store=PostgresIdentityStore(app_database_url))) as client:
        for identifier in ("char-a", "char-b", "char-c", "char-z"):
            assert (
                client.post(
                    f"/characters/{identifier}/core-identities",
                    json={"values": ["誠実"], "prohibitions": []},
                ).status_code
                == 201
            )


class TrackingStore(PostgresEvidenceStore):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.connections: list[psycopg.Connection[DictRow]] = []

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[DictRow]]:
        with super()._connection() as connection:
            self.connections.append(connection)
            yield connection


@pytest.mark.parametrize("operation", ["receipts", "turns"])
@pytest.mark.parametrize("finish", ["complete", "early", "error", "fetch-error"])
@pytest.mark.parametrize("entry", ["store", "service"])
def test_managed_lists_span_fetch_windows_and_close_connections(
    app_database_url: str, operation: str, finish: str, entry: str
) -> None:
    store = TrackingStore(app_database_url)
    store.add_session(session())
    store.add_turn(turn())
    for index in range(257):
        if operation == "receipts":
            store.add_receipt(replace(receipt("none", 0), reported_by=f"window-{index}"))
        elif index:
            store.add_turn(turn(f"turn-{index + 1:03}"))
    owner = EvidenceService(store, lambda: RECORDED) if entry == "service" else store
    source = (
        owner.list_receipts("turn-1")
        if operation == "receipts"
        else owner.list_perceived_turns("char-b")
    )

    def consume() -> None:
        with source as values:
            connection = store.connections[-1]
            assert not connection.closed
            if finish == "fetch-error":
                connection.close()
                next(values)
            elif finish == "error":
                next(values)
                raise ValueError("synthetic consumer failure")
            elif finish == "early":
                next(values)
            else:
                restored = list(values)
                assert len(restored) == 257
                if operation == "receipts":
                    assert restored == [
                        replace(receipt("none", 0), reported_by=f"window-{index}")
                        for index in range(257)
                    ]
                else:
                    assert restored == sorted(
                        [turn()] + [turn(f"turn-{index + 1:03}") for index in range(1, 257)],
                        key=lambda value: (value.occurred_at, value.recorded_at, value.turn_id),
                    )

    if finish == "error":
        with pytest.raises(ValueError):
            consume()
    elif finish == "fetch-error":
        with pytest.raises(EvidenceUnavailable):
            consume()
    else:
        consume()
    assert all(connection.closed for connection in store.connections)


@pytest.mark.parametrize(
    "first_occurred,first_recorded,second_occurred,second_recorded,expected",
    [
        (1, 0, 0, 1, ["turn-2", "turn-1"]),
        (0, 1, 0, 0, ["turn-2", "turn-1"]),
        (0, 0, 0, 0, ["turn-1", "turn-2"]),
    ],
    ids=["occurred-time", "recorded-time", "identifier"],
)
def test_turns_are_ordered_by_occurred_time_recorded_time_then_identifier(
    app_database_url: str,
    first_occurred: int,
    first_recorded: int,
    second_occurred: int,
    second_recorded: int,
    expected: list[str],
) -> None:
    store = PostgresEvidenceStore(app_database_url)
    store.add_session(session())
    store.add_session(replace(session("session-2"), surface="synthetic-diary"))
    store.add_turn(
        replace(
            turn("turn-2", "session-2"),
            surface="synthetic-diary",
            occurred_at=OCCURRED + timedelta(seconds=second_occurred),
            recorded_at=RECORDED + timedelta(seconds=second_recorded),
        )
    )
    store.add_turn(
        replace(
            turn("turn-1"),
            occurred_at=OCCURRED + timedelta(seconds=first_occurred),
            recorded_at=RECORDED + timedelta(seconds=first_recorded),
        )
    )
    with store.list_perceived_turns("char-b") as values:
        assert [value.turn_id for value in values] == expected


def seed(url: str) -> PostgresEvidenceStore:
    store = PostgresEvidenceStore(url)
    store.add_session(session())
    store.add_turn(turn())
    return store


def test_adapter_roundtrip_all_attributes_and_separate_times(app_database_url: str) -> None:
    store = seed(app_database_url)
    store.add_receipt(receipt("partial", 1))
    restored = PostgresEvidenceStore(app_database_url)
    assert restored.get_session("session-1") == session()
    assert restored.get_turn("turn-1") == turn()
    with restored.list_receipts("turn-1") as values:
        assert list(values) == [receipt("partial", 1)]


def test_adapter_query_limits_perception_at_store_boundary(app_database_url: str) -> None:
    store = seed(app_database_url)
    store.add_session(session("private"))
    store.add_turn(turn("private", "private", perceived_by=("char-c",)))
    for character, expected in [
        ("char-a", set()),
        ("char-b", {"turn-1"}),
        ("char-c", {"turn-1", "private"}),
        ("nobody", set()),
    ]:
        with store.list_perceived_turns(character) as values:
            assert {value.turn_id for value in values} == expected


@pytest.mark.parametrize("mode,targets", [("direct", ("char-a",)), ("broadcast", ()), ("self", ())])
def test_postgres_perception_ignores_addressing(
    app_database_url: str, mode: str, targets: tuple[str, ...]
) -> None:
    store = PostgresEvidenceStore(app_database_url)
    store.add_session(session())
    store.add_turn(turn(address_mode=mode, addressees=targets))
    for character, expected in [("char-a", []), ("char-b", ["turn-1"]), ("char-c", ["turn-1"])]:
        with store.list_perceived_turns(character) as values:
            assert [v.turn_id for v in values] == expected


def test_receipts_append_and_never_truncate_generated_text(app_database_url: str) -> None:
    store = seed(app_database_url)
    with store.list_receipts("turn-1") as values:
        assert list(values) == []
    expected: list[tuple[str, int]] = []
    for index, (status, end) in enumerate(
        [("none", 0), ("partial", 1), ("full", len(TEXT)), ("partial", 2)]
    ):
        store.add_receipt(
            replace(receipt(status, end), reported_at=RECORDED - timedelta(seconds=index))
        )
        expected.append((status, end))
        with store.list_receipts("turn-1") as values:
            assert [(r.status, r.spoken_until) for r in values] == expected
        assert store.get_turn("turn-1") == turn()


@pytest.mark.parametrize(
    "table,key,column",
    [
        ("sessions", "session_id", "surface"),
        ("turns", "turn_id", "text"),
        ("delivery_receipts", "turn_id", "reported_by"),
    ],
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_direct_sql_mutation_is_denied_for_normal_nonowner_role(
    app_database_url: str, table: str, key: str, column: str, operation: str
) -> None:
    store = seed(app_database_url)
    store.add_receipt(receipt("none", 0))
    identifier = "session-1" if table == "sessions" else "turn-1"
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        assert connection.execute(
            "SELECT rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user"
        ).fetchone() == (False, False)
        assert connection.execute(
            "SELECT tableowner <> current_user FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename = %s",
            (table,),
        ).fetchone() == (True,)
        statement = (
            sql.SQL("UPDATE {} SET {} = 'changed' WHERE {} = %s").format(
                sql.Identifier(table), sql.Identifier(column), sql.Identifier(key)
            )
            if operation == "UPDATE"
            else sql.SQL("DELETE FROM {} WHERE {} = %s").format(
                sql.Identifier(table), sql.Identifier(key)
            )
        )
        with pytest.raises(psycopg.Error):
            connection.execute(statement, (identifier,))
    assert store.get_session("session-1") == session()
    assert store.get_turn("turn-1") == turn()
    with store.list_receipts("turn-1") as values:
        assert list(values) == [receipt("none", 0)]


@pytest.mark.parametrize(
    "table,column",
    [("sessions", column) for column in ("session_id", "started_at", "surface", "reports_delivery")]
    + [
        ("turns", column)
        for column in (
            "turn_id",
            "session_id",
            "surface",
            "actor",
            "kind",
            "text",
            "perceived_by",
            "addressees",
            "address_mode",
            "language",
            "register",
            "sensitivity",
            "classifier_version",
            "occurred_at",
            "recorded_at",
        )
    ]
    + [
        ("delivery_receipts", column)
        for column in (
            "turn_id",
            "status",
            "spoken_until",
            "reported_by",
            "reported_at",
        )
    ],
)
def test_direct_sql_null_required_column_is_rejected(
    app_database_url: str, table: str, column: str
) -> None:
    store = seed(app_database_url)
    store.add_receipt(receipt("none", 0))
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        statement = sql.SQL(
            "INSERT INTO {} SELECT (jsonb_populate_record(NULL::{}, "
            "to_jsonb(t) || jsonb_build_object(%s::text, NULL))).* FROM {} t"
        ).format(sql.Identifier(table), sql.Identifier(table), sql.Identifier(table))
        with pytest.raises(psycopg.errors.NotNullViolation):
            connection.execute(statement, (column,))
        assert connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone() == (1,)
    assert store.get_session("session-1") == session()
    assert store.get_turn("turn-1") == turn()
    with store.list_receipts("turn-1") as values:
        assert list(values) == [receipt("none", 0)]


def test_foreign_keys_reject_dangling_turn_and_receipt(app_database_url: str) -> None:
    store = seed(app_database_url)
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                "INSERT INTO turns SELECT (jsonb_populate_record(NULL::turns, to_jsonb(t) || "
                "jsonb_build_object('turn_id', 'orphan', 'session_id', 'missing'))).* "
                "FROM turns t WHERE turn_id = 'turn-1'"
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                "INSERT INTO delivery_receipts "
                "(turn_id, status, spoken_until, reported_by, reported_at) "
                "VALUES ('missing', 'none', 0, 'synthetic-window', %s)",
                (RECORDED,),
            )
    assert store.get_turn("orphan") is None
    with store.list_receipts("missing") as values:
        assert list(values) == []
    store.add_receipt(receipt("none", 0))
    with store.list_receipts("turn-1") as values:
        assert list(values) == [receipt("none", 0)]


def test_sql_like_instructions_are_bound_as_text(app_database_url: str) -> None:
    store = PostgresEvidenceStore(app_database_url)
    store.add_session(session())
    store.add_turn(turn(text=INSTRUCTION))
    assert PostgresEvidenceStore(app_database_url).get_turn("turn-1") == turn(text=INSTRUCTION)
    store.add_turn(turn("turn-2"))
    assert store.get_turn("turn-2") == turn("turn-2")


def test_default_factory_uses_real_evidence_storage(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as environment:
        environment.setenv("MEMORY_DATABASE_URL", app_database_url)
        with TestClient(create_app()) as client:
            assert client.post("/sessions", json=session_payload()).is_success
            assert client.post("/turns", json=turn_payload()).is_success
            assert client.post("/turns/turn-1/receipts", json=receipt_payload("none", 0)).is_success
        with TestClient(create_app()) as restored:
            assert restored.get("/sessions/session-1").json()["reports_delivery"] is True
            assert restored.get("/turns/turn-1").json()["text"] == TEXT
            assert restored.get("/turns/turn-1/receipts").json()[0]["status"] == "none"
    assert PostgresEvidenceStore(app_database_url).get_session("session-1") == session()


def test_adapter_constraint_failures_do_not_poison_later_operations(app_database_url: str) -> None:
    store = seed(app_database_url)
    with pytest.raises(EvidenceConflict):
        store.add_session(session())
    with pytest.raises(EvidenceConflict):
        store.add_turn(turn(text="changed"))
    with pytest.raises(EvidenceNotFound):
        store.add_turn(turn("orphan", "missing"))
    with pytest.raises(EvidenceValidationError):
        store.add_turn(turn("invalid-reply", reply_to_turn_id="missing"))
    with pytest.raises(EvidenceNotFound):
        store.add_receipt(replace(receipt("none", 0), turn_id="missing"))
    with pytest.raises(EvidenceValidationError):
        store.add_receipt(receipt("full", 1))
    assert store.get_turn("turn-1") == turn()
    assert store.get_turn("orphan") is None
    assert store.get_turn("invalid-reply") is None
    with store.list_receipts("turn-1") as values:
        assert list(values) == []
    store.add_turn(turn("turn-2"))
    store.add_receipt(receipt("none", 0))
    assert store.get_turn("turn-2") == turn("turn-2")
    with store.list_receipts("turn-1") as values:
        assert list(values) == [receipt("none", 0)]


def test_connection_failure_is_unavailable_in_adapter_and_http(
    app_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = (
        make_url(app_database_url)
        .update_query_dict({"host": str(tmp_path)})
        .render_as_string(hide_password=False)
    )
    with pytest.raises(EvidenceUnavailable):
        PostgresEvidenceStore(url).get_turn("turn-1")
    with monkeypatch.context() as environment:
        environment.setenv("MEMORY_DATABASE_URL", url)
        with TestClient(create_app()) as client:
            assert client.get("/characters/char-a/turns").status_code == 503
            assert client.get("/health").status_code == 200


@pytest.mark.parametrize("value", [True, False])
def test_postgres_delivery_declaration_roundtrip(app_database_url: str, value: bool) -> None:
    store = PostgresEvidenceStore(app_database_url)
    store.add_session(session(reports_delivery=value))
    assert PostgresEvidenceStore(app_database_url).get_session("session-1") == session(
        reports_delivery=value
    )


@pytest.mark.parametrize("mode", ["missing", None, "direct", "self", "broadcast"])
@pytest.mark.parametrize("actor", ["viewer", "char-c"])
def test_http_address_mode_is_restored_from_new_postgres_store(
    app_database_url: str, mode: object, actor: str
) -> None:
    with TestClient(
        create_app(
            store=PostgresEvidenceStore(app_database_url),
            identity_store=PostgresIdentityStore(app_database_url),
            clock=lambda: RECORDED,
        )
    ) as client:
        assert client.post("/sessions", json=session_payload()).status_code == 201
        payload = turn_payload() | {
            "actor": actor,
            "addressees": ["viewer", "char-a"] if mode == "direct" else [],
        }
        if mode == "missing":
            del payload["address_mode"]
        else:
            payload["address_mode"] = mode
        response = client.post("/turns", json=payload)
        if actor == "char-c" and mode in ("missing", None):
            assert response.status_code == 422
            assert PostgresEvidenceStore(app_database_url).get_turn("turn-1") is None
        else:
            assert response.status_code == 201
            saved = PostgresEvidenceStore(app_database_url).get_turn("turn-1")
            assert saved is not None
            assert saved.address_mode == ("broadcast" if mode in ("missing", None) else mode)
            assert list(saved.addressees) == payload["addressees"]
            assert saved.text == TEXT


@pytest.mark.parametrize(
    "field,value",
    [
        ("perceived_by", None),
        ("perceived_by", ["char-b", "char-b"]),
        ("perceived_by", [""]),
        ("perceived_by", [" \t"]),
        ("perceived_by", [None]),
        ("perceived_by", ["viewer", "char-c"]),
        ("perceived_by", ["char-b"]),
        ("perceived_by", []),
        ("addressees", ["viewer", "viewer"]),
        ("addressees", [None]),
        ("addressees", [""]),
        ("addressees", [" \t"]),
        ("address_mode", "unknown"),
        ("address_mode", "direct"),
        ("addressees", ["viewer"]),
        ("reply_to_turn_id", "missing"),
    ],
)
def test_direct_insert_rejects_invalid_new_turn_state(
    app_database_url: str, field: str, value: object
) -> None:
    store = seed(app_database_url)
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        with pytest.raises((psycopg.IntegrityError, psycopg.DataError)):
            connection.execute(
                "INSERT INTO turns SELECT (jsonb_populate_record(NULL::turns, "
                "to_jsonb(t) || %s)).* FROM turns t WHERE turn_id = 'turn-1'",
                (Jsonb({"turn_id": "invalid", field: value}),),
            )
        assert connection.execute("SELECT count(*) FROM turns").fetchone() == (1,)
    assert store.get_turn("invalid") is None


@pytest.mark.parametrize(
    "actor,perceived",
    [
        ("viewer", ["viewer"]),
        ("char-c", []),
        ("char-c", ["char-b"]),
        ("char-c", ["char-c", "char-b"]),
    ],
)
def test_direct_insert_rejects_invalid_thought(
    app_database_url: str, actor: str, perceived: list[str]
) -> None:
    seed(app_database_url)
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        with pytest.raises((psycopg.IntegrityError, psycopg.DataError)):
            connection.execute(
                "INSERT INTO turns SELECT (jsonb_populate_record(NULL::turns, "
                "to_jsonb(t) || %s)).* FROM turns t WHERE turn_id = 'turn-1'",
                (
                    Jsonb(
                        {
                            "turn_id": "invalid",
                            "kind": "thought",
                            "actor": actor,
                            "perceived_by": perceived,
                        }
                    ),
                ),
            )
        assert connection.execute("SELECT count(*) FROM turns").fetchone() == (1,)


@pytest.mark.parametrize(
    "delivery,kind", [(False, "utterance"), (False, "thought"), (True, "thought")]
)
def test_postgres_rejects_ineligible_receipt_at_adapter_and_sql(
    app_database_url: str, delivery: bool, kind: str
) -> None:
    store = PostgresEvidenceStore(app_database_url)
    store.add_session(session(reports_delivery=delivery))
    store.add_turn(turn(kind=kind, perceived_by=("char-c",)))
    with pytest.raises(EvidenceValidationError):
        store.add_receipt(receipt("none", 0))
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        with pytest.raises(psycopg.IntegrityError):
            connection.execute(
                "INSERT INTO delivery_receipts (turn_id, status, spoken_until, "
                "reported_by, reported_at) VALUES ('turn-1', 'none', 0, "
                "'synthetic-window', %s)",
                (RECORDED,),
            )
        assert connection.execute("SELECT count(*) FROM delivery_receipts").fetchone() == (0,)
    saved = store.get_turn("turn-1")
    assert saved is not None
    assert saved.text == TEXT


@pytest.mark.parametrize(
    "column,value",
    [
        ("perceived_by", ["char-c"]),
        ("addressees", []),
        ("address_mode", "self"),
        ("reply_to_turn_id", "turn-1"),
    ],
)
def test_new_turn_attributes_are_append_only(
    app_database_url: str, column: str, value: object
) -> None:
    store = seed(app_database_url)
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "UPDATE turns t SET "
                + column
                + " = (jsonb_populate_record(NULL::turns, to_jsonb(t) || %s))."
                + column,
                (Jsonb({column: value}),),
            )
    assert store.get_turn("turn-1") == turn()


def test_new_database_schema_has_no_legacy_columns(app_database_url: str) -> None:
    with psycopg.connect(app_database_url, connect_timeout=5) as connection:
        columns = connection.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name IN ('sessions', "
            "'turns')"
        ).fetchall()
    assert ("sessions", "participants") not in columns
    assert ("turns", "audience") not in columns
    assert ("turns", "reply_to_turn_id") in columns


@pytest.mark.parametrize("mode", ["missing", None])
def test_user_mode_default_cannot_keep_nonempty_addressees(
    app_database_url: str, mode: object
) -> None:
    with TestClient(
        create_app(
            store=PostgresEvidenceStore(app_database_url),
            identity_store=PostgresIdentityStore(app_database_url),
        )
    ) as client:
        assert client.post("/sessions", json=session_payload()).status_code == 201
        payload = turn_payload() | {"actor": "viewer", "addressees": ["char-a"]}
        if mode == "missing":
            del payload["address_mode"]
        else:
            payload["address_mode"] = mode
        assert client.post("/turns", json=payload).status_code == 422
    assert PostgresEvidenceStore(app_database_url).get_turn("turn-1") is None


@pytest.mark.parametrize(
    "actor,mode,kind,perceived",
    [
        ("char-c", "self", "utterance", ("char-c", "char-b")),
        ("viewer", "self", "utterance", ("char-c", "char-b")),
        ("viewer", "self", "utterance", ()),
        ("char-c", "broadcast", "thought", ("char-c",)),
    ],
)
def test_postgres_self_and_thought_keep_declared_perception(
    app_database_url: str, actor: str, mode: str, kind: str, perceived: tuple[str, ...]
) -> None:
    store = PostgresEvidenceStore(app_database_url)
    store.add_session(session())
    value = turn(actor=actor, address_mode=mode, kind=kind, perceived_by=perceived)
    store.add_turn(value)
    assert PostgresEvidenceStore(app_database_url).get_turn("turn-1") == value
    for character in ("char-a", "char-b", "char-c", "char-z"):
        with store.list_perceived_turns(character) as values:
            assert [v.turn_id for v in values] == (["turn-1"] if character in perceived else [])


def test_postgres_reply_roundtrip_does_not_expand_perception(app_database_url: str) -> None:
    store = seed(app_database_url)
    value = turn(
        "reply",
        perceived_by=("char-c",),
        address_mode="direct",
        addressees=("viewer", "char-a"),
        reply_to_turn_id="turn-1",
    )
    store.add_turn(value)
    assert PostgresEvidenceStore(app_database_url).get_turn("reply") == value
    with store.list_perceived_turns("char-b") as values:
        assert [v.turn_id for v in values] == ["turn-1"]


def test_delivery_declaration_cannot_be_changed_in_sql(app_database_url: str) -> None:
    store = seed(app_database_url)
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE sessions SET reports_delivery = false")
    assert store.get_session("session-1") == session()
