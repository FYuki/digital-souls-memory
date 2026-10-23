"""Construct explicitly configured local comparison ports."""

from copy import deepcopy
from ipaddress import ip_address

from psycopg.conninfo import conninfo_to_dict

from ..models import record, string, validate_candidate, validate_endpoint, validate_reranker
from ..ports import EmbeddingFactory, RerankerFactory, SearchFactory, TokenCounterFactory
from .llamacpp import LlamaCppEmbedding
from .postgres import PostgresSearch
from .reranker import LlamaCppReranker
from .token_counter import LlamaCppTokenCounter


def real_token_counter_factory(
    config: dict[str, object], candidates: list[dict[str, object]]
) -> TokenCounterFactory:
    endpoints = deepcopy(record(config["endpoints"]))
    identities = record(config["model_identities"])
    for candidate in candidates:
        validate_candidate(candidate)
        alias = string(candidate["model_alias"])
        validate_endpoint(string(endpoints[alias]))
        observed = record(identities[alias])
        fields = (
            "model_alias",
            "sha256",
            "revision",
            "dimensions",
            "pooling",
            "query_prefix",
            "passage_prefix",
            "quantization",
            "max_context_length",
        )
        if any(candidate.get(k) != observed.get(k) for k in fields):
            raise ValueError("Tokenizer identity mismatch")

    def counter(candidate: dict[str, object]) -> LlamaCppTokenCounter:
        return LlamaCppTokenCounter(
            candidate, endpoint=string(endpoints[string(candidate["model_alias"])])
        )

    return counter


def real_factories(
    config: dict[str, object], candidates: list[dict[str, object]]
) -> tuple[EmbeddingFactory, SearchFactory]:
    admin_url = string(config["admin_url"])
    connection = conninfo_to_dict(admin_url)
    if connection.get("dbname") != "memory_synthetic":
        raise ValueError("Explicit synthetic administration database required")
    host = string(connection.get("host"))
    if not host.startswith("/") and not ip_address(host).is_loopback:
        raise ValueError("Local synthetic database required")
    if "hostaddr" in connection and not ip_address(string(connection["hostaddr"])).is_loopback:
        raise ValueError("Local synthetic database address required")
    endpoints = record(config["endpoints"])
    identities = record(config["model_identities"])
    for candidate in candidates:
        validate_candidate(candidate)
        alias = string(candidate["model_alias"])
        validate_endpoint(string(endpoints[alias]))
        observed = record(identities[alias])
        fields = [
            "model_alias",
            "sha256",
            "revision",
            "dimensions",
            "pooling",
            "query_prefix",
            "passage_prefix",
        ]
        if "max_context_length" in candidate:
            fields.extend(("quantization", "max_context_length"))
        if any(candidate[key] != observed[key] for key in fields):
            raise ValueError("Preflight identity does not match candidate")
    endpoints = deepcopy(endpoints)

    def embedding(candidate: dict[str, object]) -> LlamaCppEmbedding:
        return LlamaCppEmbedding(
            candidate, endpoint=string(endpoints[string(candidate["model_alias"])])
        )

    def search(candidate: dict[str, object]) -> PostgresSearch:
        return PostgresSearch(candidate, admin_url=admin_url)

    return embedding, search


def real_reranker_factory(
    config: dict[str, object], candidates: list[dict[str, object]]
) -> RerankerFactory | None:
    enabled = ["reranker" in c for c in candidates]
    if not any(enabled):
        return None
    if not all(enabled):
        raise ValueError("Run enabled and disabled reranker comparisons separately")
    endpoints = record(config["reranker_endpoints"])
    identities = record(config["reranker_identities"])
    for candidate in candidates:
        definition = record(candidate["reranker"])
        validate_reranker(definition)
        alias = string(definition["model_alias"])
        validate_endpoint(string(endpoints[alias]))
        observed = record(identities[alias])
        if any(
            observed[k] != definition[k]
            for k in ("model_alias", "revision", "sha256", "quantization", "max_context_length")
        ):
            raise ValueError("Reranker identity mismatch")
    endpoints = deepcopy(endpoints)

    def reranker(candidate: dict[str, object]) -> LlamaCppReranker:
        definition = record(candidate["reranker"])
        return LlamaCppReranker(
            definition, endpoint=string(endpoints[string(definition["model_alias"])])
        )

    return reranker
