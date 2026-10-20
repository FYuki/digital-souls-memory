"""Local llama.cpp OpenAI-compatible embedding boundary."""

from typing import Self

import httpx

from ..models import (
    integer,
    record,
    rows,
    string,
    validate_candidate,
    validate_endpoint,
    validate_vectors,
)


class LlamaCppEmbedding:
    def __init__(
        self,
        candidate: dict[str, object],
        *,
        endpoint: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        validate_endpoint(endpoint)
        validate_candidate(candidate)
        self.candidate = dict(candidate)
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

    def embed(self, texts: tuple[str, ...], *, purpose: str) -> tuple[tuple[float, ...], ...]:
        if purpose not in ("query", "passage") or not texts:
            raise ValueError("Nonempty query or passage batch required")
        prefix = self.candidate[purpose + "_prefix"]
        if not isinstance(prefix, str):
            raise ValueError("Explicit prefix required")
        alias = string(self.candidate["model_alias"])
        self.real_model_calls += 1
        response = self.client.post(
            "/v1/embeddings",
            json={
                "model": alias,
                "input": [prefix + string(text) for text in texts],
                "encoding_format": "float",
            },
        )
        response.raise_for_status()
        payload = record(response.json())
        if payload["model"] != alias:
            raise ValueError("Model identity mismatch")
        data = rows(payload["data"])
        indices = [row["index"] for row in data]
        if (
            any(type(index) is not int for index in indices)
            or len(data) != len(texts)
            or set(indices) != set(range(len(texts)))
        ):
            raise ValueError("Embedding indices mismatch")
        ordered = sorted(data, key=lambda row: int(str(row["index"])))
        return validate_vectors(
            [row["embedding"] for row in ordered],
            count=len(texts),
            dimensions=integer(self.candidate["dimensions"]),
        )
