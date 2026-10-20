"""Ports owned by the comparison use case."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable


class Embedding(Protocol):
    def embed(self, texts: tuple[str, ...], *, purpose: str) -> tuple[tuple[float, ...], ...]: ...


@runtime_checkable
class MeasuredEmbedding(Embedding, Protocol):
    @property
    def real_model_calls(self) -> int: ...


class Search(Protocol):
    def index(self, case: dict[str, object], vectors: tuple[tuple[float, ...], ...]) -> None: ...

    def search(self, query_vector: tuple[float, ...], *, limit: int) -> list[dict[str, object]]: ...


@runtime_checkable
class ObservedSearch(Search, Protocol):
    def observe(self, query_vector: tuple[float, ...]) -> list[dict[str, object]]: ...


class Reranker(Protocol):
    def rerank(
        self, query: str, documents: tuple[dict[str, object], ...]
    ) -> list[dict[str, object]]: ...


@runtime_checkable
class MeasuredReranker(Reranker, Protocol):
    @property
    def real_model_calls(self) -> int: ...


@runtime_checkable
class MeasuredSearch(Search, Protocol):
    @property
    def measurements(self) -> dict[str, float]: ...


class ResourceObserver(Protocol):
    @property
    def observation(self) -> dict[str, object]: ...


type EmbeddingFactory = Callable[[dict[str, object]], AbstractContextManager[Embedding]]
type SearchFactory = Callable[[dict[str, object]], AbstractContextManager[Search]]
type ResourceFactory = Callable[[dict[str, object]], AbstractContextManager[ResourceObserver]]
type RerankerFactory = Callable[[dict[str, object]], AbstractContextManager[Reranker]]
