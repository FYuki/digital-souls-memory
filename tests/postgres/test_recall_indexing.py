from dataclasses import replace
from datetime import timedelta
from threading import Event
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import DerivedMemory
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from tests.evidence_support import RECORDED, TEXT, receipt, session, turn
from tests.it1.test_recall import request
from tests.memory_fakes import EchoGateway
from tests.memory_support import memory
from tests.postgres.test_recall import eventually

pytestmark = pytest.mark.postgres


def register_characters() -> None:
    with TestClient(create_app()) as registration:
        for identifier in ("char-b", "char-c"):
            assert registration.post(
                f"/characters/{identifier}/core-identities",
                json={"values": ["誠実"], "prohibitions": []},
            ).is_success


@pytest.mark.parametrize("change", ["receipt", "projection"])
def test_index_publication_rejects_changed_input_and_can_publish_current_work(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
    from digital_souls_memory.recall.domain import EmbeddingBatch
    from digital_souls_memory.recall.inputs import InputLabel
    from digital_souls_memory.recall.inputs import LocalOnly as RecallLocalOnly
    from tests.recall_fixtures import SPACE

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    now = RECORDED + timedelta(minutes=10)
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    evidence.add_turn(turn())
    memories = PostgresMemoryStore(app_database_url)
    original = replace(memory(), content=TEXT)
    with memories.transaction("char-b") as tx:
        tx.add_memory(original)
    index = PostgresRetrievalStore(app_database_url)
    (work,) = index.claim(space=SPACE, now=now, limit=1)
    loaded = index.load(work, now=now)
    assert loaded is not None
    batch = EmbeddingBatch(space=SPACE, vectors=((1.0,) + (0.0,) * 1023,))

    if change == "receipt":
        evidence.add_receipt(receipt("none", 0))
    else:
        with memories.transaction("char-b") as tx:
            tx.replace_memory(replace(original, content="更新された検索文書"))

    assert index.publish(work, input=loaded, embedding=batch, now=now) is False
    query = RecallLocalOnly(texts=("日本語",), labels=(InputLabel("sensitive", "v1"),))
    assert (
        index.search(character_id="char-b", query=query, query_vector=batch, now=now, limit=20)
        == ()
    )
    if change == "projection":
        (current_work,) = index.claim(space=SPACE, now=now, limit=1)
        current = index.load(current_work, now=now)
        assert current is not None
        assert index.publish(current_work, input=current, embedding=batch, now=now) is True
        (found,) = index.search(
            character_id="char-b", query=query, query_vector=batch, now=now, limit=20
        )
        assert found.memory is not None
        assert found.memory.id == original.id
        assert found.search_text == "更新された検索文書"


@pytest.mark.parametrize("changed_field", ["model", "digest", "dimensions"])
def test_persisted_passages_are_never_searched_in_a_different_vector_space(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, changed_field: str
) -> None:
    from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
    from digital_souls_memory.recall.domain import EmbeddingBatch
    from digital_souls_memory.recall.inputs import InputLabel
    from digital_souls_memory.recall.inputs import LocalOnly as RecallLocalOnly
    from tests.recall_fixtures import SPACE

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn())
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        tx.add_memory(memory())
    index = PostgresRetrievalStore(app_database_url)
    (work,) = index.claim(space=SPACE, now=RECORDED, limit=1)
    loaded = index.load(work, now=RECORDED)
    assert loaded is not None
    batch = EmbeddingBatch(space=SPACE, vectors=((1.0,) + (0.0,) * 1023,))
    assert index.publish(work, input=loaded, embedding=batch, now=RECORDED) is True
    query = RecallLocalOnly(texts=("日本語",), labels=(InputLabel("sensitive", "v1"),))
    assert (
        len(
            index.search(
                character_id="char-b", query=query, query_vector=batch, now=RECORDED, limit=20
            )
        )
        == 1
    )
    if changed_field == "model":
        other = replace(SPACE, model="synthetic-other-model")
    elif changed_field == "digest":
        other = replace(SPACE, digest="e" * 64)
    else:
        other = replace(SPACE, dimensions=512)
    mismatched = EmbeddingBatch(space=other, vectors=((1.0,) + (0.0,) * (other.dimensions - 1),))
    assert (
        index.search(
            character_id="char-b", query=query, query_vector=mismatched, now=RECORDED, limit=20
        )
        == ()
    )


