"""Only projected equivalent candidates can receive durable sources and effects."""

from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import DerivedMemory, ExtractionProposal, MemoryRelation
from digital_souls_memory.memory.projection import collect_evidence, projection_input
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.recall.inputs import InputLabel, LocalOnly
from tests.evidence_support import RECORDED, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import memory
from tests.postgres.test_deletion import HEADERS, seed_evidence
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_memory_continuous_ingestion import LockMeasurements, publish_and_search
from tests.postgres.test_memory_restatement import assert_consistent
from tests.postgres.test_restatement_remediation import Recalculate
from tests.recall_support import RecordingEmbedding

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


def assert_recalled_values(url: str, expected: dict[str, DerivedMemory]) -> None:
    def unexpected_projection(*args: object, **kwargs: object) -> None:
        pytest.fail("Recall must use persisted projections")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MemoryService, "active", unexpected_projection)
        patch.setattr(MemoryService, "rebuild", unexpected_projection)
        publish_and_search(url)
        query = LocalOnly(texts=("飲み物",), labels=(InputLabel("sensitive", "v1"),))
        results = PostgresRetrievalStore(url).search(
            character_id="char-b",
            query=query,
            query_vector=RecordingEmbedding().embed(query, purpose="query"),
            now=RECORDED,
            limit=20,
        )
        recalled = {c.memory.id: c.memory for c in results.candidates if c.memory is not None}
        assert {identifier: recalled[identifier] for identifier in expected} == expected


