from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from math import sqrt
from typing import Self

import psycopg
import pytest
from psycopg import sql
from psycopg.abc import Params, Query
from psycopg.rows import DictRow

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.adapters.postgres_voice import PostgresVoiceStore
from digital_souls_memory.evaluation.retrieval import lexical_terms, normalized_key
from digital_souls_memory.recall.domain import EmbeddingBatch, RecallCandidate
from digital_souls_memory.recall.inputs import InputLabel, LocalOnly
from tests.evidence_support import RECORDED, receipt, session, turn
from tests.memory_support import KINDS, memory
from tests.postgres.test_recall_indexing import register_characters
from tests.recall_fixtures import SPACE
from tests.voice_support import anchor

pytestmark = pytest.mark.postgres


def batch(similarity: float) -> EmbeddingBatch:
    return EmbeddingBatch(
        space=SPACE,
        vectors=((similarity, sqrt(1 - similarity**2)) + (0.0,) * 1022,),
    )


def search(
    index: PostgresRetrievalStore, text: str, *, reader: str, now: datetime, limit: int
) -> tuple[RecallCandidate, ...]:
    return index.search(
        character_id=reader,
        query=LocalOnly(texts=(text,), labels=(InputLabel("synthetic-sensitive", "v1"),)),
        query_vector=batch(1),
        now=now,
        limit=limit,
    )


def publish(index: PostgresRetrievalStore, *, now: datetime) -> None:
    for work in index.claim(space=SPACE, now=now, limit=100):
        loaded = index.load(work, now=now)
        assert loaded is not None
        similarity = (
            0.99 if str(work.ref.memory_id or work.ref.anchor_id).startswith("high-") else 0.2
        )
        assert index.publish(work, input=loaded, embedding=batch(similarity), now=now)


