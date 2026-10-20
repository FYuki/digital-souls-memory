import json
from collections.abc import Callable
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

from digital_souls_memory.evaluation.adapters.fixture import FixtureEmbedding, FixtureSearch
from digital_souls_memory.evaluation.comparison import comparison_protocol
from digital_souls_memory.evaluation.report import public_report, validate_public_report
from digital_souls_memory.evaluation.runner import run_evaluation

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("fault", [None, "digest", "candidate-missing", "source-verdict"])
def test_saved_v1_tuning_is_validated_before_reference_rescoring(
    make_suite: Callable[[], dict[str, object]],
    v2_suite: dict[str, object],
    v2_fixture: dict[str, object],
    candidate: dict[str, object],
    fault: str | None,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
    tmp_path: Path,
) -> None:
    source_suite = deepcopy(make_suite())
    source_suite["split"] = "tuning"
    source_suite["protocol"] = comparison_protocol()
    target_suite = {**deepcopy(v2_suite), "split": "tuning"}
    for suite in (source_suite, target_suite):
        case = cast(list[dict[str, object]], suite["cases"])[0]
        memory = cast(list[dict[str, object]], case["memories"])[0]
        case["memories"] = [{**memory, "id": f"m{i}"} for i in range(1, 4)]
        cast(list[dict[str, object]], suite["gold"])[0]["forbidden_ids"] = ["m2"]
    cast(list[dict[str, object]], target_suite["gold"])[0]["quality_forbidden_ids"] = ["m2"]
    observations = cast(dict[str, object], v2_fixture["observations"])
    original_observation = cast(list[dict[str, object]], observations["case-a"])[0]
    observations["case-a"] = [
        {**deepcopy(original_observation), "memory_id": f"m{i}"} for i in range(1, 4)
    ]

    def embedding(settings: dict[str, object]) -> FixtureEmbedding:
        return FixtureEmbedding(settings, v2_fixture)

    def search(settings: dict[str, object]) -> FixtureSearch:
        return FixtureSearch(v2_fixture)

    original = public_report(
        run_evaluation(
            source_suite,
            [candidate],
            mode="fixture",
            embedding_factory=embedding,
            search_factory=search,
        )
    )
    validate_public_report(original, suite=source_suite, candidates=[candidate])
    before = deepcopy(original)
    assert all(run["passed"] is False for run in cast(list[dict[str, object]], original["runs"]))
    if fault == "candidate-missing":
        cast(
            list[dict[str, object]],
            cast(list[dict[str, object]], original["runs"])[0]["case_results"],
        )[0]["retrieved_hits"] = []
    elif fault == "source-verdict":
        cast(list[dict[str, object]], original["runs"])[0]["passed"] = True
    payload = json.dumps(original, sort_keys=True).encode()
    if fault != "digest":
        digest = sha256(payload).hexdigest()
    else:
        digest = "0" * 64
    from digital_souls_memory.evaluation.rescore import main, rescore_report

    def rescore() -> dict[str, object]:
        return rescore_report(
            payload,
            expected_sha256=digest,
            source_suite=source_suite,
            target_suite=target_suite,
            candidates=[candidate],
        )

    if fault is not None:
        with rejected():
            rescore()
        return
    result = rescore()
    assert original == before
    assert result["source_sha256"] == digest
    assert result["new_measurement"] is False
    assert result["acceptance_measured"] is False
    assert cast(dict[str, object], result["protocol"])["comparison_version"] == "memory-v2"
    for run in cast(list[dict[str, object]], result["runs"]):
        assert run["real_model_calls"] == 0
        case_result = cast(list[dict[str, object]], run["case_results"])[0]
        assert case_result["passed"] is True
        assert case_result["safety_passed"] is True
        assert case_result["precision"] == pytest.approx(1 / 3)
        assert case_result["false_positive_count"] == 2
        assert (
            cast(dict[str, object], cast(dict[str, object], run["categories"])["detail"])[
                "false_positive_count"
            ]
            == 2
        )
        observed = cast(list[dict[str, object]], case_result["candidate_observations"])
        assert {o["memory_id"] for o in observed} == {"m1", "m2", "m3"}
        assert all(o["cosine"] is None and o["reranker_score"] is None for o in observed)
    for name, value in (
        ("source", original),
        ("source-suite", source_suite),
        ("target-suite", target_suite),
        ("candidates", [candidate]),
    ):
        (tmp_path / f"{name}.json").write_text(json.dumps(value, sort_keys=True))
    arguments: list[str] = []
    for name in ("source", "source-suite", "target-suite"):
        path = tmp_path / f"{name}.json"
        arguments.extend(
            (f"--{name}", str(path), f"--{name}-sha256", sha256(path.read_bytes()).hexdigest())
        )
    output = tmp_path / "reference.json"
    assert (
        main(
            [
                *arguments,
                "--candidates",
                str(tmp_path / "candidates.json"),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    reference = cast(dict[str, object], json.loads(output.read_bytes()))
    assert reference["new_measurement"] is False
    for run in cast(list[dict[str, object]], reference["runs"]):
        for row in cast(list[dict[str, object]], run["case_results"]):
            for hit in cast(list[dict[str, object]], row["hits"]):
                assert all(
                    set(span) == {"turn_id", "start", "end", "sha256"}
                    for span in cast(list[dict[str, object]], hit["source_spans"])
                )
