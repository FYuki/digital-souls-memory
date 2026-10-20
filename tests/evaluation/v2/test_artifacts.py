import json
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

from digital_souls_memory.evaluation.models import validate_candidate
from digital_souls_memory.evaluation.report import artifact_summary

pytestmark = pytest.mark.ut


def test_external_summary_uses_relative_reference_and_exact_byte_hash() -> None:
    payload = json.dumps(
        {
            "mode": "fixture",
            "quality_passed": None,
            "input_sha256": {},
            "runs": [
                {
                    "candidate_id": "synthetic",
                    "run_index": 1,
                    "case_results": [{"query": "private"}],
                }
            ],
        }
    ).encode()
    result = artifact_summary(payload, "eval-artifacts/2026-10-20/report.json")
    assert result["artifact"] == {
        "reference": "eval-artifacts/2026-10-20/report.json",
        "sha256": sha256(payload).hexdigest(),
        "bytes": len(payload),
    }
    assert "private" not in json.dumps(result)
    assert artifact_summary(payload + b" ", "eval-artifacts/2026-10-20/report.json") != result


@pytest.mark.parametrize(
    "reference",
    [
        "/tmp/report.json",
        "../report.json",
        "eval-artifacts/date/report.json",
        "eval-artifacts/2026-10-20/../report.json",
        "eval-artifacts/2026-10-20//report.json",
    ],
)
def test_external_summary_rejects_noncanonical_reference(reference: str) -> None:
    with pytest.raises(ValueError):
        artifact_summary(b"{}", reference)


def test_additional_templates_remain_explicitly_unresolved_until_parent_confirmation() -> None:
    path = (
        Path(__file__).resolve().parents[3] / "evals/retrieval/memory-v2/candidate-templates.json"
    )
    templates = cast(list[dict[str, object]], json.loads(path.read_bytes()))
    for candidate in templates:
        if candidate["model_alias"] == "bge-m3":
            validate_candidate(
                {key: value for key, value in candidate.items() if key != "max_context_length"}
            )
            assert candidate["max_context_length"] is None
            with pytest.raises(ValueError):
                validate_candidate(candidate, require_context=True)
        else:
            assert all(
                candidate[field] is None
                for field in (
                    "revision",
                    "sha256",
                    "dimensions",
                    "pooling",
                    "quantization",
                    "query_prefix",
                    "passage_prefix",
                    "max_context_length",
                )
            )
            with pytest.raises((ValueError, TypeError)):
                validate_candidate(candidate)
