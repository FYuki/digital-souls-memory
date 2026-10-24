"""Continuous synthetic ingestion through the real projection and index stores."""

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from time import perf_counter
from typing import cast

import psycopg
import pytest
from psycopg import sql
from psycopg.abc import Params, Query
from psycopg.rows import DictRow

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.memory.domain import ExtractionProposal, MemoryRelation
from digital_souls_memory.memory.extraction import MemoryExtractor
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.memory.worker import MemoryWorker
from digital_souls_memory.recall.inputs import InputLabel, LocalOnly
from digital_souls_memory.recall.worker import IndexWorker
from tests.evidence_support import RECORDED, TEXT, receipt, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_fakes import EchoGateway
from tests.memory_support import NEW_ID, OLD_ID, memory
from tests.postgres.test_recall_indexing import register_characters
from tests.recall_fixtures import SPACE
from tests.recall_support import RecordingEmbedding

pytestmark = pytest.mark.postgres


def extraction_worker(url: str) -> MemoryWorker:
    evidence = PostgresEvidenceStore(url)
    materials = EvidenceMaterials(
        evidence,
        IdentityCharacters(lambda: PostgresIdentityStore(url)),
        lambda: RECORDED,
        timedelta(minutes=10),
    )
    inputs = MemoryInputs(
        evidence,
        materials,
        conversation_surfaces=frozenset({"synthetic-chat"}),
        comment_surfaces=frozenset(),
    )
    service = MemoryService(PostgresMemoryStore(url), inputs, lambda: RECORDED)
    return MemoryWorker(
        service,
        MemoryExtractor(inputs=inputs, gateway=EchoGateway(), non_sensitive_labels=frozenset()),
    )


def document_states(url: str) -> dict[str, tuple[object, ...]]:
    with psycopg.connect(url) as connection:
        rows = connection.execute(
            "SELECT identifier,ready,pending,version,claim_token,lease_until "
            "FROM recall_documents WHERE owner_id='char-b' AND kind='memory'"
        ).fetchall()
    return {row[0]: tuple(row[1:]) for row in rows}


