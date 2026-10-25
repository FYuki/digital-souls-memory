"""Acceptance and withdrawal of restatements through durable public operations."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event

import psycopg
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import DerivedMemory, ExtractionProposal, MemoryRelation
from digital_souls_memory.memory.extraction import LocalOnly
from digital_souls_memory.memory.projection import collect_evidence, projection_input
from digital_souls_memory.memory.use_cases import MemoryService
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import OCCURRED, RECORDED, TEXT, receipt, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import memory
from tests.postgres.test_deletion import HEADERS, seed_evidence
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_memory_continuous_ingestion import LockMeasurements, publish_and_search

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


def test_contribution_attributes_survive_reconnect_and_duplicate_is_rejected(
    database_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    for number in (1, 2):
        source = f"turn-{number}"
        evidence.add_turn(turn(source, actor="user-a"))
        gateway.output = (
            replace(
                memory(),
                id="a" if number == 1 else "b",
                source_turn_ids=(source,),
            ),
        )
        worker.process("char-b", source)
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            "SELECT character_id,fact_id,extraction_turn_id,source_turn_id,occurred_at "
            "FROM memory_source_contributions"
        ).fetchall()
        assert rows == [("char-b", "a", "turn-2", "turn-2", OCCURRED)]
        with pytest.raises(psycopg.errors.UniqueViolation), connection.transaction():
            connection.execute(
                "INSERT INTO memory_source_contributions VALUES(%s,%s,%s,%s,%s)",
                rows[0],
            )
        assert connection.execute(
            "SELECT count(*) FROM memory_source_contributions"
        ).fetchone() == (1,)


def test_rewritten_effect_tracks_equivalent_target_reextraction(database_url: str) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    old = replace(memory(), id="x", source_turn_ids=("turn-x",))
    current = replace(memory(), id="a", object="tea", valid_from=RECORDED)
    for candidate in (old, current):
        source = candidate.source_turn_ids[0]
        evidence.add_turn(turn(source, actor="user-a"))
        gateway.output = (candidate,)
        worker.process("char-b", source)
    evidence.add_turn(turn("turn-2", actor="user-a"))
    gateway.output = ExtractionProposal(
        memories=(replace(current, id="b", source_turn_ids=("turn-2",)),),
        relations=(MemoryRelation(from_id="b", to_id="x", type="updates"),),
    )
    worker.process("char-b", "turn-2")
    worker.service.requeue("turn-x")
    gateway.output = (replace(old, id="replacement"),)
    worker.process("char-b", "turn-x")
    replacement = store.get_memory("char-b", "replacement")
    assert replacement is not None and replacement.valid_to == RECORDED
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT to_id FROM memory_candidate_relations WHERE effect IS NOT NULL"
        ).fetchall() == [("replacement",)]
    with store.list_relations("char-b", "a") as relations:
        assert tuple(relations) == (
            MemoryRelation(from_id="a", to_id="replacement", type="updates"),
        )
    assert_consistent(worker.service, database_url, {"a"})
    worker.service.requeue("turn-2")
    assert store.get_memory("char-b", "replacement") == replace(old, id="replacement")
    assert_consistent(worker.service, database_url, {"a", "replacement"})


def assert_consistent(service: MemoryService, url: str, expected: set[str]) -> None:
    # Read persistence before active() so a read cannot repair a broken partial rebuild.
    with service.store.transaction("char-b") as tx:
        saved = {v.id: v for v in tx.memories()}
        relations = set(tx.applied_relations())
        snapshot = projection_input(tx)
    assert {v.id for v in service.active("char-b")} == expected
    full = collect_evidence(snapshot, "char-b", service.inputs).project(snapshot, service.clock())
    assert saved == full.memories
    assert relations == set(full.relations)

    def unexpected_projection(*args: object, **kwargs: object) -> None:
        pytest.fail("Recall must consume persisted memory without active() or rebuild()")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MemoryService, "active", unexpected_projection)
        patch.setattr(MemoryService, "rebuild", unexpected_projection)
        assert publish_and_search(url, now=service.clock()) == expected


@pytest.mark.parametrize("reference", ["restatement", "existing"])
def test_acceptance_rewrites_structured_references_and_preserves_content(
    database_url: str, reference: str
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    evidence.add_turn(turn("turn-1", actor="user-a"))
    root = replace(memory(), id="a")
    gateway.output = (root,)
    worker.process("char-b", "turn-1")
    evidence.add_turn(turn("turn-x", actor="user-a"))
    other = replace(root, id="x", object="water", source_turn_ids=("turn-x",))
    gateway.output = (other,)
    worker.process("char-b", "turn-x")
    evidence.add_turn(turn("turn-2", actor="user-a"))
    repeated = replace(root, id="b", source_turn_ids=("turn-2",))
    derived = replace(
        memory("summary"), id="d", content="bという文字を含む", source_turn_ids=("turn-2",)
    )
    gateway.output = ExtractionProposal(
        memories=(derived, repeated),
        relations=(
            MemoryRelation(
                from_id="d", to_id="b" if reference == "restatement" else "x", type="derives"
            ),
        ),
    )
    worker.process("char-b", "turn-2")
    state = store.get_extraction("char-b", "turn-2")
    assert state is not None and state.status == "done"
    with store.list_relations("char-b", "d") as relations:
        assert tuple(relations) == (
            MemoryRelation(
                from_id="d", to_id="a" if reference == "restatement" else "x", type="derives"
            ),
        )
    assert store.get_memory("char-b", "d") == derived
    assert store.get_memory("char-b", "b") is None
    with psycopg.connect(database_url) as conn:
        assert conn.execute(
            "SELECT turn_id,from_id,to_id FROM memory_candidate_relations "
            "WHERE character_id='char-b' AND from_id='d'"
        ).fetchall() == [("turn-2", "d", "a" if reference == "restatement" else "x")]
        for table in ("memory_candidates", "memory_generated"):
            assert (
                conn.execute(
                    "SELECT memory_id FROM "
                    + table
                    + " WHERE character_id='char-b' AND memory_id='b'"
                ).fetchall()
                == []
            )
    assert_consistent(worker.service, database_url, {"a", "x", "d"})


@pytest.mark.parametrize(
    "actor,reply,expected_count",
    [("user-a", None, 1), ("user-b", "turn-1", 1), ("user-b", None, 2)],
)
def test_restatement_unions_sources_and_preserves_fact_identity(
    database_url: str, actor: str, reply: str | None, expected_count: int
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1", actor="user-a"))
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    root = replace(memory(), id="a")
    gateway.output = (root,)
    worker.process("char-b", "turn-1")
    # An older event arrives after the original acceptance.
    older = OCCURRED - timedelta(hours=1)
    evidence.add_turn(
        replace(
            turn(
                "turn-2",
                actor=actor,
                reply_to_turn_id=reply,
                addressees=("user-a",) if actor != "user-a" else (),
                address_mode="direct" if actor != "user-a" else "broadcast",
            ),
            occurred_at=older,
        )
    )
    gateway.output = (
        replace(
            root,
            id="b",
            valid_from=older,
            source_turn_ids=("turn-2",),
            derivation=replace(root.derivation, model="another-synthetic-model"),
            epistemics=replace(root.epistemics, source_type="direct_user", trust_tier="E"),
        ),
    )
    worker.process("char-b", "turn-2")
    restored = PostgresMemoryStore(database_url).get_memory("char-b", "a")
    assert restored is not None
    assert set(restored.source_turn_ids) == {"turn-1", "turn-2"}
    assert restored.last_reinforced_at == older
    assert restored.epistemics.independent_evidence_count == expected_count
    assert (
        restored.id,
        restored.valid_from,
        restored.derivation,
        restored.epistemics.source_type,
        restored.epistemics.trust_tier,
    ) == (
        root.id,
        root.valid_from,
        root.derivation,
        root.epistemics.source_type,
        root.epistemics.trust_tier,
    )
    assert store.get_memory("char-b", "b") is None


@pytest.mark.parametrize(
    "condition", ["closed", "superseded", "future", "commitment", "different-spo"]
)
def test_acceptance_keeps_new_versions_and_non_reinforceable_kinds(
    database_url: str, condition: str
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1", actor="user-a"))
    evidence.add_turn(
        turn(
            "turn-2",
            actor="user-a",
            addressees=("char-b",) if condition == "commitment" else (),
            address_mode="direct" if condition == "commitment" else "broadcast",
        )
    )
    root = replace(memory("commitment" if condition == "commitment" else "fact"), id="a")
    if condition == "closed":
        root = replace(root, valid_to=RECORDED)
    elif condition == "superseded":
        root = replace(root, superseded_at=RECORDED)
    elif condition == "future":
        root = replace(root, valid_from=RECORDED + timedelta(seconds=1))
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    with store.transaction("char-b") as tx:
        tx.add_memory(root)
    candidate = replace(
        root,
        id="b",
        source_turn_ids=("turn-2",),
        valid_from=OCCURRED,
        valid_to=None,
        superseded_at=None,
        object="tea" if condition == "different-spo" else root.object,
        verbatim_turn_id="turn-2" if condition == "commitment" else None,
        counterpart="user-a" if condition == "commitment" else root.counterpart,
    )
    gateway.output = ExtractionProposal(
        memories=(candidate,),
        relations=(MemoryRelation(from_id="b", to_id="a", type="extends"),)
        if condition == "different-spo"
        else (),
    )
    worker = make_worker(database_url, store, gateway)
    worker.process("char-b", "turn-2")
    state = store.get_extraction("char-b", "turn-2")
    assert state is not None and state.status == "done"
    if condition == "different-spo":
        assert store.get_memory("char-b", "b") == replace(candidate, superseded_at=RECORDED)
        assert store.get_memory("char-b", "a") == replace(
            root, source_turn_ids=("turn-1", "turn-2"), last_reinforced_at=OCCURRED
        )
        with store.list_relations("char-b", "b") as relations:
            assert tuple(relations) == (MemoryRelation(from_id="b", to_id="a", type="extends"),)
    else:
        assert store.get_memory("char-b", "b") == candidate
        assert store.get_memory("char-b", "a") == root


@pytest.mark.parametrize("relation_type", ["updates", "contradicts", "derives"])
@pytest.mark.parametrize("other_origin", [False, True], ids=["one-origin", "two-origins"])
def test_requeue_withdraws_restatement_and_its_relation_effects_atomically(
    database_url: str, relation_type: str, other_origin: bool
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    root = replace(memory(), id="a", valid_from=RECORDED)
    old = replace(root, id="x", object="tea", valid_from=OCCURRED, source_turn_ids=("turn-x",))
    for candidate in (old, root):
        source = candidate.source_turn_ids[0]
        evidence.add_turn(replace(turn(source, actor="user-a"), occurred_at=candidate.valid_from))
        gateway.output = (candidate,)
        worker.process("char-b", source)
    evidence.add_turn(replace(turn("turn-2", actor="user-a"), occurred_at=RECORDED))
    gateway.output = ExtractionProposal(
        memories=(replace(root, id="b", source_turn_ids=("turn-2",)),),
        relations=(MemoryRelation(from_id="b", to_id="x", type=relation_type),),
    )
    worker.process("char-b", "turn-2")
    with store.list_relations("char-b", "a") as relations:
        assert tuple(relations) == (MemoryRelation(from_id="a", to_id="x", type=relation_type),)
    before = store.get_memory("char-b", "x")
    assert before is not None
    assert before.valid_to == (RECORDED if relation_type == "updates" else None)
    assert before.superseded_at == (RECORDED if relation_type == "contradicts" else None)
    assert_consistent(
        worker.service, database_url, {"a", "x"} if relation_type == "derives" else {"a"}
    )
    with TestClient(
        create_app(clock=lambda: RECORDED, conversation_surfaces=frozenset({"synthetic-chat"}))
    ) as client:
        if other_origin:
            evidence.add_turn(replace(turn("turn-3", actor="user-a"), occurred_at=RECORDED))
            gateway.output = ExtractionProposal(
                memories=(replace(root, id="c", source_turn_ids=("turn-3",)),),
                relations=(MemoryRelation(from_id="c", to_id="x", type=relation_type),),
            )
            worker.process("char-b", "turn-3")
        assert client.post("/turns/turn-2/extractions").status_code == 202
        if other_origin:
            remaining = store.get_memory("char-b", "a")
            assert remaining is not None and set(remaining.source_turn_ids) == {"turn-1", "turn-3"}
            assert remaining.last_reinforced_at == RECORDED
            assert store.get_memory("char-b", "x") == before
            with store.list_relations("char-b", "a") as relations:
                assert tuple(relations) == (
                    MemoryRelation(from_id="a", to_id="x", type=relation_type),
                )
            assert_consistent(
                worker.service, database_url, {"a", "x"} if relation_type == "derives" else {"a"}
            )
            assert client.post("/turns/turn-3/extractions").status_code == 202
    # No worker or global rebuild is allowed between requeue and these observations.
    assert store.get_memory("char-b", "a") == root
    assert store.get_memory("char-b", "x") == old
    with store.list_relations("char-b", "a") as relations:
        assert tuple(relations) == ()
    assert_consistent(worker.service, database_url, {"a", "x"})


def test_two_retries_replace_contributions_and_recompute_latest_event(database_url: str) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    root = replace(memory(), id="a")
    for number, event in ((1, OCCURRED), (2, RECORDED), (3, OCCURRED + timedelta(minutes=1))):
        source = f"turn-{number}"
        evidence.add_turn(
            replace(
                turn(
                    source,
                    actor="user-a" if number == 1 else "user-b",
                    addressees=("user-a",) if number != 1 else (),
                    address_mode="direct" if number != 1 else "broadcast",
                ),
                occurred_at=event,
            )
        )
        gateway.output = (
            replace(
                root,
                id="a" if number == 1 else f"b-{number}",
                source_turn_ids=(source,),
                valid_from=event,
            ),
        )
        worker.process("char-b", source)
    strengthened = store.get_memory("char-b", "a")
    assert strengthened is not None
    assert strengthened.last_reinforced_at == RECORDED
    with TestClient(
        create_app(clock=lambda: RECORDED, conversation_surfaces=frozenset({"synthetic-chat"}))
    ) as client:
        for _ in range(2):
            assert client.post("/turns/turn-2/extractions").status_code == 202
            gateway.fail = True
            worker.process("char-b", "turn-2")
            state = store.get_extraction("char-b", "turn-2")
            assert state is not None and state.status == "retry"
            withdrawn = store.get_memory("char-b", "a")
            assert withdrawn is not None
            assert set(withdrawn.source_turn_ids) == {"turn-1", "turn-3"}
            assert withdrawn.last_reinforced_at == OCCURRED + timedelta(minutes=1)
            assert_consistent(worker.service, database_url, {"a"})
            assert client.post("/turns/turn-2/extractions").status_code == 202
            gateway.fail = False
            gateway.output = (
                replace(root, id="replacement", source_turn_ids=("turn-2",), valid_from=RECORDED),
            )
            worker.process("char-b", "turn-2")
            retried = store.get_memory("char-b", "a")
            assert retried is not None
            assert set(retried.source_turn_ids) == set(strengthened.source_turn_ids)
            assert replace(retried, source_turn_ids=strengthened.source_turn_ids) == strengthened
            assert_consistent(worker.service, database_url, {"a"})


def test_requeue_projection_failure_rolls_back_notification_and_allows_retry(
    database_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    root = replace(memory(), id="a")
    for number in (1, 2):
        source = f"turn-{number}"
        evidence.add_turn(turn(source, actor="user-a"))
        gateway.output = (replace(root, id="a" if number == 1 else "b", source_turn_ids=(source,)),)
        worker.process("char-b", source)
    strengthened = store.get_memory("char-b", "a")
    assert strengthened is not None and set(strengthened.source_turn_ids) == {"turn-1", "turn-2"}
    with psycopg.connect(database_url, autocommit=True) as conn:
        version = conn.execute(
            "SELECT version FROM memory_notifications WHERE turn_id='turn-2'"
        ).fetchone()
        conn.execute(
            "CREATE FUNCTION synthetic_requeue_failure() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'synthetic projection failure'; END; $$"
        )
        conn.execute(
            "CREATE TRIGGER synthetic_requeue_failure BEFORE UPDATE ON derived_memories "
            "FOR EACH ROW EXECUTE FUNCTION synthetic_requeue_failure()"
        )
        try:
            with TestClient(
                create_app(
                    clock=lambda: RECORDED, conversation_surfaces=frozenset({"synthetic-chat"})
                )
            ) as client:
                assert client.post("/turns/turn-2/extractions").status_code == 503
        finally:
            conn.execute("DROP TRIGGER synthetic_requeue_failure ON derived_memories")
            conn.execute("DROP FUNCTION synthetic_requeue_failure()")
        assert (
            conn.execute(
                "SELECT version FROM memory_notifications WHERE turn_id='turn-2'"
            ).fetchone()
            == version
        )
    assert store.get_memory("char-b", "a") == strengthened
    assert_consistent(worker.service, database_url, {"a"})
    with TestClient(
        create_app(clock=lambda: RECORDED, conversation_surfaces=frozenset({"synthetic-chat"}))
    ) as client:
        assert client.post("/turns/turn-2/extractions").status_code == 202
    assert store.get_memory("char-b", "a") == root
    assert_consistent(worker.service, database_url, {"a"})


def test_original_extraction_retry_preserves_consistency_for_surviving_restatement(
    database_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    root = replace(memory(), id="a")
    evidence.add_turn(turn("turn-1", actor="user-a"))
    gateway.output = (root,)
    worker.process("char-b", "turn-1")
    evidence.add_turn(turn("turn-2", actor="user-a"))
    gateway.output = ExtractionProposal(
        memories=(
            replace(root, id="b", source_turn_ids=("turn-2",)),
            replace(memory("summary"), id="d", source_turn_ids=("turn-2",)),
        ),
        relations=(MemoryRelation(from_id="d", to_id="b", type="derives"),),
    )
    worker.process("char-b", "turn-2")
    assert_consistent(worker.service, database_url, {"a", "d"})
    with TestClient(
        create_app(clock=lambda: RECORDED, conversation_surfaces=frozenset({"synthetic-chat"}))
    ) as client:
        assert client.post("/turns/turn-1/extractions").status_code == 202
    gateway.fail = True
    worker.process("char-b", "turn-1")
    state = store.get_extraction("char-b", "turn-1")
    assert state is not None and state.status == "retry"
    # The four observations must agree even when the original extraction has no result.
    active = {v.id for v in worker.service.active("char-b")}
    assert_consistent(worker.service, database_url, active)


@pytest.mark.parametrize("recalculable", [False, True])
def test_author_deletion_retains_same_fact_or_withdraws_without_reassigning_sources(
    database_url: str, recalculable: bool
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    root = replace(memory(), id="a")
    for number in (1, 2, 3):
        source = f"turn-{number}"
        evidence.add_turn(turn(source, actor="user-a"))
        gateway.output = (
            replace(root, id="a" if number == 1 else f"b-{number}", source_turn_ids=(source,)),
        )
        worker.process("char-b", source)
    assert_consistent(worker.service, database_url, {"a"})
    result = (
        replace(root, source_turn_ids=("turn-2", "turn-3"), content="残存出典からの再計算")
        if recalculable
        else None
    )
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            recalculation_gateway=RecordingRecalculationGateway(result),
        )
    ) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-root", "target_turn_ids": ["turn-1"]},
        )
        assert response.status_code == 200, response.json()
        assert evidence.get_turn("turn-1") is None
        assert_consistent(worker.service, database_url, {"a"} if recalculable else set())
        if recalculable:
            assert result is not None
            saved = store.get_memory("char-b", "a")
            assert saved is not None
            assert saved.content == result.content
            assert set(saved.source_turn_ids) == {"turn-2", "turn-3"}
            assert client.post("/turns/turn-3/extractions").status_code == 202
            saved = store.get_memory("char-b", "a")
            assert saved is not None and saved.source_turn_ids == ("turn-2",)
            assert_consistent(worker.service, database_url, {"a"})
            assert client.post("/turns/turn-2/extractions").status_code == 202
            assert_consistent(worker.service, database_url, set())
        else:
            assert not ({"b-2", "b-3"} & {v.id for v in worker.service.active("char-b")})
            evidence.add_turn(turn("turn-4", actor="user-a"))
            replacement = replace(root, id="new-fact", source_turn_ids=("turn-4",))
            gateway.output = (replacement,)
            worker.process("char-b", "turn-4")
            assert store.get_memory("char-b", "new-fact") == replacement
            assert_consistent(worker.service, database_url, {"new-fact"})


def test_deletion_detects_restatement_committed_after_snapshot(database_url: str) -> None:
    evidence = seed_evidence(database_url)
    for source in ("turn-1", "turn-2", "turn-3"):
        evidence.add_turn(turn(source, actor="user-a"))
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    root = replace(memory(), id="a", source_turn_ids=("turn-1", "turn-2"))
    with store.transaction("char-b") as tx:
        tx.add_memory(root)
    assert publish_and_search(database_url) == {"a"}
    waiting, release = Event(), Event()

    class PausedRecalculation(RecordingRecalculationGateway):
        def recalculate_local(
            self, value: LocalOnly, *, memory_id: str, kind: str
        ) -> DerivedMemory:
            waiting.set()
            assert release.wait(10)
            return replace(root, source_turn_ids=tuple(v.turn_id for v in value.turns))

    with TestClient(
        create_app(clock=lambda: RECORDED, recalculation_gateway=PausedRecalculation(None))
    ) as client:
        payload = {"request_id": "delete-racing", "target_turn_ids": ["turn-1"]}
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, "/deletion-requests", headers=HEADERS, json=payload)
            try:
                assert waiting.wait(5)
                gateway.output = (replace(root, id="b", source_turn_ids=("turn-3",)),)
                worker.process("char-b", "turn-3")
            finally:
                release.set()
            response = pending.result(timeout=10)
        assert response.status_code == 409, response.json()
        assert evidence.get_turn("turn-1") is not None
        saved = store.get_memory("char-b", "a")
        assert saved is not None and set(saved.source_turn_ids) == {"turn-1", "turn-2", "turn-3"}
        assert client.post("/deletion-requests", headers=HEADERS, json=payload).status_code == 200
        assert_consistent(worker.service, database_url, {"a"})
        saved = store.get_memory("char-b", "a")
        assert saved is not None and set(saved.source_turn_ids) == {"turn-2", "turn-3"}


def test_receipt_correction_withdraws_restatement_from_reply_context(database_url: str) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1", actor="user-a"))
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    root = replace(memory(), id="a")
    gateway.output = (root,)
    worker.process("char-b", "turn-1")
    evidence.add_session(session("delivery-session"))
    evidence.add_turn(turn("question", session_id="delivery-session"))
    evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id="question"))
    evidence.add_turn(turn("turn-2", actor="user-a", reply_to_turn_id="question"))
    gateway.output = (replace(root, id="b", source_turn_ids=("turn-2",)),)
    worker.process("char-b", "turn-2")
    assert_consistent(worker.service, database_url, {"a"})
    evidence.add_receipt(replace(receipt("none", 0), turn_id="question"))
    # The already published index must stop serving the stale contribution immediately.
    assert publish_and_search(database_url) == set()
    gateway.output = ()
    worker.process("char-b", "turn-2")
    assert store.get_memory("char-b", "a") == root
    assert_consistent(worker.service, database_url, {"a"})


def test_shared_fact_contributions_do_not_multiply_partial_rebuild_queries(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1", actor="user-a"))
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    root = replace(memory(), id="a")
    gateway.output = (root,)
    worker.process("char-b", "turn-1")
    measurements: dict[int, int] = {}
    withdrawals: dict[int, int] = {}
    closures: dict[int, frozenset[str]] = {}
    seeded = 0
    for count in (1, 100):
        for number in range(seeded, count):
            source = f"support-{number}"
            evidence.add_turn(turn(source, actor="user-a"))
            gateway.output = (replace(root, id=f"repeated-{number}", source_turn_ids=(source,)),)
            worker.process("char-b", source)
        with store.transaction("char-b") as tx:
            for number in range(seeded, count):
                tx.add_memory(replace(memory("summary"), id=f"referrer-{number}"))
                tx.add_relation(
                    MemoryRelation(from_id=f"referrer-{number}", to_id="a", type="derives")
                )
        seeded = count
        source = f"measured-{count}"
        evidence.add_turn(turn(source, actor="user-a"))
        gateway.output = (replace(root, id=f"measured-memory-{count}", source_turn_ids=(source,)),)
        with monkeypatch.context() as patch:
            measured = LockMeasurements(patch)
            worker.process("char-b", source)
        state = store.get_extraction("char-b", source)
        assert state is not None and state.status == "done"
        measurements[count] = measured.queries
        with store.transaction("char-b") as tx:
            closures[count] = worker.service.affected(tx, source)
        record_property(
            str(count), {"closure": sorted(closures[count]), "queries": measured.queries}
        )
        assert closures[count] == frozenset({"a"})
        with monkeypatch.context() as patch:
            measured = LockMeasurements(patch)
            worker.service.requeue(source)
        withdrawals[count] = measured.queries
        record_property(f"withdraw-{count}", {"queries": measured.queries})
    assert measurements[100] <= measurements[1]
    assert withdrawals[100] <= withdrawals[1]
