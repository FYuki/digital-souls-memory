from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from time import monotonic
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import DerivedMemory, ExtractionProposal
from digital_souls_memory.memory.ports import SurfacePage, WorkCursor, WorkPage
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.memory.worker import MemoryWorker
from digital_souls_memory.memory_http import memory_router
from tests.evidence_support import RECORDED, TEXT, receipt, session_payload, turn_payload
from tests.it1.test_memory_extraction import Harness
from tests.memory_fakes import EchoGateway, FakeMemoryStore
from tests.memory_support import memory

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("relation", [None, "derives", "updates", "contradicts"])
@pytest.mark.parametrize("object_value", ["coffee", "tea"])
def test_fact_relation_controls_update_and_restatement(
    relation: str | None, object_value: str
) -> None:
    from digital_souls_memory.memory.domain import MemoryRelation
    from tests.evidence_support import turn
    from tests.memory_support import NEW_ID, OLD_ID

    h, store, service, worker = setup_worker()
    h.evidence.add_turn(turn(actor="user-a"))
    h.gateway.output = (memory(),)
    worker.process("char-b", "turn-1")
    h.evidence.add_turn(replace(turn(turn_id="turn-2", actor="user-a"), occurred_at=RECORDED))
    candidate = replace(
        memory(), id=NEW_ID, object=object_value, source_turn_ids=("turn-2",), valid_from=RECORDED
    )
    h.gateway.output = ExtractionProposal(
        memories=(candidate,),
        relations=()
        if relation is None
        else (MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type=relation),),
    )
    worker.process("char-b", "turn-2")
    old = store.get_memory("char-b", OLD_ID)
    assert old is not None
    restatement = relation is None and object_value == "coffee"
    assert old.valid_to == (RECORDED if relation == "updates" else None)
    assert old.last_reinforced_at == (RECORDED if restatement else None)
    assert old.source_turn_ids == (("turn-1", "turn-2") if restatement else ("turn-1",))
    expected_ids = (
        {OLD_ID}
        if restatement
        else ({NEW_ID} if relation in ("updates", "contradicts") else {OLD_ID, NEW_ID})
    )
    assert {v.id for v in service.active("char-b")} == expected_ids
    app = FastAPI()
    app.include_router(memory_router(lambda: service))
    with TestClient(app) as client:
        response = client.get("/characters/char-b/memories")
        assert response.is_success
        assert {v["id"] for v in response.json()} == expected_ids
    with store.list_relations("char-b", NEW_ID) as values:
        assert {r.type for r in values} == (
            {"extends"} if restatement else {relation} if relation else set()
        )


def setup_worker() -> tuple[Harness, FakeMemoryStore, MemoryService, MemoryWorker]:
    h = Harness(reports_delivery=False)
    store = FakeMemoryStore(h.evidence)
    service = MemoryService(store, h.extractor.inputs, lambda: h.now)
    return h, store, service, MemoryWorker(service, h.extractor)


def test_background_scan_bounds_pages_and_outstanding_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Lock

    from tests.evidence_support import turn

    h, store, _, worker = setup_worker()
    for surface in range(9):
        for item in range(65):
            h.evidence.add_turn(
                replace(turn(turn_id=f"turn-{surface}-{item}"), surface=f"surface-{surface}")
            )
    entered, release = Event(), Event()
    lock = Lock()
    processed: list[tuple[str, str]] = []
    surface_reads: list[int] = []
    work_reads: list[int] = []
    surfaces = store.surface_page
    notifications = store.work_page

    def read_surfaces(*, after: str | None, limit: int) -> SurfacePage:
        page = surfaces(after=after, limit=limit)
        surface_reads.append(len(page.surfaces))
        return page

    def read_work(surface: str, *, after: WorkCursor | None, limit: int) -> WorkPage:
        page = notifications(surface, after=after, limit=limit)
        work_reads.append(len(page.items))
        return page

    def consume(character: str, turn_id: str) -> None:
        with lock:
            processed.append((character, turn_id))
            if len(processed) == 4:
                entered.set()
        assert release.wait(3)

    monkeypatch.setattr(store, "surface_page", read_surfaces)
    monkeypatch.setattr(store, "work_page", read_work)
    monkeypatch.setattr(worker, "process", consume)
    with (
        ThreadPoolExecutor(max_workers=4) as surfaces_pool,
        ThreadPoolExecutor(max_workers=1) as scan,
    ):
        pending = scan.submit(worker._scan, surfaces_pool)
        try:
            assert entered.wait(2)
            assert surface_reads == [4]
            assert len(work_reads) == 4
            assert max(work_reads) <= 64
        finally:
            release.set()
        pending.result(timeout=5)
    assert len(processed) == 9 * 65 * 2
    assert len(set(processed)) == len(processed)
    assert max(surface_reads) <= 4 and max(work_reads) <= 64


