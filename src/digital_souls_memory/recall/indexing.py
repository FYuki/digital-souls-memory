"""Versioned work values for rebuildable persistent search documents."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from digital_souls_memory.recall.domain import (
    CandidateRef,
    EmbeddingBatch,
    RecallCandidate,
    VectorSpace,
)
from digital_souls_memory.recall.inputs import ModelInput


@dataclass(frozen=True, kw_only=True)
class IndexWork:
    ref: CandidateRef
    version: int
    token: str
    space: VectorSpace


@dataclass(frozen=True, kw_only=True)
class IndexInput:
    candidate: RecallCandidate
    input: ModelInput


class IndexStore(Protocol):
    def claim(self, *, space: VectorSpace, now: datetime, limit: int) -> tuple[IndexWork, ...]: ...
    def load(self, work: IndexWork, *, now: datetime) -> IndexInput | None: ...
    def cached(self, value: IndexInput) -> EmbeddingBatch | None: ...
    def publish(
        self, work: IndexWork, *, input: IndexInput, embedding: EmbeddingBatch, now: datetime
    ) -> bool: ...
    def retry(self, work: IndexWork, *, not_before: datetime) -> None: ...
