from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import monotonic

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import DeliveryBasis
from digital_souls_memory.memory.inputs import MemoryInputs
from tests.evidence_support import (
    OCCURRED,
    RECORDED,
    TEXT,
    receipt_payload,
    session_payload,
    turn_payload,
)
from tests.memory_fakes import EchoGateway, ExplicitUpdatesGateway

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("correction_before_read", [True, False])
def test_postgres_http_read_and_partial_correction_interleave(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, correction_before_read: bool
) -> None:
    from fastapi import FastAPI

    from digital_souls_memory.adapters.identity_characters import IdentityCharacters
    from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
    from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
    from digital_souls_memory.evidence.materials import EvidenceMaterials, MaterialDecision
    from digital_souls_memory.memory.extraction import MemoryExtractor
    from digital_souls_memory.memory.use_cases import MemoryService
    from digital_souls_memory.memory.worker import MemoryWorker
    from digital_souls_memory.memory_http import memory_router
    from tests.evidence_support import receipt, session, turn

    with TestClient(create_app()) as registration:
        register_characters(registration)
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    evidence.add_turn(turn())
    now = RECORDED + timedelta(minutes=10)
    materials = EvidenceMaterials(
        evidence,
        IdentityCharacters(lambda: PostgresIdentityStore(app_database_url)),
        lambda: now,
        timedelta(minutes=10),
    )
    store = PostgresMemoryStore(app_database_url)
    service = MemoryService(
        store,
        MemoryInputs(
            evidence,
            materials,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            comment_surfaces=frozenset(),
        ),
        lambda: now,
    )
    gateway = EchoGateway()
    extractor = MemoryExtractor(
        inputs=service.inputs,
        gateway=gateway,
        non_sensitive_labels=frozenset(),
    )
    worker = MemoryWorker(service, extractor)
    worker.process("char-b", "turn-1")
    worker.process("char-c", "turn-1")
    assert active_contents(store, "char-b") == [TEXT]
    captured, release = Event(), Event()
    original = materials.get_decision
    evidence.add_receipt(receipt("partial", 9))

    def pause_once(character: str, turn_id: str) -> MaterialDecision | None:
        if not captured.is_set():
            captured.set()
            assert release.wait(5)
        return original(character, turn_id)

    def correct() -> None:
        worker.process("char-b", "turn-1")
        assert active_contents(store, "char-b") == [TEXT[:9]]

    if correction_before_read:
        correct()
    else:
        monkeypatch.setattr(materials, "get_decision", pause_once)
    app = FastAPI()
    app.include_router(memory_router(lambda: service))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.get, "/characters/char-b/memories")
        try:
            if not correction_before_read:
                assert captured.wait(3)
                correct()
        finally:
            release.set()
        response = pending.result(timeout=5)
        assert response.is_success
        assert [v["content"] for v in response.json()] == (
            [TEXT[:9]] if correction_before_read else []
        )
        assert [v["content"] for v in client.get("/characters/char-b/memories").json()] == [
            TEXT[:9]
        ]
        assert [v["content"] for v in client.get("/characters/char-c/memories").json()] == [TEXT]


def eventually(check: Callable[[], bool]) -> None:
    deadline = monotonic() + 5
    wake = Event()
    while monotonic() < deadline:
        if check():
            return
        wake.wait(0.01)
    assert check(), "Synthetic worker did not reach the observed state"


def register_characters(client: TestClient) -> None:
    for identifier in ("char-b", "char-c"):
        assert client.post(
            f"/characters/{identifier}/core-identities",
            json={"values": ["誠実"], "prohibitions": []},
        ).is_success


def active_contents(store: PostgresMemoryStore, character: str) -> list[str]:
    with store.list_memories(character) as values:
        return [v.content for v in values if v.superseded_at is None and v.valid_to is None]