@pytest.mark.parametrize("interruption", ["stop", "failure", "unclaimed"])
def test_notification_scan_releases_lease_and_restarts_from_beginning(
    monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager
    from datetime import datetime

    from digital_souls_memory.memory.ports import MemoryUnavailable
    from tests.evidence_support import turn

    h, store, _, worker = setup_worker()
    for i in range(34):
        h.evidence.add_turn(turn(turn_id=f"turn-{i}"))
    leases: list[str] = []
    consumed: list[tuple[str, str]] = []
    original = store.work_page
    failed = False

    @contextmanager
    def claim(surface: str, now: datetime) -> Iterator[bool]:
        leases.append(surface)
        try:
            yield interruption != "unclaimed"
        finally:
            leases.remove(surface)

    def read(surface: str, *, after: WorkCursor | None, limit: int) -> WorkPage:
        nonlocal failed
        if interruption == "failure" and after is not None and not failed:
            failed = True
            raise MemoryUnavailable()
        return original(surface, after=after, limit=limit)

    def consume(character: str, turn_id: str) -> None:
        consumed.append((character, turn_id))
        if interruption == "stop":
            worker.stop.set()

    monkeypatch.setattr(store, "claim_surface", claim)
    monkeypatch.setattr(store, "work_page", read)
    monkeypatch.setattr(worker, "process", consume)
    if interruption == "failure":
        with pytest.raises(MemoryUnavailable):
            worker._surface("synthetic-chat")
    else:
        worker._surface("synthetic-chat")
    assert leases == []
    assert len(consumed) == {"stop": 1, "failure": 64, "unclaimed": 0}[interruption]
    worker.stop.clear()
    interruption = "resume"
    consumed.clear()
    worker._surface("synthetic-chat")
    assert leases == []
    assert len(consumed) == len(set(consumed)) == 68


@pytest.mark.parametrize("reply_actor", ["char-b", "char-c"])
def test_background_scan_revisits_comment_after_owner_reply(reply_actor: str) -> None:
    from tests.evidence_support import turn

    h, _, service, worker = setup_worker()
    h.extractor.inputs.conversation_surfaces = frozenset()
    h.extractor.inputs.comment_surfaces = frozenset({"synthetic-chat"})
    h.evidence.add_turn(turn(actor="user-a"))
    h.gateway.output = (memory(),)
    worker.start()
    try:
        eventually(lambda: service.store.get_extraction("char-b", "turn-1") is not None)
        assert service.active("char-b") == ()
        h.evidence.add_turn(turn(turn_id="reply", actor=reply_actor, reply_to_turn_id="turn-1"))
        eventually(lambda: service.store.get_extraction("char-b", "reply") is not None)
        if reply_actor == "char-b":
            eventually(lambda: len(service.active("char-b")) == 1)
            assert service.active("char-b")[0].source_turn_ids == ("turn-1",)
        else:
            assert service.active("char-b") == ()
    finally:
        worker.close()


@pytest.mark.parametrize("target_first", [False, True])
def test_same_proposal_dependency_survives_order_and_rebuild(target_first: bool) -> None:
    from digital_souls_memory.memory.domain import MemoryRelation
    from tests.evidence_support import turn
    from tests.memory_support import NEW_ID, OLD_ID

    h, store, service, worker = setup_worker()
    h.evidence.add_turn(turn(actor="user-a"))
    fact = replace(memory(), id=OLD_ID if target_first else NEW_ID)
    summary = replace(memory("summary"), id=NEW_ID if target_first else OLD_ID)
    relation = MemoryRelation(from_id=summary.id, to_id=fact.id, type="derives")
    h.gateway.output = ExtractionProposal(memories=(summary, fact), relations=(relation,))
    worker.process("char-b", "turn-1")
    assert {v.id for v in service.active("char-b")} == {OLD_ID, NEW_ID}
    for _ in range(2):
        with store.transaction("char-b") as tx:
            service.rebuild(tx)
        worker.process("char-b", "turn-1")
        assert {v.id for v in service.active("char-b")} == {OLD_ID, NEW_ID}
        with store.list_relations("char-b", summary.id) as values:
            assert tuple(values) == (relation,)
    app = FastAPI()
    app.include_router(memory_router(lambda: service))
    with TestClient(app) as client:
        response = client.get("/characters/char-b/memories")
        assert response.is_success
        assert {v["id"] for v in response.json()} == {OLD_ID, NEW_ID}


def test_old_extends_resolves_target_and_keeps_latest_reinforcement() -> None:
    from digital_souls_memory.memory.domain import MemoryRelation
    from tests.evidence_support import OCCURRED, turn
    from tests.memory_support import NEW_ID, OLD_ID

    h, store, service, worker = setup_worker()
    h.evidence.add_turn(turn(actor="user-a"))
    original = replace(memory(), last_reinforced_at=RECORDED)
    h.gateway.output = (original,)
    worker.process("char-b", "turn-1")
    h.evidence.add_turn(
        replace(turn(turn_id="turn-2", actor="user-a"), occurred_at=OCCURRED - timedelta(days=1))
    )
    h.gateway.output = ExtractionProposal(
        memories=(
            replace(
                memory("summary"),
                id=NEW_ID,
                source_turn_ids=("turn-2",),
                valid_from=OCCURRED - timedelta(days=1),
            ),
        ),
        relations=(MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type="extends"),),
    )
    worker.process("char-b", "turn-2")
    for _ in range(2):
        with store.transaction("char-b") as tx:
            service.rebuild(tx)
        worker.process("char-b", "turn-2")
        (value,) = service.active("char-b")
        assert value.id == OLD_ID
        assert value.source_turn_ids == ("turn-1", "turn-2")
        assert value.last_reinforced_at == RECORDED
        assert (value.epistemics.source_type, value.epistemics.trust_tier) == ("inferred", "D")


