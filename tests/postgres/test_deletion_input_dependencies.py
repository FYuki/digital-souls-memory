"""Normalized extraction dependencies reach saved copies and recall consumers."""

from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import DerivedMemory, ExtractionProposal, MemoryRelation
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from digital_souls_memory.recall.domain import EmbeddingBatch
from digital_souls_memory.recall.worker import IndexWorker
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import RECORDED, turn
from tests.it1.test_recall import request
from tests.memory_fakes import EchoGateway
from tests.memory_support import NEW_ID, OLD_ID, memory
from tests.postgres.test_deletion import HEADERS, seed_evidence
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_deletion_recalculation import make_worker
from tests.recall_fixtures import SPACE
from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]
OLD_CONTENT = 'はい / 削除する質問 / {"extraction_turn_id":"reply","turn_id":"question"}'


@pytest.mark.parametrize("index_state", ["pending", "published", "paused"])
@pytest.mark.parametrize("dependency,delete", [(True, True), (False, True), (True, False)])
def test_reply_input_deletion_recalculates_saved_copies_and_cannot_republish_old_content(
    database_url: str,
    index_state: str,
    dependency: bool,
    delete: bool,
) -> None:
    evidence = seed_evidence(database_url)
    question = turn("question", text="削除する質問")
    evidence.add_turn(question)
    reply = turn(
        "reply", actor="user-a", text="はい", reply_to_turn_id="question" if dependency else None
    )
    evidence.add_turn(reply)
    evidence.add_turn(turn("kept"))
    store = PostgresMemoryStore(database_url)
    unaffected = replace(
        memory("summary"),
        id="00000000-0000-4000-8000-000000000003",
        content="影響外の内容",
        source_turn_ids=("kept",),
    )
    with store.transaction("char-b") as tx:
        tx.add_memory(unaffected)

    class Reply(EchoGateway):
        def _extract(self, value: LocalOnly | CloudInput) -> tuple[DerivedMemory, ...]:
            super()._extract(value)
            content = (
                OLD_CONTENT
                if any(v.turn_id == "question" for v in value.turns) or not dependency
                else "はい"
            )
            return (
                replace(
                    memory("summary"),
                    id=OLD_ID if content == OLD_CONTENT else NEW_ID,
                    content=content,
                    source_turn_ids=("reply",),
                ),
            )

    gateway = Reply()
    worker = make_worker(database_url, store, gateway)
    worker.process("char-b", "reply")
    assert store.get_memory("char-b", OLD_ID) is not None
    index = PostgresRetrievalStore(database_url)
    indexer = IndexWorker(index, RecordingEmbedding(), space=SPACE, clock=lambda: RECORDED)
    works = index.claim(space=SPACE, now=RECORDED, limit=10)
    old_work = next(w for w in works if w.ref.memory_id == OLD_ID)
    for work in works:
        if work.ref.memory_id != OLD_ID or index_state == "published":
            indexer.process(work)
    loaded = index.load(old_work, now=RECORDED) if index_state == "paused" else None
    if index_state == "paused":
        assert loaded is not None
    with psycopg.connect(database_url) as conn:
        kept_document = conn.execute(
            "SELECT * FROM recall_documents WHERE identifier=%s", (unaffected.id,)
        ).fetchall()
        old_digest = conn.execute(
            "SELECT content_digest FROM recall_documents WHERE identifier=%s", (OLD_ID,)
        ).fetchone()
    final = replace(memory("summary"), content="はい", source_turn_ids=("reply",))
    recalculator = RecordingRecalculationGateway(final)
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            recalculation_gateway=recalculator,
            retriever=index,
            embedding=RecordingEmbedding(),
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
        )
    ) as client:
        if delete:
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-input", "target_turn_ids": ["question"]},
            )
            assert response.status_code == 200
        affected = dependency and delete
        # Observe stored projection, candidate, generated and the final baseline separately.
        with store.transaction("char-b") as tx:
            copies = [
                store.get_memory("char-b", OLD_ID),
                *tx.baselines(),
                *(c.memory for c in tx.candidates()),
            ]
        with psycopg.connect(database_url) as conn:
            if affected and index_state == "published":
                assert old_digest is not None
                assert (
                    conn.execute(
                        "SELECT content_digest FROM recall_embeddings WHERE content_digest=%s",
                        old_digest,
                    ).fetchall()
                    == []
                )
            for table in ("memory_candidates", "memory_generated", "memory_baselines"):
                payloads = conn.execute(
                    "SELECT payload FROM " + table + " WHERE memory_id=%s", (OLD_ID,)
                ).fetchall()
                if table != "memory_baselines" or affected:
                    assert payloads
                    assert payloads[0][0]["content"] == ("はい" if affected else OLD_CONTENT)
            assert (
                conn.execute(
                    "SELECT * FROM recall_documents WHERE identifier=%s", (unaffected.id,)
                ).fetchall()
                == kept_document
            )
        assert (
            all(v.content != OLD_CONTENT for v in copies if v is not None)
            if affected
            else any(v is not None and v.content == OLD_CONTENT for v in copies)
        )
        assert bool(recalculator.calls) == affected
        assert evidence.get_turn("reply") == (
            replace(reply, reply_to_turn_id=None) if delete else reply
        )
        assert store.get_memory("char-b", unaffected.id) == unaffected
        if loaded is not None:
            batch = EmbeddingBatch(space=SPACE, vectors=((1.0,) + (0.0,) * 1023,))
            assert index.publish(old_work, input=loaded, embedding=batch, now=RECORDED) is (
                not affected
            )
        elif index_state == "pending":
            indexer.process(old_work)
        for work in index.claim(space=SPACE, now=RECORDED, limit=10):
            indexer.process(work)
        response = client.post("/characters/char-b/recall", json=request())
        assert response.status_code == 200
        contents = [v["content"] for v in response.json()["memories"]]
        assert (OLD_CONTENT in contents) is (not affected)
        if affected:
            gateway.inputs.clear()
            worker.process("char-b", "reply")
            assert gateway.inputs and all(
                [v.turn_id for v in i.turns] == ["reply"] for i in gateway.inputs
            )
            for work in index.claim(space=SPACE, now=RECORDED, limit=10):
                indexer.process(work)
            response = client.post("/characters/char-b/recall", json=request())
            assert response.status_code == 200
            assert all(v["content"] != OLD_CONTENT for v in response.json()["memories"])