def test_registered_turn_returns_while_model_is_blocked_then_publishes_memory(
    app_database_url: str,
) -> None:
    store = PostgresMemoryStore(app_database_url)
    gateway = EchoGateway()
    gateway.release.clear()
    with TestClient(
        create_app(memory_store=store, extraction_gateway=gateway, clock=lambda: RECORDED)
    ) as client:
        try:
            register_characters(client)
            assert client.post(
                "/sessions", json=session_payload() | {"reports_delivery": False}
            ).is_success
            with ThreadPoolExecutor(max_workers=1) as pool:
                registration = pool.submit(client.post, "/turns", json=turn_payload())
                try:
                    assert registration.result(timeout=2).status_code == 201
                    assert gateway.started.wait(2)
                    assert active_contents(store, "char-b") == []
                finally:
                    gateway.release.set()
        finally:
            gateway.release.set()
        eventually(lambda: active_contents(store, "char-b") == [TEXT])
        response = client.get("/characters/char-b/memories")
        assert response.is_success
        assert [value["content"] for value in response.json()] == [TEXT]
        assert client.get("/turns/turn-1").json()["text"] == TEXT


@pytest.mark.parametrize("second_text", ["coffee", "tea"])
def test_late_none_removes_reinforcement_or_fact_update_contribution(
    app_database_url: str,
    second_text: str,
) -> None:
    gateway = ExplicitUpdatesGateway()
    store = PostgresMemoryStore(app_database_url)
    now = RECORDED
    with TestClient(
        create_app(memory_store=store, extraction_gateway=gateway, clock=lambda: now)
    ) as client:
        register_characters(client)
        assert client.post("/sessions", json=session_payload()).is_success
        assert client.post(
            "/turns",
            json=turn_payload()
            | {
                "turn_id": "seed-turn",
                "text": "coffee",
            },
        ).is_success
        assert client.post(
            "/turns/seed-turn/receipts", json=receipt_payload("full", len("coffee"))
        ).is_success
        eventually(lambda: active_contents(store, "char-b") == ["coffee"])
        with store.list_memories("char-b") as values:
            original = next(v for v in values if v.superseded_at is None)
        happened = OCCURRED + timedelta(hours=1)
        assert client.post(
            "/turns",
            json=turn_payload()
            | {
                "text": second_text,
                "occurred_at": happened.isoformat(),
            },
        ).is_success
        now += timedelta(minutes=10)

        def applied() -> bool:
            old = store.get_memory("char-b", original.id)
            if old is None:
                return False
            if second_text == "coffee":
                return old.last_reinforced_at == happened and "turn-1" in old.source_turn_ids
            return old.valid_to == happened and second_text in active_contents(store, "char-b")

        eventually(applied)
        old = store.get_memory("char-b", original.id)
        assert old is not None
        assert old.content == "coffee"
        assert client.post("/turns/turn-1/receipts", json=receipt_payload("none", 0)).is_success

        def corrected() -> bool:
            current = store.get_memory("char-b", original.id)
            return (
                current is not None
                and current.valid_to is None
                and current.last_reinforced_at == original.last_reinforced_at
                and current.source_turn_ids == original.source_turn_ids
                and active_contents(store, "char-b") == ["coffee"]
            )

        eventually(corrected)
        response = client.get("/characters/char-b/memories")
        assert response.is_success
        assert [v["content"] for v in response.json()] == ["coffee"]
        assert client.get("/turns/turn-1").json()["text"] == second_text


def test_pending_work_resumes_with_original_recorded_time_and_custom_timeout(
    app_database_url: str,
) -> None:
    gateway = EchoGateway()
    store = PostgresMemoryStore(app_database_url)
    now = RECORDED
    with TestClient(
        create_app(
            memory_store=store,
            extraction_gateway=gateway,
            clock=lambda: now,
            delivery_timeout=timedelta(seconds=30),
        )
    ) as client:
        register_characters(client)
        assert client.post("/sessions", json=session_payload()).is_success
        assert client.post("/turns", json=turn_payload()).is_success

        def pending() -> bool:
            state = store.get_extraction("char-b", "turn-1")
            return state is not None and state.delivery_basis == DeliveryBasis.PENDING

        eventually(pending)
    now += timedelta(seconds=29)
    restored = PostgresMemoryStore(app_database_url)
    with TestClient(
        create_app(
            memory_store=restored,
            extraction_gateway=gateway,
            clock=lambda: now,
            delivery_timeout=timedelta(seconds=30),
        )
    ):
        assert pending()
        assert active_contents(restored, "char-b") == []
        now += timedelta(seconds=1)
        eventually(lambda: active_contents(restored, "char-b") == [TEXT])
        state = restored.get_extraction("char-b", "turn-1")
        assert state is not None
        assert state.delivery_basis == DeliveryBasis.ASSUMED_FULL