@pytest.mark.parametrize("kind", ["observation", "utterance"])
@pytest.mark.parametrize("accepted_by_gate", [True, False])
def test_observation_fact_subject_and_gate_reach_http(kind: str, accepted_by_gate: bool) -> None:
    from tests.evidence_support import turn

    h, _, service, worker = setup_worker()
    h.evidence.add_turn(
        turn(kind=kind, address_mode="direct", addressees=("user-a",), text="Bが入ってきた")
    )
    candidate = replace(memory(), subject="B", content="Bが入ってきた")
    if not accepted_by_gate:
        candidate = replace(
            candidate,
            epistemics=replace(
                candidate.epistemics,
                confidence_features=replace(
                    candidate.epistemics.confidence_features,
                    retrieval=0,
                    source=0,
                    temporal=0,
                    corroboration=0,
                    contradiction=1,
                ),
            ),
        )
    h.gateway.output = (candidate,)
    worker.process("char-b", "turn-1")
    app = FastAPI()
    app.include_router(memory_router(lambda: service))
    with TestClient(app) as client:
        response = client.get("/characters/char-b/memories")
        assert response.is_success
        assert [(v["subject"], v["source_turn_ids"]) for v in response.json()] == (
            [("B", ["turn-1"])] if kind == "observation" and accepted_by_gate else []
        )


def eventually(check: object) -> None:
    from collections.abc import Callable

    predicate = cast(Callable[[], bool], check)
    deadline = monotonic() + 3
    wake = Event()
    while monotonic() < deadline:
        if predicate():
            return
        wake.wait(0.01)
    assert predicate()