@pytest.mark.parametrize("effect", ["extends", "derives"])
def test_input_dependencies_follow_applied_projection_effects_only(
    database_url: str,
    effect: str,
) -> None:
    evidence = seed_evidence(database_url)
    for source in (
        turn("question"),
        turn("reply", actor="user-a", reply_to_turn_id="question"),
        turn("kept"),
    ):
        evidence.add_turn(source)
    store = PostgresMemoryStore(database_url)
    baseline = replace(memory(), source_turn_ids=("kept",))
    with store.transaction("char-b") as tx:
        tx.add_memory(baseline)

    class Related:
        def extract_local(self, value: LocalOnly) -> ExtractionProposal:
            return ExtractionProposal(
                memories=(replace(baseline, id=NEW_ID, source_turn_ids=("reply",)),),
                relations=(MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type=effect),),
            )

        def extract_cloud(self, value: CloudInput) -> ExtractionProposal:
            raise AssertionError("Synthetic sensitive input must stay local")

    worker = make_worker(database_url, store, Related())
    worker.process("char-b", "reply")
    before = store.get_memory("char-b", OLD_ID)
    assert before is not None
    assert before.source_turn_ids == (("kept", "reply") if effect == "extends" else ("kept",))

    class Recalculate(RecordingRecalculationGateway):
        def recalculate_local(
            self, value: LocalOnly, *, memory_id: str, kind: str
        ) -> DerivedMemory | None:
            super().recalculate_local(value, memory_id=memory_id, kind=kind)
            return replace(
                baseline,
                id=memory_id,
                source_turn_ids=tuple(v.turn_id for v in value.turns),
                content="安全な再計算内容",
            )

    gateway = Recalculate(None)
    with TestClient(create_app(clock=lambda: RECORDED, recalculation_gateway=gateway)) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-effect", "target_turn_ids": ["question"]},
        )
        assert response.status_code == 200
        assert any(identifier == OLD_ID for _, identifier, _ in gateway.calls) is (
            effect == "extends"
        )
        saved = store.get_memory("char-b", OLD_ID)
        assert saved is not None
        if effect == "derives":
            assert saved == before
        else:
            assert saved.content == "安全な再計算内容"
            assert worker.service.active("char-b") == (saved,)
            with store.transaction("char-b") as tx:
                worker.service.rebuild(tx)
            assert store.get_memory("char-b", OLD_ID) == saved
            history = store.get_memory("char-b", NEW_ID)
            assert history is not None and history.superseded_at is not None