@pytest.mark.parametrize(
    "query", ["日本語", "日", "%", "_", "\\", "'", "該当なし", "青い駅", "ＡＢＣ", "Straße", ""]
)
def test_search_unions_semantic_and_term_candidates_with_evaluation_ratios(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    index = PostgresRetrievalStore(app_database_url)
    texts = [
        "別の文書",
        "もう一つの文書",
        "無関係な文書",
        "日本語",
        "日本語あ",
        "日本語あい",
        "青い空",
        "赤い駅",
        "ＡＢのみ",
        "STRASSE",
        "100%",
        "a_b",
        "a\\b",
        "引用'文",
    ]
    for i, text in enumerate(texts):
        identifier = f"high-{i}" if i < 3 else f"match-{i}"
        evidence.add_turn(turn(turn_id=identifier, text=text))
        with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
            tx.add_memory(
                replace(memory(), id=identifier, content=text, source_turn_ids=(identifier,))
            )
        # The same identifier belongs to distinct memory and anchor candidates.
        with PostgresVoiceStore(app_database_url).transaction("char-b") as voice:
            voice.add_anchor(
                replace(anchor(), character_id="char-b", anchor_id=identifier, turn_id=identifier)
            )
    publish(index, now=RECORDED)
    with psycopg.connect(app_database_url) as conn:
        semantic = conn.execute(
            "SELECT owner_id,kind,identifier FROM recall_documents "
            "ORDER BY embedding <=> %s::vector LIMIT 6",
            ("[1," + ",".join(["0"] * 1023) + "]",),
        ).fetchall()
        documents = conn.execute(
            "SELECT owner_id,kind,identifier,search_text FROM recall_documents"
        ).fetchall()
    terms = lexical_terms(query)
    scores = {
        (owner, kind, identifier): (
            sum(term in normalized_key(text) for term in terms) / len(terms) if terms else 0.0
        )
        for owner, kind, identifier, text in documents
    }
    # Each query has at most six lexical matches, avoiding an unspecified order among ties.
    lexical = {key for key, value in scores.items() if value > 0}
    assert len(lexical) <= 6

    found = search(index, query, reader="char-b", now=RECORDED, limit=6)

    keys = [
        (v.ref.owner_id, "memory" if v.memory else "anchor", v.ref.memory_id or v.ref.anchor_id)
        for v in found
    ]
    assert set(keys) == set(semantic) | set(lexical)
    assert len(keys) == len(set(keys))
    for key, candidate in zip(keys, found, strict=True):
        assert candidate.lexical == pytest.approx(scores[key])
        assert candidate.semantic == pytest.approx(0.99 if str(key[2]).startswith("high-") else 0.2)


@pytest.mark.parametrize("query", ["日本語", "該当なし"])
@pytest.mark.parametrize("allowed_count", [2, 3])
@pytest.mark.parametrize(
    "denied",
    ["perception", "thought", "pending", "partial", "private", "future", "expired", "superseded"],
)
def test_search_refills_after_ineligible_leading_candidates(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    denied: str,
    query: str,
    allowed_count: int,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=denied in ("pending", "partial")))
    source = turn(
        perceived_by=("char-c",) if denied in ("perception", "thought") else ("char-b", "char-c"),
        kind="thought" if denied == "thought" else "utterance",
        address_mode="self" if denied == "thought" else "direct",
        addressees=() if denied == "thought" else ("char-b",),
        text="日本語の合成原文",
    )
    evidence.add_turn(source)
    evidence.add_turn(turn(turn_id="control", actor="char-b", text="日本語の許可された原文"))
    if denied == "partial":
        evidence.add_receipt(receipt("partial", 3))
    index = PostgresRetrievalStore(app_database_url)
    base = replace(
        memory(),
        character_id="char-c",
        shared_with=() if denied == "private" else ("char-b",),
        content="日本語",
        valid_from=RECORDED + timedelta(seconds=1) if denied == "future" else memory().valid_from,
        valid_to=RECORDED if denied == "expired" else None,
        superseded_at=RECORDED if denied == "superseded" else None,
    )
    with PostgresMemoryStore(app_database_url).transaction("char-c") as tx:
        for i in range(64):
            tx.add_memory(replace(base, id=f"high-{i}"))
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        for i in range(allowed_count):
            tx.add_memory(
                replace(
                    memory(),
                    id=f"allowed-{i}",
                    # Keep denied rows ahead under term-ratio ordering to exercise refill.
                    content="日本の記憶",
                    source_turn_ids=("control",),
                )
            )
    published_at = (
        RECORDED + timedelta(seconds=1)
        if denied == "future"
        else RECORDED - timedelta(seconds=1)
        if denied in ("expired", "superseded")
        else RECORDED
    )
    publish(index, now=published_at)

    fetch_sizes: list[int] = []
    fetched = 0
    closed = 0

    class ObservedCursor(psycopg.ServerCursor[DictRow]):
        def fetchmany(self, size: int = 0) -> list[DictRow]:
            nonlocal fetched
            fetch_sizes.append(size)
            rows = super().fetchmany(size)
            fetched += len(rows)
            return rows

        def close(self) -> None:
            nonlocal closed
            closed += 1
            super().close()

    connection_factory = index._connection

    @contextmanager
    def observed_connection() -> Iterator[psycopg.Connection[DictRow]]:
        with connection_factory() as conn:
            conn.server_cursor_factory = ObservedCursor
            yield conn
            assert conn.execute("SELECT count(*) AS count FROM pg_cursors").fetchone() == {
                "count": 0
            }

    monkeypatch.setattr(index, "_connection", observed_connection)

    found = search(index, query, reader="char-b", now=RECORDED, limit=3)

    assert {v.ref.memory_id for v in found} == {f"allowed-{i}" for i in range(allowed_count)}
    assert len(found) == allowed_count
    assert fetch_sizes and max(fetch_sizes) == 3
    assert closed >= 2
    if query == "日本語" or denied in ("perception", "thought", "pending", "partial"):
        assert fetched >= 64 + allowed_count


