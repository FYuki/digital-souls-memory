"""Construct explicitly configured local comparison ports."""

from copy import deepcopy
from ipaddress import ip_address

from psycopg.conninfo import conninfo_to_dict

from ..models import record, string, validate_candidate, validate_endpoint
from ..ports import EmbeddingFactory, SearchFactory
from .llamacpp import LlamaCppEmbedding
from .postgres import PostgresSearch


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
        if any(
            candidate[key] != observed[key]
            for key in (
                "model_alias",
                "sha256",
                "revision",
                "dimensions",
                "pooling",
                "query_prefix",
                "passage_prefix",
            )
        ):
            raise ValueError("Preflight identity does not match candidate")
    endpoints = deepcopy(endpoints)

    def embedding(candidate: dict[str, object]) -> LlamaCppEmbedding:
        return LlamaCppEmbedding(
            candidate, endpoint=string(endpoints[string(candidate["model_alias"])])
        )

    def search(candidate: dict[str, object]) -> PostgresSearch:
        return PostgresSearch(candidate, admin_url=admin_url)

    return embedding, search
