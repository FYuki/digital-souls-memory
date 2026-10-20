from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from digital_souls_memory.identity.ports import ReviewResult
from tests.evidence_support import receipt_payload, session_payload, turn_payload
from tests.identity_support import REGISTERED, SEED_PATH, TEMPERAMENT, core_payload, seed_payload
from tests.it1.evidence_fakes import FakeEvidenceStore
from tests.it1.identity_fakes import FakeIdentityStore
from tests.it1.voice_fakes import FakeVoiceStore, VoiceGateway, VoiceReviewer
from tests.voice_support import (
    ANCHORS,
    EXEMPLARS,
    INSTRUCTION,
    KINDS,
    TEXT,
    anchor,
    anchor_payload,
    exemplar,
    exemplar_payload,
)

pytestmark = pytest.mark.it1


class Harness:
    def __init__(self) -> None:
        self.evidence = FakeEvidenceStore()
        self.identity = FakeIdentityStore()
        self.voice = FakeVoiceStore()
        self.gateway = VoiceGateway()
        self.reviewer = VoiceReviewer()
        self.now = REGISTERED

    def clock(self) -> datetime:
        return self.now

    def client(self) -> TestClient:
        return TestClient(
            create_app(
                store=self.evidence,
                identity_store=self.identity,
                voice_store=self.voice,
                llm_gateway=self.gateway,
                reviewer=self.reviewer,
                clock=self.clock,
            )
        )


def register_identity(client: TestClient, character_id: str = "char-a") -> None:
    root = f"/characters/{character_id}"
    assert client.post(root + "/core-identities", json=core_payload()).is_success
    assert client.post(root + "/temperaments", json={"description": TEMPERAMENT}).is_success
    assert client.post(root + "/personality-profiles/author-seed", json=seed_payload()).is_success


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
def client(harness: Harness) -> Iterator[TestClient]:
    with harness.client() as value:
        register_identity(value)
        register_identity(value, "char-b")
        yield value


def register_turn(
    client: TestClient,
    *,
    delivery: bool = False,
    perceived: tuple[str, ...] = ("char-a", "char-b"),
    actor: str = "char-b",
    kind: str = "utterance",
    targets: tuple[str, ...] = (),
    mode: str = "broadcast",
) -> None:
    assert client.post(
        "/sessions", json=session_payload() | {"reports_delivery": delivery}
    ).is_success
    assert client.post(
        "/turns",
        json=turn_payload()
        | {
            "actor": actor,
            "perceived_by": list(perceived),
            "text": TEXT,
            "kind": kind,
            "addressees": list(targets),
            "address_mode": mode,
        },
    ).is_success


def add_exemplar(client: TestClient, payload: dict[str, object]) -> None:
    assert client.post(EXEMPLARS, json={"exemplars": [payload]}).is_success


def test_approved_initial_set_roundtrips_all_attributes(client: TestClient) -> None:
    payload = exemplar_payload()
    add_exemplar(client, payload)
    saved = client.get(EXEMPLARS + "/voice-1")
    assert saved.is_success
    for key, expected in payload.items():
        assert saved.json()[key] == expected
    assert client.get(EXEMPLARS).json() == [saved.json()]
    assert client.get(EXEMPLARS.replace("char-a", "char-b")).json() == []
    assert client.get((EXEMPLARS + "/voice-1").replace("char-a", "char-b")).is_client_error


@pytest.mark.parametrize("kind", KINDS)
def test_anchor_roundtrips_reference_original_text_and_audit(client: TestClient, kind: str) -> None:
    register_turn(client)
    payload = anchor_payload() | {"kind": kind}
    assert client.post(ANCHORS, json=payload).is_success
    saved = client.get(ANCHORS + "/anchor-1")
    assert saved.is_success
    for key, expected in payload.items():
        assert saved.json()[key] == expected
    assert saved.json()["text"] == TEXT
    assert client.get(ANCHORS).json() == [saved.json()]
    assert client.get(ANCHORS.replace("char-a", "char-b")).json() == []
    assert client.get((ANCHORS + "/anchor-1").replace("char-a", "char-b")).is_client_error


