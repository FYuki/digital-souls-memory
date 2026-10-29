from dataclasses import replace
from datetime import timedelta
from functools import partial
from itertools import product

import psycopg
import pytest
from psycopg.rows import dict_row

from digital_souls_memory.adapters import recall_search
from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_provision import (
    PostgresProvisionStore,
    PostgresSessionIdentityStore,
)
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.adapters.postgres_voice import PostgresVoiceStore
from digital_souls_memory.recall.context import serialize_response
from digital_souls_memory.recall.inputs import InputLabel, LocalOnly
from digital_souls_memory.recall.lexical import lexical_terms
from digital_souls_memory.recall.settings import RecallSettings
from digital_souls_memory.recall.use_cases import RecallService
from tests.evidence_support import RECORDED, session, turn
from tests.memory_support import memory
from tests.postgres.recall_legacy import LegacyRecallService, legacy_candidates
from tests.postgres.test_recall_indexing import register_characters
from tests.recall_fixtures import SPACE
from tests.recall_support import (
    RecordingEmbedding,
    RecordingReranker,
    RecordingTokenCounter,
)
from tests.voice_support import anchor

pytestmark = pytest.mark.postgres


# Equivalence applies only to documents that fit in one chunk (default length: 512).
# Longer documents intentionally differ under ADR 0021 and are covered by test_recall_chunks.
@pytest.mark.parametrize("body_length", [24, 512], ids=["short-body", "full-chunk-body"])
@pytest.mark.parametrize("query_length", [3, 50, 200, 1039])
def test_lexical_candidates_response_and_provision_equal_the_frozen_implementation(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, body_length: int, query_length: int
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    index = PostgresRetrievalStore(app_database_url)
    alphabet = "".join(chr(0x4E00 + i) for i in range(1039))
    query = alphabet[:query_length]
    assert len(query) == query_length
    evidence.add_turn(turn(turn_id="origin", text=query))
    # More equal-score documents than either retrieval limit; include a late-term-only match.
    texts = [alphabet[:body_length]] * 24 + [
        query[-2:] + "別" * (body_length - 2),
        "別" * body_length,
    ]
    for i, text in enumerate(texts):
        identifier = f"synthetic-{i:02d}"
        evidence.add_turn(turn(turn_id=identifier, text=text))
        with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
            tx.add_memory(
                replace(memory(), id=identifier, content=text, source_turn_ids=(identifier,))
            )
    with PostgresVoiceStore(app_database_url).transaction("char-b") as tx:
        tx.add_anchor(replace(anchor(), character_id="char-b", turn_id="synthetic-00"))
    works = index.claim(space=SPACE, now=RECORDED, limit=len(texts) + 1)
    assert len(works) == len(texts) + 1
    embedding = RecordingEmbedding()
    for work in works:
        loaded = index.load(work, now=RECORDED)
        assert loaded is not None
        assert index.publish(
            work,
            input=loaded,
            embedding=embedding.embed(loaded.input, purpose="passage"),
            now=RECORDED,
        )
    with psycopg.connect(app_database_url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        ordered_rows = []
        for candidates in (recall_search._candidates, legacy_candidates):
            with candidates(
                connection,
                lexical_terms(query),
                "[" + ",".join(["1"] + ["0"] * 1023) + "]",
                SPACE,
                3,
                True,
                "char-b",
                RECORDED,
                True,
            ) as cursor:
                ordered_rows.append(cursor.fetchall())
        assert ordered_rows[0] == ordered_rows[1]
        assert any(row["identifier"] == "synthetic-24" for row in ordered_rows[0])

    model_query = LocalOnly(texts=(query,), labels=(InputLabel("synthetic-sensitive", "v1"),))
    vector = embedding.embed(model_query, purpose="query")
    current_candidates = index.search(
        character_id="char-b",
        query=model_query,
        query_vector=vector,
        now=RECORDED,
        limit=3,
    )
    with monkeypatch.context() as patch:
        patch.setattr(recall_search, "_candidates", legacy_candidates)
        legacy_result = index.search(
            character_id="char-b",
            query=model_query,
            query_vector=vector,
            now=RECORDED,
            limit=3,
        )
    assert current_candidates == legacy_result
    assert len(current_candidates.candidates) >= 3

    for form, origin in product(("materials", "assembled"), (False, True)):
        results = []
        records = []
        rerank_inputs = []
        for legacy in (False, True):
            reranker = RecordingReranker()
            provisions = PostgresProvisionStore(app_database_url)
            identifier = f"{form}-{origin}-" + ("legacy" if legacy else "actual")
            cls = LegacyRecallService if legacy else RecallService
            subject = cls(
                evidence=evidence,
                characters=IdentityCharacters(lambda: PostgresIdentityStore(app_database_url)),
                retriever=index,
                embedding=embedding,
                reranker=reranker,
                counter=RecordingTokenCounter(),
                provisions=provisions,
                selections=PostgresSessionIdentityStore(app_database_url),
                clock=lambda: RECORDED,
                monotonic=lambda: 0,
                id_factory=partial(str, identifier),
                delivery_timeout=timedelta(minutes=10),
                non_sensitive_labels=frozenset(),
                settings=RecallSettings(candidate_limit=20),
            )
            with monkeypatch.context() as patch:
                if legacy:
                    patch.setattr(recall_search, "_candidates", legacy_candidates)
                result = subject.recall(
                    character_id="char-b",
                    session_id="session-1",
                    format=form,
                    query=None if origin else query,
                    turn_id="origin" if origin else None,
                    sensitivity="synthetic-sensitive",
                    classifier_version="v1",
                )
            results.append(serialize_response(replace(result, provision_id="comparison")))
            record = provisions.get(identifier)
            assert record is not None
            records.append(replace(record, provision_id="comparison"))
            rerank_inputs.append(reranker.calls)
        assert results[0] == results[1]
        assert records[0] == records[1]
        assert rerank_inputs[0] == rerank_inputs[1]
