"""Synthetic port doubles for the planned recall composition boundary."""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.api import create_app
from digital_souls_memory.evidence.domain import Session, Turn
from digital_souls_memory.evidence.materials import aggregate_receipts, material_decision
from digital_souls_memory.evidence.ports import CharacterRegistry, EvidenceStore
from digital_souls_memory.evidence.provision import (
    ProvisionConflict,
    ProvisionRecord,
    ProvisionUnavailable,
)
from digital_souls_memory.identity.sessions import SessionIdentity, choose_session_identity
from digital_souls_memory.memory.domain import DerivedMemory
from digital_souls_memory.recall.domain import (
    CandidateMaterial,
    CandidateRef,
    EmbeddingBatch,
    ProvisionSnapshot,
    RecallCandidate,
    RerankInput,
    RerankScore,
    RetrievalResult,
)
from digital_souls_memory.recall.inputs import ModelInput
from tests.evidence_support import RECORDED, session
from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities
from tests.it1.identity_fakes import FakeIdentityStore
from tests.it1.voice_fakes import FakeVoiceStore
from tests.recall_fixtures import SPACE, candidate_for


class RecordingEmbedding:
    def __init__(self) -> None:
        self.calls: list[tuple[ModelInput, str]] = []
        self.fail = False

    def embed(self, value: ModelInput, *, purpose: Literal["query", "passage"]) -> EmbeddingBatch:
        self.calls.append((value, purpose))
        if self.fail:
            raise RuntimeError("Synthetic embedding failure")
        return EmbeddingBatch(
            space=SPACE, vectors=tuple((1.0,) + (0.0,) * 1023 for _ in value.texts)
        )


class RecordingRetriever:
    """Returns candidates; source permission and final projection stay real."""

    def __init__(self, evidence: EvidenceStore, characters: CharacterRegistry) -> None:
        self.evidence = evidence
        self.characters = characters
        self.values: list[RecallCandidate] = []
        self.calls: list[tuple[str, ModelInput, EmbeddingBatch, datetime, int]] = []
        self.anchors: list[RecallCandidate] = []

    @contextmanager
    def reading(self) -> Iterator["RecordingRetriever"]:
        yield self

    def _result(self, values: tuple[RecallCandidate, ...]) -> RetrievalResult:
        materials = []
        for key in dict.fromkeys(d.turn_id for v in values for d in v.dependencies):
            source = self.evidence.get_turn(key)
            if source is None:
                continue
            session = self.evidence.get_session(source.session_id)
            if session is None:
                continue
            with self.evidence.list_receipts(key) as receipts:
                materials.append(
                    CandidateMaterial(
                        turn=source,
                        session=session,
                        actor_is_character=self.characters.is_character(source.actor),
                        receipt_state=aggregate_receipts(receipts),
                    )
                )
        return RetrievalResult(candidates=values, materials=tuple(materials))

    def search(
        self,
        *,
        character_id: str,
        query: ModelInput,
        query_vector: EmbeddingBatch,
        now: datetime,
        limit: int,
    ) -> RetrievalResult:
        self.calls.append((character_id, query, query_vector, now, limit))
        return self._result(tuple(self.values[:limit]))

    def anchors_for(
        self, *, character_id: str, turn_ids: tuple[str, ...], now: datetime
    ) -> RetrievalResult:
        return self._result(
            tuple(
                v
                for v in self.anchors
                if v.ref.owner_id == character_id
                and v.anchor is not None
                and v.anchor.turn_id in turn_ids
            )
        )


class RecordingReranker:
    def __init__(self) -> None:
        self.calls: list[RerankInput] = []
        self.scores: dict[CandidateRef, float] = {}

    def rerank(self, value: RerankInput) -> tuple[RerankScore, ...]:
        self.calls.append(value)
        if not self.scores:
            return tuple(
                RerankScore(ref=v.ref, chunk_index=v.chunk_index, relevance=1.0)
                for v in value.documents
            )
        return tuple(
            RerankScore(ref=v.ref, chunk_index=v.chunk_index, relevance=self.scores[v.ref])
            for v in value.documents
        )


class RecordingTokenCounter:
    def __init__(self) -> None:
        self.payloads: list[str] = []
        self.inputs: list[ModelInput] = []

    def count(self, value: ModelInput) -> int:
        self.inputs.append(value)
        (text,) = value.texts
        self.payloads.append(text)
        return (len(text) + 3) // 4


class FakeProvisionStore:
    """Atomic insert; both failure points leave committed records unchanged."""

    def __init__(self, validate: Callable[[ProvisionSnapshot], bool]) -> None:
        self.records: dict[str, ProvisionRecord] = {}
        self.fail_at: str | None = None
        self.validate = validate
        self.snapshots: list[ProvisionSnapshot] = []

    def save(self, record: ProvisionRecord, *, snapshot: ProvisionSnapshot) -> None:
        self.snapshots.append(snapshot)
        if not self.validate(snapshot):
            raise ProvisionUnavailable()
        if record.provision_id in self.records:
            raise ProvisionConflict()
        if self.fail_at == "insert":
            raise ProvisionUnavailable()
        pending = self.records | {record.provision_id: record}
        if self.fail_at == "commit":
            raise ProvisionUnavailable()
        self.records = pending

    def get(self, provision_id: str) -> ProvisionRecord | None:
        return self.records.get(provision_id)


