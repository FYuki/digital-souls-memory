"""Restatement effects retain ownership, input validity and dependency ordering."""

from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import (
    DerivedMemory,
    ExtractionProposal,
    MemoryRelation,
    MemoryValidationError,
)
from digital_souls_memory.memory.extraction import LocalOnly
from digital_souls_memory.memory.projection import collect_evidence, projection_input
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import OCCURRED, RECORDED, TEXT, receipt, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import memory
from tests.postgres.test_deletion import HEADERS, seed_evidence
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_memory_continuous_ingestion import publish_and_search
from tests.postgres.test_memory_restatement import assert_consistent

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


@pytest.mark.parametrize("relation_type", ["updates", "contradicts"])
def test_withdrawn_fact_reopens_target_of_another_turns_rewritten_effect(
    database_url: str, relation_type: str
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    x = replace(memory(), id="x", object="water", source_turn_ids=("turn-x",))
    a = replace(memory(), id="a", valid_from=RECORDED)
    for candidate in (x, a):
        source = candidate.source_turn_ids[0]
        evidence.add_turn(turn(source, actor="user-a"))
        gateway.output = (candidate,)
        worker.process("char-b", source)
    evidence.add_turn(turn("turn-b", actor="user-a"))
    gateway.output = ExtractionProposal(
        memories=(replace(a, id="b", source_turn_ids=("turn-b",)),),
        relations=(MemoryRelation(from_id="b", to_id="x", type=relation_type),),
    )
    worker.process("char-b", "turn-b")
    before = store.get_memory("char-b", "x")
    assert before is not None
    assert before.valid_to == (RECORDED if relation_type == "updates" else None)
    assert before.superseded_at == (RECORDED if relation_type == "contradicts" else None)

    class WithdrawFact(RecordingRecalculationGateway):
        def recalculate_local(
            self, value: LocalOnly, *, memory_id: str, kind: str
        ) -> DerivedMemory | None:
            super().recalculate_local(value, memory_id=memory_id, kind=kind)
            return x if memory_id == "x" else None

    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            recalculation_gateway=WithdrawFact(None),
        )
    ) as client:
        assert publish_and_search(database_url) == {"a"}
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "withdraw-acting-fact", "target_turn_ids": ["turn-1"]},
        )
        assert response.status_code == 200, response.json()
        assert response.json()["status"] == "done"
        assert store.get_memory("char-b", "a") is None
        assert store.get_memory("char-b", "x") == x
        with store.list_relations("char-b", "x") as relations:
            assert tuple(relations) == ()
        assert_consistent(worker.service, database_url, {"x"})
        assert {v["id"] for v in client.get("/characters/char-b/memories").json()} == {"x"}


class Recalculate(RecordingRecalculationGateway):
    def __init__(self, *values: DerivedMemory) -> None:
        super().__init__(None)
        self.values = {v.id: v for v in values}

    def recalculate_local(
        self, value: LocalOnly, *, memory_id: str, kind: str
    ) -> DerivedMemory | None:
        super().recalculate_local(value, memory_id=memory_id, kind=kind)
        old = self.values[memory_id]
        available = tuple(t.turn_id for t in value.turns)
        sources = tuple(i for i in old.source_turn_ids if i in available)
        return replace(old, source_turn_ids=sources if sources else available)


