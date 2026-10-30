import json
from dataclasses import replace
from datetime import timedelta
from typing import Literal

import psycopg
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.adapters.postgres_voice import PostgresVoiceStore
from digital_souls_memory.api import create_app
from digital_souls_memory.recall.domain import (
    CandidateRef,
    EmbeddingBatch,
    RerankInput,
    RerankScore,
)
from digital_souls_memory.recall.inputs import InputLabel, LocalOnly, ModelInput
from digital_souls_memory.recall.lexical import normalized_key
from digital_souls_memory.recall.settings import RecallSettings
from digital_souls_memory.recall.worker import IndexWorker
from tests.evidence_support import RECORDED, receipt, session, turn
from tests.it1.test_recall import request
from tests.memory_support import NEW_ID, OLD_ID, memory
from tests.postgres.test_deletion import HEADERS
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_recall import eventually
from tests.postgres.test_recall_indexing import register_characters
from tests.recall_fixtures import SPACE
from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter
from tests.voice_support import anchor

pytestmark = pytest.mark.postgres


class DistinctEmbedding(RecordingEmbedding):
    def embed(self, value: ModelInput, *, purpose: Literal["query", "passage"]) -> EmbeddingBatch:
        self.calls.append((value, purpose))
        return EmbeddingBatch(
            space=SPACE,
            vectors=tuple(
                (float(sum(map(ord, text))), 1.0) + (0.0,) * 1022 for text in value.texts
            ),
        )


def index_pending(index: PostgresRetrievalStore, embedding: RecordingEmbedding) -> None:
    worker = IndexWorker(index, embedding, space=SPACE, clock=lambda: RECORDED)
    work = index.claim(space=SPACE, now=RECORDED, limit=32)
    assert work
    for item in work:
        worker.process(item)


def search(index: PostgresRetrievalStore) -> tuple[str, ...]:
    query = LocalOnly(texts=("合成検索",), labels=(InputLabel("synthetic-sensitive", "v1"),))
    with index.reading() as reader:
        found = reader.search(
            character_id="char-b",
            query=query,
            query_vector=RecordingEmbedding().embed(query, purpose="query"),
            now=RECORDED,
            limit=200,
        )
    return tuple(c.search_text for c in found.candidates)


