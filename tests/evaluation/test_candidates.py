import json
from collections.abc import Callable
from pathlib import Path

import pytest

from digital_souls_memory.evaluation.models import validate_candidate

pytestmark = pytest.mark.ut


def test_candidate_contains_explicit_model_and_search_conditions(
    candidate: dict[str, object],
) -> None:
    validate_candidate(candidate)


@pytest.mark.parametrize(
    "field", ["revision", "sha256", "pooling", "query_prefix", "passage_prefix"]
)
def test_candidate_cannot_omit_identity_or_prefixes(
    candidate: dict[str, object],
    field: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    del candidate[field]
    with rejected():
        validate_candidate(candidate)


@pytest.mark.parametrize(
    "field,value",
    [
        ("dimensions", True),
        ("dimensions", 0),
        ("dimensions", 2.5),
        ("sha256", "latest"),
        ("pooling", ""),
        ("extension", "other"),
    ],
)
def test_invalid_candidate_conditions_are_rejected(
    candidate: dict[str, object],
    field: str,
    value: object,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    candidate[field] = value
    with rejected():
        validate_candidate(candidate)


def test_bge_conditions_must_match_the_required_baseline(
    candidate: dict[str, object],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    candidate["model_alias"] = "bge-m3"
    with rejected():
        validate_candidate(candidate)


def test_fixed_comparison_candidates_validate_both_models_and_prefix_conditions() -> None:
    candidates = json.loads(
        (Path(__file__).resolve().parents[2] / "evals/retrieval/candidates.json").read_text()
    )
    assert len(candidates) == 4
    for candidate in candidates:
        validate_candidate(candidate)
    e5 = next(c for c in candidates if c["model_alias"] == "multilingual-e5-large")
    e5["query_prefix"] = "query:"
    with pytest.raises(ValueError):
        validate_candidate(e5)
