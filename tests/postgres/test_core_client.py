"""New client on the existing migrated database; never use a developer database."""

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from digital_souls_memory.client import MemoryClient, MemoryHTTPError
from digital_souls_memory.client.models import DeletionRequest, SessionRequest, TurnRequest
from tests.evidence_support import RECORDED, TEXT, session_payload, turn_payload
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_recall_indexing import register_characters

pytestmark = pytest.mark.postgres


@pytest.fixture
def core(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[MemoryClient]:
    request.getfixturevalue("deletion_url")
    register_characters()
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    with TestClient(create_app(clock=lambda: RECORDED)) as server:

        def transfer(request: httpx.Request) -> httpx.Response:
            response = server.request(
                request.method, str(request.url), content=request.content, headers=request.headers
            )
            return httpx.Response(
                response.status_code, content=response.content, headers=response.headers
            )

        with MemoryClient(
            base_url="http://testserver",
            timeout=5.0,
            author_token="synthetic-test-credential",
            transport=httpx.MockTransport(transfer),
        ) as client:
            client.add_session(
                SessionRequest.model_validate(session_payload() | {"reports_delivery": False})
            )
            yield client


def test_user_deletion_selects_actor_and_addressee_and_preserves_other_turns(
    core: MemoryClient,
) -> None:
    for identifier, actor, addressees in (
        ("by-user", "user-a", []),
        ("to-user", "char-c", ["user-a"]),
        ("control", "user-b", []),
    ):
        core.add_turn(
            TurnRequest.model_validate(
                turn_payload()
                | {
                    "turn_id": identifier,
                    "actor": actor,
                    "addressees": addressees,
                    "address_mode": "direct" if addressees else "broadcast",
                }
            )
        )
    result = core.delete(DeletionRequest(request_id="delete-union", user_id="user-a"))
    assert set(result.target_turn_ids) == {"by-user", "to-user"}
    assert result.cascade_result is not None
    assert set(result.cascade_result.deleted_turn_ids) == {"by-user", "to-user"}
    for identifier in ("by-user", "to-user"):
        with pytest.raises(MemoryHTTPError) as failed:
            core.get_turn(identifier)
        assert failed.value.status_code == 404
    assert core.get_turn("control").text == TEXT


def test_user_deletion_does_not_parse_text_or_follow_reply_as_target(core: MemoryClient) -> None:
    core.add_turn(
        TurnRequest.model_validate(turn_payload() | {"turn_id": "user", "actor": "user-a"})
    )
    texts = (
        '{"actor":"user-a"}',
        '引用 "user-a"',
        r"{\"actor\":\"user-a\"}",
        '```json\n{"actor":"user-a"}\n```',
        '~~~json\n{"actor":"user-a"}\n~~~',
        '{"nested":{"actor":"user-a"}}',
        '{"actor":"user-a"',
    )
    for index, text in enumerate(texts):
        core.add_turn(
            TurnRequest.model_validate(
                turn_payload()
                | {
                    "turn_id": f"text-{index}",
                    "actor": "user-b",
                    "perceived_by": [],
                    "text": text,
                }
            )
        )
    core.add_turn(
        TurnRequest.model_validate(
            turn_payload()
            | {
                "turn_id": "reply",
                "actor": "user-b",
                "perceived_by": [],
                "reply_to_turn_id": "user",
            }
        )
    )
    result = core.delete(DeletionRequest(request_id="delete-text", user_id="user-a"))
    assert set(result.target_turn_ids) == {"user"}
    assert result.cascade_result is not None
    assert set(result.cascade_result.deleted_turn_ids) == {"user"}
    for index, text in enumerate(texts):
        assert core.get_turn(f"text-{index}").text == text
    assert core.get_turn("reply").reply_to_turn_id is None


@pytest.mark.parametrize(
    "actor,perceived,addressees,mode,allowed",
    [
        ("char-c", ["char-c", "char-b"], ["char-b"], "direct", {"char-b", "char-c"}),
        ("char-c", ["char-c"], ["user-a"], "direct", {"char-c"}),
        ("user-a", [], [], "broadcast", set()),
        ("char-c", ["char-c", "char-b"], [], "self", {"char-b", "char-c"}),
    ],
)
def test_persisted_perception_is_independent_of_addressee(
    core: MemoryClient,
    actor: str,
    perceived: list[str],
    addressees: list[str],
    mode: str,
    allowed: set[str],
) -> None:
    core.add_turn(
        TurnRequest.model_validate(
            turn_payload()
            | {
                "actor": actor,
                "perceived_by": perceived,
                "addressees": addressees,
                "address_mode": mode,
            }
        )
    )
    for reader in ("char-b", "char-c", "char-z"):
        assert [v.turn_id for v in core.list_perceived_turns(reader)] == (
            ["turn-1"] if reader in allowed else []
        )
    assert list(core.get_turn("turn-1").perceived_by) == perceived