@pytest.mark.parametrize("resource", ["exemplar", "anchor"])
@pytest.mark.parametrize("approval", ["missing", None, "", " \t\n"])
def test_http_approval_failure_does_not_publish_material(
    client: TestClient,
    harness: Harness,
    resource: str,
    approval: object,
) -> None:
    register_turn(client)
    payload = exemplar_payload() if resource == "exemplar" else anchor_payload()
    path = EXEMPLARS if resource == "exemplar" else ANCHORS
    if approval == "missing":
        del payload["approved_by"]
    else:
        payload["approved_by"] = approval
    body = {"exemplars": [payload]} if resource == "exemplar" else payload
    response = client.post(path, json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid voice value"}
    assert client.get(path).json() == []
    assert harness.gateway.voice_calls == []
    assert harness.voice.checks == {}


@pytest.mark.parametrize(
    "change,status",
    [
        ({"source": "unknown"}, 422),
        ({"trait_version": 999}, 404),
        ({"source": "turn_id", "turn_id": "absent"}, 404),
    ],
)
def test_invalid_exemplar_reference_rejects_entire_set(
    client: TestClient,
    harness: Harness,
    change: dict[str, object],
    status: int,
) -> None:
    invalid = exemplar_payload() | {"exemplar_id": "invalid"} | change
    response = client.post(EXEMPLARS, json={"exemplars": [exemplar_payload(), invalid]})
    assert response.status_code == status
    if status == 422:
        assert response.json() == {"detail": "Invalid voice value"}
    assert client.get(EXEMPLARS).json() == []
    assert harness.voice.checks == {}
    assert harness.gateway.voice_calls == []
    assert harness.reviewer.voice_calls == []


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("turn_id", ["turn-1", "absent"])
def test_turn_reference_registration_is_atomic(
    client: TestClient, harness: Harness, mixed: bool, turn_id: str
) -> None:
    register_turn(client)
    payload = exemplar_payload() | {
        "exemplar_id": "from-turn",
        "source": "turn_id",
        "turn_id": turn_id,
    }
    values = [exemplar_payload(), payload] if mixed else [payload]
    response = client.post(EXEMPLARS, json={"exemplars": values})
    if turn_id == "turn-1":
        assert response.status_code == 201
        assert len(client.get(EXEMPLARS).json()) == len(values)
        assert client.get(EXEMPLARS + "/from-turn").json()["text"] == TEXT
    else:
        assert response.status_code == 404
        assert client.get(EXEMPLARS).json() == []
        assert harness.voice.checks == {}
        assert harness.gateway.voice_calls == []
        assert harness.reviewer.voice_calls == []


@pytest.mark.parametrize(
    "change,status", [({"turn_id": "absent"}, 404), ({"kind": "unknown"}, 422)]
)
def test_invalid_anchor_reference_is_not_published(
    client: TestClient,
    change: dict[str, object],
    status: int,
) -> None:
    register_turn(client)
    response = client.post(ANCHORS, json=anchor_payload() | change)
    assert response.status_code == status
    if status == 422:
        assert response.json() == {"detail": "Invalid voice value"}
    assert client.get(ANCHORS).json() == []


@pytest.mark.parametrize(
    "text", ["私は約束を守る。", "  私は約束を守ります。\nI shall keep it. é🙂\t"]
)
def test_turn_exemplar_rejects_paraphrase_and_unicode_replacement(
    client: TestClient,
    text: str,
) -> None:
    register_turn(client)
    payload = exemplar_payload() | {"source": "turn_id", "turn_id": "turn-1", "text": text}
    response = client.post(EXEMPLARS, json={"exemplars": [payload]})
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid voice value"}
    assert client.get(EXEMPLARS).json() == []


@pytest.mark.parametrize(
    "perceived,actor,kind,mode,targets,allowed",
    [
        (("char-a", "char-b"), "char-b", "utterance", "direct", ("char-a",), True),
        (("char-b",), "char-b", "utterance", "direct", ("char-a",), False),
        (("char-b",), "char-b", "utterance", "direct", ("viewer",), False),
        ((), "viewer", "utterance", "broadcast", (), False),
        (("char-a", "char-b"), "char-b", "utterance", "self", (), True),
        (("char-a",), "char-a", "utterance", "self", (), True),
        (("char-b",), "char-b", "thought", "self", (), False),
        (("char-a",), "char-a", "thought", "self", (), True),
    ],
)
def test_voice_registration_uses_only_explicit_perception(
    client: TestClient,
    perceived: tuple[str, ...],
    actor: str,
    kind: str,
    mode: str,
    targets: tuple[str, ...],
    allowed: bool,
) -> None:
    register_turn(client, perceived=perceived, actor=actor, kind=kind, mode=mode, targets=targets)
    payload = exemplar_payload() | {"source": "turn_id", "turn_id": "turn-1"}
    for path, body in ((EXEMPLARS, {"exemplars": [payload]}), (ANCHORS, anchor_payload())):
        response = client.post(path, json=body)
        assert response.is_success if allowed else response.is_client_error
        values = client.get(path).json()
        assert len(values) == int(allowed)
        if allowed:
            assert values[0]["text"] == TEXT


def test_stored_materials_follow_receipt_changes_in_same_app(client: TestClient) -> None:
    register_turn(client, delivery=True)
    assert client.post("/turns/turn-1/receipts", json=receipt_payload("full", len(TEXT))).is_success
    add_exemplar(client, exemplar_payload() | {"source": "turn_id", "turn_id": "turn-1"})
    assert client.post(ANCHORS, json=anchor_payload()).is_success
    assert client.get(EXEMPLARS + "/voice-1").json()["text"] == TEXT
    assert client.get(ANCHORS + "/anchor-1").json()["text"] == TEXT
    prefix = TEXT[:12]
    assert client.post("/turns/turn-1/receipts", json=receipt_payload("partial", 12)).is_success
    assert client.get(EXEMPLARS).json() == []
    assert client.get(EXEMPLARS + "/voice-1").status_code == 404
    assert client.get(ANCHORS + "/anchor-1").json()["text"] == prefix
    assert [v["text"] for v in client.get(ANCHORS).json()] == [prefix]
    rejected_full = exemplar_payload() | {
        "exemplar_id": "undelivered",
        "source": "turn_id",
        "turn_id": "turn-1",
    }
    assert client.post(EXEMPLARS, json={"exemplars": [rejected_full]}).is_client_error
    add_exemplar(
        client,
        exemplar_payload()
        | {
            "exemplar_id": "prefix",
            "source": "turn_id",
            "turn_id": "turn-1",
            "text": prefix,
        },
    )
    assert [v["text"] for v in client.get(EXEMPLARS).json()] == [prefix]
    assert client.post("/turns/turn-1/receipts", json=receipt_payload("none", 0)).is_success
    assert client.get(EXEMPLARS).json() == []
    for identifier in ("voice-1", "prefix"):
        assert client.get(EXEMPLARS + "/" + identifier).status_code == 404
    anchor_response = client.get(ANCHORS + "/anchor-1")
    if anchor_response.is_success:
        assert anchor_response.json()["text"] == ""
    assert all(v["text"] == "" for v in client.get(ANCHORS).json())
    assert client.get("/turns/turn-1").json()["text"] == TEXT


def test_reads_filter_inaccessible_turns_even_when_store_returns_records(
    client: TestClient,
    harness: Harness,
) -> None:
    register_turn(client, perceived=("char-b",), targets=("char-a",), mode="direct")
    with harness.voice.transaction("char-a") as tx:
        tx.add_exemplar(replace(exemplar(), source="turn_id", turn_id="turn-1"))
        tx.add_anchor(anchor())
    for path, identifier in ((EXEMPLARS, "voice-1"), (ANCHORS, "anchor-1")):
        assert client.get(path).json() == []
        assert client.get(path + "/" + identifier).status_code == 404


@pytest.mark.parametrize("delivery", ["full", "none"])
def test_confirmed_delivery_range_controls_exemplar_registration(
    client: TestClient, harness: Harness, delivery: str
) -> None:
    register_turn(client, delivery=True)
    assert client.post("/turns/turn-1/receipts", json=receipt_payload("full", len(TEXT))).is_success
    if delivery == "none":
        assert client.post("/turns/turn-1/receipts", json=receipt_payload("none", 0)).is_success
    payload = exemplar_payload() | {"source": "turn_id", "turn_id": "turn-1"}
    response = client.post(EXEMPLARS, json={"exemplars": [payload]})
    if delivery == "full":
        assert response.status_code == 201
        assert client.get(EXEMPLARS + "/voice-1").json()["text"] == TEXT
    else:
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid voice value"}
        assert client.get(EXEMPLARS).json() == []
        assert harness.voice.checks == {}
        assert harness.gateway.voice_calls == []
        assert harness.reviewer.voice_calls == []


def test_missing_receipt_expires_in_same_app_and_uses_recorded_time(
    client: TestClient,
    harness: Harness,
) -> None:
    register_turn(client, delivery=True)
    payload = exemplar_payload() | {"source": "turn_id", "turn_id": "turn-1"}
    harness.now += timedelta(minutes=9, seconds=59)
    pending = client.post(EXEMPLARS, json={"exemplars": [payload]})
    assert pending.is_client_error or pending.status_code == 202
    assert client.get(EXEMPLARS).json() == []
    assert harness.voice.checks == {}
    harness.now += timedelta(seconds=1)
    add_exemplar(client, payload)
    assert client.get(EXEMPLARS + "/voice-1").json()["text"] == TEXT


def test_configured_delivery_timeout_reaches_http_registration(harness: Harness) -> None:
    with TestClient(
        create_app(
            store=harness.evidence,
            identity_store=harness.identity,
            voice_store=harness.voice,
            llm_gateway=harness.gateway,
            reviewer=harness.reviewer,
            clock=harness.clock,
            delivery_timeout=timedelta(seconds=30),
        )
    ) as client:
        register_identity(client)
        register_identity(client, "char-b")
        register_turn(client, delivery=True)
        payload = exemplar_payload() | {"source": "turn_id", "turn_id": "turn-1"}
        harness.now += timedelta(seconds=29)
        pending = client.post(EXEMPLARS, json={"exemplars": [payload]})
        assert pending.is_client_error or pending.status_code == 202
        assert client.get(EXEMPLARS).json() == []
        assert harness.voice.checks == {}
        harness.now += timedelta(seconds=1)
        add_exemplar(client, payload)
        assert client.get(EXEMPLARS + "/voice-1").json()["text"] == TEXT


@pytest.mark.parametrize("consistent", [True, False])
def test_initial_check_returns_result_once_without_changing_identity(
    client: TestClient,
    harness: Harness,
    consistent: bool,
) -> None:
    harness.reviewer.voice_result = ReviewResult(
        consistent=consistent, reason="合成応答の口調を作者が確認する"
    )
    before = harness.identity.get_current("char-a", harness.now)
    response = client.post(EXEMPLARS, json={"exemplars": [exemplar_payload()]})
    assert response.is_success
    assert response.json()["consistency_check"] == {
        "consistent": consistent,
        "reason": harness.reviewer.voice_result.reason,
    }
    temperament, profile, texts = harness.gateway.voice_calls[0]
    assert temperament == before[1]
    assert profile == before[2]
    assert texts == (TEXT,)
    assert harness.reviewer.voice_calls == [
        (temperament, profile, texts, harness.gateway.responses)
    ]
    add_exemplar(client, exemplar_payload() | {"exemplar_id": "second"})
    assert client.get(EXEMPLARS).is_success
    assert client.get(EXEMPLARS + "/voice-1").is_success
    with harness.client() as restored:
        assert restored.get(EXEMPLARS).is_success
        add_exemplar(restored, exemplar_payload() | {"exemplar_id": "third"})
    assert len(harness.gateway.voice_calls) == len(harness.reviewer.voice_calls) == 1
    assert harness.identity.get_current("char-a", harness.now) == before
    assert client.post(SEED_PATH, json=seed_payload()).is_client_error


@pytest.mark.parametrize("failure", ["generate", "review", "insert", "commit"])
def test_failed_initial_registration_rolls_back_and_can_retry(
    client: TestClient,
    harness: Harness,
    failure: str,
) -> None:
    before = harness.identity.get_current("char-a", harness.now)
    harness.gateway.fail_voice = failure == "generate"
    harness.reviewer.fail_voice = failure == "review"
    harness.voice.fail_at = failure if failure in ("insert", "commit") else None
    response = client.post(EXEMPLARS, json={"exemplars": [exemplar_payload()]})
    assert response.is_server_error
    assert client.get(EXEMPLARS).json() == []
    assert harness.voice.checks == {}
    assert harness.identity.get_current("char-a", harness.now) == before
    harness.gateway.fail_voice = harness.reviewer.fail_voice = False
    harness.voice.fail_at = None
    add_exemplar(client, exemplar_payload())
    assert client.get(EXEMPLARS + "/voice-1").json()["text"] == TEXT


def test_instruction_text_does_not_grant_identity_write_authority(
    client: TestClient,
    harness: Harness,
) -> None:
    before = harness.identity.get_current("char-a", harness.now)
    add_exemplar(client, exemplar_payload() | {"text": INSTRUCTION})
    assert client.get(EXEMPLARS + "/voice-1").json()["text"] == INSTRUCTION
    assert harness.identity.get_current("char-a", harness.now) == before
    assert client.post(SEED_PATH, json=seed_payload()).is_client_error


def test_anchor_reads_do_not_decay_or_reinforce(client: TestClient, harness: Harness) -> None:
    register_turn(client)
    assert client.post(ANCHORS, json=anchor_payload()).is_success
    saved = harness.voice.get_anchor("char-a", "anchor-1")
    before = client.get(ANCHORS + "/anchor-1").json()
    harness.now += timedelta(days=3650)
    assert client.get(ANCHORS + "/anchor-1").json() == before
    assert client.get(ANCHORS).json() == [before]
    assert harness.voice.get_anchor("char-a", "anchor-1") == saved