def test_same_store_observes_latest_receipt_without_losing_owner_full_text(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    source = turn(text="日本語の合成原文")
    evidence.add_turn(source)
    evidence.add_receipt(receipt("full", len(source.text)))
    with PostgresMemoryStore(app_database_url).transaction("char-c") as tx:
        tx.add_memory(replace(memory(), character_id="char-c", shared_with=("char-b",)))
    index = PostgresRetrievalStore(app_database_url)
    publish(index, now=RECORDED)

    def ids(reader: str) -> set[str | None]:
        return {
            v.ref.memory_id for v in search(index, "日本語", reader=reader, now=RECORDED, limit=3)
        }

    assert ids("char-b") == ids("char-c") == {memory().id}
    # Append order takes precedence even when the correction reports an older time.
    evidence.add_receipt(replace(receipt("partial", 3), reported_at=RECORDED - timedelta(hours=1)))
    assert ids("char-b") == set()
    assert ids("char-c") == {memory().id}
    evidence.add_receipt(receipt("full", len(source.text)))
    assert ids("char-b") == ids("char-c") == {memory().id}


def test_search_shares_only_facts_and_preferences_and_keeps_anchors_with_owner(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(text="日本語"))
    with PostgresMemoryStore(app_database_url).transaction("char-c") as tx:
        for kind in KINDS:
            tx.add_memory(
                replace(
                    memory(kind),
                    id=kind,
                    character_id="char-c",
                    shared_with=("char-b",),
                    content="日本語",
                )
            )
    for owner in ("char-b", "char-c"):
        with PostgresVoiceStore(app_database_url).transaction(owner) as tx:
            tx.add_anchor(replace(anchor(), character_id=owner, anchor_id="fact"))
    index = PostgresRetrievalStore(app_database_url)
    publish(index, now=RECORDED)

    found = search(index, "日本語", reader="char-b", now=RECORDED, limit=20)
    assert {(v.ref.owner_id, v.ref.memory_id, v.ref.anchor_id) for v in found} == {
        ("char-c", "fact", None),
        ("char-c", "preference", None),
        ("char-b", None, "fact"),
    }
    owned = search(index, "日本語", reader="char-c", now=RECORDED, limit=20)
    assert {v.ref.memory_id for v in owned if v.memory} == set(KINDS)
    assert {(v.ref.owner_id, v.ref.anchor_id) for v in owned if v.anchor} == {("char-c", "fact")}


def plan_nodes(node: DictRow) -> Iterator[DictRow]:
    yield node
    for child in node.get("Plans", []):
        yield from plan_nodes(child)


def test_product_search_uses_lexical_index_without_document_seq_scan_at_ten_thousand(
    app_database_url: str, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn())
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        tx.add_memory(replace(memory(), content="日本語 合成記憶"))
    index = PostgresRetrievalStore(app_database_url)
    publish(index, now=RECORDED)
    # Earlier migration tests leave WAL on the disposable container's limited tmpfs.
    with psycopg.connect(database_url, autocommit=True) as owner:
        owner.execute("CHECKPOINT")
    # Clone a published synthetic document, retaining real sources and payload shape.
    with psycopg.connect(app_database_url) as conn:
        conn.execute(
            "INSERT INTO derived_memories SELECT (jsonb_populate_record(NULL::derived_memories, "
            "to_jsonb(m)||jsonb_build_object('id','plan-'||i))).* "
            "FROM derived_memories m CROSS JOIN generate_series(1,9999) i WHERE m.id=%s",
            (memory().id,),
        )
        conn.execute(
            "INSERT INTO memory_sources SELECT 'char-b','plan-'||i,'turn-1',0 "
            "FROM generate_series(1,9999) i"
        )
        conn.execute(
            "UPDATE recall_documents d SET ready=TRUE,pending=FALSE,search_text=t.search_text, "
            "content_digest=t.content_digest,model=t.model,revision=t.revision,digest=t.digest, "
            "dimensions=t.dimensions,embedding=t.embedding, "
            "payload=t.payload||jsonb_build_object('id',d.identifier), "
            "dependencies=t.dependencies,labels=t.labels FROM recall_documents t "
            "WHERE t.identifier=%s AND d.identifier<>t.identifier",
            (memory().id,),
        )
        conn.execute(
            "INSERT INTO recall_document_sources SELECT 'char-b','memory','plan-'||i,'turn-1' "
            "FROM generate_series(1,9999) i"
        )
        assert conn.execute("SELECT count(*) FROM recall_documents WHERE ready").fetchone() == (
            10000,
        )
        conn.execute("ANALYZE recall_documents")
        conn.execute("ANALYZE derived_memories")

    with psycopg.connect(database_url, autocommit=True) as owner:
        owner.execute("CHECKPOINT")

    nodes: list[DictRow] = []

    class PlanCursor(psycopg.ServerCursor[DictRow]):
        def execute(
            self,
            query: Query,
            params: Params | None = None,
            *,
            binary: bool | None = None,
            **kwargs: object,
        ) -> Self:
            if not isinstance(query, (str, bytes, sql.SQL, sql.Composed)):
                return super().execute(query, params, binary=binary, **kwargs)
            text = (
                query
                if isinstance(query, str)
                else query.decode()
                if isinstance(query, bytes)
                else query.as_string(self.connection)
            )
            if text.lstrip().upper().startswith(("SELECT", "WITH")) and "recall_documents" in text:
                with psycopg.Cursor(self.connection) as cursor:
                    assert self.scrollable is False and not self.withhold
                    cursor.execute(
                        sql.SQL(
                            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) DECLARE {} "
                            "NO SCROLL CURSOR WITHOUT HOLD FOR "
                        ).format(sql.Identifier(self.name))
                        + sql.SQL(text),
                        params,
                    )
                    row = cursor.fetchone()
                    assert row is not None
                    nodes.extend(plan_nodes(row["QUERY PLAN"][0]["Plan"]))
            return super().execute(query, params, binary=binary, **kwargs)

    connection_factory = index._connection

    @contextmanager
    def observed_connection() -> Iterator[psycopg.Connection[DictRow]]:
        with connection_factory() as conn:
            conn.server_cursor_factory = PlanCursor
            yield conn

    monkeypatch.setattr(index, "_connection", observed_connection)

    found = search(index, "青い駅 日本語", reader="char-b", now=RECORDED, limit=200)

    assert len(found) >= 200
    assert nodes, "No product candidate query was explained"
    indexes = {node["Index Name"] for node in nodes if "Index Name" in node}
    scans = [
        node
        for node in nodes
        if node.get("Relation Name") == "recall_documents" and node["Node Type"] == "Seq Scan"
    ]
    assert "recall_lexical" in indexes and not scans, {
        "indexes": indexes,
        "document_seq_scans": scans,
    }


