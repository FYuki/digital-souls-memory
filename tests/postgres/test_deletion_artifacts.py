"""Saved index and resident artifacts follow deletion on the same stores."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from itertools import count
from typing import Literal

import psycopg
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.api import create_app
from digital_souls_memory.recall.domain import EmbeddingBatch
from digital_souls_memory.recall.worker import IndexWorker
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import RECORDED, receipt, session, turn
from tests.memory_support import NEW_ID, memory
from tests.postgres.test_deletion import HEADERS, seed_evidence
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.recall_fixtures import SPACE
from tests.recall_support import RecordingEmbedding

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


def test_deletion_withdraws_old_index_and_embedding_without_rebuilding_unaffected_rows(
    database_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1", text="消す合成根拠"))
    evidence.add_turn(turn("turn-2", text="影響外の合成根拠"))
    memories = PostgresMemoryStore(database_url)
    affected = replace(memory("summary"), content="消す合成索引")
    unaffected = replace(affected, id=NEW_ID, source_turn_ids=("turn-2",), content="保持する索引")
    with memories.transaction("char-b") as tx:
        tx.add_memory(affected)
        tx.add_memory(unaffected)
    index = PostgresRetrievalStore(database_url)
    worker = IndexWorker(index, RecordingEmbedding(), space=SPACE, clock=lambda: RECORDED)
    for work in index.claim(space=SPACE, now=RECORDED, limit=10):
        worker.process(work)
    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as conn:
        kept = conn.execute(
            "SELECT * FROM recall_documents WHERE identifier=%s",
            (unaffected.id,),
        ).fetchall()
        removed_digest = conn.execute(
            "SELECT content_digest FROM recall_documents WHERE identifier=%s",
            (affected.id,),
        ).fetchone()
        assert len(kept) == 1 and removed_digest is not None
        # A new revision is loaded but has not yet been published when deletion commits.
        with memories.transaction("char-b") as tx:
            tx.replace_memory(replace(affected, content="保存待ちの合成索引"))
        (work,) = index.claim(space=SPACE, now=RECORDED, limit=10)
        loaded = index.load(work, now=RECORDED)
        assert loaded is not None
        with TestClient(create_app(clock=lambda: RECORDED)) as client:
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-index", "target_turn_ids": ["turn-1"]},
            )
            assert response.status_code == 200
        batch = EmbeddingBatch(space=SPACE, vectors=((1.0,) + (0.0,) * 1023,))
        assert index.publish(work, input=loaded, embedding=batch, now=RECORDED) is False
        assert (
            conn.execute(
                "SELECT * FROM recall_documents WHERE identifier=%s", (unaffected.id,)
            ).fetchall()
            == kept
        )
        assert (
            conn.execute(
                "SELECT identifier FROM recall_documents WHERE identifier=%s", (affected.id,)
            ).fetchall()
            == []
        )
        assert (
            conn.execute(
                "SELECT turn_id FROM recall_document_sources WHERE turn_id='turn-1'"
            ).fetchall()
            == []
        )
        assert (
            conn.execute(
                "SELECT content_digest FROM recall_embeddings WHERE content_digest=%s",
                removed_digest,
            ).fetchall()
            == []
        )
        (found,) = index.search(
            character_id="char-b", query=loaded.input, query_vector=batch, now=RECORDED, limit=20
        )
        assert found.memory == unaffected


def test_recalculated_baseline_preserves_partial_delivery_through_index_and_http_recall(
    database_url: str,
) -> None:
    from digital_souls_memory.memory.domain import DeliveryBasis, ExtractionState, InputDependency
    from tests.it1.test_recall import request
    from tests.recall_support import RecordingReranker, RecordingTokenCounter

    evidence = seed_evidence(database_url)
    delivered_session = replace(session("delivered"), ended_at=None)
    evidence.add_session(delivered_session)
    origin = turn("turn-1", text="削除する抽出起点")
    evidence.add_turn(origin)
    source = turn("turn-2", session_id="delivered", text="届いた本文・未配達の秘密末尾")
    evidence.add_turn(source)
    end = len("届いた本文")
    evidence.add_receipt(replace(receipt("partial", end), turn_id="turn-2"))
    original = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"), content="旧内容")
    memories = PostgresMemoryStore(database_url)
    with memories.transaction("char-b") as tx:
        tx.save_extraction(
            ExtractionState(
                character_id="char-b",
                turn_id=origin.turn_id,
                delivery_basis=DeliveryBasis.DECLARED_FULL,
                signature="synthetic",
                status="done",
                token=None,
                lease_until=None,
                error=None,
                deadline=RECORDED + timedelta(minutes=10),
                receipt_version=0,
                text_hash=sha256(origin.text.encode("utf-8")).hexdigest(),
                used_end=len(origin.text),
            )
        )
        tx.set_candidates(
            origin.turn_id,
            (original,),
            dependencies=tuple(
                InputDependency(
                    turn_id=identifier,
                    basis=basis,
                    receipt_version=version,
                    available_end=used,
                    used_end=used,
                    deadline=RECORDED + timedelta(minutes=10),
                )
                for identifier, basis, version, used in (
                    (origin.turn_id, "declared_full", 0, len(origin.text)),
                    (source.turn_id, "receipt_confirmed", 1, end),
                )
            ),
        )
        tx.add_generated(original)
    revised = replace(original, content="届いた本文の要約", source_turn_ids=("turn-2",))
    gateway = RecordingRecalculationGateway(revised)
    index = PostgresRetrievalStore(database_url)
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            recalculation_gateway=gateway,
            retriever=index,
            embedding=RecordingEmbedding(),
            reranker=RecordingReranker(),
            token_counter=RecordingTokenCounter(),
        )
    ) as client:
        assert (
            client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-partial", "target_turn_ids": ["turn-1"]},
            ).status_code
            == 200
        )
        assert memories.get_extraction("char-b", origin.turn_id) is None
        with memories.transaction("char-b") as tx:
            assert tx.candidates() == ()
            assert tx.baselines() == (revised,)
        for work in index.claim(space=SPACE, now=RECORDED, limit=10):
            loaded = index.load(work, now=RECORDED)
            assert loaded is not None
            assert [(s.turn_id, s.used_end) for s in loaded.candidate.dependencies] == [
                ("turn-2", end)
            ]
            IndexWorker(index, RecordingEmbedding(), space=SPACE, clock=lambda: RECORDED).process(
                work
            )
        response = client.post("/characters/char-b/recall", json=request())
        assert response.status_code == 200
        assert [v["id"] for v in response.json()["memories"]] == [original.id]
        assert [(v["turn_id"], v["text"]) for v in response.json()["source_spans"]] == [
            ("turn-2", source.text[:end])
        ]
    assert gateway.calls
    assert all(
        [t.text for t in value.turns] == [source.text[:end]] for value, _, _ in gateway.calls
    )


@pytest.mark.parametrize("operation", ["state", "episodes"])
def test_saved_context_and_paused_completion_cannot_restore_deleted_source(
    database_url: str,
    operation: Literal["state", "episodes"],
) -> None:
    from digital_souls_memory.adapters.postgres_resident import PostgresContextStore
    from digital_souls_memory.resident.domain import ContextKey
    from digital_souls_memory.resident.worker import ContextWorker
    from tests.context_gateway_support import EpisodeGateway

    evidence = seed_evidence(database_url)
    evidence.add_session(replace(session("open", reports_delivery=False), ended_at=None))
    evidence.add_turn(turn("turn-1", session_id="open", text="消す合成話題"))
    store = PostgresContextStore(database_url)
    key = ContextKey(character_id="char-b", session_id="open", surface="synthetic-chat")
    gateway = EpisodeGateway()
    ids = count()
    worker = ContextWorker(
        store=store,
        gateway=gateway,
        clock=lambda: RECORDED,
        non_sensitive_labels=frozenset(),
        state_ttl=timedelta(minutes=30),
        episode_id_factory=lambda: f"synthetic-episode-{next(ids)}",
    )
    process = worker.process_state if operation == "state" else worker.process_episodes
    process(key)
    if operation == "state":
        saved = store.get_state(key)
        assert saved is not None and saved.current_topic == "消す合成話題"
    else:
        assert [e.topic for e in store.list_episodes(key)] == ["消す合成話題"]
    evidence.add_turn(turn("turn-2", session_id="open", text="残す合成話題"))
    started = gateway.started if operation == "state" else gateway.episode_started
    release = gateway.release if operation == "state" else gateway.episode_release
    started.clear()
    release.clear()
    with TestClient(create_app(clock=lambda: RECORDED, context_store=store)) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(process, key)
            try:
                assert started.wait(3)
                assert (
                    client.post(
                        "/deletion-requests",
                        headers=HEADERS,
                        json={"request_id": "delete-context", "target_turn_ids": ["turn-1"]},
                    ).status_code
                    == 200
                )
                assert store.get_state(key) is None
                assert store.list_episodes(key) == ()
            finally:
                release.set()
            pending.result(timeout=5)
        assert store.get_state(key) is None
        assert store.list_episodes(key) == ()
        with psycopg.connect(database_url, connect_timeout=5) as conn:
            assert (
                conn.execute("SELECT turn_id FROM episode_spans WHERE turn_id='turn-1'").fetchall()
                == []
            )
        process(key)
        if operation == "state":
            current = store.get_state(key)
            assert current is not None and current.current_topic == "残す合成話題"
        else:
            assert [e.topic for e in store.list_episodes(key)] == ["残す合成話題"]


def test_direct_voice_commitment_and_profile_references_are_removed_while_identity_is_kept(
    database_url: str,
) -> None:
    from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
    from digital_souls_memory.adapters.postgres_provision import PostgresSessionIdentityStore
    from digital_souls_memory.adapters.postgres_resident import PostgresProfileStore
    from digital_souls_memory.adapters.postgres_voice import PostgresVoiceStore
    from digital_souls_memory.memory.domain import MemoryRelation
    from tests.it1.test_voice import register_identity, register_turn
    from tests.it1.voice_fakes import VoiceGateway, VoiceReviewer
    from tests.voice_support import ANCHORS, EXEMPLARS, anchor_payload, exemplar_payload

    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            llm_gateway=VoiceGateway(),
            reviewer=VoiceReviewer(),
        )
    ) as client:
        register_identity(client)
        register_identity(client, "char-b")
        register_turn(client)
        assert client.post(EXEMPLARS, json={"exemplars": [exemplar_payload()]}).is_success
        assert client.post(
            EXEMPLARS,
            json={
                "exemplars": [
                    exemplar_payload()
                    | {"exemplar_id": "from-turn", "source": "turn_id", "turn_id": "turn-1"}
                ]
            },
        ).is_success
        assert client.post(ANCHORS, json=anchor_payload()).is_success
        identities = PostgresIdentityStore(database_url)
        before_identity = identities.get_current("char-a", RECORDED)
        voice = PostgresVoiceStore(database_url)
        kept = voice.get_exemplar("char-a", "voice-1")
        assert kept is not None and voice.get_exemplar("char-a", "from-turn") is not None
        from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore

        ledger = PostgresEvidenceStore(database_url)
        ledger.add_turn(turn("turn-2", actor="user-a", perceived_by=("char-a", "char-b")))
        evidence = PostgresMemoryStore(database_url)
        commitment = replace(memory("commitment"), character_id="char-a")
        unaffected = replace(
            memory("summary"), id=NEW_ID, character_id="char-a", source_turn_ids=("turn-2",)
        )
        with evidence.transaction("char-a") as tx:
            tx.add_memory(commitment)
            tx.add_memory(unaffected)
            tx.add_relation(
                MemoryRelation(from_id=commitment.id, to_id=unaffected.id, type="derives")
            )
        source_session = ledger.get_session("session-1")
        assert source_session is not None
        selections = PostgresSessionIdentityStore(database_url)
        selected = selections.select(character_id="char-a", session=source_session)
        profiles = PostgresProfileStore(database_url)
        with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as conn:
            conn.execute(
                "INSERT INTO character_profiles VALUES ('char-a',1,10000),('char-b',1,10000)"
            )
            conn.execute(
                "INSERT INTO profile_entries(character_id,entry_id,position,kind,turn_id,"
                '"start","end") '
                "VALUES ('char-a','original',0,'turn','turn-1',0,3)"
            )
            conn.execute(
                "INSERT INTO profile_entries(character_id,entry_id,position,kind,"
                "owner_id,memory_id) "
                "VALUES ('char-a','commitment',1,'memory','char-a',%s)",
                (commitment.id,),
            )
            untouched = profiles.get("char-b")
            assert untouched is not None
            configured = profiles.get("char-a")
            assert configured is not None and len(configured.entries) == 2
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-references", "target_turn_ids": ["turn-1"]},
            )
            assert response.status_code == 200
            assert client.get(ANCHORS + "/anchor-1").status_code == 404
            assert client.get(EXEMPLARS + "/from-turn").status_code == 404
            assert voice.get_anchor("char-a", "anchor-1") is None
            assert voice.get_exemplar("char-a", "from-turn") is None
            assert voice.get_exemplar("char-a", "voice-1") == kept
            assert evidence.get_memory("char-a", commitment.id) is None
            assert evidence.get_memory("char-a", unaffected.id) == unaffected
            with evidence.list_relations("char-a", unaffected.id) as relations:
                assert tuple(relations) == ()
            current = profiles.get("char-a")
            assert current is not None and current.entries == ()
            assert current.version > configured.version
            assert profiles.get("char-b") == untouched
            assert identities.get_current("char-a", RECORDED) == before_identity
            assert selections.select(character_id="char-a", session=source_session) == selected
            assert (
                conn.execute("SELECT turn_id FROM memory_sources WHERE turn_id='turn-1'").fetchall()
                == []
            )
            assert (
                conn.execute(
                    "SELECT turn_id FROM memory_notifications WHERE turn_id='turn-1'"
                ).fetchall()
                == []
            )