@pytest.mark.parametrize("relation_type", ["updates", "contradicts", "extends"])
@pytest.mark.parametrize("dependency", [False, True])
@pytest.mark.parametrize("other_effect", [False, True])
def test_context_deletion_withdraws_rewritten_effect(
    database_url: str, relation_type: str, dependency: bool, other_effect: bool
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    x = replace(memory(), id="x", object="tea", source_turn_ids=("turn-x",))
    a = replace(memory(), id="a", valid_from=RECORDED)
    for candidate in (x, a):
        origin = candidate.source_turn_ids[0]
        evidence.add_turn(turn(origin, actor="user-a"))
        gateway.output = (candidate,)
        worker.process("char-b", origin)
    evidence.add_turn(turn("question"))
    evidence.add_turn(
        turn("reply", actor="user-a", reply_to_turn_id="question" if dependency else None)
    )
    gateway.output = ExtractionProposal(
        memories=(replace(a, id="b", source_turn_ids=("reply",)),),
        relations=(MemoryRelation(from_id="b", to_id="x", type=relation_type),),
    )
    worker.process("char-b", "reply")
    if other_effect:
        evidence.add_turn(turn("turn-3", actor="user-a"))
        gateway.output = ExtractionProposal(
            memories=(replace(a, id="e", source_turn_ids=("turn-3",)),),
            relations=(MemoryRelation(from_id="e", to_id="x", type=relation_type),),
        )
        worker.process("char-b", "turn-3")
        state = store.get_extraction("char-b", "turn-3")
        assert state is not None and state.status == "done"
    before = store.get_memory("char-b", "x")
    assert before is not None
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            recalculation_gateway=Recalculate(x, a),
            conversation_surfaces=frozenset({"synthetic-chat"}),
        )
    ) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={
                "request_id": "delete-context",
                "target_turn_ids": ["question"],
            },
        )
        assert response.status_code == 200, response.json()
        saved = store.get_memory("char-b", "x")
        assert saved is not None
        expected_value = x if dependency and not other_effect else before
        assert (saved.valid_to, saved.superseded_at, saved.last_reinforced_at) == (
            expected_value.valid_to,
            expected_value.superseded_at,
            expected_value.last_reinforced_at,
        )
        expected_sources = set(expected_value.source_turn_ids)
        if dependency:
            expected_sources.discard("reply")
        assert set(saved.source_turn_ids) == expected_sources
        expected = (
            {"a", "x"} if (dependency and not other_effect) or relation_type == "extends" else {"a"}
        )
        assert {v["id"] for v in client.get("/characters/char-b/memories").json()} == expected
        assert_consistent(worker.service, database_url, expected)