def test_equivalent_reverse_closure_is_limited_to_its_origin(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    evidence.add_turn(turn("turn-1", actor="user-a"))
    a = replace(memory(), id="a")
    gateway.output = (a,)
    worker.process("char-b", "turn-1")
    worker.service.requeue("turn-1")
    z = replace(a, id="z")
    summary = replace(memory("summary"), id="summary")
    gateway.output = (z, summary)
    worker.process("char-b", "turn-1")
    state = store.get_extraction("char-b", "turn-1")
    assert state is not None and state.status == "done"
    evidence.add_turn(turn("other-origin", actor="user-a"))
    measurements: dict[int, LockMeasurements] = {}
    closures: dict[int, frozenset[str]] = {}
    for count in (1, 30):
        with store.transaction("char-b") as tx:
            # Same payload and SPO from another extraction must stay outside the closure.
            tx.save_extraction(replace(state, turn_id="other-origin"))
            tx.set_candidates(
                "other-origin",
                tuple(replace(a, id=f"unrelated-{n}") for n in range(count)),
                dependencies=(),
            )
        with monkeypatch.context() as patch:
            measured = LockMeasurements(patch)
            with store.transaction("char-b") as tx:
                identifiers = frozenset({"z"})
                while True:
                    expanded = identifiers | tx.projection_neighbors(identifiers, frozenset())
                    if expanded == identifiers:
                        break
                    identifiers = expanded
                closures[count] = identifiers
            measurements[count] = measured
    assert closures == {1: frozenset({"z"}), 30: frozenset({"z"})}
    assert measurements[30].rows <= measurements[1].rows
    assert measurements[30].queries <= measurements[1].queries
    with store.transaction("char-b") as tx:
        tx.add_memory(replace(a, id="referrer", object="referrer"))
        tx.add_relation(MemoryRelation(from_id="referrer", to_id="a", type="derives"))
        assert "referrer" not in tx.projection_neighbors(frozenset({"z"}), frozenset({"z"}))
        assert "referrer" in tx.projection_neighbors(frozenset({"z"}), frozenset())


@pytest.mark.parametrize(
    "restatement,content,historical_destination",
    [
        (False, "a", False),
        (True, "a", False),
        (True, "a と `z`", False),
        (True, "a", True),
    ],
    ids=["without-support", "with-support", "literal-content", "retired-destination"],
)
def test_unprojected_equivalent_preserves_independent_candidates_and_consistency(
    database_url: str, restatement: bool, content: str, historical_destination: bool
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    x = replace(memory(), id="x", object="water", source_turn_ids=("turn-x",))
    a = replace(memory(), id="a", valid_from=RECORDED)
    c = replace(memory(), id="c", object="milk", source_turn_ids=("turn-c",))
    d = replace(memory(), id="d", object="juice", source_turn_ids=("turn-d",))
    for candidate in (x, a, c, d):
        origin = candidate.source_turn_ids[0]
        evidence.add_turn(turn(origin, actor="user-a"))
        if candidate.id == "a" and historical_destination:
            gateway.output = (replace(a, id="z"),)
            worker.process("char-b", origin)
            state = store.get_extraction("char-b", origin)
            assert state is not None and state.status == "done"
            worker.service.requeue(origin)
        gateway.output = ExtractionProposal(
            memories=(candidate,),
            relations=(MemoryRelation(from_id="d", to_id="c", type="contradicts"),)
            if candidate.id == "d"
            else (),
        )
        worker.process("char-b", origin)
        state = store.get_extraction("char-b", origin)
        assert state is not None and state.status == "done"
    if restatement:
        evidence.add_turn(replace(turn("turn-b", actor="user-a"), occurred_at=RECORDED))
        gateway.output = ExtractionProposal(
            memories=(replace(a, id="b", source_turn_ids=("turn-b",)),),
            relations=(MemoryRelation(from_id="b", to_id="x", type="updates"),),
        )
        worker.process("char-b", "turn-b")
        state = store.get_extraction("char-b", "turn-b")
        assert state is not None and state.status == "done"
    before = store.get_memory("char-b", "x")
    assert before is not None and before.valid_to == (RECORDED if restatement else None)
    invalidated = store.get_memory("char-b", "c")
    assert invalidated is not None and invalidated.superseded_at is not None
    assert publish_and_search(database_url) == ({"a", "d"} if restatement else {"a", "d", "x"})

    z = replace(a, id="z")
    summary = replace(memory("summary"), id="summary", content=content)
    independent = replace(memory("key"), id="key", content="独立した有効な候補")
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            recalculation_gateway=Recalculate(x, a, c, d, z, summary, independent),
        )
    ) as client:
        assert client.post("/turns/turn-1/extractions").status_code == 202
        gateway.output = ExtractionProposal(
            memories=(z, summary, independent),
            relations=(MemoryRelation(from_id="z", to_id="c", type="derives"),),
        )
        worker.process("char-b", "turn-1")

        # Observe the committed partial rebuild before a full projection or recall.
        state = store.get_extraction("char-b", "turn-1")
        assert state is not None and state.status == "done", state
        assert state.error is None
        assert store.get_memory("char-b", "summary") == summary
        assert store.get_memory("char-b", "key") == independent
        rejected = store.get_memory("char-b", "z")
        assert rejected is None or rejected.superseded_at is not None
        assert store.get_memory("char-b", "x") == x
        with psycopg.connect(database_url) as conn:
            sources = conn.execute(
                "SELECT fact_id,extraction_turn_id,source_turn_id,occurred_at "
                "FROM memory_source_contributions WHERE character_id='char-b'"
            ).fetchall()
            assert all(row == ("a", "turn-b", "turn-b", RECORDED) for row in sources)
            effects = conn.execute(
                "SELECT turn_id,from_id,to_id,type,effect->>'id' "
                "FROM memory_candidate_relations "
                "WHERE character_id='char-b' AND effect IS NOT NULL"
            ).fetchall()
            assert all(row == ("turn-b", "a", "x", "updates", "b") for row in effects)
        expected = {"x", "d", "summary", "key"}
        assert_consistent(worker.service, database_url, expected)
        if restatement:
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-support", "target_turn_ids": ["turn-b"]},
            )
            assert response.status_code == 200, response.json()
            assert response.json()["status"] == "done"
            assert store.get_memory("char-b", "x") == x
            assert_consistent(worker.service, database_url, expected)
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-origin", "target_turn_ids": ["turn-1"]},
        )
        assert response.status_code == 200, response.json()
        assert response.json()["status"] == "done"
        assert_consistent(worker.service, database_url, {"x", "d"})


