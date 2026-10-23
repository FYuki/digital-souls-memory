"""Deletion request ownership is checked at the durable commit boundary."""

import os
from collections.abc import Callable
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from tests.evidence_support import RECORDED, receipt, session, turn
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_deletion import seed_evidence
from tests.postgres.test_recall_indexing import register_characters

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


def test_normal_sql_cannot_forge_or_change_deletion_authorization(
    database_url: str,
    app_database_url: str,
) -> None:
    from dataclasses import asdict

    from digital_souls_memory.adapters.postgres_deletion import PostgresDeletionStore
    from digital_souls_memory.governance.domain import AuthorPrincipal, DeletionCommand

    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("t1"))
    evidence.add_turn(turn("t2"))
    command = DeletionCommand(request_id="protected", target_turn_ids=("t1",))
    store = PostgresDeletionStore(os.environ["MEMORY_DELETION_DATABASE_URL"])
    with psycopg.connect(app_database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO deletion_requests(request_id,command,requested_by,target_turn_ids,"
                "status,requested_at) VALUES(%s,%s,%s,%s,'failed',%s)",
                (command.request_id, Jsonb(asdict(command)), "synthetic-author", ["t2"], RECORDED),
            )
        first = store.begin(
            command, principal=AuthorPrincipal(author_id="synthetic-author"), now=RECORDED
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE deletion_requests SET target_turn_ids=ARRAY['t2'] WHERE request_id=%s",
                (command.request_id,),
            )
        store.fail(first, error="dependency_unavailable", now=RECORDED)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE deletion_requests SET target_turn_ids=ARRAY['t2'] WHERE request_id=%s",
                (command.request_id,),
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM deletion_requests WHERE request_id=%s", (command.request_id,))
    second = store.begin(
        command, principal=AuthorPrincipal(author_id="synthetic-author"), now=RECORDED
    )
    done = store.commit(second, replacements=(), now=RECORDED)
    assert done.target_turn_ids == ("t1",)
    assert evidence.get_turn("t1") is None
    assert evidence.get_turn("t2") is not None


def test_expired_attempt_cannot_commit_or_fail_the_replacement_attempt(
    database_url: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    from digital_souls_memory.adapters.postgres_deletion import PostgresDeletionStore
    from digital_souls_memory.governance.domain import AuthorPrincipal, DeletionCommand

    evidence = seed_evidence(database_url)
    evidence.add_turn(turn(actor="U"))
    store = PostgresDeletionStore(os.environ["MEMORY_DELETION_DATABASE_URL"])
    principal = AuthorPrincipal(author_id="synthetic-author")
    command = DeletionCommand(request_id="delete-lease", user_id="U")
    first = store.begin(command, principal=principal, now=RECORDED)
    assert first.request.status == "running" and first.token is not None
    with rejected():
        store.begin(command, principal=principal, now=RECORDED + timedelta(minutes=1))
    evidence.add_turn(turn("later", actor="U"))
    now = RECORDED + timedelta(minutes=6)
    second = store.begin(command, principal=principal, now=now)
    assert second.token is not None and second.token != first.token
    assert second.request.target_turn_ids == first.request.target_turn_ids == ("turn-1",)
    with rejected():
        store.commit(first, replacements=(), now=now)
    # A stale failure may be rejected or ignored; it must not change the current attempt.
    before = store.get(command.request_id)
    try:
        store.fail(first, error="synthetic_old_failure", now=now)
    except Exception:
        pass
    assert store.get(command.request_id) == before
    done = store.commit(second, replacements=(), now=now)
    assert done.status == "done"
    assert done.cascade_result is not None
    assert done.cascade_result.deleted_turn_ids == ("turn-1",)
    assert evidence.get_turn("turn-1") is None
    assert evidence.get_turn("later") is not None


@pytest.mark.parametrize("field", ["command", "requested_by", "target_turn_ids"])
def test_commit_rejects_saved_authorization_that_differs_from_its_attempt(
    database_url: str,
    field: str,
) -> None:
    from digital_souls_memory.adapters.postgres_deletion import PostgresDeletionStore
    from digital_souls_memory.governance.domain import AuthorPrincipal, DeletionCommand
    from digital_souls_memory.memory.ports import MemoryConflict

    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("t1"))
    evidence.add_turn(turn("t2"))
    store = PostgresDeletionStore(os.environ["MEMORY_DELETION_DATABASE_URL"])
    attempt = store.begin(
        DeletionCommand(request_id="changed", target_turn_ids=("t1",)),
        principal=AuthorPrincipal(author_id="synthetic-author"),
        now=RECORDED,
    )
    # Migration administrator can maintain the table; commit still checks the saved grant.
    with psycopg.connect(database_url) as conn:
        if field == "command":
            conn.execute(
                "UPDATE deletion_requests SET "
                "command=jsonb_set(command,'{target_turn_ids}','[\"t2\"]')"
            )
        elif field == "requested_by":
            conn.execute("UPDATE deletion_requests SET requested_by='different-author'")
        else:
            conn.execute("UPDATE deletion_requests SET target_turn_ids=ARRAY['t2']")
    with pytest.raises(MemoryConflict):
        store.commit(attempt, replacements=(), now=RECORDED)
    assert evidence.get_turn("t1") is not None
    assert evidence.get_turn("t2") is not None
    audit = store.get("changed")
    assert audit is not None and audit.status == "running" and audit.cascade_result is None


def test_receipt_change_after_snapshot_prevents_partial_commit_and_allows_retry(
    database_url: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    from digital_souls_memory.adapters.postgres_deletion import PostgresDeletionStore
    from digital_souls_memory.governance.domain import AuthorPrincipal, DeletionCommand

    register_characters()
    evidence = PostgresEvidenceStore(database_url)
    evidence.add_session(session(reports_delivery=True))
    original = turn()
    evidence.add_turn(original)
    store = PostgresDeletionStore(os.environ["MEMORY_DELETION_DATABASE_URL"])
    principal = AuthorPrincipal(author_id="synthetic-author")
    command = DeletionCommand(request_id="delete-snapshot", target_turn_ids=("turn-1",))
    attempt = store.begin(command, principal=principal, now=RECORDED)
    evidence.add_receipt(receipt("none", 0))
    with rejected():
        store.commit(attempt, replacements=(), now=RECORDED)
    assert evidence.get_turn("turn-1") == original
    audit = store.get(command.request_id)
    assert audit is not None and audit.status != "done" and audit.cascade_result is None
    store.fail(attempt, error="snapshot_conflict", now=RECORDED)
    current = store.begin(command, principal=principal, now=RECORDED + timedelta(minutes=6))
    assert (
        store.commit(current, replacements=(), now=RECORDED + timedelta(minutes=6)).status == "done"
    )
    assert evidence.get_turn("turn-1") is None
