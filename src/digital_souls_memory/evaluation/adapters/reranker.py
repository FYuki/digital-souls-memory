"""Local rerank HTTP adapter. The configured build must support the /rerank contract."""

from copy import deepcopy
from typing import Self

import httpx

from ..models import number, record, rows, string, unique, validate_endpoint, validate_reranker


class LlamaCppReranker:
    def __init__(
        self,
        candidate: dict[str, object],
        *,
        endpoint: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        validate_endpoint(endpoint)
        validate_reranker(candidate)
        self.candidate = deepcopy(candidate)
        self.real_model_calls = 0
        self.client = httpx.Client(
            base_url=endpoint,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(120, connect=5),
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.client.close()

    def rerank(
        self, query: str, documents: tuple[dict[str, object], ...]
    ) -> list[dict[str, object]]:
        unique(list(documents), "memory_id")
        if not documents:
            raise ValueError("Nonempty rerank candidates required")
        self.real_model_calls += 1
        response = self.client.post(
            "/rerank",
            json={
                "model": string(self.candidate["model_alias"]),
                "query": string(query),
                "documents": [string(d["content"]) for d in documents],
                "top_n": len(documents),
            },
        )
        response.raise_for_status()
        data = rows(record(response.json())["results"])
        indices = [row["index"] for row in data]
        if (
            any(type(i) is not int for i in indices)
            or len(indices) != len(documents)
            or set(indices) != set(range(len(documents)))
        ):
            raise ValueError("Rerank response indices mismatch")
        return [
            {
                "memory_id": documents[int(str(row["index"]))]["memory_id"],
                "score": number(row["relevance_score"]),
            }
            for row in data
        ]