def test_fake_http_registration_extracts_without_blocking_model_and_preserves_perception() -> None:
    h, store, _, _ = setup_worker()
    started, release = Event(), Event()
    original = h.gateway.extract_local

    def blocked(value: object) -> tuple[DerivedMemory, ...] | ExtractionProposal:
        from digital_souls_memory.memory.extraction import LocalOnly

        started.set()
        assert release.wait(3)
        return original(cast(LocalOnly, value))

    h.gateway.extract_local = blocked  # type: ignore[method-assign]
    h.gateway.output = (replace(memory(), subject="char-c"),)
    with TestClient(
        create_app(
            store=h.evidence,
            identity_store=h.identity,
            memory_store=store,
            extraction_gateway=h.gateway,
            clock=lambda: h.now,
        )
    ) as client:
        assert (
            client.post(
                "/sessions",
                json=session_payload() | {"session_id": "new-session", "reports_delivery": False},
            ).status_code
            == 201
        )
        try:
            assert (
                client.post(
                    "/turns",
                    json=turn_payload()
                    | {"session_id": "new-session", "perceived_by": ["char-b", "char-c"]},
                ).status_code
                == 201
            )
            assert started.wait(2)
            assert client.get("/characters/char-b/memories").json() == []
        finally:
            release.set()
        eventually(lambda: bool(client.get("/characters/char-b/memories").json()))
        assert client.get("/characters/char-a/memories").json() == []
        assert client.get("/turns/turn-1").json()["text"] == TEXT


def test_invalid_model_batch_is_failed_without_partial_writes_and_requires_requeue() -> None:
    from tests.evidence_support import turn

    h, store, service, worker = setup_worker()
    h.evidence.add_turn(turn())
    h.gateway.output = (
        replace(memory(), subject="char-c"),
        cast(DerivedMemory, {"policy": "grant"}),
    )
    worker.process("char-b", "turn-1")
    assert service.active("char-b") == ()
    state = store.get_extraction("char-b", "turn-1")
    assert state is not None and state.status == "failed"
    calls = len(h.gateway.local_calls)
    worker.process("char-b", "turn-1")
    assert len(h.gateway.local_calls) == calls
    h.gateway.output = (replace(memory(), subject="char-c"),)
    store.requeue("turn-1")
    worker.process("char-b", "turn-1")
    assert len(service.active("char-b")) == 1


def test_receipt_during_model_call_discards_stale_input_and_duplicate_worker_has_one_effect() -> (
    None
):
    from tests.evidence_support import turn

    h, store, service, worker = setup_worker()
    h.evidence.sessions["session-1"] = replace(
        h.evidence.sessions["session-1"], reports_delivery=True
    )
    h.evidence.add_turn(turn())
    h.now += timedelta(minutes=10)
    started, release = Event(), Event()
    original = h.gateway.extract_local

    def blocked(value: object) -> tuple[DerivedMemory, ...] | ExtractionProposal:
        from digital_souls_memory.memory.extraction import LocalOnly

        started.set()
        assert release.wait(3)
        return original(cast(LocalOnly, value))

    h.gateway.extract_local = blocked  # type: ignore[method-assign]
    h.gateway.output = (replace(memory(), subject="char-c"),)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(worker.process, "char-b", "turn-1")
        try:
            assert started.wait(2)
            worker.process("char-b", "turn-1")
            h.evidence.add_receipt(receipt("none", 0))
        finally:
            release.set()
        pending.result(timeout=3)
    assert service.active("char-b") == ()
    assert len(h.gateway.local_calls) == 1
    worker.process("char-b", "turn-1")
    assert store.values == {}


def test_failed_model_retries_after_due_time_without_modifying_original() -> None:
    from tests.evidence_support import turn

    h, store, service, worker = setup_worker()
    source = turn()
    h.evidence.add_turn(source)
    h.gateway.fail = True
    worker.process("char-b", "turn-1")
    state = store.get_extraction("char-b", "turn-1")
    assert state is not None and state.status == "retry"
    assert service.active("char-b") == ()
    h.gateway.fail = False
    h.gateway.output = (replace(memory(), subject="char-c"),)
    h.now += timedelta(seconds=1)
    worker.process("char-b", "turn-1")
    assert len(service.active("char-b")) == 1
    assert h.evidence.get_turn("turn-1") == source


