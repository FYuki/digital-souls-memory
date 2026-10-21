from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Event
from time import monotonic
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from tests.evidence_support import RECORDED, TEXT, receipt, session, turn
from tests.it1.identity_fakes import FakeReviewer
from tests.it1.test_recall import request

if TYPE_CHECKING:
    from digital_souls_memory.recall.domain import RerankInput, RerankScore

pytestmark = pytest.mark.postgres


def eventually(predicate: Callable[[], bool]) -> None:
    deadline = monotonic() + 5
    wait = Event()
    while monotonic() < deadline:
        if predicate():
            return
        wait.wait(0.01)
    assert predicate()


@pytest.mark.parametrize(
    "actor,kind,perceived,mode,targets,visible",
    [
        ("user-a", "utterance", (), "broadcast", (), False),
        ("user-a", "utterance", ("char-b",), "broadcast", (), True),
        ("char-b", "utterance", ("char-b",), "direct", ("user-a",), True),
        ("char-c", "utterance", ("char-b", "char-c"), "direct", ("char-b",), True),
        ("char-c", "utterance", ("char-c",), "direct", ("user-a",), False),
        ("char-c", "utterance", ("char-c",), "direct", ("char-b",), False),
        ("char-b", "thought", ("char-b",), "self", (), True),
        ("char-c", "thought", ("char-c",), "self", (), False),
        ("char-c", "utterance", ("char-b", "char-c"), "self", (), True),
    ],
    ids=[
        "empty-perception",
        "perceived-user",
        "own-whisper",
        "character-whisper",
        "user-whisper",
        "addressee-without-perception",
        "own-thought",
        "other-thought",
        "spoken-self",
    ],
)
def test_database_recall_filters_perception_without_supplementing_from_addressees(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    actor: str,
    kind: str,
    perceived: tuple[str, ...],
    mode: str,
    targets: tuple[str, ...],
    visible: bool,
) -> None:
    from tests.memory_support import NEW_ID, OLD_ID, memory
    from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    with TestClient(create_app()) as registration:
        for identifier in ("char-b", "char-c"):
            assert registration.post(
                f"/characters/{identifier}/core-identities",
                json={"values": ["誠実"], "prohibitions": []},
            ).is_success
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(
        turn(actor=actor, kind=kind, perceived_by=perceived, address_mode=mode, addressees=targets)
    )
    evidence.add_turn(turn(turn_id="control", actor="char-b", perceived_by=("char-b",)))
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        if visible:
            tx.add_memory(replace(memory(), content=TEXT))
        tx.add_memory(replace(memory(), id=NEW_ID, source_turn_ids=("control",), content=TEXT))
    if not visible and perceived:
        with PostgresMemoryStore(app_database_url).transaction(perceived[0]) as tx:
            tx.add_memory(
                replace(memory(), character_id=perceived[0], shared_with=("char-b",), content=TEXT)
            )
    with TestClient(
        create_app(
            embedding=RecordingEmbedding(),
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
            clock=lambda: RECORDED,
        )
    ) as client:

        def indexed() -> bool:
            response = client.post("/characters/char-b/recall", json=request())
            assert response.is_success
            return NEW_ID in {v["id"] for v in response.json()["memories"]}

        eventually(indexed)
        response = client.post("/characters/char-b/recall", json=request())
        assert response.is_success
        assert {v["id"] for v in response.json()["memories"]} == (
            {OLD_ID, NEW_ID} if visible else {NEW_ID}
        )
        assert {v["turn_id"] for v in response.json()["source_spans"]} == (
            {"turn-1", "control"} if visible else {"control"}
        )
        if not visible:
            origin = client.post(
                "/characters/char-b/recall",
                json={
                    "session_id": "session-1",
                    "format": "materials",
                    "turn_id": "turn-1",
                },
            )
            assert origin.status_code == 404


