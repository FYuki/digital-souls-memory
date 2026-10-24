"""Absorbed Fact references agree across projection, persistence and recall."""

from dataclasses import replace

import pytest

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.memory.domain import ExtractionProposal, MemoryRelation
from tests.evidence_support import RECORDED, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import memory
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_memory_continuous_ingestion import document_states, publish_and_search
from tests.postgres.test_recall_indexing import register_characters

pytestmark = pytest.mark.postgres


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
