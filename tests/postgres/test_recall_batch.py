"""Synthetic HTTP measurements and whole-candidate snapshot regressions."""

from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.abc import Params, Query
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.adapters.postgres_voice import PostgresVoiceStore
from digital_souls_memory.api import create_app
from digital_souls_memory.evidence.provision import ProvisionRecord
from digital_souls_memory.recall.domain import ProvisionSnapshot, RerankInput, RerankScore
from digital_souls_memory.recall.settings import RecallSettings
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import RECORDED, TEXT, receipt, session, turn
from tests.it1.test_recall import request
from tests.memory_support import memory
from tests.postgres.test_deletion import HEADERS
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_recall_indexing import register_characters
from tests.recall_fixtures import SPACE
from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter
from tests.voice_support import anchor

pytestmark = pytest.mark.postgres
NOW = RECORDED + timedelta(minutes=10)


def seed_candidates(url: str, start: int, end: int, *, shared_source: bool) -> None:
    evidence = PostgresEvidenceStore(url)
    for i in range(start, end):
        source_id = "source-000" if shared_source else f"source-{i:03}"
        if not shared_source or i == 0:
            source_session = f"source-session-{i:03}"
            evidence.add_session(session(source_session))
            evidence.add_turn(turn(source_id, session_id=source_session))
            evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id=source_id))
        with PostgresMemoryStore(url).transaction("char-b") as tx:
            tx.add_memory(replace(memory(), id=f"memory-{i:03}", source_turn_ids=(source_id,)))
    index = PostgresRetrievalStore(url)
    embedding = RecordingEmbedding()
    for work in index.claim(space=SPACE, now=NOW, limit=end):
        loaded = index.load(work, now=NOW)
        assert loaded is not None
        assert index.publish(
            work, input=loaded, embedding=embedding.embed(loaded.input, purpose="passage"), now=NOW
        )


class DatabaseMeasurements:
    """Observe real driver calls without changing SQL or transaction behavior."""

    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.phase = "read"
        self.counts: Counter[str] = Counter()
        self.connections: list[psycopg.Connection[DictRow]] = []
        connect = cast(Callable[..., psycopg.Connection[DictRow]], psycopg.connect)
        execute = cast(Callable[..., psycopg.Cursor[DictRow]], psycopg.Cursor.execute)
        server_execute = psycopg.ServerCursor.execute
        fetchmany = psycopg.ServerCursor.fetchmany
        commit = psycopg.Connection.commit
        save = PostgresProvisionStore.save

        def observed_connect(conninfo: str = "", **kwargs: object) -> psycopg.Connection[DictRow]:
            connection = connect(conninfo, **kwargs)
            self.connections.append(connection)
            self.counts["connections"] += 1
            return connection

        def observed_execute(
            cursor: psycopg.Cursor[DictRow],
            query: Query,
            params: Params | None = None,
            *,
            prepare: bool | None = None,
            binary: bool | None = None,
        ) -> psycopg.Cursor[DictRow]:
            self.counts[self.phase] += 1
            return execute(cursor, query, params, prepare=prepare, binary=binary)

        def observed_server_execute(
            cursor: psycopg.ServerCursor[DictRow],
            query: Query,
            params: Params | None = None,
            *,
            binary: bool | None = None,
            **kwargs: object,
        ) -> psycopg.ServerCursor[DictRow]:
            self.counts[self.phase] += 1
            return server_execute(cursor, query, params, binary=binary, **kwargs)

        def observed_fetchmany(
            cursor: psycopg.ServerCursor[DictRow], size: int = 0
        ) -> list[DictRow]:
            self.counts[self.phase] += 1
            return fetchmany(cursor, size)

        def observed_commit(connection: psycopg.Connection[DictRow]) -> None:
            commit(connection)
            self.counts["commits"] += 1

        def observed_save(
            store: PostgresProvisionStore, record: ProvisionRecord, *, snapshot: ProvisionSnapshot
        ) -> None:
            self.phase = "save"
            save(store, record, snapshot=snapshot)

        patch.setattr(psycopg, "connect", observed_connect)
        patch.setattr(psycopg.Cursor, "execute", observed_execute)
        patch.setattr(psycopg.ServerCursor, "execute", observed_server_execute)
        patch.setattr(psycopg.ServerCursor, "fetchmany", observed_fetchmany)
        patch.setattr(psycopg.Connection, "commit", observed_commit)
        patch.setattr(PostgresProvisionStore, "save", observed_save)