@pytest.mark.parametrize(
    "relation_type,survivor",
    [
        (kind, owner)
        for kind in ("updates", "contradicts", "extends")
        for owner in ("none", "ordinary", "rewritten")
        if (kind, owner) != ("extends", "ordinary")
    ],
)
@pytest.mark.parametrize("target_reextracted", [False, True])
def test_deletion_restores_effects_only_from_their_owner(
    database_url: str, relation_type: str, survivor: str, target_reextracted: bool
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    x = replace(memory(), id="x", object="tea", source_turn_ids=("turn-x",))
    a = replace(memory(), id="a", valid_from=RECORDED)
    evidence.add_turn(turn("turn-x", actor="user-a"))
    gateway.output = (x,)
    worker.process("char-b", "turn-x")
    evidence.add_turn(turn("turn-1", actor="user-a"))
    gateway.output = ExtractionProposal(
        memories=(a,),
        relations=(MemoryRelation(from_id="a", to_id="x", type=relation_type),)
        if survivor == "ordinary"
        else (),
    )
    worker.process("char-b", "turn-1")
    original_x = x
    for origin in ("turn-2", "turn-3") if survivor == "rewritten" else ("turn-2",):
        evidence.add_turn(turn(origin, actor="user-a"))
        gateway.output = ExtractionProposal(
            memories=(replace(a, id="b-" + origin, source_turn_ids=(origin,)),),
            relations=(MemoryRelation(from_id="b-" + origin, to_id="x", type=relation_type),),
        )
        worker.process("char-b", origin)
        state = store.get_extraction("char-b", origin)
        assert state is not None and state.status == "done"
    if target_reextracted:
        worker.service.requeue("turn-x")
        x = replace(x, id="replacement-x")
        gateway.output = (x,)
        worker.process("char-b", "turn-x")
        state = store.get_extraction("char-b", "turn-x")
        assert state is not None and state.status == "done"
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            recalculation_gateway=Recalculate(original_x, x, a),
            conversation_surfaces=frozenset({"synthetic-chat"}),
        )
    ) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={
                "request_id": "delete-owner",
                "target_turn_ids": ["turn-2"],
            },
        )
        assert response.status_code == 200, response.json()
        saved = store.get_memory("char-b", x.id)
        assert saved is not None
        assert saved.valid_to == (
            RECORDED if survivor != "none" and relation_type == "updates" else None
        )
        assert saved.superseded_at == (
            RECORDED if survivor != "none" and relation_type == "contradicts" else None
        )
        assert saved.last_reinforced_at == (
            OCCURRED if survivor != "none" and relation_type == "extends" else None
        )
        expected = {"a", x.id} if survivor == "none" or relation_type == "extends" else {"a"}
        assert {v["id"] for v in client.get("/characters/char-b/memories").json()} == expected
        assert_consistent(worker.service, database_url, expected)


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("relation_type", ["updates", "extends"])
def test_rewritten_change_checks_target_state(
    database_url: str, closed: bool, relation_type: str
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    x = replace(
        memory(),
        id="x",
        object="tea",
        source_turn_ids=("turn-x",),
        valid_to=OCCURRED + timedelta(minutes=1) if closed else None,
    )
    a = replace(memory(), id="a", valid_from=RECORDED)
    evidence.add_turn(turn("turn-x", actor="user-a"))
    with store.transaction("char-b") as tx:
        tx.add_memory(x)
    evidence.add_turn(turn("turn-1", actor="user-a"))
    gateway.output = (a,)
    worker.process("char-b", "turn-1")
    evidence.add_turn(turn("turn-2", actor="user-a"))
    gateway.output = ExtractionProposal(
        memories=(replace(a, id="b", source_turn_ids=("turn-2",)),),
        relations=(MemoryRelation(from_id="b", to_id="x", type=relation_type),),
    )
    worker.process("char-b", "turn-2")
    state = store.get_extraction("char-b", "turn-2")
    assert state is not None and state.status == ("failed" if closed else "done")
    with TestClient(create_app(clock=lambda: RECORDED)) as client:
        response = client.get("/characters/char-b/extractions/turn-2")
        assert response.status_code == 200
        assert response.json()["status"] == state.status
    if closed:
        assert store.get_memory("char-b", "x") == x
        with store.list_relations("char-b", "a") as relations:
            assert tuple(relations) == ()
    else:
        saved = store.get_memory("char-b", "x")
        assert saved is not None
        assert saved.valid_to == (RECORDED if relation_type == "updates" else None)
        assert saved.last_reinforced_at == (OCCURRED if relation_type == "extends" else None)


@pytest.mark.parametrize("entry", ["requeue", "worker", "deletion"])
@pytest.mark.parametrize("withdraw", [False, True])
@pytest.mark.parametrize("contradiction", [False, True])
def test_later_derives_observes_rewritten_contradiction_and_its_withdrawal(
    database_url: str, entry: str, withdraw: bool, contradiction: bool
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    x = replace(memory(), id="x", object="tea", source_turn_ids=("turn-x",))
    a = replace(memory(), id="a")
    d = replace(memory("summary"), id="d", source_turn_ids=("turn-d",))
    for candidate in (x, a):
        origin = candidate.source_turn_ids[0]
        evidence.add_turn(turn(origin, actor="user-a"))
        gateway.output = (candidate,)
        worker.process("char-b", origin)
    evidence.add_session(session("delivery-session"))
    evidence.add_turn(turn("question", session_id="delivery-session"))
    evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id="question"))
    evidence.add_turn(turn("turn-2", actor="user-a", reply_to_turn_id="question"))
    gateway.output = ExtractionProposal(
        memories=(replace(a, id="b", source_turn_ids=("turn-2",)),),
        relations=(MemoryRelation(from_id="b", to_id="x", type="contradicts"),)
        if contradiction
        else (),
    )
    worker.process("char-b", "turn-2")
    evidence.add_turn(turn("turn-d", actor="user-a"))
    gateway.output = ExtractionProposal(
        memories=(d,),
        relations=(MemoryRelation(from_id="d", to_id="x", type="derives"),),
    )
    worker.process("char-b", "turn-d")
    # Read the stored rejection before testing any full projection.
    saved = store.get_memory("char-b", "d")
    assert (saved is None or saved.superseded_at is not None) if contradiction else saved == d
    expected = {"a", "x", "d"} if withdraw or not contradiction else {"a"}
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            recalculation_gateway=Recalculate(x, a),
            conversation_surfaces=frozenset({"synthetic-chat"}),
        )
    ) as client:
        if entry == "deletion":
            evidence.add_turn(turn("unrelated"))
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={
                    "request_id": "delete-effect",
                    "target_turn_ids": ["turn-2" if withdraw else "unrelated"],
                },
            )
            assert response.status_code == 200, response.json()
        elif withdraw and entry == "requeue":
            assert client.post("/turns/turn-2/extractions").status_code == 202
        elif withdraw:
            evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id="question"))

            def unavailable(value: LocalOnly) -> ExtractionProposal:
                assert_consistent(worker.service, database_url, expected)
                raise RuntimeError("Synthetic model unavailable after withdrawal")

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(gateway, "extract_local", unavailable)
                worker.process("char-b", "turn-2")
        assert {v["id"] for v in client.get("/characters/char-b/memories").json()} == expected
        assert_consistent(worker.service, database_url, expected)


