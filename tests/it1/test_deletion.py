"""Core authorization and HTTP DTOs use the planned deletion port."""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from tests.evidence_support import RECORDED

if TYPE_CHECKING:
    from digital_souls_memory.governance.domain import (
        AuthorPrincipal,
        DeletionAttempt,
        DeletionCommand,
        DeletionRequest,
        MemoryReplacement,
    )

pytestmark = pytest.mark.it1


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("MEMORY_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("MEMORY_AUTHOR_ID", "synthetic-author")
    monkeypatch.setenv("MEMORY_AUTHOR_TOKEN", "synthetic-test-credential")


class CompletedDeletionStore:
    """Only a completed request; atomic mutation is covered by PostgreSQL tests."""

    def __init__(self, result: DeletionRequest) -> None:
        self.result = result
        self.begins: list[tuple[DeletionCommand, AuthorPrincipal, datetime]] = []
        self.reads: list[str] = []

    def begin(
        self, command: DeletionCommand, *, principal: AuthorPrincipal, now: datetime
    ) -> DeletionAttempt:
        from digital_souls_memory.governance.domain import DeletionAttempt

        self.begins.append((command, principal, now))
        return DeletionAttempt(request=self.result, token=None, snapshot=None)

    def commit(
        self,
        attempt: DeletionAttempt,
        *,
        replacements: tuple[MemoryReplacement, ...],
        now: datetime,
    ) -> DeletionRequest:
        raise AssertionError("A completed request must not commit again")

    def fail(self, attempt: DeletionAttempt, *, error: str, now: datetime) -> None:
        raise AssertionError("A completed request must not be marked failed")

    def get(self, request_id: str) -> DeletionRequest | None:
        self.reads.append(request_id)
        return self.result if request_id == self.result.command.request_id else None


def completed_store() -> CompletedDeletionStore:
    from digital_souls_memory.governance.domain import (
        CascadeResult,
        DeletionCommand,
        DeletionRequest,
    )

    return CompletedDeletionStore(
        DeletionRequest(
            command=DeletionCommand(request_id="delete-1", target_turn_ids=("t1", "t2")),
            requested_by="synthetic-author",
            target_turn_ids=("t1", "t2"),
            status="done",
            cascade_result=CascadeResult(
                deleted_turn_ids=("t1", "t2"),
                recalculated_memories=(),
                invalidated_memories=(),
                invalidated_provision_ids=(),
                removed_references=(),
            ),
            error=None,
            requested_at=RECORDED,
            completed_at=RECORDED,
        )
    )


def test_verified_author_replays_completed_request_and_reads_same_audit() -> None:
    store = completed_store()
    app = create_app(deletion_store=store, clock=lambda: RECORDED)
    headers = {"Authorization": "Bearer synthetic-test-credential"}
    with TestClient(app) as client:
        response = client.post(
            "/deletion-requests",
            headers=headers,
            json={"request_id": "delete-1", "target_turn_ids": ["t2", "t1", "t2"]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["requested_by"] == "synthetic-author"
        assert body["target_turn_ids"] == ["t1", "t2"]
        assert body["status"] == "done"
        assert body["cascade_result"]["deleted_turn_ids"] == ["t1", "t2"]
        audit = client.get("/deletion-requests/delete-1", headers=headers)
        assert audit.status_code == 200
        assert audit.json() == body
        assert client.get("/deletion-requests/absent", headers=headers).status_code == 404
    assert len(store.begins) == 1
    command, principal, now = store.begins[0]
    assert command.target_turn_ids == ("t1", "t2")
    assert principal.author_id == "synthetic-author"
    assert now == RECORDED


def test_injected_authorizer_takes_precedence_over_environment() -> None:
    from digital_souls_memory.adapters.core_authorization import CoreAuthorizer

    store = completed_store()
    app = create_app(
        deletion_store=store,
        authorizer=CoreAuthorizer(author_id="injected-author", token="injected-credential"),
        clock=lambda: RECORDED,
    )
    with TestClient(app) as client:
        response = client.post(
            "/deletion-requests",
            headers={"Authorization": "Bearer injected-credential"},
            json={"request_id": "delete-1", "target_turn_ids": ["t1", "t2"]},
        )
        assert response.status_code == 200
        assert store.begins[0][1].author_id == "injected-author"
        denied = client.get(
            "/deletion-requests/delete-1",
            headers={"Authorization": "Bearer synthetic-test-credential"},
        )
        assert denied.status_code == 401
        assert store.reads == []


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic synthetic-test-credential"])
def test_unverified_caller_never_reaches_deletion_or_audit_store(
    authorization: str | None,
) -> None:
    store = completed_store()
    app = create_app(deletion_store=store, clock=lambda: RECORDED)
    headers = {} if authorization is None else {"Authorization": authorization}
    with TestClient(app) as client:
        response = client.post(
            "/deletion-requests",
            headers=headers,
            json={"request_id": "delete-1", "user_id": "user-a"},
        )
        assert response.status_code == 401
        assert client.get("/deletion-requests/delete-1", headers=headers).status_code == 401
    assert store.begins == []
    assert store.reads == []


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": "delete-1"},
        {"request_id": "delete-1", "target_turn_ids": [], "user_id": ""},
        {"request_id": "delete-1", "target_turn_ids": ["t1"], "user_id": "user-a"},
        {"request_id": "", "user_id": "user-a"},
        {"request_id": "delete-1", "user_id": "  "},
        {"request_id": "delete-1", "target_turn_ids": [" "]},
        {"user_id": "user-a"},
        {"request_id": "delete-1", "user_id": "user-a", "requested_by": "synthetic-author"},
    ],
)
def test_invalid_selection_or_claimed_author_is_rejected_before_store(
    body: dict[str, object],
) -> None:
    store = completed_store()
    with TestClient(create_app(deletion_store=store)) as client:
        response = client.post(
            "/deletion-requests",
            headers={"Authorization": "Bearer synthetic-test-credential"},
            json=body,
        )
        assert response.status_code == 422
    assert store.begins == []


@pytest.mark.parametrize("missing", ["MEMORY_AUTHOR_ID", "MEMORY_AUTHOR_TOKEN"])
def test_missing_author_configuration_keeps_health_available_and_does_not_call_store(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing)
    store = completed_store()
    with TestClient(create_app(deletion_store=store)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert (
            client.post(
                "/deletion-requests",
                headers={"Authorization": "Bearer synthetic-test-credential"},
                json={"request_id": "delete-1", "user_id": "user-a"},
            ).status_code
            == 503
        )
    assert store.begins == []


def test_normal_database_configuration_does_not_supply_deletion_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql://synthetic.invalid/unused")
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert (
            client.post(
                "/deletion-requests",
                headers={"Authorization": "Bearer synthetic-test-credential"},
                json={"request_id": "delete-1", "user_id": "user-a"},
            ).status_code
            == 503
        )
