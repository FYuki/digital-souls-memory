"""Model, retrieval and final persistence boundaries."""

from datetime import datetime
from typing import Literal, Protocol

from digital_souls_memory.evidence.provision import ProvisionRecord
from digital_souls_memory.recall.domain import (
    EmbeddingBatch,
    ProvisionSnapshot,
    RecallCandidate,
    RerankInput,
    RerankScore,
)
from digital_souls_memory.recall.inputs import ModelInput


class Embedding(Protocol):
    def embed(
        self, value: ModelInput, *, purpose: Literal["query", "passage"]
    ) -> EmbeddingBatch: ...


class Retriever(Protocol):
    def search(
        self,
        *,
        character_id: str,
        query: ModelInput,
        query_vector: EmbeddingBatch,
        now: datetime,
        limit: int,
    ) -> tuple[RecallCandidate, ...]: ...
    def anchors_for(
        self, *, character_id: str, turn_ids: tuple[str, ...], now: datetime
    ) -> tuple[RecallCandidate, ...]: ...


class Reranker(Protocol):
    def rerank(self, value: RerankInput) -> tuple[RerankScore, ...]: ...


class TokenCounter(Protocol):
    def count(self, value: ModelInput) -> int: ...


class ProvisionStore(Protocol):
    def save(self, record: ProvisionRecord, *, snapshot: ProvisionSnapshot) -> None: ...
    def get(self, provision_id: str) -> ProvisionRecord | None: ...