def test_pending_assumption_and_late_partial_then_none_reconcile_saved_memory(
    app_database_url: str,
) -> None:
    store = PostgresMemoryStore(app_database_url)
    gateway = EchoGateway()
    now = RECORDED
    with TestClient(
        create_app(memory_store=store, extraction_gateway=gateway, clock=lambda: now)
    ) as client:
        register_characters(client)
        assert client.post("/sessions", json=session_payload()).is_success
        assert client.post("/turns", json=turn_payload()).is_success

        def basis(expected: DeliveryBasis) -> bool:
            state = store.get_extraction("char-b", "turn-1")
            return state is not None and state.delivery_basis == expected

        eventually(lambda: basis(DeliveryBasis.PENDING))
        assert active_contents(store, "char-b") == []
        eventually(lambda: active_contents(store, "char-c") == [TEXT])
        now += timedelta(seconds=599)
        assert basis(DeliveryBasis.PENDING)
        assert active_contents(store, "char-b") == []
        now += timedelta(seconds=1)
        eventually(lambda: basis(DeliveryBasis.ASSUMED_FULL))
        eventually(lambda: active_contents(store, "char-b") == [TEXT])
        assert client.post("/turns/turn-1/receipts", json=receipt_payload("partial", 9)).is_success
        eventually(lambda: basis(DeliveryBasis.RECEIPT_CONFIRMED))
        eventually(lambda: active_contents(store, "char-b") == [TEXT[:9]])
        assert client.post("/turns/turn-1/receipts", json=receipt_payload("none", 0)).is_success
        eventually(lambda: active_contents(store, "char-b") == [])
        assert active_contents(store, "char-c") == [TEXT]
        assert client.get("/turns/turn-1").json()["text"] == TEXT


def test_declared_delivery_state_and_result_survive_new_app_and_adapter(
    app_database_url: str,
) -> None:
    gateway = EchoGateway()
    store = PostgresMemoryStore(app_database_url)
    with TestClient(
        create_app(memory_store=store, extraction_gateway=gateway, clock=lambda: RECORDED)
    ) as client:
        register_characters(client)
        assert client.post(
            "/sessions", json=session_payload() | {"reports_delivery": False}
        ).is_success
        assert client.post("/turns", json=turn_payload()).is_success
        eventually(lambda: active_contents(store, "char-b") == [TEXT])
        saved = store.get_extraction("char-b", "turn-1")
        assert saved is not None
        assert saved.delivery_basis == DeliveryBasis.DECLARED_FULL
    restored = PostgresMemoryStore(app_database_url)
    with TestClient(
        create_app(memory_store=restored, extraction_gateway=gateway, clock=lambda: RECORDED)
    ) as client:
        state = restored.get_extraction("char-b", "turn-1")
        assert state is not None
        assert state.delivery_basis == DeliveryBasis.DECLARED_FULL
        assert active_contents(restored, "char-b") == [TEXT]
        assert [value["content"] for value in client.get("/characters/char-b/memories").json()] == [
            TEXT
        ]


def test_longer_timeout_rechecks_existing_assumption_and_withdraws_saved_result(
    app_database_url: str,
) -> None:
    gateway = EchoGateway()
    store = PostgresMemoryStore(app_database_url)
    now = RECORDED
    with TestClient(
        create_app(
            memory_store=store,
            extraction_gateway=gateway,
            clock=lambda: now,
            delivery_timeout=timedelta(0),
        )
    ) as client:
        register_characters(client)
        assert client.post("/sessions", json=session_payload()).is_success
        assert client.post("/turns", json=turn_payload()).is_success
        eventually(lambda: active_contents(store, "char-b") == [TEXT])
    with TestClient(
        create_app(
            memory_store=store,
            extraction_gateway=gateway,
            clock=lambda: now,
            delivery_timeout=timedelta(seconds=60),
        )
    ):

        def pending() -> bool:
            state = store.get_extraction("char-b", "turn-1")
            return state is not None and state.delivery_basis == DeliveryBasis.PENDING

        eventually(pending)
        eventually(lambda: active_contents(store, "char-b") == [])
        now += timedelta(seconds=60)
        eventually(lambda: active_contents(store, "char-b") == [TEXT])