@pytest.mark.parametrize("field", ["valid_to", "superseded_at"])
def test_projection_expiry_withdraws_a_previously_searchable_document_in_the_same_app(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    from tests.memory_support import memory
    from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    with TestClient(create_app()) as registration:
        for identifier in ("char-b", "char-c"):
            assert registration.post(
                f"/characters/{identifier}/core-identities",
                json={"values": ["誠実"], "prohibitions": []},
            ).is_success
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn())
    store = PostgresMemoryStore(app_database_url)
    original = replace(memory(), content=TEXT)
    with store.transaction("char-b") as tx:
        tx.add_memory(original)
    with TestClient(
        create_app(
            embedding=RecordingEmbedding(),
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
            clock=lambda: RECORDED,
        )
    ) as client:

        def indexed() -> bool:
            response = client.post("/characters/char-b/recall", json=request())
            assert response.is_success
            return [v["id"] for v in response.json()["memories"]] == [original.id]

        eventually(indexed)
        with store.transaction("char-b") as tx:
            expired = (
                replace(original, valid_to=RECORDED)
                if field == "valid_to"
                else replace(original, superseded_at=RECORDED)
            )
            tx.replace_memory(expired)
        response = client.post("/characters/char-b/recall", json=request())
        assert response.is_success
        assert response.json()["memories"] == response.json()["source_spans"] == []


def test_receipt_committed_after_save_transaction_starts_rejects_the_old_snapshot(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
    from tests.memory_support import memory
    from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    with TestClient(create_app()) as registration:
        for identifier in ("char-b", "char-c"):
            assert registration.post(
                f"/characters/{identifier}/core-identities",
                json={"values": ["誠実"], "prohibitions": []},
            ).is_success
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    evidence.add_turn(turn())
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        tx.add_memory(replace(memory(), subject="char-c", content=TEXT))
    provisions = PostgresProvisionStore(app_database_url)
    connection = provisions._connection
    entered, release = Event(), Event()
    armed = False
    next_id: str | None = None

    @contextmanager
    def pause_before_validation() -> Iterator[psycopg.Connection[DictRow]]:
        nonlocal armed
        with connection() as conn:
            if armed:
                armed = False
                conn.execute("SELECT 1")
                assert conn.info.transaction_status == TransactionStatus.INTRANS
                entered.set()
                assert release.wait(5), "Receipt writer did not release the save transaction"
            yield conn

    monkeypatch.setattr(provisions, "_connection", pause_before_validation)
    with TestClient(
        create_app(
            embedding=RecordingEmbedding(),
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
            provision_store=provisions,
            clock=lambda: RECORDED + timedelta(minutes=10),
            provision_id_factory=lambda: next_id if next_id is not None else uuid4().hex,
        ),
        raise_server_exceptions=False,
    ) as client:

        def indexed() -> bool:
            response = client.post("/characters/char-b/recall", json=request())
            assert response.is_success
            return bool(response.json()["memories"])

        eventually(indexed)
        armed = True
        next_id = "save-after-receipt"
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, "/characters/char-b/recall", json=request())
            try:
                assert entered.wait(5), "Recall did not start the provision transaction"
                evidence.add_receipt(receipt("none", 0))
            finally:
                release.set()
            response = pending.result(timeout=5)
        assert response.status_code == 503
        assert provisions.get("save-after-receipt") is None
        next_id = "save-after-correction"
        following = client.post("/characters/char-b/recall", json=request())
        assert following.is_success
        assert following.json()["memories"] == following.json()["source_spans"] == []
        assert provisions.get("save-after-correction") is not None