def test_continuous_ingestion_keeps_ready_documents_and_publishes_each_new_memory(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    worker = extraction_worker(app_database_url)
    index = PostgresRetrievalStore(app_database_url)
    indexing = IndexWorker(index, RecordingEmbedding(), space=SPACE, clock=lambda: RECORDED)
    published: dict[str, tuple[object, ...]] = {}
    processing_counts: list[int] = []
    for number in range(201):
        identifier = f"ingestion-{number:03}"
        evidence.add_turn(
            turn(
                identifier,
                actor="user-a",
                text=f"合成の独立した事実{number}",
                perceived_by=("char-b",),
            )
        )
        worker.process("char-b", identifier)
        state = worker.service.store.get_extraction("char-b", identifier)
        assert state is not None and state.status == "done"
        after_extraction = document_states(app_database_url)
        assert {key: after_extraction[key] for key in published} == published, number
        processed = 0
        for _ in range(3):
            works = index.claim(space=SPACE, now=RECORDED, limit=32)
            for work in works:
                indexing.process(work)
                processed += 1
            current = document_states(app_database_url)
            if len(current) == number + 1 and all(row[0] for row in current.values()):
                break
        assert len(current) == number + 1
        assert all(row[0] and not row[1] for row in current.values()), (number, current)
        processing_counts.append(processed)
        published = current
    record_property("index_process_calls_per_turn", processing_counts)


@pytest.mark.parametrize("relation", [False, True], ids=["unrelated", "derives"])
def test_extraction_keeps_claim_and_revalidates_current_index_input(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, relation: bool
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    worker = extraction_worker(app_database_url)
    evidence.add_turn(turn(actor="user-a", text="最初の合成事実"))
    worker.process("char-b", "turn-1")
    index = PostgresRetrievalStore(app_database_url)
    (work,) = index.claim(space=SPACE, now=RECORDED, limit=1)
    assert work.ref.memory_id is not None
    loaded = index.load(work, now=RECORDED)
    assert loaded is not None
    before = document_states(app_database_url)[work.ref.memory_id]
    evidence.add_turn(turn("turn-2", actor="user-a", text="別の合成事実"))
    original = worker.service.store.get_memory("char-b", work.ref.memory_id)
    if relation:
        gateway = RecordingGateway()
        gateway.output = ExtractionProposal(
            memories=(replace(memory("summary"), id=NEW_ID, source_turn_ids=("turn-2",)),),
            relations=(MemoryRelation(from_id=NEW_ID, to_id=work.ref.memory_id, type="derives"),),
        )
        worker.extractor.gateway = gateway
    worker.process("char-b", "turn-2")
    assert worker.service.store.get_memory("char-b", work.ref.memory_id) == original
    after = document_states(app_database_url)[work.ref.memory_id]
    embedding = RecordingEmbedding().embed(loaded.input, purpose="passage")
    published = index.publish(work, input=loaded, embedding=embedding, now=RECORDED)
    assert after == before
    if relation:
        assert not published
        assert document_states(app_database_url)[work.ref.memory_id] == before
        current = index.load(work, now=RECORDED)
        assert current is not None and current != loaded
        embedding = RecordingEmbedding().embed(current.input, purpose="passage")
        assert index.publish(work, input=current, embedding=embedding, now=RECORDED)
    else:
        assert published, "Unrelated extraction must not discard in-flight index work"
    assert work.ref.memory_id in publish_and_search(app_database_url)


def publish_and_search(url: str) -> set[str]:
    index = PostgresRetrievalStore(url)
    embedding = RecordingEmbedding()
    indexing = IndexWorker(index, embedding, space=SPACE, clock=lambda: RECORDED)
    for _ in range(3):
        for work in index.claim(space=SPACE, now=RECORDED, limit=32):
            indexing.process(work)
    query = LocalOnly(texts=("飲み物",), labels=(InputLabel("sensitive", "v1"),))
    return {
        candidate.memory.id
        for candidate in index.search(
            character_id="char-b",
            query=query,
            query_vector=embedding.embed(query, purpose="query"),
            now=RECORDED,
            limit=20,
        ).candidates
        if candidate.memory is not None
    }


@pytest.mark.parametrize("origin", ["generated", "baseline"])
@pytest.mark.parametrize("correction", ["source", "context", "context_failure", "invalid_target"])
def test_receipt_correction_recovers_only_valid_derives_target(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    correction: str,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    evidence.add_turn(turn(address_mode="direct", addressees=("user-a",)))
    evidence.add_receipt(receipt("full", len(TEXT)))
    worker = extraction_worker(app_database_url)
    gateway = RecordingGateway()
    worker.extractor.gateway = gateway
    if origin == "generated":
        worker.process("char-b", "turn-1")
    else:
        with worker.service.store.transaction("char-b") as tx:
            tx.add_memory(memory())
    context = correction.startswith("context")
    if context:
        evidence.add_turn(turn("question"))
        evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id="question"))
    evidence.add_turn(turn("turn-2", reply_to_turn_id="question" if context else None))
    evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id="turn-2"))
    gateway.output = ExtractionProposal(
        memories=(replace(memory("summary"), id=NEW_ID, source_turn_ids=("turn-2",)),),
        relations=(MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type="derives"),),
    )
    worker.process("char-b", "turn-2")
    assert publish_and_search(app_database_url) == {OLD_ID, NEW_ID}
    assert {v.id for v in worker.service.active("char-b")} == {OLD_ID, NEW_ID}
    with psycopg.connect(app_database_url) as connection:
        assert connection.execute(
            "SELECT turn_id FROM memory_input_dependencies "
            "WHERE character_id='char-b' AND extraction_turn_id='turn-2' ORDER BY turn_id"
        ).fetchall() == ([("question",), ("turn-2",)] if context else [("turn-2",)])
    if correction == "invalid_target":
        evidence.add_receipt(receipt("none", 0))
    corrected = "question" if context else "turn-2"
    evidence.add_receipt(replace(receipt("none", 0), turn_id=corrected))
    assert {v.id for v in worker.service.active("char-b")} == (
        set() if correction == "invalid_target" else {OLD_ID}
    )
    with psycopg.connect(app_database_url) as connection:
        assert connection.execute(
            "SELECT id FROM derived_memories WHERE NOT search_eligible ORDER BY id"
        ).fetchall() == [(OLD_ID,), (NEW_ID,)]
    # A's unchanged extraction signature must not be needed to repair its eligibility.
    if origin == "generated":
        worker.process("char-b", "turn-1")
    gateway.fail = correction == "context_failure"
    gateway.output = ()
    worker.process("char-b", "turn-2")
    state = worker.service.store.get_extraction("char-b", "turn-2")
    assert state is not None and state.status == ("retry" if gateway.fail else "done")
    assert publish_and_search(app_database_url) == (
        set() if correction == "invalid_target" else {OLD_ID}
    )
    assert {v.id for v in worker.service.active("char-b")} == (
        set() if correction == "invalid_target" else {OLD_ID}
    )