def test_search_reuses_source_reads_in_one_snapshot_and_refreshes_next_search(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from digital_souls_memory.adapters.recall_records import read_material
    from digital_souls_memory.recall.domain import CandidateMaterial

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    source = turn(text="日本語の合成原文")
    evidence.add_turn(source)
    evidence.add_receipt(receipt("full", len(source.text)))
    with PostgresMemoryStore(app_database_url).transaction("char-c") as tx:
        for i in range(4):
            tx.add_memory(
                replace(
                    memory(),
                    id=f"snapshot-{i}",
                    character_id="char-c",
                    shared_with=("char-b",),
                    content="日本語",
                )
            )
    index = PostgresRetrievalStore(app_database_url)
    publish(index, now=RECORDED)
    reads: list[str] = []

    def concurrent_receipt(
        connection: psycopg.Connection[DictRow], turn_id: str
    ) -> CandidateMaterial | None:
        if not reads:
            # The candidate query has already established this search's snapshot.
            evidence.add_receipt(receipt("partial", 3))
        reads.append(turn_id)
        return read_material(connection, turn_id)

    monkeypatch.setattr(
        "digital_souls_memory.adapters.recall_search.read_material", concurrent_receipt
    )
    first = search(index, "日本語", reader="char-b", now=RECORDED, limit=4)
    assert {candidate.ref.memory_id for candidate in first} == {f"snapshot-{i}" for i in range(4)}
    assert reads == [source.turn_id]
    assert search(index, "日本語", reader="char-b", now=RECORDED, limit=4) == ()
    assert reads == [source.turn_id, source.turn_id]
