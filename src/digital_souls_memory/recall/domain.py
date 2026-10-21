"""Values crossing recall's retrieval and final commit boundaries."""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from digital_souls_memory.evidence.domain import Session, Turn
from digital_souls_memory.evidence.materials import ReceiptState
from digital_souls_memory.evidence.provision import CandidateRef as CandidateRef
from digital_souls_memory.evidence.provision import SourceSpan
from digital_souls_memory.memory.domain import DerivedMemory
from digital_souls_memory.recall.inputs import InputLabel, ModelInput
from digital_souls_memory.voice.domain import VerbatimAnchor, VoiceExemplar


@dataclass(frozen=True, kw_only=True)
class VectorSpace:
    model: str
    revision: str
    digest: str
    dimensions: int


@dataclass(frozen=True, kw_only=True)
class EmbeddingBatch:
    space: VectorSpace
    vectors: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        if not self.vectors or any(
            len(v) != self.space.dimensions or not all(isfinite(x) for x in v) for v in self.vectors
        ):
            raise ValueError("Invalid embedding batch")


@dataclass(frozen=True, kw_only=True)
class SourceStamp:
    turn_id: str
    character_id: str
    basis: str
    receipt_version: int
    available_end: int
    used_end: int
    deadline: datetime


@dataclass(frozen=True, kw_only=True)
class DocumentStamp:
    ref: CandidateRef
    version: int
    content_digest: str
    space: VectorSpace


@dataclass(frozen=True, kw_only=True)
class RecallCandidate:
    ref: CandidateRef
    memory: DerivedMemory | None
    anchor: VerbatimAnchor | None
    search_text: str
    semantic: float
    lexical: float
    importance: float
    occurred_at: datetime
    document: DocumentStamp
    dependencies: tuple[SourceStamp, ...]
    labels: tuple[InputLabel, ...]


@dataclass(frozen=True, kw_only=True)
class CandidateMaterial:
    turn: Turn
    session: Session
    actor_is_character: bool
    receipt_state: ReceiptState


@dataclass(frozen=True, kw_only=True)
class ResolvedCandidate:
    candidate: RecallCandidate
    spans: tuple[SourceSpan, ...]
    sources: tuple[SourceStamp, ...]


@dataclass(frozen=True, kw_only=True)
class ProvisionSnapshot:
    character_id: str
    session_id: str
    temperament_version: int | None
    personality_version: int | None
    documents: tuple[DocumentStamp, ...]
    sources: tuple[SourceStamp, ...]
    profile_version: int | None = None
    core_identity_version: int | None = None
    voice_exemplars: tuple[VoiceExemplar, ...] = ()


@dataclass(frozen=True, kw_only=True)
class RerankDocument:
    ref: CandidateRef
    input: ModelInput


@dataclass(frozen=True, kw_only=True)
class RerankInput:
    query: ModelInput
    documents: tuple[RerankDocument, ...]


@dataclass(frozen=True, kw_only=True)
class RerankScore:
    ref: CandidateRef
    relevance: float