@pytest.mark.parametrize("shared_source", [False, True], ids=["distinct-sources", "shared-source"])
@pytest.mark.parametrize("measurement", ["connections", "read", "save"])
def test_http_database_round_trips_do_not_grow_per_candidate(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    shared_source: bool,
    measurement: str,
    record_property: Callable[[str, object], None],
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    PostgresEvidenceStore(app_database_url).add_session(session(reports_delivery=False))
    reranker = RecordingReranker()
    # No lifespan: publish synchronously before measuring, with no background index polling.
    client = TestClient(
        create_app(
            embedding=RecordingEmbedding(),
            reranker=reranker,
            token_counter=RecordingTokenCounter(),
            clock=lambda: NOW,
        ),
        raise_server_exceptions=False,
    )
    observations: dict[int, Counter[str]] = {}
    for start, count in ((0, 20), (20, 100)):
        seed_candidates(app_database_url, start, count, shared_source=shared_source)
        with monkeypatch.context() as patch:
            measured = DatabaseMeasurements(patch)
            response = client.post("/characters/char-b/recall", json=request())
        assert response.is_success
        assert len(response.json()["memories"]) == 5
        assert len(reranker.calls[-1].documents) == 20
        record = PostgresProvisionStore(app_database_url).get(response.json()["provision_id"])
        assert record is not None and len(record.scores) == count
        assert measured.counts["commits"] > 0
        assert all(
            connection.closed or connection.info.transaction_status == TransactionStatus.IDLE
            for connection in measured.connections
        )
        observations[count] = measured.counts
        record_property(str(count), dict(measured.counts))
    assert observations[100][measurement] <= observations[20][measurement], dict(observations)


@pytest.mark.parametrize("change", ["receipt", "projection", "deletion"])
@pytest.mark.usefixtures("deletion_url")
def test_change_to_a_candidate_outside_rerank_rejects_the_entire_provision(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    PostgresEvidenceStore(app_database_url).add_session(session(reports_delivery=False))
    seed_candidates(app_database_url, 0, 21, shared_source=False)
    provisions = PostgresProvisionStore(app_database_url)
    next_id = "before-change"
    armed = False
    evidence = PostgresEvidenceStore(app_database_url)
    memories = PostgresMemoryStore(app_database_url)

    class ChangingReranker(RecordingReranker):
        def rerank(self, value: RerankInput) -> tuple[RerankScore, ...]:
            if armed:
                assert all(document.ref.memory_id != "memory-020" for document in value.documents)
                if change == "receipt":
                    # Same range, new receipt version: checking just text lengths is insufficient.
                    evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id="source-020"))
                elif change == "projection":
                    old = memories.get_memory("char-b", "memory-020")
                    assert old is not None
                    with memories.transaction("char-b") as tx:
                        tx.replace_memory(replace(old, content="更新された合成の記憶"))
                else:
                    deleted = client.post(
                        "/deletion-requests",
                        headers=HEADERS,
                        json={"request_id": "delete-unreturned", "target_turn_ids": ["source-020"]},
                    )
                    assert deleted.is_success
            return super().rerank(value)

    client = TestClient(
        create_app(
            embedding=RecordingEmbedding(),
            reranker=ChangingReranker(),
            token_counter=RecordingTokenCounter(),
            clock=lambda: NOW,
            provision_id_factory=lambda: next_id,
            recalculation_gateway=RecordingRecalculationGateway(None),
        ),
        raise_server_exceptions=False,
    )
    before = client.post("/characters/char-b/recall", json=request())
    assert before.is_success
    assert "memory-020" not in {v["id"] for v in before.json()["memories"]}
    saved = provisions.get("before-change")
    assert saved is not None and len(saved.scores) == 21
    next_id, armed = "during-change", True
    failed = client.post("/characters/char-b/recall", json=request())
    assert failed.status_code == 503
    assert provisions.get("during-change") is None
    assert provisions.get("before-change") == saved
    next_id, armed = "after-change", False
    following = client.post("/characters/char-b/recall", json=request())
    assert following.is_success
    assert provisions.get("after-change") is not None


def test_additional_anchor_uses_the_search_snapshot_but_save_rechecks_a_late_receipt(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from digital_souls_memory.recall.domain import EmbeddingBatch

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    seed_candidates(app_database_url, 0, 20, shared_source=True)
    with PostgresVoiceStore(app_database_url).transaction("char-b") as tx:
        tx.add_anchor(replace(anchor(), character_id="char-b", turn_id="source-000"))
    index = PostgresRetrievalStore(app_database_url)
    (work,) = index.claim(space=SPACE, now=NOW, limit=1)
    loaded = index.load(work, now=NOW)
    assert loaded is not None
    # Keep the anchor out of both initial top-20 lists so anchors_for is required.
    assert index.publish(
        work,
        input=loaded,
        embedding=EmbeddingBatch(space=SPACE, vectors=((0.0, 1.0) + (0.0,) * 1022,)),
        now=NOW,
    )
    counter = RecordingTokenCounter()
    changed = False

    class ReceiptReranker(RecordingReranker):
        def rerank(self, value: RerankInput) -> tuple[RerankScore, ...]:
            nonlocal changed
            assert len(value.documents) == 20
            assert all(document.ref.anchor_id is None for document in value.documents)
            evidence.add_receipt(replace(receipt("partial", 3), turn_id="source-000"))
            changed = True
            return super().rerank(value)

    client = TestClient(
        create_app(
            embedding=RecordingEmbedding(),
            reranker=ReceiptReranker(),
            token_counter=counter,
            clock=lambda: NOW,
            recall_settings=RecallSettings(candidate_limit=20),
            provision_id_factory=lambda: "anchor-in-flight",
        ),
        raise_server_exceptions=False,
    )
    response = client.post("/characters/char-b/recall", json=request() | {"query": "コーヒー"})
    assert changed
    assert response.status_code == 503
    assert PostgresProvisionStore(app_database_url).get("anchor-in-flight") is None
    assembled = [json.loads(payload) for payload in counter.payloads]
    assert any(
        [(a["anchor_id"], a["text"]) for a in payload["verbatim_anchors"]] == [("anchor-1", TEXT)]
        for payload in assembled
    ), "Additional anchors must use the original read snapshot before fresh save validation"
