"""The planned Core deletion endpoint must reject self-declared authority."""

import os

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from tests.evidence_support import RECORDED, session, turn
from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities

pytestmark = pytest.mark.it1


@pytest.fixture(autouse=True)
def isolate_memory_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("MEMORY_"):
            monkeypatch.delenv(key)


@pytest.mark.parametrize("selection", [{"target_turn_ids": ["turn-1"]}, {"user_id": "user-a"}])
@pytest.mark.parametrize("claimed_author", ["author", "user-a"])
def test_body_requested_by_cannot_authorize_deletion(
    selection: dict[str, object], claimed_author: str
) -> None:
    evidence = FakeEvidenceStore()
    evidence.add_session(session(reports_delivery=False))
    source = turn(actor="user-a")
    evidence.add_turn(source)
    app = create_app(store=evidence, identity_store=registered_identities(), clock=lambda: RECORDED)
    with TestClient(app) as client:
        response = client.post(
            "/deletion-requests",
            json={"request_id": "synthetic-deletion", "requested_by": claimed_author} | selection,
        )
        assert "/deletion-requests" in app.openapi()["paths"]
        assert not response.is_success
        assert client.get("/turns/turn-1").json()["text"] == source.text
        assert evidence.get_turn("turn-1") == source


def test_deletion_audit_cannot_be_read_without_author_authority() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/deletion-requests/synthetic-deletion")
        assert "/deletion-requests/{request_id}" in app.openapi()["paths"]
        assert not response.is_success
        assert client.get("/health").json() == {"status": "ok"}
