"""Model, retrieval and final persistence boundaries."""

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Literal, Protocol

from digital_souls_memory.evidence.provision import ProvisionRecord
from digital_souls_memory.recall.domain import (
    EmbeddingBatch,
    ProvisionSnapshot,
    RerankInput,
    RerankScore,
    RetrievalResult,
)
from digital_souls_memory.recall.inputs import ModelInput


class Embedding(Protocol):
    def embed(
        self, value: ModelInput, *, purpose: Literal["query", "passage"]
    ) -> EmbeddingBatch: ...


class Retriever(Protocol):
    def reading(self) -> AbstractContextManager["Retriever"]: ...
    def search(
        self,
        *,
        character_id: str,
        query: ModelInput,
        query_vector: EmbeddingBatch,
        now: datetime,
        limit: int,
    ) -> RetrievalResult: ...
    def anchors_for(
        self, *, character_id: str, turn_ids: tuple[str, ...], now: datetime
    ) -> RetrievalResult: ...


class Reranker(Protocol):
    def rerank(self, value: RerankInput) -> tuple[RerankScore, ...]: ...


class TokenCounter(Protocol):
    def count(self, value: ModelInput) -> int: ...


class ProvisionStore(Protocol):
    def save(self, record: ProvisionRecord, *, snapshot: ProvisionSnapshot) -> None: ...
    def get(self, provision_id: str) -> ProvisionRecord | None: ...