class FakeSessionIdentityStore:
    def __init__(self, identity: FakeIdentityStore) -> None:
        self.identity = identity
        self.selections: dict[tuple[str, str], SessionIdentity] = {}

    def select(self, *, character_id: str, session: Session) -> SessionIdentity:
        key = (session.session_id, character_id)
        if key not in self.selections:
            self.selections[key] = choose_session_identity(
                character_id=character_id,
                session=session,
                temperaments=tuple(self.identity.temperaments.values()),
                profiles=tuple(self.identity.profiles.values()),
            )
        return self.selections[key]


@dataclass
class StepClock:
    seconds: float = 0

    def __call__(self) -> float:
        current = self.seconds
        self.seconds += 0.01
        return current


class RecallHarness:
    def __init__(self) -> None:
        self.now = RECORDED
        self.next_id: str | None = None
        self.id_sequence = 0
        self.evidence = FakeEvidenceStore()
        self.evidence.add_session(session(reports_delivery=False))
        self.identity = registered_identities()
        self.voice = FakeVoiceStore()
        self.retriever = RecordingRetriever(
            self.evidence, IdentityCharacters(lambda: self.identity)
        )
        self.embedding = RecordingEmbedding()
        self.reranker = RecordingReranker()
        self.counter = RecordingTokenCounter()
        self.selections = FakeSessionIdentityStore(self.identity)
        self.provisions = FakeProvisionStore(self.snapshot_is_current)
        from tests.resident_profile_support import FakeProfileStore, StoredCandidates

        self.profiles = FakeProfileStore()
        self.resident_candidates = StoredCandidates()
        self.client = TestClient(
            create_app(
                store=self.evidence,
                identity_store=self.identity,
                voice_store=self.voice,
                profile_store=self.profiles,
                resident_candidates=self.resident_candidates,
                clock=lambda: self.now,
                retriever=self.retriever,
                embedding=self.embedding,
                reranker=self.reranker,
                token_counter=self.counter,
                provision_store=self.provisions,
                session_identity_store=self.selections,
                provision_id_factory=self.new_provision_id,
                monotonic=StepClock(),
            ),
            raise_server_exceptions=False,
        )

    def new_provision_id(self) -> str:
        if self.next_id is not None:
            return self.next_id
        self.id_sequence += 1
        return f"provision-{self.id_sequence}"

    def seed(self, source: Turn, value: DerivedMemory) -> None:
        self.evidence.add_turn(source)
        self.retriever.values.append(candidate_for(value, sources=(source,)))

    def snapshot_is_current(self, snapshot: ProvisionSnapshot) -> bool:
        selection = self.selections.selections.get((snapshot.session_id, snapshot.character_id))
        if selection is None or (
            selection.temperament_version != snapshot.temperament_version
            or selection.personality_version != snapshot.personality_version
        ):
            return False
        documents = {
            v.document.key: v.document for v in (*self.retriever.values, *self.retriever.anchors)
        }
        documents.update(
            {v.document.key: v.document for v in self.resident_candidates.values.values()}
        )
        if snapshot.profile_version is not None:
            profile = self.profiles.get(snapshot.character_id)
            if (profile.version if profile is not None else 0) != snapshot.profile_version:
                return False
            core = self.identity.get_core(snapshot.character_id)
            if (core.version if core is not None else None) != snapshot.core_identity_version:
                return False
            for value in snapshot.voice_exemplars:
                if self.voice.get_exemplar(snapshot.character_id, value.exemplar_id) != value:
                    return False
        if any(documents.get(v.key) != v for v in snapshot.documents):
            return False
        for stamp in snapshot.sources:
            source = self.evidence.get_turn(stamp.turn_id)
            if source is None:
                return False
            source_session = self.evidence.get_session(source.session_id)
            assert source_session is not None
            with self.evidence.list_receipts(source.turn_id) as receipts:
                actual = material_decision(
                    character_id=stamp.character_id,
                    session=source_session,
                    turn=source,
                    actor_is_character=source.actor.startswith("char-"),
                    receipts=receipts,
                    now=self.now,
                    delivery_timeout=timedelta(minutes=10),
                )
            if (actual.basis, actual.receipt_version, len(actual.text or ""), actual.deadline) != (
                stamp.basis,
                stamp.receipt_version,
                stamp.available_end,
                stamp.deadline,
            ) or stamp.used_end > len(actual.text or ""):
                return False
        return True

    def assert_record_matches(self, body: dict[str, object]) -> None:
        record = self.provisions.get(str(body["provision_id"]))
        assert record is not None
        assert record.kind == "turn"
        assert record.character_id == "char-b"
        assert record.session_id == "session-1"
        assert record.surface == "synthetic-chat"
        assert record.temperament_version == body["temperament_version"]
        assert record.personality_version == body["personality_version"]
        encoded = json.loads(json.dumps(body))
        assert list(record.memory_ids) == [v["id"] for v in encoded["memories"]]
        assert [
            (
                s.turn_id,
                s.start,
                s.end,
                s.text,
                s.actor,
                s.language,
                s.truncated,
                s.original_start,
                s.original_end,
            )
            for s in record.source_spans
        ] == [
            (
                s["turn_id"],
                s["start"],
                s["end"],
                s["text"],
                s["actor"],
                s["language"],
                s["truncated"],
                s["original_start"],
                s["original_end"],
            )
            for s in encoded["source_spans"]
        ]
        assert list(record.verbatim_anchor_ids) == [
            v["anchor_id"] for v in encoded["verbatim_anchors"]
        ]
        assert record.voice_exemplar_ids == record.motivation_ids == ()
        assert len(record.scores) >= len(record.memory_ids)
        assert record.provided_at == self.now
        assert record.duration_ms == pytest.approx(10)