def test_surviving_rewritten_contradiction_keeps_its_application_time(database_url: str) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    x = replace(memory(), id="x", object="tea", source_turn_ids=("x1", "x2"))
    a = replace(memory(), id="a")
    for origin in ("x1", "x2", "turn-1", "turn-2"):
        evidence.add_turn(turn(origin, actor="user-a"))
    with store.transaction("char-b") as tx:
        tx.add_memory(x)
    gateway.output = (a,)
    worker.process("char-b", "turn-1")
    gateway.output = ExtractionProposal(
        memories=(replace(a, id="b", source_turn_ids=("turn-2",)),),
        relations=(MemoryRelation(from_id="b", to_id="x", type="contradicts"),),
    )
    worker.process("char-b", "turn-2")
    later = RECORDED + timedelta(days=1)
    worker.service.clock = lambda: later
    worker.service.inputs.materials.clock = lambda: later
    with TestClient(
        create_app(
            clock=lambda: later,
            recalculation_gateway=Recalculate(x, a),
            conversation_surfaces=frozenset({"synthetic-chat"}),
        )
    ) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={
                "request_id": "delete-one-source",
                "target_turn_ids": ["x1"],
            },
        )
        assert response.status_code == 200, response.json()
        for rebuild in (False, True):
            with store.transaction("char-b") as tx:
                if rebuild:
                    worker.service.rebuild(tx)
                saved = next(v for v in tx.memories() if v.id == "x")
                baseline = next(v for v in tx.baselines() if v.id == "x")
            assert saved.superseded_at == baseline.superseded_at == RECORDED
            assert {v["id"] for v in client.get("/characters/char-b/memories").json()} == {"a"}
            assert_consistent(worker.service, database_url, {"a"})
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={
                "request_id": "delete-contradiction",
                "target_turn_ids": ["turn-2"],
            },
        )
        assert response.status_code == 200, response.json()
        for rebuild in (False, True):
            with store.transaction("char-b") as tx:
                if rebuild:
                    worker.service.rebuild(tx)
                saved = next(v for v in tx.memories() if v.id == "x")
                baseline = next(v for v in tx.baselines() if v.id == "x")
            assert saved.superseded_at is baseline.superseded_at is None
            assert {v["id"] for v in client.get("/characters/char-b/memories").json()} == {"a", "x"}
            assert_consistent(worker.service, database_url, {"a", "x"})