def test_unprojected_equivalent_target_does_not_receive_restatement_effect(
    database_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    x = replace(memory(), id="x", object="water", source_turn_ids=("turn-x",))
    a = replace(memory(), id="a", valid_from=RECORDED)
    c = replace(memory(), id="c", object="milk", source_turn_ids=("turn-c",))
    d = replace(memory(), id="d", object="juice", source_turn_ids=("turn-d",))
    for candidate in (x, a, c, d):
        origin = candidate.source_turn_ids[0]
        evidence.add_turn(turn(origin, actor="user-a"))
        gateway.output = ExtractionProposal(
            memories=(candidate,),
            relations=(MemoryRelation(from_id="d", to_id="c", type="contradicts"),)
            if candidate.id == "d"
            else (),
        )
        worker.process("char-b", origin)
        state = store.get_extraction("char-b", origin)
        assert state is not None and state.status == "done"
    evidence.add_turn(turn("turn-b", actor="user-a"))
    gateway.output = ExtractionProposal(
        memories=(replace(a, id="b", source_turn_ids=("turn-b",)),),
        relations=(MemoryRelation(from_id="b", to_id="x", type="updates"),),
    )
    worker.process("char-b", "turn-b")
    state = store.get_extraction("char-b", "turn-b")
    assert state is not None and state.status == "done"
    closed = store.get_memory("char-b", "x")
    assert closed is not None and closed.valid_to == RECORDED
    assert publish_and_search(database_url) == {"a", "d"}

    z = replace(x, id="z")
    summary = replace(memory("summary"), id="summary", source_turn_ids=("turn-x",))
    worker.service.requeue("turn-x")
    gateway.output = ExtractionProposal(
        memories=(z, summary),
        relations=(MemoryRelation(from_id="z", to_id="c", type="derives"),),
    )
    worker.process("char-b", "turn-x")
    state = store.get_extraction("char-b", "turn-x")
    assert state is not None and state.status == "done", state
    assert store.get_memory("char-b", "summary") == summary
    assert store.get_memory("char-b", "z") is None
    with psycopg.connect(database_url) as conn:
        effects = conn.execute(
            "SELECT from_id,to_id,effect->>'id' FROM memory_candidate_relations "
            "WHERE character_id='char-b' AND effect IS NOT NULL"
        ).fetchall()
        assert all(row == ("a", "x", "b") for row in effects)
    assert_consistent(worker.service, database_url, {"a", "d", "summary"})


@pytest.mark.parametrize(
    "actor,content,historical_destination,expected_count",
    [
        ("user-a", "a", False, 1),
        ("user-a", "a と `z`", False, 1),
        ("user-a", "a", True, 1),
        ("user-b", "a", False, 2),
    ],
    ids=["same-speaker", "literal-content", "retired-destination", "different-speaker"],
)
@pytest.mark.parametrize("origin_first", [False, True], ids=["support-first", "origin-first"])
def test_dependency_revival_recovers_stranded_contribution_and_effect(
    database_url: str,
    actor: str,
    content: str,
    historical_destination: bool,
    expected_count: int,
    origin_first: bool,
) -> None:
    reinforced_at = RECORDED - timedelta(hours=1)
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    x = replace(memory(), id="x", object="water", source_turn_ids=("turn-x",))
    a = replace(memory(), id="a", valid_from=RECORDED)
    c = replace(memory(), id="c", object="milk", source_turn_ids=("turn-c",))
    d = replace(memory(), id="d", object="juice", source_turn_ids=("turn-d",))
    for candidate in (x, a, c, d):
        origin = candidate.source_turn_ids[0]
        evidence.add_turn(turn(origin, actor="user-a"))
        if candidate.id == "a" and historical_destination:
            gateway.output = (replace(a, id="z"),)
            worker.process("char-b", origin)
            state = store.get_extraction("char-b", origin)
            assert state is not None and state.status == "done"
            worker.service.requeue(origin)
        gateway.output = ExtractionProposal(
            memories=(candidate,),
            relations=(MemoryRelation(from_id="d", to_id="c", type="contradicts"),)
            if candidate.id == "d"
            else (),
        )
        worker.process("char-b", origin)
        state = store.get_extraction("char-b", origin)
        assert state is not None and state.status == "done"
    evidence.add_turn(
        replace(
            turn(
                "turn-b",
                actor=actor,
                addressees=("user-a",) if actor != "user-a" else (),
                address_mode="direct" if actor != "user-a" else "broadcast",
            ),
            occurred_at=reinforced_at,
        )
    )
    gateway.output = ExtractionProposal(
        memories=(replace(a, id="b", source_turn_ids=("turn-b",)),),
        relations=(MemoryRelation(from_id="b", to_id="x", type="updates"),),
    )
    worker.process("char-b", "turn-b")
    state = store.get_extraction("char-b", "turn-b")
    assert state is not None and state.status == "done"
    before = store.get_memory("char-b", "x")
    assert before is not None and before.valid_to == RECORDED
    invalidated = store.get_memory("char-b", "c")
    assert invalidated is not None and invalidated.superseded_at is not None
    assert publish_and_search(database_url) == {"a", "d"}

    z = replace(a, id="z")
    summary = replace(memory("summary"), id="summary", content=content)
    independent = replace(memory("key"), id="key", content="独立した有効な候補")
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            recalculation_gateway=Recalculate(x, a, c, d, z, summary, independent),
        )
    ) as client:
        assert client.post("/turns/turn-1/extractions").status_code == 202
        gateway.output = ExtractionProposal(
            memories=(z, summary, independent),
            relations=(MemoryRelation(from_id="z", to_id="c", type="derives"),),
        )
        worker.process("char-b", "turn-1")

        # Observe the committed partial rebuild before a full projection or recall.
        state = store.get_extraction("char-b", "turn-1")
        assert state is not None and state.status == "done", state
        assert state.error is None
        assert store.get_memory("char-b", "summary") == summary
        assert store.get_memory("char-b", "key") == independent
        rejected = store.get_memory("char-b", "z")
        assert rejected is None or rejected.superseded_at is not None
        assert store.get_memory("char-b", "x") == x
        with psycopg.connect(database_url) as conn:
            sources = conn.execute(
                "SELECT fact_id,extraction_turn_id,source_turn_id,occurred_at "
                "FROM memory_source_contributions WHERE character_id='char-b'"
            ).fetchall()
            assert sources == [("a", "turn-b", "turn-b", reinforced_at)]
            effects = conn.execute(
                "SELECT turn_id,from_id,to_id,type,effect->>'id' "
                "FROM memory_candidate_relations "
                "WHERE character_id='char-b' AND effect IS NOT NULL"
            ).fetchall()
            assert effects == [("turn-b", "a", "x", "updates", "b")]
        expected = {"x", "d", "summary", "key"}
        assert_consistent(worker.service, database_url, expected)
        assert_recalled_values(database_url, {"x": x})
        assert client.post("/turns/turn-d/extractions").status_code == 202
        gateway.output = (d,)
        worker.process("char-b", "turn-d")
        state = store.get_extraction("char-b", "turn-d")
        assert state is not None and state.status == "done", state
        with store.transaction("char-b") as tx:
            snapshot = projection_input(tx)
        full = collect_evidence(snapshot, "char-b", worker.service.inputs).project(
            snapshot, RECORDED
        )
        assert set(full.memories["z"].source_turn_ids) == {"turn-1", "turn-b"}
        assert full.memories["z"].last_reinforced_at == reinforced_at
        assert full.memories["z"].epistemics.independent_evidence_count == expected_count
        assert full.memories["x"] == replace(x, valid_to=RECORDED)
        revived = store.get_memory("char-b", "z")
        assert revived is not None
        assert set(revived.source_turn_ids) == {"turn-1", "turn-b"}
        assert revived.last_reinforced_at == reinforced_at
        assert revived.epistemics.independent_evidence_count == expected_count
        assert store.get_memory("char-b", "x") == replace(x, valid_to=RECORDED)
        with psycopg.connect(database_url) as conn:
            assert conn.execute(
                "SELECT fact_id,extraction_turn_id,source_turn_id,occurred_at "
                "FROM memory_source_contributions WHERE character_id='char-b'"
            ).fetchall() == [("z", "turn-b", "turn-b", reinforced_at)]
            assert conn.execute(
                "SELECT turn_id,from_id,to_id,type,effect->>'id' "
                "FROM memory_candidate_relations "
                "WHERE character_id='char-b' AND effect IS NOT NULL"
            ).fetchall() == [("turn-b", "z", "x", "updates", "b")]
        assert_consistent(worker.service, database_url, {"c", "d", "z", "summary", "key"})
        assert_recalled_values(database_url, {"z": revived})
        if origin_first:
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-origin-first", "target_turn_ids": ["turn-1"]},
            )
            assert response.status_code == 200, response.json()
            assert response.json()["status"] == "done"
            surviving = replace(
                z,
                source_turn_ids=("turn-b",),
                last_reinforced_at=reinforced_at,
            )
            assert store.get_memory("char-b", "z") == surviving
            assert store.get_memory("char-b", "x") == replace(x, valid_to=RECORDED)
            with store.list_relations("char-b", "z") as relations:
                assert set(relations) == {MemoryRelation(from_id="z", to_id="x", type="updates")}
            assert_consistent(worker.service, database_url, {"c", "d", "z"})
            assert_recalled_values(database_url, {"z": surviving})
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-last-support", "target_turn_ids": ["turn-b"]},
            )
            assert response.status_code == 200, response.json()
            assert response.json()["status"] == "done"
            assert store.get_memory("char-b", "z") is None
            assert store.get_memory("char-b", "x") == x
            assert_consistent(worker.service, database_url, {"x", "c", "d"})
            return
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-support", "target_turn_ids": ["turn-b"]},
        )
        assert response.status_code == 200, response.json()
        assert response.json()["status"] == "done"
        assert store.get_memory("char-b", "x") == x
        assert store.get_memory("char-b", "z") == z
        assert_consistent(worker.service, database_url, {"x", "c", "d", "z", "summary", "key"})
        assert_recalled_values(database_url, {"x": x, "z": z})
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-origin", "target_turn_ids": ["turn-1"]},
        )
        assert response.status_code == 200, response.json()
        assert response.json()["status"] == "done"
        assert_consistent(worker.service, database_url, {"x", "c", "d"})
