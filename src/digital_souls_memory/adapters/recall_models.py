"""Local llama.cpp HTTP adapters; no redirects or environment proxies."""

from collections.abc import Mapping
from ipaddress import ip_address
from math import isfinite
from typing import Literal, Self

import httpx

from digital_souls_memory.evidence.provision import ProvisionUnavailable
from digital_souls_memory.recall.domain import EmbeddingBatch, RerankInput, RerankScore, VectorSpace
from digital_souls_memory.recall.inputs import ModelInput


class _LocalModel:
    def __init__(
        self,
        model: Mapping[str, object],
        *,
        endpoint: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        url = httpx.URL(endpoint)
        try:
            local = ip_address(url.host).is_loopback
        except ValueError:
            local = url.host == "localhost"
        if not local or url.scheme not in ("http", "https") or url.userinfo or url.query:
            raise ValueError("A loopback model endpoint is required")
        self.model = dict(model)
        self.client = httpx.Client(
            base_url=endpoint,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=30,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        try:
            response = self.client.post(path, json=payload)
            response.raise_for_status()
            value: object = response.json()
            if not isinstance(value, dict):
                raise ValueError("Expected model response object")
            return value
        except (httpx.HTTPError, ValueError) as error:
            raise ProvisionUnavailable() from error


class LlamaCppEmbedding(_LocalModel):
    def embed(self, value: ModelInput, *, purpose: Literal["query", "passage"]) -> EmbeddingBatch:
        body = self._post(
            "/v1/embeddings",
            {
                "model": self.model["model_alias"],
                "input": list(value.texts),
                "encoding_format": "float",
            },
        )
        try:
            if body["model"] != self.model["model_alias"]:
                raise ValueError("Wrong model")
            data = body["data"]
            if not isinstance(data, list) or len(data) != len(value.texts):
                raise ValueError("Wrong vector count")
            ordered = sorted(data, key=lambda v: v["index"])
            if [v["index"] for v in ordered] != list(range(len(value.texts))):
                raise ValueError("Wrong vector indices")
            vectors = tuple(tuple(float(x) for x in item["embedding"]) for item in ordered)
            return EmbeddingBatch(
                space=VectorSpace(
                    model=str(self.model["model_alias"]),
                    revision=str(self.model["revision"]),
                    digest=str(self.model["sha256"]),
                    dimensions=int(str(self.model["dimensions"])),
                ),
                vectors=vectors,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProvisionUnavailable() from error


class LlamaCppTokenCounter(_LocalModel):
    def count(self, value: ModelInput) -> int:
        if len(value.texts) != 1:
            raise ProvisionUnavailable()
        body = self._post("/tokenize", {"content": value.texts[0], "add_special": False})
        tokens = body.get("tokens")
        if not isinstance(tokens, list) or any(type(v) is not int for v in tokens):
            raise ProvisionUnavailable()
        return len(tokens)


class LlamaCppReranker(_LocalModel):
    def rerank(self, value: RerankInput) -> tuple[RerankScore, ...]:
        body = self._post(
            "/rerank",
            {
                "model": self.model["model_alias"],
                "query": value.query.texts[0],
                "documents": ["\n".join(v.input.texts) for v in value.documents],
                "top_n": len(value.documents),
            },
        )
        try:
            data = body["results"]
            if (
                not isinstance(data, list)
                or len(data) != len(value.documents)
                or sorted(v["index"] for v in data) != list(range(len(value.documents)))
            ):
                raise ValueError("Invalid reranker correspondence")
            result = tuple(
                RerankScore(
                    ref=value.documents[v["index"]].ref, relevance=float(v["relevance_score"])
                )
                for v in data
            )
            if any(not isfinite(v.relevance) for v in result):
                raise ValueError("Nonfinite relevance")
            return result
        except (KeyError, TypeError, ValueError, IndexError) as error:
            raise ProvisionUnavailable() from error
