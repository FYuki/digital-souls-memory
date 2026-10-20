import json
from collections.abc import Callable
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from digital_souls_memory.evaluation.models import load_suite, validate_suite

pytestmark = pytest.mark.ut


def test_valid_suite_has_separate_gold(make_suite: Callable[[], dict[str, object]]) -> None:
    suite = make_suite()
    before = deepcopy(suite)
    validate_suite(suite, tuning_case_ids={"tuning-only"})
    assert suite == before


def test_unknown_gold_memory_id_is_rejected(
    make_suite: Callable[[], dict[str, object]],
    gold: dict[str, object],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    gold["required_ids"] = ["unknown-memory"]
    with rejected():
        validate_suite(make_suite(), tuning_case_ids=set())


@pytest.mark.parametrize("field", ["cases", "gold"])
def test_empty_evaluation_inputs_are_rejected(
    make_suite: Callable[[], dict[str, object]],
    field: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    suite = make_suite()
    suite[field] = []
    with rejected():
        validate_suite(suite, tuning_case_ids=set())


def test_tuning_and_acceptance_overlap_is_rejected(
    make_suite: Callable[[], dict[str, object]],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    with rejected():
        validate_suite(make_suite(), tuning_case_ids={"case-a"})


@pytest.mark.parametrize("fault", ["unknown", "missing", "duplicate"])
def test_gold_must_cover_exactly_the_evaluation_cases(
    make_suite: Callable[[], dict[str, object]],
    gold: dict[str, object],
    fault: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    suite = make_suite()
    if fault == "unknown":
        suite["gold"] = [{**gold, "case_id": "other-case"}]
    elif fault == "missing":
        suite["gold"] = []
    else:
        suite["gold"] = [gold, gold]
    with rejected():
        validate_suite(suite, tuning_case_ids=set())


def test_loading_fixed_suite_checks_content_hash(
    tmp_path: Path,
    make_suite: Callable[[], dict[str, object]],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    path = tmp_path / "suite.json"
    payload = json.dumps(make_suite(), ensure_ascii=False).encode()
    path.write_bytes(payload)
    expected = sha256(payload).hexdigest()
    loaded = load_suite(path, expected_sha256=expected)
    assert loaded["cases"] == make_suite()["cases"]
    path.write_bytes(payload + b" ")
    with rejected():
        load_suite(path, expected_sha256=expected)
