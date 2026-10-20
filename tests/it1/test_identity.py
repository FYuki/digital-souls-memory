import json
from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from digital_souls_memory.identity.ports import ReviewResult
from tests.evidence_support import session_payload, turn_payload
from tests.identity_support import (
    DESCRIPTION,
    INSTRUCTION,
    REGISTERED,
    ROOT,
    SEED_PATH,
    TEMPERAMENT,
    aspects,
    core_payload,
    seed_payload,
)
from tests.it1.evidence_fakes import FakeEvidenceStore
from tests.it1.identity_fakes import FakeIdentityStore, FakeLLMGateway, FakeReviewer

pytestmark = pytest.mark.it1


@pytest.fixture
def store() -> FakeIdentityStore:
    return FakeIdentityStore()


@pytest.fixture
def gateway() -> FakeLLMGateway:
    return FakeLLMGateway()


@pytest.fixture
def reviewer() -> FakeReviewer:
    return FakeReviewer()


@pytest.fixture
def client(
    store: FakeIdentityStore, gateway: FakeLLMGateway, reviewer: FakeReviewer
) -> Iterator[TestClient]:
    with TestClient(
        create_app(
            store=FakeEvidenceStore(),
            identity_store=store,
            llm_gateway=gateway,
            reviewer=reviewer,
            clock=lambda: REGISTERED,
        )
    ) as value:
        yield value


def register_base(client: TestClient) -> None:
    assert client.post(ROOT + "/core-identities", json=core_payload()).is_success
    assert client.post(ROOT + "/temperaments", json={"description": TEMPERAMENT}).is_success


def register_profile(client: TestClient) -> None:
    register_base(client)
    assert client.post(SEED_PATH, json=seed_payload()).is_success


@pytest.mark.parametrize(
    "resource,payload,field,new_value",
    [
        ("core-identities", core_payload(), "values", ["誠実", "勇気"]),
        ("temperaments", {"description": TEMPERAMENT}, "description", "静かに喜ぶ。"),
    ],
)
def test_registration_appends_versions_and_keeps_original(
    client: TestClient, resource: str, payload: dict[str, object], field: str, new_value: object
) -> None:
    path = ROOT + "/" + resource
    first = client.post(path, json=payload)
    assert first.is_success
    saved = first.json()
    assert saved["character_id"] == "char-a"
    assert datetime.fromisoformat(saved["registered_at"]) == REGISTERED
    assert saved[field] == payload[field]
    second = client.post(path, json=payload | {field: new_value})
    assert second.is_success
    assert second.json()["version"] != saved["version"]
    assert second.json()[field] == new_value
    assert client.get(path + f"/{saved['version']}").json() == saved
    assert client.get(path.replace("char-a", "char-b") + f"/{saved['version']}").status_code == 404


def test_initial_seed_passes_confirmed_inputs_and_persists_generated_description(
    client: TestClient, store: FakeIdentityStore, gateway: FakeLLMGateway, reviewer: FakeReviewer
) -> None:
    register_profile(client)
    assert len(gateway.calls) == 1
    numbers, temperament, renderer = gateway.calls[0]
    assert numbers == aspects()
    assert (temperament.description, temperament.version) == (TEMPERAMENT, 1)
    assert renderer == "synthetic-renderer-v1"
    saved = store.get_profile("char-a", 1)
    assert saved is not None
    assert dict(saved.aspects) == aspects()
    assert saved.description == DESCRIPTION
    assert saved.renderer_version == renderer
    assert saved.temperament_version == temperament.version
    assert saved.valid_from == REGISTERED
    assert saved.valid_to is None
    assert tuple(saved.evidence_belief_ids) == ()
    assert saved.change_kind == "author_seed"
    assert reviewer.calls == [(temperament, saved)]
    current = client.get(ROOT + "/identity/current")
    assert current.status_code == 200
    personality = current.json()["personality_profile"]
    assert personality["description"] == DESCRIPTION
    assert personality["version"] == saved.version
    assert "aspects" not in personality
    assert all(key not in personality for key in aspects())


