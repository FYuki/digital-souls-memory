"""Versioned work values for rebuildable persistent search documents."""

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from digital_souls_memory.recall.chunking import TextChunk
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
    chunks: tuple[TextChunk, ...]

    @property
    def candidates(self) -> tuple[RecallCandidate, ...]:
        return tuple(
            replace(
                self.candidate,
                search_text=chunk.text,
                chunk_start=chunk.start,
                chunk_end=chunk.end,
                document=replace(
                    self.candidate.document,
                    chunk_index=chunk.index,
                    content_digest=(
                        self.candidate.document.content_digest
                        if len(self.chunks) == 1
                        else sha256(
                            f"{self.candidate.document.content_digest}:{chunk.index}:"
                            f"{chunk.start}:{chunk.end}:{chunk.text}".encode()
                        ).hexdigest()
                    ),
                ),
            )
            for chunk in self.chunks
        )


class IndexStore(Protocol):
    def claim(self, *, space: VectorSpace, now: datetime, limit: int) -> tuple[IndexWork, ...]: ...
    def load(self, work: IndexWork, *, now: datetime) -> IndexInput | None: ...
    def cached(self, value: IndexInput) -> EmbeddingBatch | None: ...
    def publish(
        self, work: IndexWork, *, input: IndexInput, embedding: EmbeddingBatch, now: datetime
    ) -> bool: ...
    def retry(self, work: IndexWork, *, not_before: datetime) -> None: ...