def test_late_receipt_excludes_old_memory_even_when_reextraction_fails(
    app_database_url: str,
) -> None:
    gateway = EchoGateway()
    store = PostgresMemoryStore(app_database_url)
    now = RECORDED
    with TestClient(
        create_app(memory_store=store, extraction_gateway=gateway, clock=lambda: now)
    ) as client:
        register_characters(client)
        assert client.post("/sessions", json=session_payload()).is_success
        assert client.post("/turns", json=turn_payload()).is_success
        now += timedelta(minutes=10)
        eventually(lambda: active_contents(store, "char-b") == [TEXT])
        gateway.fail = True
        assert client.post("/turns/turn-1/receipts", json=receipt_payload("partial", 9)).is_success
        eventually(lambda: active_contents(store, "char-b") == [])
        response = client.get("/characters/char-b/memories")
        assert response.is_success
        assert response.json() == []
        gateway.fail = False
        assert client.post("/turns/turn-1/receipts", json=receipt_payload("partial", 9)).is_success
        eventually(lambda: active_contents(store, "char-b") == [TEXT[:9]])
        assert client.get("/turns/turn-1").json()["text"] == TEXT


def test_two_apps_share_durable_claims_and_publish_one_effect_per_character(
    app_database_url: str,
) -> None:
    gateway = EchoGateway()
    gateway.release.clear()
    store = PostgresMemoryStore(app_database_url)
    with (
        TestClient(
            create_app(memory_store=store, extraction_gateway=gateway, clock=lambda: RECORDED)
        ) as first,
        TestClient(
            create_app(
                memory_store=PostgresMemoryStore(app_database_url),
                extraction_gateway=gateway,
                clock=lambda: RECORDED,
            )
        ),
    ):
        try:
            register_characters(first)
            assert first.post(
                "/sessions", json=session_payload() | {"reports_delivery": False}
            ).is_success
            assert first.post("/turns", json=turn_payload()).is_success
            assert gateway.started.wait(2)
        finally:
            gateway.release.set()
        eventually(
            lambda: active_contents(store, "char-b") == [TEXT]
            and active_contents(store, "char-c") == [TEXT]
        )
        assert gateway.calls.count("char-b") == 1
        assert gateway.calls.count("char-c") == 1


@pytest.mark.parametrize("delivery", [False, True])
@pytest.mark.parametrize(
    "actor,kind,perceived,mode,targets",
    [
        ("char-c", "thought", ["char-c"], "self", []),
        ("char-c", "utterance", ["char-c"], "direct", ["user-a"]),
        ("char-c", "utterance", ["char-c"], "direct", ["char-b"]),
        ("user-a", "utterance", [], "broadcast", []),
        ("user-a", "utterance", ["char-b"], "broadcast", []),
        ("char-c", "observation", ["char-b", "char-c"], "broadcast", []),
    ],
)
def test_perception_thought_and_noncharacter_input_survive_postgres_extraction(
    app_database_url: str,
    delivery: bool,
    actor: str,
    kind: str,
    perceived: list[str],
    mode: str,
    targets: list[str],
) -> None:
    gateway = EchoGateway()
    store = PostgresMemoryStore(app_database_url)
    with TestClient(
        create_app(
            memory_store=store,
            extraction_gateway=gateway,
            clock=lambda: RECORDED,
            conversation_surfaces=frozenset({"synthetic-chat"}),
        )
    ) as client:
        register_characters(client)
        assert client.post(
            "/sessions", json=session_payload() | {"reports_delivery": delivery}
        ).is_success
        assert client.post(
            "/turns",
            json=turn_payload()
            | {
                "actor": actor,
                "kind": kind,
                "perceived_by": perceived,
                "address_mode": mode,
                "addressees": targets,
            },
        ).is_success
        for character in perceived:

            def extracted(character_id: str = character) -> bool:
                return active_contents(store, character_id) == [TEXT]

            eventually(extracted)
        for character in ("char-b", "char-c"):
            assert active_contents(store, character) == ([TEXT] if character in perceived else [])
            if character not in perceived:
                assert store.get_extraction(character, "turn-1") is None
        assert set(gateway.calls) == set(perceived)
        assert all(t.text == TEXT for value in gateway.inputs for t in value.turns)
        assert client.get("/turns/turn-1").json()["text"] == TEXT
