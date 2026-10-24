"""Absorbed Fact references agree across projection, persistence and recall."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import DerivedMemory, ExtractionProposal, MemoryRelation
from digital_souls_memory.memory.extraction import LocalOnly
from digital_souls_memory.memory.projection import collect_evidence, projection_input
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import RECORDED, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import memory
from tests.postgres.test_deletion import HEADERS, seed_evidence
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_memory_continuous_ingestion import document_states, publish_and_search
from tests.postgres.test_recall_indexing import register_characters

pytestmark = pytest.mark.postgres


@pytest.mark.usefixtures("deletion_url")
@pytest.mark.parametrize(
    "deleted_source,reextract",
    [("turn-x", False), ("turn-x", True), ("turn-b", False)],
    ids=["retry-rebuild", "same-id-success", "support-deleted"],
)
def test_context_deletion_preserves_surviving_baseline_representative(
    database_url: str, deleted_source: str, reextract: bool
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-x", actor="user-a"))
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    original = replace(memory(), id="a", source_turn_ids=("turn-a",))
    absorbed = replace(original, id="b", source_turn_ids=("turn-b",))
    derived = replace(original, id="d", object="tea", source_turn_ids=("turn-d",))
    for candidate in (original, absorbed, derived):
        source = candidate.source_turn_ids[0]
        evidence.add_turn(
            turn(source, actor="user-a", reply_to_turn_id="turn-x" if candidate.id == "b" else None)
        )
        gateway.output = ExtractionProposal(
            memories=(candidate,),
            relations=(MemoryRelation(from_id="d", to_id="b", type="derives"),)
            if candidate.id == "d"
            else (),
        )
        worker.process("char-b", source)
        state = store.get_extraction("char-b", source)
        assert state is not None and state.status == "done"
    assert {v.id for v in worker.service.active("char-b")} == {"a", "d"}
    assert publish_and_search(database_url) == {"a", "d"}

    class SurvivingSources(RecordingRecalculationGateway):
        def recalculate_local(
            self, value: LocalOnly, *, memory_id: str, kind: str
        ) -> DerivedMemory:
            template = store.get_memory("char-b", memory_id)
            assert template is not None
            return replace(template, source_turn_ids=tuple(v.turn_id for v in value.turns))

    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            recalculation_gateway=SurvivingSources(original),
        )
    ) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-context", "target_turn_ids": [deleted_source]},
        )
        assert response.status_code == 200, response.json()
        assert evidence.get_turn(deleted_source) is None
        state = store.get_extraction("char-b", "turn-b")
        if deleted_source == "turn-x":
            assert state is not None and state.status == "retry"
        else:
            assert state is None
        expected_mapping = {"b": "a"} if deleted_source == "turn-x" else {}
        with store.transaction("char-b") as tx:
            baselines = tx.baselines()
            assert tx.representatives() == expected_mapping
        for after_operation in (False, True):
            if after_operation:
                if reextract:
                    gateway.output = ExtractionProposal(memories=(absorbed,), relations=())
                    worker.process("char-b", "turn-b")
                    state = store.get_extraction("char-b", "turn-b")
                    assert state is not None and state.status == "done"
                else:
                    with store.transaction("char-b") as tx:
                        worker.service.rebuild(tx, frozenset({"a", "b", "d"}))
            with store.transaction("char-b") as tx:
                assert tx.representatives() == expected_mapping
                assert tx.baselines() == baselines
                saved = {v.id: v for v in tx.memories()}
                saved_relations = tx.applied_relations()
                snapshot = projection_input(tx)
            if deleted_source == "turn-x":
                assert saved["b"].superseded_at == RECORDED
                assert saved["b"].source_turn_ids == ("turn-b",)
            else:
                assert "b" not in saved
            assert MemoryRelation(from_id="d", to_id="a", type="derives") in saved_relations
            response = client.get("/characters/char-b/memories")
            assert response.status_code == 200, response.json()
            assert {v["id"] for v in response.json()} == {"a", "d"}
            assert {v.id for v in worker.service.active("char-b")} == {"a", "d"}
            assert publish_and_search(database_url) == {"a", "d"}
            full = collect_evidence(snapshot, "char-b", worker.service.inputs).project(
                snapshot, RECORDED
            )
            assert full.memories == saved
            assert {r for r in full.relations if r.from_id == "d"} == {
                r for r in saved_relations if r.from_id == "d"
            }
            with store.transaction("char-b") as tx:
                assert {v.id: v for v in tx.memories()} == saved
                assert tx.representatives() == expected_mapping


def test_derives_from_absorbed_chain_is_saved_listed_and_indexed(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    store, gateway = PostgresMemoryStore(app_database_url), RecordingGateway()
    worker = make_worker(app_database_url, store, gateway)
    evidence.add_turn(turn("unrelated-turn", actor="user-a"))
    unrelated = replace(
        memory(), id="unrelated", object="paper", source_turn_ids=("unrelated-turn",)
    )
    with store.transaction("char-b") as tx:
        tx.add_memory(unrelated)
    assert publish_and_search(app_database_url) == {unrelated.id}
    unrelated_index = document_states(app_database_url)[unrelated.id]

    original = replace(memory(), id="a")
    candidates = (
        original,
        replace(original, id="b", source_turn_ids=("turn-2",)),
        replace(original, id="c", source_turn_ids=("turn-3",)),
        replace(original, id="d", object="tea", content="紅茶", source_turn_ids=("turn-4",)),
    )
    for number, candidate in enumerate(candidates, 1):
        source = f"turn-{number}"
        evidence.add_turn(turn(source, actor="user-a"))
        gateway.output = ExtractionProposal(
            memories=(candidate,),
            relations=()
            if number == 1
            else (
                MemoryRelation(
                    from_id=candidate.id,
                    to_id=candidates[number - 2].id,
                    type="derives" if number == 4 else "extends",
                ),
            ),
        )
        worker.process("char-b", source)
        state = store.get_extraction("char-b", source)
        assert state is not None and state.status == "done"
        assert store.get_memory("char-b", unrelated.id) == unrelated
        assert document_states(app_database_url)[unrelated.id] == unrelated_index

    # Observe all four boundaries before any global rebuild can repair persistence.
    active = {v.id: v for v in worker.service.active("char-b")}
    assert set(active) == {"a", "d", unrelated.id}
    with store.list_relations("char-b", "d") as relations:
        saved_relations = tuple(relations)
    recalled = publish_and_search(app_database_url)
    assert (
        store.get_memory("char-b", "d"),
        saved_relations,
        recalled,
    ) == (
        active["d"],
        (MemoryRelation(from_id="d", to_id="a", type="derives"),),
        set(active),
    )
    assert store.get_memory("char-b", "a") == active["a"]
    for identifier in ("b", "c"):
        absorbed = store.get_memory("char-b", identifier)
        assert absorbed is not None and absorbed.superseded_at == RECORDED


@pytest.mark.usefixtures("deletion_url")
@pytest.mark.parametrize(
    "relation_type,deleted_source",
    [
        ("updates", "turn-3"),
        ("contradicts", "turn-3"),
        ("derives", "turn-2"),
        ("derives", "turn-3"),
    ],
)
@pytest.mark.parametrize("reextract", [False, True], ids=["original-ids", "changed-id"])
def test_author_deletion_keeps_absorbed_references_consistent(
    database_url: str, relation_type: str, deleted_source: str, reextract: bool
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    original = replace(memory(), id="a")
    candidates = (
        original,
        replace(original, id="b", source_turn_ids=("turn-2",)),
        replace(original, id="c", source_turn_ids=("turn-3",)),
        replace(
            original,
            id="d",
            object="tea",
            content="紅茶",
            valid_from=RECORDED,
            source_turn_ids=("turn-4",),
        ),
    )
    for number, candidate in enumerate(candidates, 1):
        source = f"turn-{number}"
        evidence.add_turn(replace(turn(source, actor="user-a"), occurred_at=candidate.valid_from))
        gateway.output = ExtractionProposal(
            memories=(candidate,),
            relations=()
            if number == 1
            else (
                MemoryRelation(
                    from_id=candidate.id,
                    to_id=candidates[number - 2].id,
                    type=relation_type if number == 4 else "extends",
                ),
            ),
        )
        worker.process("char-b", source)
        state = store.get_extraction("char-b", source)
        assert state is not None and state.status == "done"

    if reextract:
        gateway.output = (replace(original, id="z-reextracted"),)
        store.requeue("turn-1")
        worker.process("char-b", "turn-1")
        state = store.get_extraction("char-b", "turn-1")
        assert state is not None and state.status == "done"
    representative = "z-reextracted" if reextract else "a"
    before = store.get_memory("char-b", representative)
    assert before is not None
    expected_ids = {representative, "d"} if relation_type == "derives" else {"d"}
    assert {v.id for v in worker.service.active("char-b")} == expected_ids
    assert publish_and_search(database_url) == expected_ids
    remaining = tuple(v for v in before.source_turn_ids if v != deleted_source)
    revised = replace(
        before,
        source_turn_ids=remaining,
        epistemics=replace(before.epistemics, independent_evidence_count=1),
    )

    class RemainingOriginals(RecordingRecalculationGateway):
        def recalculate_local(
            self, value: LocalOnly, *, memory_id: str, kind: str
        ) -> DerivedMemory:
            template = super().recalculate_local(value, memory_id=memory_id, kind=kind)
            if memory_id != representative:
                template = next(v for v in candidates if v.id == memory_id)
            assert template is not None
            return replace(template, source_turn_ids=tuple(v.turn_id for v in value.turns))

    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            recalculation_gateway=RemainingOriginals(revised),
        )
    ) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-absorbed", "target_turn_ids": [deleted_source]},
        )
        assert response.status_code == 200, response.json()
        assert evidence.get_turn(deleted_source) is None
        # The same service and pre-existing index survive deletion. Do not globally repair them.
        for partial_rebuild in (False, True):
            if partial_rebuild:
                with store.transaction("char-b") as tx:
                    worker.service.rebuild(tx, frozenset({representative, "b", "c", "d"}))
            saved = store.get_memory("char-b", representative)
            assert saved is not None
            assert saved.source_turn_ids == remaining
            assert saved.object == "coffee"
            assert saved.valid_to == (RECORDED if relation_type == "updates" else None)
            assert saved.superseded_at == (RECORDED if relation_type == "contradicts" else None)
            with store.list_relations("char-b", "d") as relations:
                assert tuple(relations) == (
                    MemoryRelation(from_id="d", to_id=representative, type=relation_type),
                )
            response = client.get("/characters/char-b/memories")
            assert response.status_code == 200, response.json()
            assert {v["id"] for v in response.json()} == expected_ids
            assert {v.id for v in worker.service.active("char-b")} == expected_ids
            assert publish_and_search(database_url) == expected_ids
            with store.transaction("char-b") as tx:
                snapshot = projection_input(tx)
                full = collect_evidence(snapshot, "char-b", worker.service.inputs).project(
                    snapshot, RECORDED
                )
                assert {v.id: v for v in tx.memories()} == full.memories
                assert set(tx.applied_relations()) == set(full.relations)
        restored = PostgresMemoryStore(database_url)
        assert make_worker(database_url, restored, gateway).service.active("char-b") == (
            worker.service.active("char-b")
        )


def test_absorbed_identifier_is_resolved_within_its_character(database_url: str) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    for character, root in (("char-b", "root-b"), ("char-c", "root-c")):
        for number, identifier in enumerate((root, "shared-absorbed-id", "shared-derived-id")):
            source = f"{character}-source-{number}"
            evidence.add_turn(turn(source, actor="user-a", perceived_by=(character,)))
            candidate = replace(
                memory(),
                id=identifier,
                character_id=character,
                shared_with=(),
                source_turn_ids=(source,),
                object="tea" if number == 2 else "coffee",
            )
            gateway.output = ExtractionProposal(
                memories=(candidate,),
                relations=()
                if number == 0
                else (
                    MemoryRelation(
                        from_id=identifier,
                        to_id=root if number == 1 else "shared-absorbed-id",
                        type="extends" if number == 1 else "derives",
                    ),
                ),
            )
            worker.process(character, source)
            state = store.get_extraction(character, source)
            assert state is not None and state.status == "done"
    for character, root in (("char-b", "root-b"), ("char-c", "root-c")):
        with store.list_relations(character, "shared-derived-id") as relations:
            assert tuple(relations) == (
                MemoryRelation(from_id="shared-derived-id", to_id=root, type="derives"),
            )
        assert {v.id for v in worker.service.active(character)} == {root, "shared-derived-id"}