class LockMeasurements:
    """Measure real SQL results and time from gate acquisition through commit."""

    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.rows = 0
        self.queries = 0
        self.wait_seconds: list[float] = []
        self.hold_seconds: list[float] = []
        self.acquired: dict[int, float] = {}
        self.neighbor_queries: list[tuple[str, Params | None]] = []
        execute = cast(Callable[..., psycopg.Cursor[DictRow]], psycopg.Cursor.execute)
        commit = psycopg.Connection.commit

        def observed_execute(
            cursor: psycopg.Cursor[DictRow],
            query: Query,
            params: Params | None = None,
            *,
            prepare: bool | None = None,
            binary: bool | None = None,
        ) -> psycopg.Cursor[DictRow]:
            started = perf_counter()
            result = execute(cursor, query, params, prepare=prepare, binary=binary)
            completed = perf_counter()
            rendered = (
                query.as_string(cursor.connection) if isinstance(query, sql.Composable) else query
            )
            if isinstance(rendered, str) and "relations AS NOT MATERIALIZED" in rendered:
                self.neighbor_queries.append((rendered, params))
            if (
                isinstance(query, str)
                and "pg_advisory_xact_lock" in query
                and "recall-state" in query
            ):
                self.wait_seconds.append(completed - started)
                self.acquired[id(cursor.connection)] = completed
            elif self.acquired:
                self.queries += 1
                if cursor.description is not None:
                    self.rows += max(0, cursor.rowcount)
            return result

        def observed_commit(connection: psycopg.Connection[DictRow]) -> None:
            commit(connection)
            acquired = self.acquired.pop(id(connection), None)
            if acquired is not None:
                self.hold_seconds.append(perf_counter() - acquired)

        patch.setattr(psycopg.Cursor, "execute", observed_execute)
        patch.setattr(psycopg.Connection, "commit", observed_commit)


def test_fixed_change_does_not_read_all_unrelated_memories_under_recall_lock(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    worker = extraction_worker(app_database_url)
    measurements: dict[int, LockMeasurements] = {}
    seeded = 0
    for count in (20, 100, 300):
        for number in range(seeded, count):
            identifier = f"unrelated-{number}"
            evidence.add_turn(turn(identifier, actor="user-a"))
            with worker.service.store.transaction("char-b") as tx:
                tx.add_memory(
                    replace(
                        memory(),
                        id=identifier,
                        object=identifier,
                        source_turn_ids=(identifier,),
                    )
                )
        seeded = count
        identifier = f"measured-{count}"
        evidence.add_turn(turn(identifier, actor="user-a", text=identifier))
        with monkeypatch.context() as patch:
            measured = LockMeasurements(patch)
            worker.process("char-b", identifier)
        state = worker.service.store.get_extraction("char-b", identifier)
        assert state is not None and state.status == "done"
        assert measured.hold_seconds and not measured.acquired
        measurements[count] = measured
        record_property(
            str(count),
            {
                "rows_under_lock": measured.rows,
                "queries_under_lock": measured.queries,
                "gate_wait_seconds": measured.wait_seconds,
                "gate_hold_seconds": measured.hold_seconds,
            },
        )
    assert measurements[300].rows <= measurements[20].rows, {
        count: value.rows for count, value in measurements.items()
    }
    assert measurements[300].queries <= measurements[20].queries


def test_derives_from_stable_representative_does_not_expand_to_existing_referrers(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    store, gateway = PostgresMemoryStore(app_database_url), RecordingGateway()
    worker = extraction_worker(app_database_url)
    worker.extractor.gateway = gateway
    for number, identifier in enumerate(("a", "b"), 1):
        source = f"turn-{number}"
        evidence.add_turn(turn(source, actor="user-a"))
        gateway.output = (replace(memory(), id=identifier, source_turn_ids=(source,)),)
        worker.process("char-b", source)
        state = store.get_extraction("char-b", source)
        assert state is not None and state.status == "done"
    measurements: dict[int, LockMeasurements] = {}
    closures: dict[int, frozenset[str]] = {}
    seeded = 0
    for count in (1, 100):
        with store.transaction("char-b") as tx:
            for number in range(seeded, count):
                identifier = f"referrer-{number}"
                tx.add_memory(replace(memory(), id=identifier, object=identifier))
                tx.add_relation(MemoryRelation(from_id=identifier, to_id="a", type="derives"))
        seeded = count
        before_values = {
            f"referrer-{n}": store.get_memory("char-b", f"referrer-{n}") for n in range(count)
        }
        before_index = document_states(app_database_url)
        source = f"new-source-{count}"
        identifier = f"new-derived-{count}"
        evidence.add_turn(turn(source, actor="user-a"))
        gateway.output = ExtractionProposal(
            memories=(
                replace(memory(), id=identifier, object=identifier, source_turn_ids=(source,)),
            ),
            relations=(MemoryRelation(from_id=identifier, to_id="a", type="derives"),),
        )
        with monkeypatch.context() as patch:
            measured = LockMeasurements(patch)
            worker.process("char-b", source)
        state = store.get_extraction("char-b", source)
        assert state is not None and state.status == "done"
        measurements[count] = measured
        with store.transaction("char-b") as tx:
            closures[count] = worker.service.affected(tx, source)
        record_property(
            str(count),
            {
                "closure": sorted(closures[count]),
                "queries": measured.queries,
                "rows": measured.rows,
                "lock_seconds": measured.hold_seconds,
            },
        )
        assert {key: store.get_memory("char-b", key) for key in before_values} == before_values
        after_index = document_states(app_database_url)
        assert {key: after_index[key] for key in before_index} == before_index
    assert closures == {1: frozenset({"new-derived-1"}), 100: frozenset({"new-derived-100"})}
    assert measurements[100].queries <= measurements[1].queries
    assert measurements[100].rows <= measurements[1].rows
