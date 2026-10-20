import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

import pytest

from digital_souls_memory.evaluation.cli import main

pytestmark = pytest.mark.it1


def test_command_builds_explicit_fixture_ports_without_using_gold(
    tmp_path: Path,
    make_suite: Callable[[], dict[str, object]],
    candidate: dict[str, object],
    case: dict[str, object],
    memory: dict[str, object],
    gold: dict[str, object],
) -> None:
    fixture = {
        "vectors": {
            "passage": {str(memory["content"]): [1.0, 0.0]},
            "query": {str(case["query"]): [1.0, 0.0]},
        },
        "observations": {str(case["id"]): [{"memory_id": "m1", "score": 0.55}]},
    }
    suite_path, candidates_path, fixture_path = [
        tmp_path / name for name in ("suite.json", "candidates.json", "fixture.json")
    ]
    candidates_path.write_text(json.dumps([candidate]))
    fixture_path.write_text(json.dumps(fixture))
    for required in (True, False):
        gold["required_ids"] = ["m1"] if required else []
        suite_path.write_text(json.dumps(make_suite()))
        output = tmp_path / f"report-{required}.json"
        assert (
            main(
                [
                    "--suite",
                    str(suite_path),
                    "--candidates",
                    str(candidates_path),
                    "--fixture",
                    str(fixture_path),
                    "--mode",
                    "fixture",
                    "--output",
                    str(output),
                ]
            )
            == 0
        )
        report = json.loads(output.read_text())
        assert report["input_files_sha256"] == {
            "suite": sha256(suite_path.read_bytes()).hexdigest(),
            "candidates": sha256(candidates_path.read_bytes()).hexdigest(),
            "fixture": sha256(fixture_path.read_bytes()).hexdigest(),
        }
        for run in report["runs"]:
            assert run["case_results"][0]["hits"][0]["memory_id"] == "m1"
            assert run["case_results"][0]["passed"] is required
            assert run["real_model_calls"] == 0
        assert report["quality_passed"] is None


def test_real_command_cannot_invent_a_pinned_input_hash(
    tmp_path: Path,
    make_suite: Callable[[], dict[str, object]],
    candidate: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    suite = tmp_path / "private-suite.json"
    candidates = tmp_path / "candidates.json"
    output = tmp_path / "report.json"
    suite.write_text(json.dumps(make_suite()))
    candidates.write_text(json.dumps([candidate]))
    assert (
        main(
            [
                "--suite",
                str(suite),
                "--candidates",
                str(candidates),
                "--mode",
                "real",
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert not output.exists()
    assert str(suite) not in capsys.readouterr().err
