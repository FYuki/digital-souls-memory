"""Synthetic regressions for evidence, voice and author deletion contracts."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Event

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from digital_souls_memory.adapters.postgres_deletion import PostgresDeletionStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.adapters.postgres_voice import PostgresVoiceStore
from digital_souls_memory.api import create_app
from digital_souls_memory.evidence.provision import CandidateRef
from digital_souls_memory.governance.domain import AuthorPrincipal, DeletionCommand
from digital_souls_memory.memory.domain import DerivedMemory, ExtractionProposal, MemoryRelation
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from digital_souls_memory.memory.ports import MemoryConflict
from digital_souls_memory.recall.worker import IndexWorker
from digital_souls_memory.voice.ports import VoiceTransaction
from tests.context_gateway_support import eventually
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import (
    OCCURRED,
    RECORDED,
    receipt_payload,
    session_payload,
    turn,
    turn_payload,
)
from tests.it1.test_recall import request
from tests.it1.test_voice import register_identity, register_turn
from tests.it1.voice_fakes import VoiceGateway, VoiceReviewer
from tests.memory_support import NEW_ID, memory
from tests.postgres.test_deletion import HEADERS, seed_evidence
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_deletion_recalculation import make_worker
from tests.recall_fixtures import SPACE
from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter
from tests.voice_support import ANCHORS, EXEMPLARS, TEXT, anchor_payload, exemplar_payload

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


@pytest.mark.parametrize("expired", [False, True])
def test_deletion_commit_checks_current_time_at_lease_boundary(
    database_url: str, expired: bool
) -> None:
    evidence = seed_evidence(database_url)
    original = turn()
    evidence.add_turn(original)
    store = PostgresDeletionStore(os.environ["MEMORY_DELETION_DATABASE_URL"])
    attempt = store.begin(
        DeletionCommand(request_id="lease-boundary", target_turn_ids=(original.turn_id,)),
        principal=AuthorPrincipal(author_id="synthetic-author"),
        now=RECORDED,
    )
    now = RECORDED + timedelta(minutes=5)
    if expired:
        with pytest.raises(MemoryConflict):
            store.commit(attempt, replacements=(), now=now)
        assert store.get("lease-boundary") == attempt.request
        assert evidence.get_turn(original.turn_id) == original
    else:
        done = store.commit(attempt, replacements=(), now=now - timedelta(microseconds=1))
        assert done.status == "done"
        assert done.completed_at == now - timedelta(microseconds=1)
        assert evidence.get_turn(original.turn_id) is None


def test_deletion_commit_rejects_only_stale_token_on_current_snapshot(
    database_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    original = turn()
    evidence.add_turn(original)
    store = PostgresDeletionStore(os.environ["MEMORY_DELETION_DATABASE_URL"])
    command = DeletionCommand(request_id="stale-token", target_turn_ids=(original.turn_id,))
    principal = AuthorPrincipal(author_id="synthetic-author")
    first = store.begin(command, principal=principal, now=RECORDED)
    store.fail(first, error="dependency_unavailable", now=RECORDED)
    current = store.begin(command, principal=principal, now=RECORDED)
    assert current.token != first.token
    stale = replace(current, token=first.token)
    with pytest.raises(MemoryConflict):
        store.commit(stale, replacements=(), now=RECORDED)
    store.fail(stale, error="snapshot_conflict", now=RECORDED)
    assert store.get(command.request_id) == current.request
    assert evidence.get_turn(original.turn_id) == original
    assert store.commit(current, replacements=(), now=RECORDED).status == "done"
    assert evidence.get_turn(original.turn_id) is None


def test_failed_turn_and_addressee_reference_do_not_reserve_user_actor_kind(
    database_url: str,
) -> None:
    seed_evidence(database_url)
    with TestClient(create_app(clock=lambda: RECORDED)) as client:
        body = turn_payload() | {
            "actor": "failed-actor",
            "perceived_by": ["char-b"],
            "session_id": "missing",
        }
        assert client.post("/turns", json=body).status_code == 404
        assert (
            client.post(
                "/characters/failed-actor/core-identities",
                json={"values": ["合成価値"], "prohibitions": []},
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/turns",
                json=body
                | {
                    "actor": "user-new",
                    "session_id": "session-1",
                    "address_mode": "direct",
                    "addressees": ["only-addressee"],
                },
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/characters/only-addressee/temperaments",
                json={"description": "合成気質"},
            ).status_code
            == 201
        )


@pytest.mark.parametrize(
    "resource,payload,table",
    [
        ("core-identities", {"values": ["合成価値"], "prohibitions": []}, "core_identities"),
        ("temperaments", {"description": "合成気質"}, "temperaments"),
    ],
)
def test_failed_identity_commit_does_not_reserve_character_actor_kind(
    database_url: str,
    resource: str,
    payload: dict[str, object],
    table: str,
) -> None:
    seed_evidence(database_url)
    with psycopg.connect(database_url, autocommit=True) as owner:
        owner.execute(
            "CREATE FUNCTION synthetic_identity_failure() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'synthetic commit failure'; END $$"
        )
        owner.execute(
            sql.SQL(
                "CREATE CONSTRAINT TRIGGER synthetic_commit AFTER INSERT ON {} "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION synthetic_identity_failure()"
            ).format(sql.Identifier(table))
        )
        try:
            with TestClient(create_app(clock=lambda: RECORDED)) as client:
                assert (
                    client.post(f"/characters/user-new/{resource}", json=payload).status_code == 503
                )
        finally:
            owner.execute(
                sql.SQL("DROP TRIGGER synthetic_commit ON {}").format(sql.Identifier(table))
            )
            owner.execute("DROP FUNCTION synthetic_identity_failure()")
    with TestClient(create_app(clock=lambda: RECORDED)) as client:
        body = turn_payload() | {"actor": "user-new", "perceived_by": ["char-b"]}
        del body["address_mode"]
        response = client.post("/turns", json=body)
        assert response.status_code == 201
        assert response.json()["address_mode"] == "broadcast"


@pytest.mark.parametrize(
    "resource,payload,table",
    [
        ("core-identities", {"values": ["合成価値"], "prohibitions": []}, "core_identities"),
        ("temperaments", {"description": "合成気質"}, "temperaments"),
    ],
)
@pytest.mark.parametrize("first", ["turn", "identity"])
def test_actor_first_registration_is_serialized_with_turn_insert(
    database_url: str, resource: str, payload: dict[str, object], table: str, first: str
) -> None:
    seed_evidence(database_url)
    target = "turns" if first == "turn" else table
    with psycopg.connect(database_url, autocommit=True) as owner:
        owner.execute(
            "CREATE FUNCTION synthetic_registration_gate() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN PERFORM pg_advisory_xact_lock(hashtextextended("
            "'synthetic-registration-gate',0)); RETURN NEW; END $$"
        )
        owner.execute(
            sql.SQL(
                "CREATE TRIGGER synthetic_gate BEFORE INSERT ON {} "
                "FOR EACH ROW EXECUTE FUNCTION synthetic_registration_gate()"
            ).format(sql.Identifier(target))
        )
        try:
            with TestClient(create_app(clock=lambda: RECORDED)) as client:

                def insert_turn() -> int:
                    body = turn_payload() | {"actor": "user-new", "perceived_by": ["char-b"]}
                    del body["address_mode"]
                    return client.post("/turns", json=body).status_code

                def insert_identity() -> int:
                    return client.post(f"/characters/user-new/{resource}", json=payload).status_code

                def waiting(count: int) -> bool:
                    row = owner.execute(
                        "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted"
                    ).fetchone()
                    return row is not None and row[0] >= count

                with ThreadPoolExecutor(max_workers=2) as pool:
                    with psycopg.connect(database_url, autocommit=True) as gate:
                        gate.execute(
                            "SELECT pg_advisory_lock(hashtextextended("
                            "'synthetic-registration-gate',0))"
                        )
                        try:
                            earlier = pool.submit(
                                insert_turn if first == "turn" else insert_identity
                            )
                            eventually(lambda: waiting(1))
                            later = pool.submit(insert_identity if first == "turn" else insert_turn)
                            eventually(lambda: later.done() or waiting(2))
                        finally:
                            gate.execute(
                                "SELECT pg_advisory_unlock(hashtextextended("
                                "'synthetic-registration-gate',0))"
                            )
                    assert earlier.result(timeout=10) == 201
                    assert 400 <= later.result(timeout=10) < 500
                source = client.get("/turns/turn-1")
                assert source.status_code == (200 if first == "turn" else 404)
        finally:
            owner.execute(
                sql.SQL("DROP TRIGGER synthetic_gate ON {}").format(sql.Identifier(target))
            )
            owner.execute("DROP FUNCTION synthetic_registration_gate()")


@pytest.mark.parametrize("material", ["anchor", "exemplar"])
def test_voice_registration_rechecks_receipt_committed_before_save(
    database_url: str, monkeypatch: pytest.MonkeyPatch, material: str
) -> None:
    voice = PostgresVoiceStore(database_url)
    entered, release = Event(), Event()
    transaction = voice.transaction

    @contextmanager
    def pause(character_id: str) -> Iterator[VoiceTransaction]:
        entered.set()
        assert release.wait(10), "Receipt writer did not release registration"
        with transaction(character_id) as tx:
            yield tx

    with TestClient(
        create_app(
            voice_store=voice,
            llm_gateway=VoiceGateway(),
            reviewer=VoiceReviewer(),
            clock=lambda: RECORDED,
        )
    ) as client:
        register_identity(client)
        register_identity(client, "char-b")
        register_turn(client, delivery=True)
        assert (
            client.post(
                "/turns/turn-1/receipts", json=receipt_payload("full", len(TEXT))
            ).status_code
            == 201
        )
        monkeypatch.setattr(voice, "transaction", pause)
        path, payload = (
            (ANCHORS, anchor_payload())
            if material == "anchor"
            else (
                EXEMPLARS,
                {
                    "exemplars": [
                        exemplar_payload()
                        | {
                            "source": "turn_id",
                            "turn_id": "turn-1",
                        }
                    ]
                },
            )
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, path, json=payload)
            try:
                assert entered.wait(5), "Registration did not reach its save boundary"
                assert (
                    client.post(
                        "/turns/turn-1/receipts", json=receipt_payload("none", 0)
                    ).status_code
                    == 201
                )
            finally:
                release.set()
            response = pending.result(timeout=10)
        if material == "anchor":
            assert response.status_code == 201
            assert response.json()["text"] == ""
            assert client.get(ANCHORS + "/anchor-1").json()["text"] == ""
        else:
            assert response.status_code == 422
            assert voice.get_exemplar("char-a", "voice-1") is None
            with psycopg.connect(database_url) as conn:
                assert conn.execute("SELECT count(*) FROM voice_initial_checks").fetchone() == (0,)
            # A failed first set must not prevent a later valid initial registration.
            assert (
                client.post(
                    "/turns/turn-1/receipts", json=receipt_payload("full", len(TEXT))
                ).status_code
                == 201
            )
            assert client.post(path, json=payload).status_code == 201
            assert client.get(EXEMPLARS + "/voice-1").json()["text"] == TEXT


@pytest.mark.parametrize(
    "resource,payload",
    [
        ("core-identities", {"values": ["合成価値"], "prohibitions": []}),
        ("temperaments", {"description": "合成気質"}),
    ],
)
@pytest.mark.parametrize("delete_turn", [False, True])
def test_user_actor_cannot_become_character_after_restart_or_source_deletion(
    database_url: str, resource: str, payload: dict[str, object], delete_turn: bool
) -> None:
    from digital_souls_memory.adapters.identity_characters import IdentityCharacters
    from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
    from digital_souls_memory.evidence.materials import EvidenceMaterials

    evidence = seed_evidence(database_url)
    with TestClient(create_app(clock=lambda: RECORDED)) as client:
        assert (
            client.post(
                "/sessions", json=session_payload() | {"session_id": "reported-session"}
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/turns",
                json=turn_payload()
                | {
                    "actor": "user-new",
                    "session_id": "reported-session",
                    "perceived_by": ["char-b"],
                },
            ).status_code
            == 201
        )
    materials = EvidenceMaterials(
        evidence,
        IdentityCharacters(lambda: PostgresIdentityStore(database_url)),
        lambda: RECORDED,
        timedelta(minutes=10),
    )
    before = materials.get_decision("char-b", "turn-1")
    assert before is not None and before.text == turn_payload()["text"]
    with TestClient(create_app(clock=lambda: RECORDED)) as restarted:
        if delete_turn:
            response = restarted.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-user-source", "target_turn_ids": ["turn-1"]},
            )
            assert response.status_code == 200
        response = restarted.post(f"/characters/user-new/{resource}", json=payload)
        assert response.is_client_error
        assert restarted.get(f"/characters/user-new/{resource}/1").status_code == 404
        if not delete_turn:
            assert materials.get_decision("char-b", "turn-1") == before
            assert restarted.get("/turns/turn-1").json()["perceived_by"] == ["char-b"]


@pytest.mark.parametrize("shared_digest", [False, True])
def test_deletion_removes_both_published_embedding_revisions_and_keeps_unrelated_index(
    database_url: str,
    shared_digest: bool,
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1"))
    evidence.add_turn(turn("turn-2"))
    store = PostgresMemoryStore(database_url)
    original = replace(memory("summary"), content="公開した最初の本文")
    kept = replace(
        original,
        id=NEW_ID,
        source_turn_ids=("turn-2",),
        content=original.content if shared_digest else "影響外の本文",
    )
    with store.transaction("char-b") as tx:
        tx.add_memory(original)
        tx.add_memory(kept)
    index = PostgresRetrievalStore(database_url)
    worker = IndexWorker(index, RecordingEmbedding(), space=SPACE, clock=lambda: RECORDED)
    for work in index.claim(space=SPACE, now=RECORDED, limit=10):
        worker.process(work)
    with psycopg.connect(database_url, autocommit=True) as conn:
        first = conn.execute(
            "SELECT content_digest FROM recall_documents WHERE identifier=%s",
            (original.id,),
        ).fetchone()
        assert first is not None
        if shared_digest:
            # Establish a shared cache reference explicitly; equal content alone
            # does not share a digest because the index also hashes source IDs.
            conn.execute(
                "UPDATE recall_documents SET content_digest=%s WHERE identifier=%s",
                (first[0], kept.id),
            )
            assert (
                conn.execute(
                    "SELECT content_digest FROM recall_documents WHERE identifier=%s", (kept.id,)
                ).fetchone()
                == first
            )
        unaffected = conn.execute(
            "SELECT * FROM recall_documents WHERE identifier=%s",
            (kept.id,),
        ).fetchall()
        assert first is not None and unaffected
        with store.transaction("char-b") as tx:
            tx.replace_memory(replace(original, content="公開した二番目の本文"))
        for work in index.claim(space=SPACE, now=RECORDED, limit=10):
            worker.process(work)
        second = conn.execute(
            "SELECT content_digest FROM recall_documents WHERE identifier=%s",
            (original.id,),
        ).fetchone()
        assert second is not None and second != first
        with TestClient(create_app(clock=lambda: RECORDED)) as client:
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-revisions", "target_turn_ids": ["turn-1"]},
            )
            assert response.status_code == 200
        assert conn.execute(
            "SELECT content_digest FROM recall_embeddings WHERE content_digest=ANY(%s)",
            ([first[0], second[0]],),
        ).fetchall() == ([first] if shared_digest else [])
        assert (
            conn.execute(
                "SELECT * FROM recall_documents WHERE identifier=%s",
                (kept.id,),
            ).fetchall()
            == unaffected
        )


def test_anchor_reads_receipt_after_waiting_for_shared_state_lock(database_url: str) -> None:
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            llm_gateway=VoiceGateway(),
            reviewer=VoiceReviewer(),
        )
    ) as client:
        register_identity(client)
        register_identity(client, "char-b")
        register_turn(client, delivery=True)
        assert (
            client.post(
                "/turns/turn-1/receipts",
                json=receipt_payload("full", len(TEXT)),
            ).status_code
            == 201
        )
        with psycopg.connect(database_url, autocommit=True) as observer:

            def waiting() -> bool:
                row = observer.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted"
                ).fetchone()
                return row is not None and row[0] > 0

            with ThreadPoolExecutor(max_workers=1) as pool:
                with psycopg.connect(database_url) as writer:
                    writer.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended('recall-state',0))"
                    )
                    pending = pool.submit(client.post, ANCHORS, json=anchor_payload())
                    eventually(waiting)
                    writer.execute(
                        "INSERT INTO delivery_receipts(turn_id,status,spoken_until,reported_by,"
                        "reported_at) VALUES ('turn-1','none',0,'synthetic-window',%s)",
                        (RECORDED,),
                    )
                response = pending.result(timeout=10)
        assert response.status_code == 201
        assert response.json()["text"] == ""
        assert client.get(ANCHORS + "/anchor-1").json()["text"] == ""


@pytest.mark.parametrize("relation_type", ["updates", "contradicts", "extends"])
@pytest.mark.parametrize("delete_effect", [True, False])
@pytest.mark.parametrize(
    "elapsed", [timedelta(0), timedelta(seconds=1)], ids=["fixed", "advancing"]
)
def test_deletion_reconstructs_temporal_effects_through_baseline_rebuild_and_http(
    database_url: str, relation_type: str, delete_effect: bool, elapsed: timedelta
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1", actor="user-a"))
    later = replace(turn("turn-2", actor="user-a"), occurred_at=OCCURRED + timedelta(hours=1))
    evidence.add_turn(later)
    evidence.add_turn(turn("turn-3", actor="user-a"))
    store = PostgresMemoryStore(database_url)
    original = replace(memory(), source_turn_ids=("turn-1", "turn-3"))
    with store.transaction("char-b") as tx:
        tx.add_memory(original)

    class Change:
        def extract_local(self, value: LocalOnly) -> ExtractionProposal:
            return self._extract(value)

        def extract_cloud(self, value: CloudInput) -> ExtractionProposal:
            return self._extract(value)

        def _extract(self, value: LocalOnly | CloudInput) -> ExtractionProposal:
            return ExtractionProposal(
                memories=(
                    replace(
                        original,
                        id=NEW_ID,
                        source_turn_ids=("turn-2",),
                        valid_from=later.occurred_at,
                        object="tea" if relation_type != "extends" else original.object,
                    ),
                ),
                relations=(MemoryRelation(from_id=NEW_ID, to_id=original.id, type=relation_type),),
            )

    worker = make_worker(database_url, store, Change())
    worker.process("char-b", "turn-2")
    before = store.get_memory("char-b", original.id)
    assert before is not None
    if relation_type == "updates":
        assert before.valid_to == later.occurred_at
    elif relation_type == "contradicts":
        assert before.superseded_at == RECORDED
    else:
        assert before.last_reinforced_at == later.occurred_at
    remaining = ("turn-1", "turn-3") if delete_effect else ("turn-3",)
    recalculated = replace(original, source_turn_ids=remaining, content="残存原文による本文")

    class Clock:
        now = RECORDED

        def __call__(self) -> datetime:
            return self.now

    clock = Clock()

    class Gateway(RecordingRecalculationGateway):
        def recalculate_local(
            self, value: LocalOnly, *, memory_id: str, kind: str
        ) -> DerivedMemory | None:
            clock.now = RECORDED + elapsed
            return super().recalculate_local(value, memory_id=memory_id, kind=kind)

        def recalculate_cloud(
            self, value: CloudInput, *, memory_id: str, kind: str
        ) -> DerivedMemory | None:
            clock.now = RECORDED + elapsed
            return super().recalculate_cloud(value, memory_id=memory_id, kind=kind)

    gateway = Gateway(recalculated)
    with TestClient(create_app(clock=clock, recalculation_gateway=gateway)) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={
                "request_id": "delete-effect",
                "target_turn_ids": [
                    "turn-2" if delete_effect else "turn-1",
                ],
            },
        )
        audit = client.get("/deletion-requests/delete-effect", headers=HEADERS).json()
        assert response.status_code == 200, audit
        assert response.json()["status"] == "done"
        assert datetime.fromisoformat(audit["requested_at"]) == RECORDED
        assert datetime.fromisoformat(audit["completed_at"]) == RECORDED + elapsed
        current = store.get_memory("char-b", original.id)
        assert current is not None
        expected = original if delete_effect else before
        assert (current.valid_to, current.superseded_at, current.last_reinforced_at) == (
            expected.valid_to,
            expected.superseded_at,
            expected.last_reinforced_at,
        )
        expected_sources: tuple[str, ...] = remaining
        if not delete_effect and relation_type == "extends":
            expected_sources += ("turn-2",)
        assert set(current.source_turn_ids) == set(expected_sources)
        with store.transaction("char-b") as tx:
            assert next(v for v in tx.baselines() if v.id == original.id) == current
            worker.service.rebuild(tx)
        assert store.get_memory("char-b", original.id) == current
        active = worker.service.active("char-b")
        should_be_active = delete_effect or relation_type == "extends"
        assert (original.id in {v.id for v in active}) == should_be_active
        response = client.get("/characters/char-b/memories")
        assert response.status_code == 200
        assert (original.id in {v["id"] for v in response.json()}) == should_be_active
        if not delete_effect:
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-surviving-effect", "target_turn_ids": ["turn-2"]},
            )
            assert response.status_code == 200
            current = store.get_memory("char-b", original.id)
            assert current is not None
            assert (current.valid_to, current.superseded_at, current.last_reinforced_at) == (
                original.valid_to,
                original.superseded_at,
                original.last_reinforced_at,
            )
            with store.transaction("char-b") as tx:
                assert next(v for v in tx.baselines() if v.id == original.id) == current
                worker.service.rebuild(tx)
            assert store.get_memory("char-b", original.id) == current
            assert original.id in {v.id for v in worker.service.active("char-b")}
            assert original.id in {
                v["id"] for v in client.get("/characters/char-b/memories").json()
            }


@pytest.mark.parametrize("kind", ["anchor", "memory", "voice"])
def test_deletion_keeps_provided_same_identifier_owned_by_another_character(
    database_url: str, kind: str
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1", actor="user-a", perceived_by=("char-b",)))
    evidence.add_turn(turn("turn-2", actor="user-a", perceived_by=("char-c",)))
    index = PostgresRetrievalStore(database_url)
    provisions = PostgresProvisionStore(database_url)
    marker = '{"ref":{"owner_id":"char-b","anchor_id":"anchor-1"}}'
    query = (
        f"char-b/anchor-1 {marker}\n```json\n{marker}\n```\n"
        f"~~~json\n{marker}\n~~~\n```json\n{marker}"
    )
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            llm_gateway=VoiceGateway(),
            reviewer=VoiceReviewer(),
            retriever=index,
            embedding=RecordingEmbedding(),
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
        )
    ) as client:
        for owner, source in (("char-b", "turn-1"), ("char-c", "turn-2")):
            if kind == "anchor":
                response = client.post(
                    f"/characters/{owner}/verbatim-anchors",
                    json=anchor_payload() | {"turn_id": source},
                )
                assert response.status_code == 201
            elif kind == "voice":
                register_identity(client, owner)
                response = client.post(
                    f"/characters/{owner}/voice-exemplars",
                    json={
                        "exemplars": [
                            exemplar_payload()
                            | {
                                "source": "turn_id",
                                "turn_id": source,
                                "text": turn_payload()["text"],
                            }
                        ]
                    },
                )
                assert response.status_code == 201
            else:
                with PostgresMemoryStore(database_url).transaction(owner) as tx:
                    tx.add_memory(
                        replace(
                            memory(),
                            character_id=owner,
                            source_turn_ids=(source,),
                            shared_with=(),
                        )
                    )
        worker = IndexWorker(index, RecordingEmbedding(), space=SPACE, clock=lambda: RECORDED)
        for work in index.claim(space=SPACE, now=RECORDED, limit=10):
            worker.process(work)
        if kind == "voice":
            assert (
                client.post(
                    "/sessions",
                    json=session_payload()
                    | {
                        "session_id": "resident-session",
                        "started_at": RECORDED.isoformat(),
                        "ended_at": None,
                    },
                ).status_code
                == 201
            )
        records = {}
        for owner in ("char-b", "char-c"):
            response = client.post(
                f"/characters/{owner}/{'resident' if kind == 'voice' else 'recall'}",
                json={"session_id": "resident-session", "query": query}
                if kind == "voice"
                else request() | {"query": query},
            )
            assert response.status_code == 200
            field = {
                "anchor": "verbatim_anchors",
                "memory": "memories",
                "voice": "voice_exemplars",
            }[kind]
            assert len(response.json()[field]) == 1
            record = provisions.get(response.json()["provision_id"])
            assert record is not None
            records[owner] = record
        removed, kept = records["char-b"], records["char-c"]
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-owned-reference", "target_turn_ids": ["turn-1"]},
        )
        assert response.status_code == 200
        invalidated = response.json()["cascade_result"]["invalidated_provision_ids"]
        assert removed.provision_id in invalidated
        assert provisions.get(removed.provision_id) is None
        assert kept.provision_id not in invalidated
        assert provisions.get(kept.provision_id) == kept


def test_shared_memory_provision_tracks_owner_instead_of_recipient(database_url: str) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1", actor="user-a", perceived_by=("char-b", "char-c")))
    shared = memory()
    evidence.add_turn(turn("turn-2", actor="user-a", perceived_by=("char-c",)))
    with PostgresMemoryStore(database_url).transaction("char-b") as tx:
        tx.add_memory(shared)
    with PostgresMemoryStore(database_url).transaction("char-c") as tx:
        tx.add_memory(
            replace(
                shared,
                character_id="char-c",
                source_turn_ids=("turn-2",),
                shared_with=(),
            )
        )
    index = PostgresRetrievalStore(database_url)
    worker = IndexWorker(index, RecordingEmbedding(), space=SPACE, clock=lambda: RECORDED)
    for work in index.claim(space=SPACE, now=RECORDED, limit=10):
        worker.process(work)
    reranker = RecordingReranker()
    reranker.scores = {
        CandidateRef(owner_id=owner, memory_id=shared.id, anchor_id=None): score
        for owner, score in (("char-b", 1.0), ("char-c", -3.0))
    }
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            retriever=index,
            embedding=RecordingEmbedding(),
            reranker=reranker,
            token_counter=RecordingTokenCounter(),
        )
    ) as client:
        response = client.post("/characters/char-c/recall", json=request())
        assert response.status_code == 200
        assert [v["id"] for v in response.json()["memories"]] == [shared.id]
        provision_id = response.json()["provision_id"]
        assert [v["turn_id"] for v in response.json()["source_spans"]] == ["turn-1"]
        provisions = PostgresProvisionStore(database_url)
        saved = provisions.get(provision_id)
        assert saved is not None
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-unprovided", "target_turn_ids": ["turn-2"]},
        )
        assert response.status_code == 200
        assert provision_id not in response.json()["cascade_result"]["invalidated_provision_ids"]
        assert provisions.get(provision_id) == saved
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-shared", "target_turn_ids": ["turn-1"]},
        )
        assert response.status_code == 200
        assert provision_id in response.json()["cascade_result"]["invalidated_provision_ids"]
        assert PostgresProvisionStore(database_url).get(provision_id) is None