def test_commit_failure_rolls_back_projection_and_processed_state_before_retry() -> None:
    from digital_souls_memory.memory.extraction import LocalOnly
    from tests.evidence_support import turn

    h, store, service, worker = setup_worker()
    h.evidence.add_turn(turn())
    h.gateway.output = (replace(memory(), subject="char-c"),)
    original = h.gateway.extract_local

    def fail_commit(value: LocalOnly) -> tuple[DerivedMemory, ...] | ExtractionProposal:
        store.fail_commit = True
        return original(value)

    h.gateway.extract_local = fail_commit  # type: ignore[method-assign]
    worker.process("char-b", "turn-1")
    assert store.values == {}
    assert store.proposals == {}
    assert store.relations == []
    state = store.get_extraction("char-b", "turn-1")
    assert state is not None and state.status == "retry"
    h.gateway.extract_local = original  # type: ignore[method-assign]
    h.now += timedelta(seconds=1)
    worker.process("char-b", "turn-1")
    assert len(service.active("char-b")) == 1
    worker.process("char-b", "turn-1")
    assert len(store.values) == 1


def test_contradicting_original_supersedes_old_memory_at_query_boundary_and_is_reversible() -> None:
    from digital_souls_memory.memory.domain import MemoryRelation
    from tests.evidence_support import turn
    from tests.memory_support import NEW_ID, OLD_ID

    h, store, service, worker = setup_worker()
    h.evidence.sessions["session-1"] = replace(
        h.evidence.sessions["session-1"], reports_delivery=True
    )
    h.now += timedelta(minutes=10)
    h.evidence.add_turn(turn(address_mode="direct", addressees=("user-a",)))
    h.gateway.output = (memory(),)
    worker.process("char-b", "turn-1")
    assert service.active("char-b") == (memory(),)
    h.evidence.add_turn(turn(turn_id="turn-2", address_mode="direct", addressees=("user-a",)))
    newer = replace(
        memory(),
        id=NEW_ID,
        source_turn_ids=("turn-2",),
        kind="summary",
        subject=None,
        predicate=None,
        object=None,
        content="訂正された情報",
    )
    h.gateway.output = ExtractionProposal(
        memories=(newer,),
        relations=(MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type="contradicts"),),
    )
    worker.process("char-b", "turn-2")
    assert service.active("char-b") == (newer,)
    old = store.get_memory("char-b", OLD_ID)
    assert old is not None and old.superseded_at == h.now
    assert old.content == memory().content
    h.evidence.add_receipt(replace(receipt("none", 0), turn_id="turn-2"))
    worker.process("char-b", "turn-2")
    assert service.active("char-b") == (memory(),)


def test_memory_extraction_update_failure_and_retry_leave_registered_identity_unchanged() -> None:
    from digital_souls_memory.identity.domain import Temperament
    from digital_souls_memory.memory.domain import MemoryRelation
    from tests.evidence_support import INSTRUCTION, turn
    from tests.memory_support import NEW_ID

    h, store, service, worker = setup_worker()
    h.identity.temperaments[("char-b", 1)] = Temperament("char-b", "合成気質", 1, RECORDED)
    before = h.identity.get_current("char-b", RECORDED)
    h.evidence.add_turn(turn(actor="user-a", text=INSTRUCTION))
    h.gateway.output = (memory(),)
    worker.process("char-b", "turn-1")
    h.evidence.add_turn(replace(turn(turn_id="turn-2", actor="user-a"), occurred_at=RECORDED))
    h.gateway.fail = True
    worker.process("char-b", "turn-2")
    h.now += timedelta(seconds=1)
    h.gateway.fail = False
    h.gateway.output = ExtractionProposal(
        memories=(
            replace(
                memory(),
                id=NEW_ID,
                object="tea",
                content="紅茶",
                source_turn_ids=("turn-2",),
                valid_from=RECORDED,
            ),
        ),
        relations=(MemoryRelation(from_id=NEW_ID, to_id=memory().id, type="updates"),),
    )
    worker.process("char-b", "turn-2")
    assert [v.object for v in service.active("char-b")] == ["tea"]
    assert h.identity.get_current("char-b", RECORDED) == before
    assert len(h.identity.cores) == 6 and len(h.identity.temperaments) == 1