@pytest.mark.parametrize("changed", [False, True])
def test_reseed_is_rejected_without_changing_published_profile(
    client: TestClient, store: FakeIdentityStore, gateway: FakeLLMGateway, changed: bool
) -> None:
    register_profile(client)
    before = store.get_profile("char-a", 1)
    payload = seed_payload()
    if changed:
        payload["aspects"] = aspects() | {"Volatility": 0.8}
    assert client.post(SEED_PATH, json=payload).status_code == 409
    assert store.get_profile("char-a", 1) == before
    assert store.get_profile("char-a", 2) is None
    assert len(gateway.calls) == 1


def test_selected_versions_remain_available_after_temperament_and_core_update(
    client: TestClient, store: FakeIdentityStore, reviewer: FakeReviewer, gateway: FakeLLMGateway
) -> None:
    register_profile(client)
    selected = client.get(ROOT + "/identity/current").json()
    profile_before = store.get_profile("char-a", 1)
    assert client.post(
        ROOT + "/core-identities", json=core_payload() | {"values": ["勇気"]}
    ).is_success
    assert client.post(ROOT + "/temperaments", json={"description": "静かに喜ぶ。"}).is_success
    current = client.get(ROOT + "/identity/current").json()
    assert current["core_identity"]["version"] != selected["core_identity"]["version"]
    assert current["temperament"]["version"] != selected["temperament"]["version"]
    for resource, key in [
        ("core-identities", "core_identity"),
        ("temperaments", "temperament"),
        ("personality-profiles", "personality_profile"),
    ]:
        version = selected[key]["version"]
        assert client.get(f"{ROOT}/{resource}/{version}").json() == selected[key]
        assert client.get(f"{ROOT}/{resource}/999").status_code == 404
    assert store.get_profile("char-a", 1) == profile_before
    assert len(gateway.calls) == 1
    assert reviewer.calls[-1][0].description == "静かに喜ぶ。"
    assert reviewer.calls[-1][1] == profile_before


@pytest.mark.parametrize("failure", ["render", "review", "reject", "insert", "commit"])
def test_failed_seed_is_not_published_and_can_be_retried(
    client: TestClient,
    store: FakeIdentityStore,
    gateway: FakeLLMGateway,
    reviewer: FakeReviewer,
    failure: str,
) -> None:
    register_base(client)
    before = client.get(ROOT + "/identity/current").json()
    gateway.fail = failure == "render"
    reviewer.fail = failure == "review"
    if failure == "reject":
        reviewer.result = ReviewResult(consistent=False, reason="合成気質と矛盾する")
    store.fail_at = failure if failure in {"insert", "commit"} else None
    response = client.post(SEED_PATH, json=seed_payload())
    assert response.status_code == (422 if failure == "reject" else 503)
    if failure == "reject":
        assert "合成気質と矛盾する" in response.text
    assert client.get(ROOT + "/identity/current").json() == before
    assert store.get_profile("char-a") is None
    assert client.get(ROOT + "/personality-profiles/1").status_code == 404
    gateway.fail = reviewer.fail = False
    reviewer.result = ReviewResult(consistent=True, reason="整合する")
    store.fail_at = None
    assert client.post(SEED_PATH, json=seed_payload()).is_success


def test_rejected_temperament_update_keeps_previous_versions(
    client: TestClient, reviewer: FakeReviewer, store: FakeIdentityStore
) -> None:
    register_profile(client)
    before = client.get(ROOT + "/identity/current").json()
    reviewer.result = ReviewResult(consistent=False, reason="新しい気質と矛盾する")
    response = client.post(ROOT + "/temperaments", json={"description": "矛盾する候補"})
    assert response.status_code == 422
    assert "新しい気質と矛盾する" in response.text
    assert client.get(ROOT + "/identity/current").json() == before
    assert store.get_temperament("char-a", 2) is None


