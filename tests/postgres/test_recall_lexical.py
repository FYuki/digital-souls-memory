from dataclasses import replace
from math import sqrt

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.adapters.postgres_voice import PostgresVoiceStore
from digital_souls_memory.api import create_app
from digital_souls_memory.recall.domain import CandidateRef, EmbeddingBatch
from digital_souls_memory.recall.inputs import InputLabel, LocalOnly
from digital_souls_memory.recall.settings import RecallSettings
from tests.evidence_support import RECORDED, session, turn
from tests.it1.test_recall import request
from tests.memory_support import memory
from tests.postgres.test_recall_indexing import register_characters
from tests.recall_fixtures import SPACE
from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter
from tests.voice_support import anchor

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("kind", ["memory", "anchor"])
@pytest.mark.parametrize("origin", [False, True], ids=["query", "turn"])
def test_lexical_match_outside_semantic_limit_reaches_http_and_provision(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, kind: str, origin: bool
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    index = PostgresRetrievalStore(app_database_url)
    embedding = RecordingEmbedding()
    reranker = RecordingReranker()
    target = CandidateRef(
        owner_id="char-b",
        memory_id="target" if kind == "memory" else None,
        anchor_id="target" if kind == "anchor" else None,
    )
    for i in range(21):
        identifier = "target" if i == 20 else f"distractor-{i:02d}"
        text = "ＡＢの原文" if i == 20 else "合成された別の文書"
        evidence.add_turn(turn(turn_id=identifier, text=text))
        if kind == "memory":
            with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
                tx.add_memory(
                    replace(memory(), id=identifier, content=text, source_turn_ids=(identifier,))
                )
        else:
            with PostgresVoiceStore(app_database_url).transaction("char-b") as tx:
                tx.add_anchor(
                    replace(
                        anchor(), character_id="char-b", anchor_id=identifier, turn_id=identifier
                    )
                )
    works = index.claim(space=SPACE, now=RECORDED, limit=21)
    assert len(works) == 21
    for work in works:
        loaded = index.load(work, now=RECORDED)
        assert loaded is not None
        similarity = 0.79 if work.ref == target else 0.80
        vector = (similarity, sqrt(1 - similarity**2)) + (0.0,) * 1022
        assert index.publish(
            work,
            input=loaded,
            embedding=EmbeddingBatch(space=SPACE, vectors=(vector,)),
            now=RECORDED,
        )
        reranker.scores[work.ref] = 1 if work.ref == target else -3

    with TestClient(
        create_app(
            retriever=index,
            embedding=embedding,
            reranker=reranker,
            token_counter=RecordingTokenCounter(),
            recall_settings=RecallSettings(candidate_limit=20),
            clock=lambda: RECORDED,
        )
    ) as client:
        for text in ("英語", "ＡＢＣ"):
            query = LocalOnly(texts=(text,), labels=(InputLabel("synthetic-sensitive", "v1"),))
            candidates = index.search(
                character_id="char-b",
                query=query,
                query_vector=embedding.embed(query, purpose="query"),
                now=RECORDED,
                limit=20,
            )
            for candidate in candidates:
                assert candidate.semantic == pytest.approx(
                    0.79 if candidate.ref == target else 0.80
                )
            if origin:
                identifier = f"origin-{text}"
                evidence.add_turn(turn(turn_id=identifier, text=text))
                payload: dict[str, object] = {
                    "session_id": "session-1",
                    "format": "materials",
                    "turn_id": identifier,
                }
            else:
                payload = request() | {"query": text}
            response = client.post("/characters/char-b/recall", json=payload)
            assert response.is_success
            body = response.json()
            expected = ["target"] if text == "ＡＢＣ" else []
            assert [v["id"] for v in body["memories"]] == (expected if kind == "memory" else [])
            assert [v["anchor_id"] for v in body["verbatim_anchors"]] == (
                expected if kind == "anchor" else []
            )
            assert len(candidates) == (21 if expected else 20)
            assert (target in {v.ref for v in candidates}) is bool(expected)
            if expected:
                assert next(v.lexical for v in candidates if v.ref == target) == pytest.approx(0.5)
            assert [v["text"] for v in body["source_spans"]] == (["ＡＢの原文"] if expected else [])
            record = PostgresProvisionStore(app_database_url).get(body["provision_id"])
            assert record is not None
            assert list(record.memory_ids) == (expected if kind == "memory" else [])
            assert list(record.verbatim_anchor_ids) == (expected if kind == "anchor" else [])
            assert [v.text for v in record.source_spans] == [
                v["text"] for v in body["source_spans"]
            ]
            assert (target in {v.ref for v in reranker.calls[-1].documents}) is bool(expected)

            if expected:
                scores = {value.ref: value for value in record.scores}
                assert scores[target].lexical == pytest.approx(0.5)
                document = next(v for v in reranker.calls[-1].documents if v.ref == target)
                assert document.input.texts == ("ＡＢの原文",)
                assert reranker.calls[-1].query.texts == (text,)
