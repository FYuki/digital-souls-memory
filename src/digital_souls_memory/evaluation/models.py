"""Validation of the evaluation input boundary."""

import hashlib
import ipaddress
import json
import math
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


def record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError("Expected an object")
    return {k: v for k, v in value.items()}


def rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("Expected an object array")
    return [record(item) for item in value]


def string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected a nonempty string")
    return value


def strings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Expected a string array")
    result = [string(item) for item in value]
    if len(set(result)) != len(result):
        raise ValueError("Duplicate identifiers")
    return result


def number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Expected a finite number")
    return float(value)


def integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Expected a positive integer")
    return value


def unit(value: object) -> float:
    result = number(value)
    if not 0 <= result <= 1:
        raise ValueError("Expected a number in [0,1]")
    return result


def timestamp(value: object) -> datetime:
    result = datetime.fromisoformat(string(value))
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result


def unique(items: list[dict[str, object]], key: str) -> set[str]:
    identifiers = [string(item[key]) for item in items]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Duplicate identifiers")
    return set(identifiers)


COMPONENTS = frozenset(("semantic", "lexical", "recency", "importance", "self_relevance", "trust"))
COMPARISON_VERSION = "memory-v1"
SEMANTIC_PERCENTAGES = (55, 65, 75)
THRESHOLD_PERCENTAGES = tuple(range(20, 91, 5))
SEMANTIC_LEXICAL_PERCENTAGE = 80
COMPARISON_ATTRIBUTE_WEIGHT = 0.05


def validate_candidate(candidate: dict[str, object]) -> None:
    for field in ("id", "model_alias", "revision", "pooling"):
        string(candidate[field])
    if not re.fullmatch(r"[a-f0-9]{64}", string(candidate["sha256"])):
        raise ValueError("Model SHA-256 required")
    integer(candidate["dimensions"])
    for field in ("query_prefix", "passage_prefix"):
        if not isinstance(candidate[field], str):
            raise ValueError("Explicit prefix required")
    if candidate["extension"] not in ("pg_bigm", "pgroonga"):
        raise ValueError("Unknown full text extension")
    if candidate["model_alias"] == "bge-m3":
        required: dict[str, object] = {
            "dimensions": 1024,
            "pooling": "cls",
            "quantization": "Q8_0",
            "query_prefix": "",
            "passage_prefix": "",
            "revision": "9eba04c5d75ba5a1595e45de734d36bef4e5cb98",
            "sha256": "aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173",
        }
        if any(candidate.get(field) != value for field, value in required.items()):
            raise ValueError("bge-m3 baseline mismatch")
    if candidate["model_alias"] == "multilingual-e5-large":
        required_e5: dict[str, object] = {
            "dimensions": 1024,
            "pooling": "mean",
            "quantization": "Q8_0",
            "query_prefix": "query: ",
            "passage_prefix": "passage: ",
            "revision": "272a0975bd1a67886c0719767af84dfd2cbd9aa4",
            "sha256": "6fc086a43fa9f9a766ee664312ecf2f301ff24c89f866c64936e5b4c5099b3be",
        }
        if any(candidate.get(field) != value for field, value in required_e5.items()):
            raise ValueError("e5 baseline mismatch")


def validated_weights(value: object) -> dict[str, float]:
    weights = record(value)
    if set(weights) != COMPONENTS:
        raise ValueError("Six weights required")
    values = {key: unit(value) for key, value in weights.items()}
    if not math.isclose(sum(values.values()), 1) or values["recency"] > 0.1:
        raise ValueError("Invalid weight sum or recency")
    return values


def validate_protocol(protocol: dict[str, object]) -> None:
    validated_weights(protocol["weights"])
    unit(protocol["threshold"])
    if integer(protocol["top_k"]) > 5 or integer(protocol["token_budget"]) > 1500:
        raise ValueError("Return budget exceeded")
    if integer(protocol["repetitions"]) != 3 or protocol["percentile_method"] != "linear":
        raise ValueError("Three repetitions and linear percentiles required")
    if "comparison_version" in protocol:
        weights = validated_weights(protocol["weights"])
        if (
            protocol["comparison_version"] != COMPARISON_VERSION
            or protocol["top_k"] != 5
            or protocol["token_budget"] != 1500
            or protocol["threshold"] not in [i / 100 for i in THRESHOLD_PERCENTAGES]
            or weights["semantic"] not in [i / 100 for i in SEMANTIC_PERCENTAGES]
            or not math.isclose(
                weights["lexical"], SEMANTIC_LEXICAL_PERCENTAGE / 100 - weights["semantic"]
            )
            or any(
                weights[key] != COMPARISON_ATTRIBUTE_WEIGHT
                for key in COMPONENTS - {"semantic", "lexical"}
            )
        ):
            raise ValueError("Fixed comparison rules mismatch")


