"""Committed author deletion wins over a paused extraction in the same worker."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from itertools import count
from threading import Event

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import DictRow

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.api import create_app
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.memory.domain import DerivedMemory
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly, MemoryExtractor
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.ports import MemoryTransaction
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.memory.worker import MemoryWorker
from digital_souls_memory.recall.worker import IndexWorker
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import RECORDED, turn
from tests.memory_fakes import EchoGateway
from tests.memory_support import memory
from tests.postgres.test_deletion import HEADERS, seed_evidence
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_deletion_recalculation import make_worker
from tests.recall_fixtures import SPACE
from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("transaction_number", [1, 2], ids=["claim", "result"])
@pytest.mark.parametrize("delete_context", [False, True], ids=["primary", "reply-input"])
@pytest.mark.usefixtures("deletion_url")
def test_author_deletion_prevents_paused_extraction_from_restoring_source(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    transaction_number: int,
    delete_context: bool,
) -> None:
    evidence = seed_evidence(database_url)
    if delete_context:
        evidence.add_turn(turn("question", text="削除する返信先"))
    evidence.add_turn(turn(actor="user-a", reply_to_turn_id="question" if delete_context else None))
    store = PostgresMemoryStore(database_url)
    inputs = MemoryInputs(
        evidence,
        EvidenceMaterials(
            evidence,
            IdentityCharacters(lambda: PostgresIdentityStore(database_url)),
            lambda: RECORDED,
            timedelta(minutes=10),
        ),
        conversation_surfaces=frozenset({"synthetic-chat"}),
        comment_surfaces=frozenset(),
    )
    worker = MemoryWorker(
        MemoryService(store, inputs, lambda: RECORDED),
        MemoryExtractor(inputs=inputs, gateway=EchoGateway(), non_sensitive_labels=frozenset()),
    )
    waiting, release = Event(), Event()
    original = store.transaction
    calls = 0

    @contextmanager
    def pause_before_lock(character: str) -> Iterator[MemoryTransaction]:
        nonlocal calls
        calls += 1
        if calls == transaction_number:
            waiting.set()
            assert release.wait(5)
        with original(character) as tx:
            yield tx

    monkeypatch.setattr(store, "transaction", pause_before_lock)
    with TestClient(create_app(clock=lambda: RECORDED)) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(worker.process, "char-b", "turn-1")
            try:
                assert waiting.wait(3)
                response = client.post(
                    "/deletion-requests",
                    headers=HEADERS,
                    json={
                        "request_id": "delete-1",
                        "target_turn_ids": ["question" if delete_context else "turn-1"],
                    },
                )
                assert response.status_code == 200
            finally:
                release.set()
            pending.result(timeout=5)
        assert (
            client.get("/turns/question" if delete_context else "/turns/turn-1").status_code == 404
        )
    if not delete_context:
        assert store.get_extraction("char-b", "turn-1") is None
    elif transaction_number == 2:
        state = store.get_extraction("char-b", "turn-1")
        assert state is not None and state.status == "retry"
    if delete_context and transaction_number == 1:
        # The worker reads after deletion and may safely finish from the surviving reply.
        with store.list_memories("char-b") as values:
            assert all(v.source_turn_ids == ("turn-1",) for v in values)
        return
    with store.list_memories("char-b") as values:
        assert tuple(values) == ()
    with psycopg.connect(database_url, connect_timeout=5) as conn:
        for table in ("memory_candidates", "memory_generated", "memory_input_dependencies"):
            assert conn.execute("SELECT count(*) FROM " + table).fetchone() == (0,)
    if delete_context:
        worker.process("char-b", "turn-1")
        with store.list_memories("char-b") as values:
            assert tuple(v.source_turn_ids for v in values) == (("turn-1",),)


@pytest.mark.parametrize(
    "kind,delete_context", [("turn", False), ("resident", False), ("turn", True)]
)
@pytest.mark.usefixtures("deletion_url")
def test_provision_save_rejects_a_snapshot_loaded_before_author_deletion(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    delete_context: bool,
) -> None:
    from tests.it1.test_recall import request

    evidence = seed_evidence(database_url)
    if delete_context:
        evidence.add_turn(turn("question", text="削除する返信先"))
    evidence.add_turn(turn(reply_to_turn_id="question" if delete_context else None))
    memories = PostgresMemoryStore(database_url)
    if delete_context:

        class ReplyOnly(EchoGateway):
            def _extract(self, value: LocalOnly | CloudInput) -> tuple[DerivedMemory, ...]:
                return (
                    replace(
                        memory("summary"),
                        content="返信と削除する返信先",
                        source_turn_ids=("turn-1",),
                    ),
                )

        make_worker(database_url, memories, ReplyOnly()).process("char-b", "turn-1")
    else:
        with memories.transaction("char-b") as tx:
            tx.add_memory(memory())
    index = PostgresRetrievalStore(database_url)
    for work in index.claim(space=SPACE, now=RECORDED, limit=10):
        IndexWorker(index, RecordingEmbedding(), space=SPACE, clock=lambda: RECORDED).process(work)
    provisions = PostgresProvisionStore(database_url)
    connection = provisions._connection
    waiting, release = Event(), Event()
    identifiers = count()

    @contextmanager
    def pause_before_save_lock() -> Iterator[psycopg.Connection[DictRow]]:
        with connection() as conn:
            waiting.set()
            assert release.wait(5)
            yield conn

    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            retriever=index,
            resident_candidates=index,
            embedding=RecordingEmbedding(),
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
            provision_store=provisions,
            provision_id_factory=lambda: f"provided-{next(identifiers)}",
            recalculation_gateway=RecordingRecalculationGateway(None),
        )
    ) as client:
        if kind == "resident":
            assert client.put(
                "/characters/char-b/profile",
                json={
                    "token_limit": 10000,
                    "entries": [
                        {
                            "entry_id": "original",
                            "kind": "turn",
                            "turn_id": "turn-1",
                            "start": 0,
                            "end": 3,
                        }
                    ],
                },
            ).is_success
        path = "/characters/char-b/recall" if kind == "turn" else "/characters/char-b/resident"
        payload = request() if kind == "turn" else {"session_id": "session-1"}
        assert client.post(path, json=payload).status_code == 200
        previous = provisions.get("provided-0")
        assert previous is not None
        if delete_context:
            assert previous.memory_ids == (memory().id,)
            assert all(s.turn_id != "question" for s in previous.source_spans)
        monkeypatch.setattr(provisions, "_connection", pause_before_save_lock)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                client.post,
                path,
                json=payload,
            )
            try:
                assert waiting.wait(3)
                assert (
                    client.post(
                        "/deletion-requests",
                        headers=HEADERS,
                        json={
                            "request_id": "delete-provision",
                            "target_turn_ids": ["question" if delete_context else "turn-1"],
                        },
                    ).status_code
                    == 200
                )
            finally:
                release.set()
            response = pending.result(timeout=5)
        monkeypatch.setattr(provisions, "_connection", connection)
        assert response.status_code == 503
        assert provisions.get("provided-0") is None
        assert provisions.get("provided-1") is None
        assert (
            client.get("/turns/question" if delete_context else "/turns/turn-1").status_code == 404
        )
