from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import DictRow, dict_row

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.adapters.postgres_voice import PostgresVoiceStore
from digital_souls_memory.adapters.recall_records import current_stamp, read_material
from digital_souls_memory.api import create_app
from tests.evidence_support import RECORDED, receipt, session, turn
from tests.it1.test_recall import request
from tests.postgres.test_recall_indexing import register_characters
from tests.recall_fixtures import SPACE
from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter
from tests.voice_support import anchor

pytestmark = pytest.mark.postgres
TIMEOUT = timedelta(minutes=10)


def test_database_material_reads_one_receipt_state_with_exact_count_and_append_order(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    evidence.add_turn(turn(text="日本語の原文"))
    evidence.add_turn(turn(turn_id="other", text="日本語の原文"))
    transfers: list[int] = []

    class ObservedCursor(psycopg.Cursor[DictRow]):
        def fetchone(self) -> DictRow | None:
            row = super().fetchone()
            if self._query is not None and b"delivery_receipts" in self._query.query:
                transfers.append(int(row is not None))
            return row

        def fetchall(self) -> list[DictRow]:
            rows = super().fetchall()
            if self._query is not None and b"delivery_receipts" in self._query.query:
                transfers.append(len(rows))
            return rows

    with psycopg.connect(
        app_database_url, row_factory=dict_row, cursor_factory=ObservedCursor
    ) as conn:
        empty = read_material(conn, "turn-1")
        assert empty is not None
        assert (empty.receipt_state.spoken_until, empty.receipt_state.count) == (None, 0)
        for i, (status, end) in enumerate(
            (("none", 0), ("partial", 3), ("full", 6), ("partial", 3))
        ):
            # Other turns share the sequence, but must not contribute to this version.
            evidence.add_receipt(replace(receipt("full", 6), turn_id="other"))
            evidence.add_receipt(
                replace(receipt(status, end), reported_at=RECORDED - timedelta(hours=int(i == 3)))
            )
        material = read_material(conn, "turn-1")
        assert material is not None
        assert (material.receipt_state.spoken_until, material.receipt_state.count) == (3, 4)
        stamp = current_stamp(
            conn,
            turn_id="turn-1",
            character_id="char-b",
            used_end=3,
            now=RECORDED,
            timeout=TIMEOUT,
        )
        assert stamp is not None
        assert (stamp.receipt_version, stamp.available_end, stamp.used_end) == (4, 3, 3)
    assert transfers == [1, 1, 1]
    with evidence.list_receipts("turn-1") as history:
        assert [r.spoken_until for r in history] == [0, 3, 6, 3]
    evidence.add_receipt(receipt("partial", 3))
    with evidence.list_receipts("turn-1") as history:
        assert [r.spoken_until for r in history] == [0, 3, 6, 3, 3]


def test_same_range_receipt_before_save_rejects_origin_snapshot_without_documents(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    evidence.add_turn(turn(text="日本語の原文"))
    evidence.add_receipt(receipt("partial", 3))
    provisions = PostgresProvisionStore(app_database_url)
    original_connection = provisions._connection
    append = False

    @contextmanager
    def before_save() -> Iterator[psycopg.Connection[DictRow]]:
        if append:
            evidence.add_receipt(receipt("partial", 3))
        with original_connection() as conn:
            yield conn

    monkeypatch.setattr(provisions, "_connection", before_save)
    with TestClient(
        create_app(
            retriever=PostgresRetrievalStore(app_database_url),
            embedding=RecordingEmbedding(),
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
            provision_store=provisions,
            clock=lambda: RECORDED,
        ),
        raise_server_exceptions=False,
    ) as client:
        payload = {"session_id": "session-1", "format": "materials", "turn_id": "turn-1"}
        first = client.post("/characters/char-b/recall", json=payload)
        assert first.is_success
        append = True
        failed = client.post("/characters/char-b/recall", json=payload)
        append = False
        assert failed.status_code == 503
        with psycopg.connect(app_database_url) as conn:
            assert conn.execute("SELECT count(*) FROM provision_records").fetchone() == (1,)
        assert provisions.get(first.json()["provision_id"]) is not None
        assert client.post("/characters/char-b/recall", json=payload).is_success
        evidence.add_receipt(receipt("none", 0))
        assert client.post("/characters/char-b/recall", json=payload).status_code == 404
        with psycopg.connect(app_database_url) as conn:
            assert conn.execute("SELECT count(*) FROM provision_records").fetchone() == (2,)


def test_anchor_index_reloads_corrected_range_and_rejects_same_range_append(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    evidence.add_turn(turn(text="日本語の原文"))
    evidence.add_receipt(receipt("full", 6))
    with PostgresVoiceStore(app_database_url).transaction("char-b") as tx:
        tx.add_anchor(replace(anchor(), character_id="char-b"))
    index = PostgresRetrievalStore(app_database_url)
    embedding = RecordingEmbedding()
    for text, version in (("日本語の原文", 1), ("日本語", 2)):
        (work,) = index.claim(space=SPACE, now=RECORDED, limit=1)
        loaded = index.load(work, now=RECORDED)
        assert loaded is not None
        assert loaded.input.texts == (text,)
        assert loaded.candidate.dependencies[0].receipt_version == version
        batch = embedding.embed(loaded.input, purpose="passage")
        if version == 2:
            evidence.add_receipt(receipt("partial", 3))
            assert not index.publish(work, input=loaded, embedding=batch, now=RECORDED)
            (work,) = index.claim(space=SPACE, now=RECORDED, limit=1)
            loaded = index.load(work, now=RECORDED)
            assert loaded is not None
            assert loaded.input.texts == ("日本語",)
            assert loaded.candidate.dependencies[0].receipt_version == 3
        assert index.publish(work, input=loaded, embedding=batch, now=RECORDED)
        (found,) = index.search(
            character_id="char-b",
            query=loaded.input,
            query_vector=batch,
            now=RECORDED,
            limit=20,
        ).candidates
        assert found.search_text == text
        with TestClient(
            create_app(
                retriever=index,
                embedding=embedding,
                reranker=RecordingReranker(),
                token_counter=RecordingTokenCounter(),
                clock=lambda: RECORDED,
            )
        ) as client:
            response = client.post("/characters/char-b/recall", json=request())
            assert response.is_success
            assert [v["text"] for v in response.json()["source_spans"]] == [text]
            assert [v["text"] for v in response.json()["verbatim_anchors"]] == [text]
            record = PostgresProvisionStore(app_database_url).get(response.json()["provision_id"])
            assert record is not None
            assert [v.text for v in record.source_spans] == [text]
        if version == 1:
            evidence.add_receipt(receipt("partial", 3))