def validate_case(case: dict[str, object]) -> None:
    for field in ("id", "category", "character_id", "query"):
        string(case[field])
    timestamp(case["as_of"])
    sessions = rows(case["sessions"])
    session_ids = unique(sessions, "session_id")
    for session in sessions:
        strings(session["participants"])
    turns = rows(case["turns"])
    unique(turns, "turn_id")
    for turn in turns:
        if turn["session_id"] not in session_ids:
            raise ValueError("Unknown session")
        for field in ("actor", "text", "language", "classifier_version"):
            string(turn[field])
        if turn["sensitivity"] not in ("sensitive", "non_sensitive"):
            raise ValueError("Explicit sensitivity required")
        if turn["kind"] not in ("utterance", "observation"):
            raise ValueError("Unknown turn kind")
        strings(turn["audience"])
        timestamp(turn["occurred_at"])
        timestamp(turn["recorded_at"])
    memories = rows(case["memories"])
    unique(memories, "id")
    for memory in memories:
        string(memory["content"])
        string(memory["kind"])
        if not strings(memory["source_turn_ids"]):
            raise ValueError("Source required")
        derivation = record(memory["derivation"])
        string(derivation["model"])
        string(derivation["prompt_version"])
        start = timestamp(memory["valid_from"])
        if memory["valid_to"] is not None and timestamp(memory["valid_to"]) < start:
            raise ValueError("Invalid validity interval")
        timestamp(memory["last_reinforced_at"])
        for field in ("importance", "self_relevance", "trust"):
            unit(memory[field])


def validate_suite(suite: dict[str, object], *, tuning_case_ids: set[str]) -> None:
    cases, gold = rows(suite["cases"]), rows(suite["gold"])
    ids = unique(cases, "id")
    if not ids or ids & tuning_case_ids or unique(gold, "case_id") != ids:
        raise ValueError("Empty, mixed, or incomplete evaluation set")
    if suite["split"] not in ("acceptance", "tuning"):
        raise ValueError("Unknown split")
    validate_protocol(record(suite["protocol"]))
    by_id = {string(case["id"]): case for case in cases}
    for case in cases:
        validate_case(case)
    for expected in gold:
        known = unique(rows(by_id[string(expected["case_id"])]["memories"]), "id")
        required, forbidden = strings(expected["required_ids"]), strings(expected["forbidden_ids"])
        if not (set(required) | set(forbidden)) <= known or set(required) & set(forbidden):
            raise ValueError("Invalid gold identifiers")
        if "required_order" in expected and set(strings(expected["required_order"])) != set(
            required
        ):
            raise ValueError("Expected order must identify exactly the required memories")


def load_suite(path: Path, *, expected_sha256: str) -> dict[str, object]:
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("Input digest mismatch")
    suite = record(json.loads(payload))
    validate_suite(suite, tuning_case_ids=set())
    return suite


def validate_case_mapping(
    mapping: list[dict[str, object]], *, source_ids: set[str], target_ids: set[str]
) -> None:
    observed = [
        string(row["source_split"]) + ":" + string(row["source_case_id"]) for row in mapping
    ]
    if set(observed) != source_ids or len(observed) != len(source_ids):
        raise ValueError("Incomplete or duplicate mapping")
    for row in mapping:
        string(row["reason"])
        if row["target_case_id"] is None:
            string(row["replacement_test"])
        elif row["target_case_id"] not in target_ids:
            raise ValueError("Unknown target case")


def validate_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Only plain loopback HTTP endpoints allowed")
    address = ipaddress.ip_address(string(parsed.hostname))
    if not address.is_loopback or parsed.path not in ("", "/"):
        raise ValueError("Only numeric loopback endpoints allowed")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid port")


def validate_vectors(
    value: object, *, count: int, dimensions: int
) -> tuple[tuple[float, ...], ...]:
    if not isinstance(value, (list, tuple)) or len(value) != count or dimensions < 1:
        raise ValueError("Vector count mismatch")
    result = []
    for vector in value:
        if not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
            raise ValueError("Vector dimension mismatch")
        converted = tuple(number(component) for component in vector)
        if not any(converted):
            raise ValueError("Zero vector")
        result.append(converted)
    return tuple(result)
