"""Explicit llama.cpp tokenizer boundary; transport failures remain failures."""

from typing import Self

import httpx

from ..models import record, string, validate_candidate, validate_endpoint


class LlamaCppTokenCounter:
    def __init__(
        self,
        candidate: dict[str, object],
        *,
        endpoint: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        validate_endpoint(endpoint)
        validate_candidate(candidate)
        self.method = (
            "llama.cpp/tokenize:"
            + string(candidate["model_alias"])
            + ":"
            + string(candidate["sha256"])
            + ":"
            + string(candidate["revision"])
        )
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

    def count(self, text: str) -> int:
        response = self.client.post("/tokenize", json={"content": text, "add_special": False})
        response.raise_for_status()
        tokens = record(response.json())["tokens"]
        if not isinstance(tokens, list) or any(type(t) is not int or t < 0 for t in tokens):
            raise ValueError("Integer tokenizer IDs required")
        return len(tokens)
