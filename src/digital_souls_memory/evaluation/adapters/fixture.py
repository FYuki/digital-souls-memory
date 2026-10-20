"""Explicit synthetic port data, independent from gold and never used as fallback."""

from copy import deepcopy
from typing import Self

from ..models import integer, record, rows, string, validate_vectors
from ..retrieval import composite_score, source_spans


class FixtureEmbedding:
    def __init__(self, candidate: dict[str, object], fixture: dict[str, object]) -> None:
        self.dimensions = integer(candidate["dimensions"])
        self.vectors = deepcopy(record(fixture["vectors"]))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.vectors.clear()

    def embed(self, texts: tuple[str, ...], *, purpose: str) -> tuple[tuple[float, ...], ...]:
        vectors = record(self.vectors[purpose])
        return validate_vectors(
            [vectors[text] for text in texts], count=len(texts), dimensions=self.dimensions
        )


class FixtureSearch:
    def __init__(self, fixture: dict[str, object]) -> None:
        self.observations = deepcopy(record(fixture["observations"]))
        self.case: dict[str, object] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.case = None
        self.observations.clear()

    def index(self, case: dict[str, object], vectors: tuple[tuple[float, ...], ...]) -> None:
        if len(vectors) != len(rows(case["memories"])):
            raise ValueError("Fixture vector count mismatch")
        self.case = deepcopy(case)

    def search(self, query_vector: tuple[float, ...], *, limit: int) -> list[dict[str, object]]:
        if self.case is None:
            raise ValueError("Fixture not indexed")
        observations = rows(self.observations[string(self.case["id"])])
        return [
            {**hit, "source_spans": source_spans(self.case, memory_id=string(hit["memory_id"]))}
            for hit in observations[:limit]
        ]

    def observe(self, query_vector: tuple[float, ...]) -> list[dict[str, object]]:
        if self.case is None:
            raise ValueError("Fixture not indexed")
        protocol = record(self.case["protocol"])
        return [
            {
                **hit,
                "score": composite_score(hit["components"], protocol["weights"]),
                "source_spans": source_spans(self.case, memory_id=string(hit["memory_id"])),
            }
            for hit in rows(self.observations[string(self.case["id"])])
        ]


class FixtureReranker:
    def __init__(self, fixture: dict[str, object]) -> None:
        self.scores = deepcopy(record(fixture["reranker_scores"]))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.scores.clear()

    def rerank(
        self, query: str, documents: tuple[dict[str, object], ...]
    ) -> list[dict[str, object]]:
        return deepcopy(rows(self.scores[query]))