@pytest.mark.parametrize("status,end", [("partial", 9), ("none", 0), ("full", len(TEXT))])
def test_fake_http_delivery_limits_saved_memory_and_actual_gateway_input(
    status: str, end: int
) -> None:
    from tests.evidence_support import receipt_payload
    from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities

    evidence = FakeEvidenceStore()
    store = FakeMemoryStore(evidence)
    gateway = EchoGateway()
    with TestClient(
        create_app(
            store=evidence,
            identity_store=registered_identities(),
            memory_store=store,
            extraction_gateway=gateway,
            clock=lambda: RECORDED,
        )
    ) as client:
        assert client.post("/sessions", json=session_payload()).is_success
        assert client.post("/turns", json=turn_payload()).is_success
        assert client.post("/turns/turn-1/receipts", json=receipt_payload(status, end)).is_success
        eventually(
            lambda: (state := store.get_extraction("char-b", "turn-1")) is not None
            and state.status == "done"
        )
        eventually(lambda: bool(client.get("/characters/char-c/memories").json()))
        assert [v["content"] for v in client.get("/characters/char-b/memories").json()] == (
            [TEXT[:end]] if end else []
        )
        assert [v["content"] for v in client.get("/characters/char-c/memories").json()] == [TEXT]
        assert [t.text for v in gateway.inputs if v.character_id == "char-b" for t in v.turns] == (
            [TEXT[:end]] if end else []
        )
        assert client.get("/turns/turn-1").json()["text"] == TEXT


@pytest.mark.parametrize(
    "actor,kind,perceived,mode,targets",
    [
        ("char-c", "thought", ["char-c"], "self", []),
        ("char-c", "utterance", ["char-c"], "direct", ["user-a"]),
        ("char-c", "utterance", ["char-b", "char-c"], "direct", ["char-b"]),
        ("user-a", "utterance", [], "broadcast", []),
    ],
)
def test_fake_http_perception_thought_and_whispers_reach_only_declared_owners(
    actor: str,
    kind: str,
    perceived: list[str],
    mode: str,
    targets: list[str],
) -> None:
    from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities

    evidence = FakeEvidenceStore()
    store = FakeMemoryStore(evidence)
    gateway = EchoGateway()
    with TestClient(
        create_app(
            store=evidence,
            identity_store=registered_identities(),
            memory_store=store,
            extraction_gateway=gateway,
            clock=lambda: RECORDED,
            conversation_surfaces=frozenset({"synthetic-chat"}),
        )
    ) as client:
        assert client.post(
            "/sessions", json=session_payload() | {"reports_delivery": False}
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
            eventually(lambda c=character: bool(client.get(f"/characters/{c}/memories").json()))
        for character in ("char-a", "char-b", "char-c"):
            values = client.get(f"/characters/{character}/memories").json()
            assert [v["content"] for v in values] == ([TEXT] if character in perceived else [])
        assert set(gateway.calls) == set(perceived)


@pytest.mark.parametrize("relation_type", ["extends", "derives"])
def test_receipt_invalidating_relation_target_withdraws_dependent_contribution(
    relation_type: str,
) -> None:
    from digital_souls_memory.memory.domain import MemoryRelation
    from tests.evidence_support import turn
    from tests.memory_support import NEW_ID, OLD_ID

    h, store, service, worker = setup_worker()
    h.evidence.sessions["session-1"] = replace(
        h.evidence.sessions["session-1"], reports_delivery=True
    )
    h.now += timedelta(minutes=10)
    h.evidence.add_turn(turn(address_mode="direct", addressees=("user-a",)))
    h.gateway.output = (memory(),)
    worker.process("char-b", "turn-1")
    h.evidence.add_turn(turn(turn_id="turn-2", address_mode="direct", addressees=("user-a",)))
    h.gateway.output = ExtractionProposal(
        memories=(replace(memory("summary"), id=NEW_ID, source_turn_ids=("turn-2",)),),
        relations=(MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type=relation_type),),
    )
    worker.process("char-b", "turn-2")
    assert len(service.active("char-b")) == (1 if relation_type == "extends" else 2)
    h.evidence.add_receipt(receipt("none", 0))
    worker.process("char-b", "turn-1")
    assert service.active("char-b") == ()
    state = store.get_extraction("char-b", "turn-1")
    assert state is not None and state.status == "done"