def test_reply_receipt_withdraws_old_search_key_through_failed_extraction_and_recovery(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    evidence.add_turn(turn(turn_id="question"))
    evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id="question"))
    evidence.add_turn(turn(actor="user-a", text="はい", reply_to_turn_id="question"))
    failed_extraction = Event()

    class ReplyGateway(EchoGateway):
        def _extract(self, value: LocalOnly | CloudInput) -> tuple[DerivedMemory, ...]:
            if value.turns[0].turn_id != "turn-1":
                return ()
            if self.fail and value.character_id == "char-b":
                failed_extraction.set()
                raise RuntimeError("Synthetic extraction dependency failure")
            (base,) = super()._extract(value)
            content = " / ".join(v.text for v in value.turns)
            return (
                replace(
                    base,
                    id=str(uuid5(NAMESPACE_URL, f"{value.character_id}:{content}")),
                    content=content,
                    object=content,
                    source_turn_ids=("turn-1",),
                ),
            )

    gateway = ReplyGateway()
    reranker = RecordingReranker()
    with TestClient(
        create_app(
            extraction_gateway=gateway,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            embedding=RecordingEmbedding(),
            reranker=reranker,
            token_counter=RecordingTokenCounter(),
            clock=lambda: RECORDED + timedelta(minutes=10),
        )
    ) as client:
        old_id = str(uuid5(NAMESPACE_URL, f"char-b:はい / {TEXT}"))

        def recalled(identifier: str) -> bool:
            response = client.post("/characters/char-b/recall", json=request())
            assert response.is_success
            return [v["id"] for v in response.json()["memories"]] == [identifier]

        eventually(lambda: recalled(old_id))
        saved = PostgresMemoryStore(app_database_url).get_memory("char-b", old_id)
        assert saved is not None
        assert saved.source_turn_ids == ("turn-1",)
        gateway.fail = True
        evidence.add_receipt(replace(receipt("partial", 9), turn_id="question"))
        assert failed_extraction.wait(5), "Reply correction did not notify the dependent extraction"
        reranker.calls.clear()
        withdrawn = client.post("/characters/char-b/recall", json=request())
        assert withdrawn.is_success
        assert withdrawn.json()["memories"] == withdrawn.json()["source_spans"] == []
        assert all(
            document.ref.memory_id != old_id
            for call in reranker.calls
            for document in call.documents
        )
        gateway.fail = False
        evidence.add_receipt(replace(receipt("partial", 9), turn_id="question"))
        new_id = str(uuid5(NAMESPACE_URL, f"char-b:はい / {TEXT[:9]}"))
        eventually(lambda: recalled(new_id))
        recovered = client.post("/characters/char-b/recall", json=request())
        assert recovered.is_success
        assert [v["id"] for v in recovered.json()["memories"]] == [new_id]
        assert [v["text"] for v in recovered.json()["source_spans"]] == ["はい"]
        assert evidence.get_turn("question") == turn(turn_id="question")


@pytest.mark.parametrize("observed", ["used_range", "source_time"])
def test_index_preserves_combined_input_dependencies_and_supporting_source_time(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    observed: str,
) -> None:
    from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
    from digital_souls_memory.memory.domain import (
        DeliveryBasis,
        ExtractionState,
        InputDependency,
        MemoryRelation,
    )
    from digital_souls_memory.recall.domain import EmbeddingBatch
    from tests.memory_support import NEW_ID
    from tests.recall_fixtures import SPACE

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    source = turn()
    context = replace(turn(turn_id="context"), occurred_at=RECORDED + timedelta(days=30))
    evidence.add_turn(source)
    evidence.add_turn(context)
    timeout = RECORDED + timedelta(minutes=10)
    dependency = InputDependency(
        turn_id=source.turn_id,
        basis="declared_full",
        receipt_version=0,
        available_end=len(TEXT),
        used_end=len(TEXT),
        deadline=timeout,
    )
    first = memory()
    second = replace(memory(), id=NEW_ID)
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        for origin, value, dependencies in (
            ("turn-1", first, (dependency,)),
            (
                "context",
                second,
                (replace(dependency, used_end=9), replace(dependency, turn_id="context")),
            ),
        ):
            tx.save_extraction(
                ExtractionState(
                    character_id="char-b",
                    turn_id=origin,
                    delivery_basis=DeliveryBasis.DECLARED_FULL,
                    signature="synthetic",
                    status="done",
                    token=None,
                    lease_until=None,
                    deadline=timeout,
                    error=None,
                    used_text=TEXT,
                    receipt_version=0,
                )
            )
            tx.set_candidates(origin, (value,), dependencies=dependencies)
            tx.add_generated(value)
        tx.add_relation(MemoryRelation(from_id=second.id, to_id=first.id, type="extends"))
    index = PostgresRetrievalStore(app_database_url)
    works = index.claim(space=SPACE, now=RECORDED, limit=2)
    work = next(v for v in works if v.ref.memory_id == first.id)
    loaded = index.load(work, now=RECORDED)
    assert loaded is not None
    batch = EmbeddingBatch(space=SPACE, vectors=((1.0,) + (0.0,) * 1023,))
    assert index.publish(work, input=loaded, embedding=batch, now=RECORDED)
    (restored,) = index.search(
        character_id="char-b", query=loaded.input, query_vector=batch, now=RECORDED, limit=20
    )
    if observed == "source_time":
        assert loaded.candidate.occurred_at == source.occurred_at
        assert restored.occurred_at == source.occurred_at
    else:
        assert next(
            v for v in loaded.candidate.dependencies if v.turn_id == source.turn_id
        ).used_end == len(TEXT)
        assert next(
            v for v in restored.dependencies if v.turn_id == source.turn_id
        ).used_end == len(TEXT)