@pytest.mark.parametrize("entry", ["requeue", "worker"])
@pytest.mark.parametrize("other_support", [False, True])
def test_last_support_withdrawal_reevaluates_independent_derives(
    database_url: str, entry: str, other_support: bool
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    a = replace(memory(), id="a")
    d = replace(memory("summary"), id="d", source_turn_ids=("turn-d",))
    evidence.add_session(session("delivery-session"))
    evidence.add_turn(turn("question", session_id="delivery-session"))
    evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id="question"))
    for origin in ("turn-1", "turn-2", "turn-3") if other_support else ("turn-1", "turn-2"):
        evidence.add_turn(
            turn(
                origin, actor="user-a", reply_to_turn_id="question" if origin == "turn-2" else None
            )
        )
        gateway.output = (
            replace(a, id="a" if origin == "turn-1" else origin, source_turn_ids=(origin,)),
        )
        worker.process("char-b", origin)
    evidence.add_turn(turn("turn-d", actor="user-a"))
    gateway.output = ExtractionProposal(
        memories=(d,),
        relations=(MemoryRelation(from_id="d", to_id="a", type="derives"),),
    )
    worker.process("char-b", "turn-d")
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            recalculation_gateway=Recalculate(a),
            conversation_surfaces=frozenset({"synthetic-chat"}),
        )
    ) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={
                "request_id": "delete-body",
                "target_turn_ids": ["turn-1"],
            },
        )
        assert response.status_code == 200, response.json()
        assert_consistent(worker.service, database_url, {"a", "d"})
        if entry == "requeue":
            assert client.post("/turns/turn-2/extractions").status_code == 202
        else:
            evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id="question"))

            def unavailable(value: LocalOnly) -> ExtractionProposal:
                assert_consistent(
                    worker.service, database_url, {"a", "d"} if other_support else set()
                )
                raise RuntimeError("Synthetic model unavailable after withdrawal")

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(gateway, "extract_local", unavailable)
                worker.process("char-b", "turn-2")
            state = store.get_extraction("char-b", "turn-2")
            assert state is not None and state.status == "retry"
        expected = {"a", "d"} if other_support else set()
        assert {v["id"] for v in client.get("/characters/char-b/memories").json()} == expected
        assert_consistent(worker.service, database_url, expected)


def test_rewritten_update_reconstructs_applied_closed_baseline_without_tier_recheck(
    database_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    x = replace(memory(), id="x", object="tea", source_turn_ids=("x1", "x2"))
    a = replace(memory(), id="a", valid_from=RECORDED)
    for origin in ("x1", "x2", "turn-1", "turn-2"):
        evidence.add_turn(turn(origin, actor="user-a"))
    with store.transaction("char-b") as tx:
        tx.add_memory(x)
    gateway.output = (a,)
    worker.process("char-b", "turn-1")
    gateway.output = ExtractionProposal(
        memories=(replace(a, id="b", source_turn_ids=("turn-2",)),),
        relations=(MemoryRelation(from_id="b", to_id="x", type="updates"),),
    )
    worker.process("char-b", "turn-2")
    revised = replace(x, epistemics=replace(x.epistemics, trust_tier="E"))
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            recalculation_gateway=Recalculate(revised, a),
            conversation_surfaces=frozenset({"synthetic-chat"}),
        )
    ) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={
                "request_id": "recalculate-closed",
                "target_turn_ids": ["x1"],
            },
        )
        assert response.status_code == 200, response.json()
    assert_consistent(worker.service, database_url, {"a"})
    with store.transaction("char-b") as tx:
        snapshot = projection_input(tx)
        assert next(v for v in snapshot.baselines if v.id == "x").valid_to == RECORDED
    evidence_input = collect_evidence(snapshot, "char-b", worker.service.inputs)
    with pytest.raises(MemoryValidationError):
        evidence_input.project(replace(snapshot, applied=()), RECORDED)
