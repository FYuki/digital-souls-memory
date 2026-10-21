"""Approved model identities, shipped independently of evaluation reports."""

from dataclasses import dataclass, field
from math import isfinite

from digital_souls_memory.recall.domain import VectorSpace
from digital_souls_memory.recall.scoring import Weights

EMBEDDING_MODEL: dict[str, object] = {
    "model_alias": "bge-m3",
    "revision": "9eba04c5d75ba5a1595e45de734d36bef4e5cb98",
    "sha256": "aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173",
    "dimensions": 1024,
    "pooling": "cls",
    "quantization": "Q8_0",
    "query_prefix": "",
    "passage_prefix": "",
    "extension": "pg_bigm",
}
RERANKER_MODEL: dict[str, object] = {
    "model_alias": "bge-reranker-v2-m3",
    "revision": "3093af03b1a635e67b084b1d8c03c5f5e020fd05",
    "sha256": "a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3",
    "quantization": "Q8_0",
}


def approved_space() -> VectorSpace:
    return VectorSpace(
        model=str(EMBEDDING_MODEL["model_alias"]),
        revision=str(EMBEDDING_MODEL["revision"]),
        digest=str(EMBEDDING_MODEL["sha256"]),
        dimensions=1024,
    )


@dataclass(frozen=True, kw_only=True)
class RecallSettings:
    weights: Weights = field(default_factory=Weights)
    relevance_threshold: float = -2
    candidate_limit: int = 200

    def __post_init__(self) -> None:
        if not isfinite(self.relevance_threshold) or self.candidate_limit < 20:
            raise ValueError("Invalid recall settings")