@pytest.mark.parametrize(
    "invalid", ["NaN", "Infinity", "-Infinity", "-0.01", "1.01", '"0.5"', "true"]
)
def test_invalid_raw_json_aspect_returns_client_error_without_saving(
    client: TestClient, store: FakeIdentityStore, gateway: FakeLLMGateway, invalid: str
) -> None:
    register_base(client)
    raw = json.dumps(seed_payload()).replace('"Volatility": 0.0', '"Volatility": ' + invalid)
    response = client.post(SEED_PATH, content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid identity value"}
    assert store.get_profile("char-a") is None
    assert gateway.calls == []


@pytest.mark.parametrize(
    "field,value",
    [("caller", "registration"), ("source", "registration"), ("description", INSTRUCTION)],
)
def test_body_cannot_supply_authority_or_replace_generated_description(
    client: TestClient, store: FakeIdentityStore, field: str, value: str
) -> None:
    register_base(client)
    response = client.post(SEED_PATH, json=seed_payload() | {field: value})
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid identity value"}
    assert store.get_profile("char-a") is None


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_free_personality_updates_are_rejected(client: TestClient, method: str) -> None:
    register_profile(client)
    before = client.get(ROOT + "/identity/current").json()
    assert client.request(
        method, ROOT + "/personality-profiles/1", json=seed_payload()
    ).is_client_error
    assert client.get(ROOT + "/identity/current").json() == before


def test_observation_instructions_cannot_change_identity(client: TestClient) -> None:
    register_profile(client)
    before = client.get(ROOT + "/identity/current").json()
    assert client.post("/sessions", json=session_payload()).is_success
    assert client.post(
        "/turns",
        json=turn_payload()
        | {
            "kind": "observation",
            "text": INSTRUCTION,
            "actor": "char-a",
            "perceived_by": ["char-a"],
        },
    ).is_success
    assert client.get("/turns/turn-1").json()["text"] == INSTRUCTION
    assert client.get(ROOT + "/identity/current").json() == before


def test_missing_model_ports_fail_required_operations_but_allow_health_and_reads(
    store: FakeIdentityStore,
) -> None:
    with TestClient(create_app(identity_store=store, clock=lambda: REGISTERED)) as client:
        register_base(client)
        assert client.post(SEED_PATH, json=seed_payload()).status_code == 503
        assert client.get(ROOT + "/identity/current").status_code == 200
        assert client.get("/health").json() == {"status": "ok"}
    assert store.get_profile("char-a") is None


@pytest.mark.parametrize("change", ["missing", "unknown"])
def test_http_requires_exact_aspect_keys(
    client: TestClient, store: FakeIdentityStore, gateway: FakeLLMGateway, change: str
) -> None:
    register_base(client)
    numbers = aspects()
    if change == "missing":
        del numbers["Volatility"]
    else:
        numbers["Unknown"] = 0.5
    response = client.post(SEED_PATH, json=seed_payload() | {"aspects": numbers})
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid identity value"}
    assert gateway.calls == []
    assert store.get_profile("char-a") is None


def test_seed_requires_existing_temperament(
    client: TestClient, store: FakeIdentityStore, gateway: FakeLLMGateway
) -> None:
    assert client.post(SEED_PATH, json=seed_payload()).status_code == 404
    assert gateway.calls == []
    assert store.get_profile("char-a") is None


def test_instruction_text_in_temperament_and_generated_description_remains_data(
    client: TestClient, store: FakeIdentityStore, gateway: FakeLLMGateway
) -> None:
    assert client.post(ROOT + "/core-identities", json=core_payload()).is_success
    assert client.post(ROOT + "/temperaments", json={"description": INSTRUCTION}).is_success
    gateway.description = INSTRUCTION
    assert client.post(SEED_PATH, json=seed_payload()).is_success
    saved = store.get_profile("char-a", 1)
    assert saved is not None and saved.description == INSTRUCTION
    assert (
        client.get(ROOT + "/identity/current").json()["personality_profile"]["description"]
        == INSTRUCTION
    )
    assert client.post(SEED_PATH, json=seed_payload()).status_code == 409
    assert store.get_profile("char-a", 2) is None