def test_real_commit_failure_rolls_back_insert_and_allows_a_later_success(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
    from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    with TestClient(create_app()) as registration:
        assert registration.post(
            "/characters/char-b/core-identities",
            json={"values": ["誠実"], "prohibitions": []},
        ).is_success
    PostgresEvidenceStore(app_database_url).add_session(session(reports_delivery=False))
    provisions = PostgresProvisionStore(app_database_url)
    connection = provisions._connection
    observed_insert = Event()
    armed = True

    @contextmanager
    def fail_commit_once() -> Iterator[psycopg.Connection[DictRow]]:
        nonlocal armed
        with connection() as conn:
            if armed:
                armed = False
                commit = conn.commit

                def fail_commit() -> None:
                    assert conn.info.transaction_status == TransactionStatus.INTRANS
                    # Observe the uncommitted INSERT on its actual connection.
                    row = conn.execute(
                        "SELECT provision_id FROM provision_records WHERE provision_id=%s",
                        ("commit-retry",),
                    ).fetchone()
                    assert row is not None
                    observed_insert.set()
                    conn.close()
                    commit()

                monkeypatch.setattr(conn, "commit", fail_commit)
            yield conn

    monkeypatch.setattr(provisions, "_connection", fail_commit_once)
    with TestClient(
        create_app(
            embedding=RecordingEmbedding(),
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
            provision_store=provisions,
            clock=lambda: RECORDED,
            provision_id_factory=lambda: "commit-retry",
        ),
        raise_server_exceptions=False,
    ) as client:
        failed = client.post("/characters/char-b/recall", json=request())
        assert observed_insert.is_set(), "The injected failure did not follow the real INSERT"
        assert failed.status_code == 503
        assert provisions.get("commit-retry") is None
        following = client.post("/characters/char-b/recall", json=request())
        assert following.is_success
        saved = provisions.get("commit-retry")
        assert saved is not None
        assert saved.memory_ids == saved.verbatim_anchor_ids == ()
        assert saved.source_spans == ()
        assert following.json()["provision_id"] == saved.provision_id


def test_persistent_index_survives_app_restart_without_reembedding_passages_or_full_scans(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from digital_souls_memory.memory.use_cases import MemoryService
    from tests.memory_support import memory
    from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    embedding = RecordingEmbedding()
    reranker = RecordingReranker()
    counter = RecordingTokenCounter()
    with TestClient(create_app(identity_store=PostgresIdentityStore(app_database_url))) as client:
        for identifier in ("char-a", "char-b", "char-c"):
            assert client.post(
                f"/characters/{identifier}/core-identities",
                json={"values": ["誠実"], "prohibitions": []},
            ).is_success
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn())
    store = PostgresMemoryStore(app_database_url)
    value = replace(memory(), subject="char-c")
    with store.transaction("char-b") as tx:
        tx.add_memory(value)

    def forbidden_scan(*args: object, **kwargs: object) -> None:
        raise AssertionError("Recall must query the persistent index")

    monkeypatch.setattr(MemoryService, "active", forbidden_scan)
    monkeypatch.setattr(MemoryService, "rebuild", forbidden_scan)
    with TestClient(
        create_app(
            embedding=embedding, reranker=reranker, token_counter=counter, clock=lambda: RECORDED
        )
    ) as client:

        def indexed() -> bool:
            response = client.post("/characters/char-b/recall", json=request())
            assert response.is_success
            return bool(response.json()["memories"])

        eventually(indexed)
        first = client.post("/characters/char-b/recall", json=request()).json()
    passage_calls = [v for v in embedding.calls if v[1] == "passage"]
    assert passage_calls
    with TestClient(
        create_app(
            embedding=embedding, reranker=reranker, token_counter=counter, clock=lambda: RECORDED
        )
    ) as restarted:
        response = restarted.post("/characters/char-b/recall", json=request())
        assert response.is_success
        assert [v["id"] for v in response.json()["memories"]] == [value.id]
        assert response.json()["source_spans"] == first["source_spans"]
        assert [v for v in embedding.calls if v[1] == "passage"] == passage_calls
        from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore

        record = PostgresProvisionStore(app_database_url).get(response.json()["provision_id"])
        assert record is not None
        assert record.memory_ids == (value.id,)
        assert [(s.turn_id, s.start, s.end, s.text) for s in record.source_spans] == [
            (s["turn_id"], s["start"], s["end"], s["text"]) for s in response.json()["source_spans"]
        ]
        assert record.temperament_version == response.json()["temperament_version"]
        assert record.personality_version == response.json()["personality_version"]
        (score,) = record.scores
        assert (score.ref.owner_id, score.ref.memory_id, score.ref.anchor_id) == (
            "char-b",
            value.id,
            None,
        )
        expected_recency = 2 ** (-25 / (30 * 24))
        expected_trust = ((0.8 + 0.7 + 0.9 + 0.6 + 0.9 + 0.95) / 6 + 0.5) / 2
        assert score.semantic == pytest.approx(1)
        assert score.lexical == pytest.approx(0)
        assert score.recency == pytest.approx(expected_recency)
        assert score.importance == pytest.approx(0.5)
        assert score.self_relevance == pytest.approx(0.4)
        assert score.trust == pytest.approx(expected_trust)
        assert score.composite == pytest.approx(
            0.55 + 0.05 * (expected_recency + 0.5 + 0.4 + expected_trust)
        )
        assert score.relevance == 1
        assert record.query == "日本語" and record.turn_id is None
        assert record.voice_exemplar_ids == record.motivation_ids == ()
        assert record.duration_ms >= 0 and record.provided_at == RECORDED
        denied = restarted.post("/characters/char-a/recall", json=request())
        assert denied.is_success
        assert denied.json()["memories"] == denied.json()["source_spans"] == []
    assert store.get_memory("char-b", value.id) == value


def test_receipt_write_invalidates_an_already_returned_index_candidate_before_reextraction(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.memory_support import memory
    from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    now = RECORDED + timedelta(minutes=10)
    with TestClient(create_app(identity_store=PostgresIdentityStore(app_database_url))) as client:
        for identifier in ("char-b", "char-c"):
            assert client.post(
                f"/characters/{identifier}/core-identities",
                json={"values": ["誠実"], "prohibitions": []},
            ).is_success
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    evidence.add_turn(turn())
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        tx.add_memory(replace(memory(), subject="char-c", content=TEXT))
    with TestClient(
        create_app(
            embedding=RecordingEmbedding(),
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
            clock=lambda: now,
        )
    ) as client:

        def indexed() -> bool:
            response = client.post("/characters/char-b/recall", json=request())
            assert response.is_success
            return bool(response.json()["source_spans"])

        eventually(indexed)
        before = client.post("/characters/char-b/recall", json=request()).json()
        assert [s["text"] for s in before["source_spans"]] == [TEXT]
        evidence.add_receipt(receipt("partial", 9))
        corrected = client.post("/characters/char-b/recall", json=request())
        assert corrected.is_success
        # No extraction gateway is configured: the old key must remain withdrawn.
        assert corrected.json()["memories"] == corrected.json()["source_spans"] == []
        evidence.add_receipt(receipt("none", 0))
        absent = client.post("/characters/char-b/recall", json=request())
        assert absent.is_success
        assert absent.json()["memories"] == absent.json()["source_spans"] == []


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_anchor_registration_is_searchable_without_any_derived_memory_and_tracks_receipts(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
    from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter
    from tests.voice_support import anchor_payload

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    with TestClient(create_app()) as registration:
        for identifier in ("char-b", "char-c"):
            assert registration.post(
                f"/characters/{identifier}/core-identities",
                json={"values": ["誠実"], "prohibitions": []},
            ).is_success
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    evidence.add_turn(turn())
    now = RECORDED + timedelta(minutes=10)
    with TestClient(
        create_app(
            embedding=RecordingEmbedding(),
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
            clock=lambda: now,
        )
    ) as client:
        assert client.post("/characters/char-b/verbatim-anchors", json=anchor_payload()).is_success

        def searchable() -> bool:
            response = client.post("/characters/char-b/recall", json=request(form=form))
            assert response.is_success
            return bool(response.json()["verbatim_anchors"])

        eventually(searchable)
        before = client.post("/characters/char-b/recall", json=request(form=form)).json()
        assert before["memories"] == []
        assert [(a["anchor_id"], a["turn_id"], a["text"]) for a in before["verbatim_anchors"]] == [
            ("anchor-1", "turn-1", TEXT)
        ]
        if form == "assembled":
            assert TEXT in before["context"]
        record = PostgresProvisionStore(app_database_url).get(before["provision_id"])
        assert record is not None
        assert record.memory_ids == ()
        assert record.verbatim_anchor_ids == ("anchor-1",)
        evidence.add_receipt(receipt("none", 0))
        after = client.post("/characters/char-b/recall", json=request(form=form))
        assert after.is_success
        assert after.json()["verbatim_anchors"] == after.json()["source_spans"] == []
        after_record = PostgresProvisionStore(app_database_url).get(after.json()["provision_id"])
        assert after_record is not None
        assert after_record.verbatim_anchor_ids == ()
        assert after_record.source_spans == ()
        assert PostgresProvisionStore(app_database_url).get(before["provision_id"]) == record


def test_receipt_committed_while_reranking_prevents_stale_provision_in_real_database(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
    from tests.memory_support import memory
    from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    with TestClient(create_app()) as registration:
        for identifier in ("char-b", "char-c"):
            assert registration.post(
                f"/characters/{identifier}/core-identities",
                json={"values": ["誠実"], "prohibitions": []},
            ).is_success
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session())
    evidence.add_turn(turn())
    with PostgresMemoryStore(app_database_url).transaction("char-b") as tx:
        tx.add_memory(replace(memory(), subject="char-c", content=TEXT))
    entered, release = Event(), Event()
    next_id: str | None = None

    class PausingReranker(RecordingReranker):
        pause = False

        def rerank(self, value: "RerankInput") -> tuple["RerankScore", ...]:
            if self.pause:
                entered.set()
                assert release.wait(5), "Receipt writer did not release the reranker"
            return super().rerank(value)

    reranker = PausingReranker()
    with TestClient(
        create_app(
            embedding=RecordingEmbedding(),
            reranker=reranker,
            token_counter=RecordingTokenCounter(),
            clock=lambda: RECORDED + timedelta(minutes=10),
            provision_id_factory=lambda: next_id if next_id is not None else uuid4().hex,
        ),
        raise_server_exceptions=False,
    ) as client:
        for identifier in ("char-b", "char-c"):
            assert client.post(
                f"/characters/{identifier}/core-identities",
                json={"values": ["誠実"], "prohibitions": []},
            ).is_success

        def searchable() -> bool:
            response = client.post("/characters/char-b/recall", json=request())
            assert response.is_success
            return bool(response.json()["memories"])

        eventually(searchable)
        reranker.pause = True
        next_id = "provision-in-flight"
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, "/characters/char-b/recall", json=request())
            try:
                assert entered.wait(5), "Recall did not reach reranking"
                evidence.add_receipt(receipt("none", 0))
            finally:
                release.set()
            response = pending.result(timeout=5)
        assert response.status_code == 503
        assert PostgresProvisionStore(app_database_url).get("provision-in-flight") is None


@pytest.mark.parametrize("registered", [False, True], ids=["null", "initial-versions"])
def test_concurrent_session_selection_is_restored_after_restart_and_keeps_null_selection(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    registered: bool,
) -> None:
    from digital_souls_memory.identity.domain import PersonalityProfile, Temperament
    from tests.evidence_support import STARTED
    from tests.identity_support import aspects
    from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter

    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    now = STARTED

    def app() -> TestClient:
        return TestClient(
            create_app(
                embedding=RecordingEmbedding(),
                reranker=RecordingReranker(),
                token_counter=RecordingTokenCounter(),
                reviewer=FakeReviewer(),
                clock=lambda: now,
            )
        )

    with app() as first, app() as second:
        assert first.post(
            "/characters/char-b/core-identities", json={"values": ["誠実"], "prohibitions": []}
        ).is_success
        if registered:
            with PostgresIdentityStore(app_database_url).transaction(
                "char-b", source="registration"
            ) as tx:
                tx.add_temperament(Temperament("char-b", "開始時の気質", 1, STARTED))
                tx.add_profile(
                    PersonalityProfile(
                        character_id="char-b",
                        aspects=aspects(),
                        description="開始時の人格",
                        renderer_version="synthetic",
                        temperament_version=1,
                        version=1,
                        valid_from=STARTED,
                        valid_to=None,
                        evidence_belief_ids=(),
                        change_kind="author_seed",
                    )
                )
        start = Barrier(2)

        def recall(client: TestClient) -> dict[str, object]:
            start.wait(timeout=5)
            response = client.post("/characters/char-b/recall", json=request())
            assert response.is_success
            return cast(dict[str, object], response.json())

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(recall, client) for client in (first, second)]
            selected = [future.result(timeout=10) for future in futures]
        version = 1 if registered else None
        assert [body["temperament_version"] for body in selected] == [version, version]
        assert [body["personality_version"] for body in selected] == [version, version]
        assert first.post(
            "/characters/char-b/temperaments", json={"description": "同じ開始時刻に登録"}
        ).is_success
        current = first.get("/characters/char-b/identity/current")
        assert current.is_success
        assert current.json()["temperament"]["version"] == (2 if registered else 1)
        same = second.post("/characters/char-b/recall", json=request())
        assert same.is_success
        assert same.json()["temperament_version"] == version
        assert same.json()["personality_version"] == version
    with app() as restarted:
        restored = restarted.post("/characters/char-b/recall", json=request())
        assert restored.is_success
        assert restored.json()["temperament_version"] == version
        assert restored.json()["personality_version"] == version
        evidence.add_session(
            replace(session("next-session", reports_delivery=False), started_at=now)
        )
        following = restarted.post(
            "/characters/char-b/recall", json=request() | {"session_id": "next-session"}
        )
        assert following.is_success
        assert following.json()["temperament_version"] == (2 if registered else 1)
        assert following.json()["personality_version"] == version