def test_background_worker_recovers_from_evidence_dependency_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.evidence.domain import Turn
    from digital_souls_memory.evidence.ports import EvidenceUnavailable
    from tests.evidence_support import turn

    h, store, service, worker = setup_worker()
    h.evidence.add_turn(turn())
    h.gateway.output = (replace(memory(), subject="char-c"),)
    failed = Event()
    original = h.evidence.get_turn

    def unavailable_once(identifier: str) -> Turn | None:
        if not failed.is_set():
            failed.set()
            raise EvidenceUnavailable()
        return original(identifier)

    monkeypatch.setattr(h.evidence, "get_turn", unavailable_once)
    worker.start()
    try:
        assert failed.wait(2)
        eventually(lambda: len(service.active("char-b")) == 1)
        state = store.get_extraction("char-b", "turn-1")
        assert state is not None and state.status == "done"
    finally:
        worker.close()


def test_http_reextraction_reuses_original_without_duplicate_results_and_reports_state() -> None:
    from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities

    evidence = FakeEvidenceStore()
    store = FakeMemoryStore(evidence)
    gateway = EchoGateway()
    with TestClient(
        create_app(
            store=evidence,
            identity_store=registered_identities(),
            memory_store=store,
            extraction_gateway=gateway,
            clock=lambda: RECORDED,
        )
    ) as client:
        assert client.post(
            "/sessions", json=session_payload() | {"reports_delivery": False}
        ).is_success
        assert client.post("/turns", json=turn_payload()).is_success
        eventually(lambda: len(client.get("/characters/char-b/memories").json()) == 1)
        before = client.get("/characters/char-b/memories").json()
        assert client.post("/turns/turn-1/extractions").status_code == 202
        eventually(lambda: gateway.calls.count("char-b") == 2)
        eventually(
            lambda: client.get("/characters/char-b/extractions/turn-1").json()["status"] == "done"
        )
        assert client.get("/characters/char-b/memories").json() == before
        state = client.get("/characters/char-b/extractions/turn-1")
        assert state.status_code == 200 and state.json()["delivery_basis"] == "declared_full"
        assert client.post("/turns/missing/extractions").status_code == 404
        assert client.get("/characters/char-a/extractions/turn-1").status_code == 404
        assert client.get("/turns/turn-1").json()["text"] == TEXT


def test_memory_http_without_dependencies_is_unavailable_while_health_remains_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMORY_DATABASE_URL", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/characters/char-b/memories").status_code == 503


@pytest.mark.parametrize("correction_before_read", [True, False])
def test_http_read_does_not_attach_corrected_state_to_old_content(
    monkeypatch: pytest.MonkeyPatch, correction_before_read: bool
) -> None:
    from tests.evidence_support import turn

    h, store, service, worker = setup_worker()
    h.evidence.sessions["session-1"] = replace(
        h.evidence.sessions["session-1"], reports_delivery=True
    )
    h.now += timedelta(minutes=10)
    h.evidence.add_turn(turn())
    h.gateway.output = (replace(memory(), subject="char-c", content=TEXT),)
    worker.process("char-b", "turn-1")
    captured, release = Event(), Event()
    original = h.materials.get_decision
    h.evidence.add_receipt(receipt("partial", 9))

    def correct() -> None:
        h.gateway.output = (
            replace(
                memory(),
                id="00000000-0000-4000-8000-000000000002",
                subject="char-c",
                content=TEXT[:9],
            ),
        )
        worker.process("char-b", "turn-1")
        assert [v.content for v in service.active("char-b")] == [TEXT[:9]]

    from digital_souls_memory.evidence.materials import MaterialDecision

    def pause_once(character: str, turn_id: str) -> MaterialDecision | None:
        if not captured.is_set():
            captured.set()
            assert release.wait(3)
        return original(character, turn_id)

    app = FastAPI()
    app.include_router(memory_router(lambda: service))
    if correction_before_read:
        correct()
    else:
        monkeypatch.setattr(h.materials, "get_decision", pause_once)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.get, "/characters/char-b/memories")
        try:
            if not correction_before_read:
                assert captured.wait(2)
                correct()
        finally:
            release.set()
        response = pending.result(timeout=3)
        assert response.is_success
        assert [v["content"] for v in response.json()] == (
            [TEXT[:9]] if correction_before_read else []
        )
        assert [v["source_turn_ids"] for v in response.json()] == (
            [["turn-1"]] if correction_before_read else []
        )