@pytest.mark.parametrize("kind", ["memory", "anchor"])
def test_long_document_indexes_each_raw_chunk_with_its_own_vector_and_search_key(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    original = "Ａ" * 448 + "🙂e\u0301Straßeｶﾞ" * 20
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    source = turn(text=original)
    evidence.add_turn(source)
    if kind == "memory":
        with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
            tx.add_memory(replace(memory(), content=original))
    else:
        with PostgresVoiceStore(app_database_url).transaction("char-b") as tx:
            tx.add_anchor(replace(anchor(), character_id="char-b"))
    index = PostgresRetrievalStore(app_database_url)
    embedding = DistinctEmbedding()

    index_pending(index, embedding)

    expected = (original[:512], original[448:])
    assert (
        tuple(
            text
            for value, purpose in embedding.calls
            if purpose == "passage"
            for text in value.texts
        )
        == expected
    )
    with psycopg.connect(app_database_url) as conn:
        rows = conn.execute(
            "SELECT search_text,embedding::text FROM recall_documents WHERE ready"
        ).fetchall()
    assert {text: tuple(json.loads(vector)) for text, vector in rows} == {
        normalized_key(text): (float(sum(map(ord, text))), 1.0) + (0.0,) * 1022 for text in expected
    }
    assert sorted(search(index)) == sorted(expected)
    if kind == "memory":
        ref = CandidateRef(owner_id="char-b", memory_id=OLD_ID, anchor_id=None)
        (parent,) = index.get_candidates(
            refs=(ref,),
            now=RECORDED,
        )
        assert parent.ref == ref
        assert parent.memory == replace(memory(), content=original)
    assert evidence.get_turn(source.turn_id) == source


def test_nondefault_chunks_do_not_overwrite_another_owner_or_document_kind(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(text="保持本文"))
    store = PostgresMemoryStore(app_database_url)
    with store.transaction("char-c") as tx:
        tx.add_memory(replace(memory(), character_id="char-c", content="保持本文"))
    with PostgresVoiceStore(app_database_url).transaction("char-b") as tx:
        tx.add_anchor(replace(anchor(), character_id="char-b", anchor_id=OLD_ID))
    settings = RecallSettings(chunk_length=8, chunk_overlap=2)
    index = PostgresRetrievalStore(app_database_url, settings=settings)
    index_pending(index, DistinctEmbedding())
    with store.transaction("char-b") as tx:
        tx.add_memory(replace(memory(), content="abcdefghijk"))

    embedding = DistinctEmbedding()
    index_pending(index, embedding)

    assert tuple(text for value, _ in embedding.calls for text in value.texts) == (
        "abcdefgh",
        "ghijk",
    )
    with psycopg.connect(app_database_url) as conn:
        rows = conn.execute(
            "SELECT owner_id,kind,identifier,search_text FROM recall_documents WHERE ready"
        ).fetchall()
    assert sorted(rows) == sorted(
        [
            ("char-c", "memory", OLD_ID, "保持本文"),
            ("char-b", "anchor", OLD_ID, "保持本文"),
            ("char-b", "memory", OLD_ID, "abcdefgh"),
            ("char-b", "memory", OLD_ID, "ghijk"),
        ]
    )
    assert {"abcdefgh", "ghijk"} <= set(search(index))


@pytest.mark.usefixtures("deletion_url")
def test_same_store_withdraws_all_old_chunks_on_update_and_delete(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    source = turn(text="変更しない原文")
    evidence.add_turn(source)
    store = PostgresMemoryStore(app_database_url)
    original = replace(memory(), content="前" * 448 + "古い局所事実" * 40)
    with store.transaction("char-b") as tx:
        tx.add_memory(original)
    index = PostgresRetrievalStore(app_database_url)
    embedding = RecordingEmbedding()
    index_pending(index, embedding)
    assert len(search(index)) == 2
    with store.transaction("char-b") as tx:
        tx.replace_memory(replace(original, content="新しい短文"))
    assert search(index) == ()
    index_pending(index, embedding)
    assert search(index) == ("新しい短文",)
    assert evidence.get_turn(source.turn_id) == source
    with psycopg.connect(app_database_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM recall_documents WHERE owner_id='char-b' AND identifier=%s",
            (OLD_ID,),
        ).fetchone() == (1,)
    with store.transaction("char-b") as tx:
        tx.replace_memory(replace(original, content="再" * 448 + "削除する末尾" * 40))
    index_pending(index, embedding)
    assert len(search(index)) == 2
    with TestClient(create_app(clock=lambda: RECORDED)) as client:
        deleted = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-chunks", "target_turn_ids": [source.turn_id]},
        )
        assert deleted.status_code == 200
    assert search(index) == ()
    with psycopg.connect(app_database_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM recall_documents WHERE owner_id='char-b' AND identifier=%s",
            (OLD_ID,),
        ).fetchone() == (0,)
    assert evidence.get_turn(source.turn_id) is None


def test_stale_multichunk_publication_cannot_restore_a_superseded_document(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn())
    memories = PostgresMemoryStore(app_database_url)
    original = replace(memory(), content="前" * 448 + "古い本文" * 40)
    with memories.transaction("char-b") as tx:
        tx.add_memory(original)
    index = PostgresRetrievalStore(app_database_url)
    (work,) = index.claim(space=SPACE, now=RECORDED, limit=32)
    value = index.load(work, now=RECORDED)
    assert value is not None
    assert value.input.texts == (original.content[:512], original.content[448:])
    embedding = RecordingEmbedding().embed(value.input, purpose="passage")
    with memories.transaction("char-b") as tx:
        tx.replace_memory(replace(original, content="現在の本文"))

    assert index.publish(work, input=value, embedding=embedding, now=RECORDED) is False
    assert search(index) == ()
    index_pending(index, RecordingEmbedding())
    assert search(index) == ("現在の本文",)


@pytest.mark.usefixtures("deletion_url")
def test_same_store_updates_anchor_chunks_after_receipts_and_removes_them_on_deletion(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    source = turn(text="前" * 448 + "末尾の逐語出典" * 30)
    evidence.add_turn(source)
    evidence.add_receipt(receipt("full", len(source.text)))
    with PostgresVoiceStore(app_database_url).transaction("char-b") as tx:
        tx.add_anchor(replace(anchor(), character_id="char-b"))
    index = PostgresRetrievalStore(app_database_url)
    embedding = RecordingEmbedding()
    index_pending(index, embedding)
    assert len(search(index)) == 2

    evidence.add_receipt(receipt("partial", 8))
    assert search(index) == ()
    index_pending(index, embedding)
    assert search(index) == (source.text[:8],)
    evidence.add_receipt(receipt("full", len(source.text)))
    index_pending(index, embedding)
    assert len(search(index)) == 2
    assert evidence.get_turn(source.turn_id) == source
    with TestClient(create_app(clock=lambda: RECORDED)) as client:
        deleted = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-anchor-chunks", "target_turn_ids": [source.turn_id]},
        )
        assert deleted.status_code == 200
    assert search(index) == ()
    with psycopg.connect(app_database_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM recall_documents WHERE owner_id='char-b' AND kind='anchor'"
        ).fetchone() == (0,)


def test_failed_tail_embedding_exposes_no_partial_chunks_and_retry_publishes_all(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TailEmbedding(RecordingEmbedding):
        reject_tail = True

        def embed(
            self, value: ModelInput, *, purpose: Literal["query", "passage"]
        ) -> EmbeddingBatch:
            if (
                self.reject_tail
                and purpose == "passage"
                and any("失敗対象" in text for text in value.texts)
            ):
                raise RuntimeError("Synthetic tail embedding failure")
            return super().embed(value, purpose=purpose)

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn())
    content = "前" * 550 + "失敗対象" * 40
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        tx.add_memory(replace(memory(), content=content))
    index = PostgresRetrievalStore(app_database_url)
    embedding = TailEmbedding()

    index_pending(index, embedding)
    assert search(index) == ()
    with psycopg.connect(app_database_url) as conn:
        assert conn.execute("SELECT count(*) FROM recall_documents WHERE ready").fetchone() == (0,)
    embedding.reject_tail = False
    retry_at = RECORDED + timedelta(seconds=1)
    (work,) = index.claim(space=SPACE, now=retry_at, limit=32)
    IndexWorker(index, embedding, space=SPACE, clock=lambda: retry_at).process(work)
    assert sorted(search(index)) == sorted((content[:512], content[448:]))


def test_application_passes_nondefault_chunk_settings_to_its_index_worker(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn())
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        tx.add_memory(replace(memory(), content="abcdefghijk"))
    embedding = RecordingEmbedding()
    with TestClient(
        create_app(
            recall_settings=RecallSettings(chunk_length=8, chunk_overlap=2),
            embedding=embedding,
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
            clock=lambda: RECORDED,
        )
    ):

        def ready() -> bool:
            with psycopg.connect(app_database_url) as conn:
                return conn.execute(
                    "SELECT count(*) FROM recall_documents WHERE ready"
                ).fetchone() == (2,)

        eventually(ready)
        assert tuple(
            t for v, purpose in embedding.calls if purpose == "passage" for t in v.texts
        ) == ("abcdefgh", "ghijk")


class LocalFactReranker:
    def __init__(self) -> None:
        self.calls: list[RerankInput] = []

    def rerank(self, value: RerankInput) -> tuple[RerankScore, ...]:
        self.calls.append(value)
        return tuple(
            RerankScore(
                ref=v.ref,
                chunk_index=v.chunk_index,
                relevance=9 if "合言葉は瑠璃色" in v.input.texts[0] else -9,
            )
            for v in reversed(value.documents)
        )


class LocalFactEmbedding(RecordingEmbedding):
    def embed(self, value: ModelInput, *, purpose: Literal["query", "passage"]) -> EmbeddingBatch:
        self.calls.append((value, purpose))
        return EmbeddingBatch(
            space=SPACE,
            vectors=tuple(
                ((1.0, 0.0) if purpose == "query" or "合言葉は瑠璃色" in text else (0.0, 1.0))
                + (0.0,) * 1022
                for text in value.texts
            ),
        )


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_local_fact_reaches_http_as_a_verbatim_original_window(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    content = "記憶側の前置き。" * 200 + "合言葉は瑠璃色。" + "記憶側の末尾。" * 20
    original = "原文だけの長い前置き🙂e\u0301。" * 500 + content + "原文だけの後書き。" * 500
    source = turn(text=original)
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(source)
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        tx.add_memory(replace(memory(), content=content))
    embedding = LocalFactEmbedding()
    index_pending(PostgresRetrievalStore(app_database_url), embedding)
    reranker = LocalFactReranker()
    counter = RecordingTokenCounter()
    with TestClient(
        create_app(
            embedding=embedding,
            reranker=reranker,
            token_counter=counter,
            clock=lambda: RECORDED + timedelta(minutes=10),
        )
    ) as client:
        response = client.post(
            "/characters/char-b/recall", json=request(form=form) | {"query": "秘密の色は？"}
        )
        assert response.is_success
        body = response.json()
        assert [v["id"] for v in body["memories"]] == [OLD_ID]
        spans = body["source_spans"]
        assert any("合言葉は瑠璃色。" in v["text"] for v in spans)
        matching = [v for v in reranker.calls[-1].documents if "合言葉は瑠璃色" in v.input.texts[0]]
        selected = min(matching, key=lambda v: v.chunk_index)
        assert any(selected.input.texts[0] in v["text"] for v in spans)
        for span in spans:
            assert span["text"] == original[span["start"] : span["end"]]
            assert (span["original_start"], span["original_end"]) == (0, len(original))
            assert span["truncated"] is True
        assert body["context"] == (
            "\n".join(v["text"] for v in spans) if form == "assembled" else None
        )
        assert any(json.loads(v) == body for v in counter.payloads)
        assert (len(response.text) + 3) // 4 <= 1500
    assert all(len(v.input.texts[0]) <= 512 for call in reranker.calls for v in call.documents)
    assert evidence.get_turn(source.turn_id) == source


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_short_documents_preserve_fixed_order_and_verbatim_response(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    texts = ("Ａ" * 100, "🙂" * 256)
    for identifier, text in zip((OLD_ID, NEW_ID), texts, strict=True):
        evidence.add_turn(turn(turn_id=identifier, text=text))
        with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
            tx.add_memory(
                replace(memory(), id=identifier, content=text, source_turn_ids=(identifier,))
            )
    embedding = RecordingEmbedding()
    index_pending(PostgresRetrievalStore(app_database_url), embedding)
    assert tuple(t for v, _ in embedding.calls for t in v.texts) == texts
    with TestClient(
        create_app(
            embedding=embedding,
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
            clock=lambda: RECORDED,
        )
    ) as client:
        response = client.post("/characters/char-b/recall", json=request(form=form))
        assert response.is_success
        body = response.json()
        assert [(v["id"], v["content"]) for v in body["memories"]] == list(
            zip((OLD_ID, NEW_ID), texts, strict=True)
        )
        assert [
            (v["turn_id"], v["start"], v["end"], v["text"], v["truncated"])
            for v in body["source_spans"]
        ] == [
            (OLD_ID, 0, 100, texts[0], False),
            (NEW_ID, 0, 256, texts[1], False),
        ]
