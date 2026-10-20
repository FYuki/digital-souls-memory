"""Recompute verdicts and aggregates before accepting evidence."""

import hashlib
import json
import math
import re
from copy import deepcopy
from pathlib import PurePosixPath

from .comparison import resolved_protocol
from .models import (
    integer,
    number,
    record,
    rows,
    string,
    unique,
    validate_candidate,
    validate_suite,
)
from .retrieval import (
    composite_score,
    eligible_memories,
    select_observations,
    validate_observations,
)
from .scoring import aggregate_run, evaluate_case, percentile, stage_latency


def validate_manifest(manifest: dict[str, object]) -> None:
    for key in ("memory_commit", "core_commit"):
        if not re.fullmatch(r"[a-f0-9]{40}", string(manifest[key])):
            raise ValueError("Commit identity required")
    for key in ("memory_dirty", "core_dirty"):
        if type(manifest[key]) is not bool:
            raise ValueError("Explicit dirty state required")
    for key in ("db_image", "llama_cpp_image"):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", string(manifest[key])):
            raise ValueError("Immutable image identity required")
    if not re.fullmatch(r"[a-zA-Z0-9_.+-]+", string(manifest["llama_cpp_build"])):
        raise ValueError("Build identity must not contain paths or logs")
    extensions = record(manifest["db_extensions"])
    if set(extensions) != {"vector", "pg_bigm", "pgroonga"}:
        raise ValueError("Three extension versions required")
    for version in extensions.values():
        if not re.fullmatch(r"[0-9][a-zA-Z0-9_.+-]*", string(version)):
            raise ValueError("Extension version required")
    if manifest["gpu_preflight_confirmed"] is not True:
        raise ValueError("GPU preflight evidence required")


def validate_report(report: dict[str, object]) -> None:
    mode = report["mode"]
    if mode not in ("fixture", "real"):
        raise ValueError("Unknown evidence mode")
    suite = record(report["suite"])
    validate_suite(suite, tuning_case_ids=set())
    protocol = record(suite["protocol"])
    cases = {string(case["id"]): case for case in rows(suite["cases"])}
    gold = {string(item["case_id"]): item for item in rows(suite["gold"])}
    candidates = rows(report["candidates"])
    unique(candidates, "id")
    for candidate in candidates:
        validate_candidate(
            candidate, require_context=protocol.get("comparison_version") == "memory-v2"
        )
    expected = {
        (string(candidate["id"]), index)
        for candidate in candidates
        for index in range(1, integer(protocol["repetitions"]) + 1)
    }
    runs = rows(report["runs"])
    actual = [(string(run["candidate_id"]), integer(run["run_index"])) for run in runs]
    if not expected or set(actual) != expected or len(actual) != len(expected):
        raise ValueError("Missing or duplicate run")
    for run in runs:
        candidate = next(c for c in candidates if c["id"] == run["candidate_id"])
        resolved = resolved_protocol(protocol, candidate)
        if resolved.get("comparison_version") == "memory-v2":
            calls = run["real_reranker_calls"]
            if type(calls) is not int or calls < 0 or (mode == "fixture" and calls != 0):
                raise ValueError("Invalid reranker call count")
            if type(run["reranker_enabled"]) is not bool or (
                "reranker" in candidate and run["reranker_enabled"] is not True
            ):
                raise ValueError("Reranker configuration mismatch")
        if "resources" in run:
            validate_resources(record(run["resources"]))
        if mode == "fixture" and (
            run["quality_passed"] is not None or run["real_model_calls"] != 0
        ):
            raise ValueError("Fixture cannot certify real quality")
        if run["execution_error"] is not None:
            if run["execution_error"] != "execution_error" or run["quality_passed"] is not None:
                raise ValueError("Invalid execution error evidence")
            continue
        if mode == "real":
            integer(run["real_model_calls"])
        results = rows(run["case_results"])
        if {r["case_id"] for r in results} != set(cases):
            raise ValueError("Missing case result")
        for result in results:
            identifier = string(result["case_id"])
            retrieved = rows(result["retrieved_hits"])
            if resolved.get("comparison_version") == "memory-v2":
                case = cases[identifier]
                observations = rows(result["candidate_observations"])
                validate_observations(observations, case, resolved)
                if any(
                    (o["reranker_score"] is not None) is not run["reranker_enabled"]
                    for o in observations
                ):
                    raise ValueError("Reranker observation mismatch")
                if result["eligible_ids"] != sorted(
                    string(m["id"]) for m in eligible_memories(case)
                ):
                    raise ValueError("Eligibility evidence mismatch")
                expected_hits = select_observations(observations, resolved)
                score_fields = ("memory_id", "score", "components")
                if [{key: hit[key] for key in score_fields} for hit in expected_hits] != [
                    {key: hit[key] for key in score_fields} for hit in retrieved
                ]:
                    raise ValueError("Candidate to retrieval evidence mismatch")
                duration = sum(
                    number(result[k])
                    for k in ("query_embedding_ms", "search_ms", "rerank_ms", "result_selection_ms")
                )
                if not math.isclose(number(result["recall_ms"]), duration, abs_tol=2):
                    raise ValueError("Recall stage duration mismatch")
            for hit in retrieved:
                if "components" in hit and hit["score"] != composite_score(
                    hit["components"], resolved["weights"]
                ):
                    raise ValueError("Component score mismatch")
            computed = evaluate_case(cases[identifier], gold[identifier], retrieved, resolved)
            if any(result[key] != value for key, value in computed.items()):
                raise ValueError("Forged case verdict")
            for key in (
                "passage_embedding_ms",
                "index_ms",
                "query_embedding_ms",
                "search_ms",
                "scoring_ms",
                "recall_ms",
            ):
                if key in result and number(result[key]) < 0:
                    raise ValueError("Negative duration")
            if "database" in result:
                database = record(result["database"])
                if set(database) != {
                    "insertion_ms",
                    "index_creation_ms",
                    "index_bytes",
                    "table_bytes",
                    "vector_bytes",
                } or any(number(v) < 0 for v in database.values()):
                    raise ValueError("Invalid database measurement")
        summary = aggregate_run(results, protocol=resolved)
        if any(run[key] != value for key, value in summary.items()):
            raise ValueError("Forged aggregate")
        if resolved.get("comparison_version") == "memory-v2":
            if run["stage_latency"] != stage_latency(results):
                raise ValueError("Invalid stage latency summary")
        if mode == "real" and run["quality_passed"] is not summary["passed"]:
            raise ValueError("Inconsistent quality verdict")
        if "latency" in run:
            samples = [number(r["recall_ms"]) for r in results]
            p95 = percentile(samples, 0.95, method="linear")
            if run["latency"] != {
                "p50_ms": percentile(samples, 0.5, method="linear"),
                "p95_ms": p95,
                "target_p95_met": p95 <= 300,
                "required_p95_met": p95 <= 600,
            }:
                raise ValueError("Invalid latency summary")
    quality = (
        all(run["quality_passed"] is True for run in runs)
        if mode == "real" and all(run["execution_error"] is None for run in runs)
        else None
    )
    if report["quality_passed"] is not quality:
        raise ValueError("Inconsistent report verdict")


def public_report(report: dict[str, object]) -> dict[str, object]:
    validate_report(report)
    suite = record(report["suite"])
    inputs = {
        "suite": suite,
        "cases": suite["cases"],
        "gold": suite["gold"],
        "protocol": suite["protocol"],
        "candidates": report["candidates"],
    }
    hashes = {
        key: hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        for key, value in inputs.items()
    }
    runs: list[dict[str, object]] = []
    for run in rows(report["runs"]):
        results: list[dict[str, object]] = []
        for result in rows(run["case_results"]):
            hits = _public_hits(rows(result["hits"]))
            projected = {
                key: result[key]
                for key in (
                    "case_id",
                    "category",
                    "passed",
                    "threshold_passed",
                    "sources_valid",
                    "forbidden_returned",
                    "recall_at_5",
                    "precision",
                    "no_match_correct",
                    "order_passed",
                )
            }
            projected["hits"] = hits
            projected["retrieved_hits"] = _public_hits(rows(result["retrieved_hits"]))
            projected["return_budget"] = deepcopy(result["return_budget"])
            if record(suite["protocol"]).get("comparison_version") == "memory-v2":
                for key in (
                    "safety_passed",
                    "quality_passed",
                    "false_positive_count",
                    "required_ids",
                    "eligible_ids",
                    "rerank_ms",
                    "result_selection_ms",
                ):
                    projected[key] = deepcopy(result[key])
                projected["candidate_observations"] = [
                    {
                        key: deepcopy(o[key])
                        for key in ("memory_id", "cosine", "components", "score", "reranker_score")
                    }
                    for o in rows(result["candidate_observations"])
                ]
            for key in (
                "index_ms",
                "passage_embedding_ms",
                "query_embedding_ms",
                "search_ms",
                "scoring_ms",
                "recall_ms",
                "database",
            ):
                if key in result:
                    projected[key] = deepcopy(result[key])
            results.append(projected)
        projected_run = {
            key: run[key]
            for key in (
                "candidate_id",
                "run_index",
                "real_model_calls",
                "execution_error",
                "quality_passed",
            )
        }
        for key in (
            "case_count",
            "passed",
            "categories",
            "harness_cpu_ms",
            "harness_process_peak_rss_kib",
            "latency",
            "resources",
            "safety_passed",
            "score_ranges",
            "quality_gates",
            "stage_latency",
            "real_reranker_calls",
            "reranker_enabled",
        ):
            if key in run:
                projected_run[key] = deepcopy(run[key])
        projected_run["case_results"] = results
        runs.append(projected_run)
    published: dict[str, object] = {
        "mode": report["mode"],
        "created_at": report["created_at"],
        "quality_passed": report["quality_passed"],
        "input_sha256": hashes,
        "runs": runs,
        "candidates": [_public_candidate(candidate) for candidate in rows(report["candidates"])],
        "protocol": {
            key: record(suite["protocol"])[key]
            for key in (
                "threshold",
                "top_k",
                "token_budget",
                "repetitions",
                "weights",
                "percentile_method",
                "comparison_version",
            )
            if key in record(suite["protocol"])
        },
    }
    if "input_files_sha256" in report:
        file_hashes = record(report["input_files_sha256"])
        if not {"suite", "candidates"} <= set(file_hashes) or set(file_hashes) - {
            "suite",
            "candidates",
            "tuning",
            "fixture",
            "manifest",
            "tuning_report",
        }:
            raise ValueError("Unexpected input file identity")
        for digest in file_hashes.values():
            if not re.fullmatch(r"[a-f0-9]{64}", string(digest)):
                raise ValueError("Input file digest required")
        published["input_files_sha256"] = file_hashes
    if "manifest" in report:
        manifest = record(report["manifest"])
        validate_manifest(manifest)
        published["manifest"] = {
            key: manifest[key]
            for key in (
                "memory_commit",
                "core_commit",
                "memory_dirty",
                "core_dirty",
                "db_image",
                "db_extensions",
                "llama_cpp_build",
                "llama_cpp_image",
                "gpu_preflight_confirmed",
            )
        }
    return published


def _public_candidate(candidate: dict[str, object]) -> dict[str, object]:
    projected = {
        key: deepcopy(candidate[key])
        for key in (
            "id",
            "model_alias",
            "revision",
            "sha256",
            "dimensions",
            "pooling",
            "query_prefix",
            "passage_prefix",
            "extension",
            "quantization",
            "parameters",
            "max_context_length",
        )
        if key in candidate
    }
    if "reranker" in candidate:
        projected["reranker"] = {
            key: deepcopy(record(candidate["reranker"])[key])
            for key in ("model_alias", "revision", "sha256", "quantization", "max_context_length")
        }
    return projected


def _public_hits(hits: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "memory_id": hit["memory_id"],
            "score": hit["score"],
            **({"components": deepcopy(hit["components"])} if "components" in hit else {}),
            "source_spans": [
                {
                    "turn_id": span["turn_id"],
                    "start": span["start"],
                    "end": span["end"],
                    "sha256": hashlib.sha256(string(span["text"]).encode()).hexdigest(),
                }
                for span in rows(hit["source_spans"])
            ],
        }
        for hit in hits
    ]


def validate_public_report(
    report: dict[str, object], *, suite: dict[str, object], candidates: list[dict[str, object]]
) -> None:
    restored = {**deepcopy(report), "suite": suite, "candidates": candidates}
    cases = {string(case["id"]): case for case in rows(suite["cases"])}
    restored_runs: list[dict[str, object]] = []
    for run in rows(report["runs"]):
        results: list[dict[str, object]] = []
        for result in rows(run["case_results"]):
            turns = {
                string(turn["turn_id"]): turn
                for turn in rows(cases[string(result["case_id"])]["turns"])
            }

            restored_result = {
                **result,
                "hits": [_restore_hit(hit, turns) for hit in rows(result["hits"])],
            }
            restored_result["retrieved_hits"] = [
                _restore_hit(hit, turns) for hit in rows(result["retrieved_hits"])
            ]
            results.append(restored_result)
        restored_runs.append({**run, "case_results": results})
    restored["runs"] = restored_runs
    computed = public_report(restored)
    if computed != report:
        raise ValueError("Public evidence mismatch")


def _restore_hit(hit: dict[str, object], turns: dict[str, dict[str, object]]) -> dict[str, object]:
    spans = []
    for span in rows(hit["source_spans"]):
        turn = turns[string(span["turn_id"])]
        start, end = span["start"], span["end"]
        if type(start) is not int or type(end) is not int:
            raise ValueError("Invalid source offsets")
        text = string(turn["text"])[start:end]
        if hashlib.sha256(text.encode()).hexdigest() != span["sha256"]:
            raise ValueError("Source digest mismatch")
        spans.append(
            {
                "turn_id": span["turn_id"],
                "start": start,
                "end": end,
                "text": text,
                "actor": turn["actor"],
                "language": turn["language"],
            }
        )
    return {**hit, "source_spans": spans}


def validate_resources(evidence: dict[str, object]) -> None:
    if (
        set(evidence) != {"sample_interval_ms", "samples", "db_cpu_ms", "model_cpu_ms"}
        or evidence["sample_interval_ms"] != 100
    ):
        raise ValueError("Explicit resource observation scope required")
    samples = rows(evidence["samples"])
    if len(samples) < 2:
        raise ValueError("Before and after resource samples required")
    previous = -1.0
    for sample in samples:
        if set(sample) != {
            "elapsed_ms",
            "db_cpu_ns",
            "model_cpu_ns",
            "db_ram_bytes",
            "model_ram_bytes",
            "gpu_total_used_mib",
        } or any(number(v) < 0 for v in sample.values()):
            raise ValueError("Invalid resource sample")
        if number(sample["elapsed_ms"]) < previous:
            raise ValueError("Resource sample order mismatch")
        previous = number(sample["elapsed_ms"])
    for role in ("db", "model"):
        computed = (
            number(samples[-1][role + "_cpu_ns"]) - number(samples[0][role + "_cpu_ns"])
        ) / 1e6
        if computed < 0 or evidence[role + "_cpu_ms"] != computed:
            raise ValueError("Resource counter mismatch")


def artifact_summary(payload: bytes, reference: str) -> dict[str, object]:
    path = PurePosixPath(reference)
    if (
        len(path.parts) < 3
        or path.parts[0] != "eval-artifacts"
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.parts[1])
        or ".." in path.parts
        or path.is_absolute()
        or str(path) != reference
        or "\\" in reference
    ):
        raise ValueError("Relative dated artifact reference required")
    report = record(json.loads(payload))
    return {
        "artifact": {
            "reference": reference,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        },
        "mode": report["mode"],
        "quality_passed": report["quality_passed"],
        "input_sha256": report["input_sha256"],
        "runs": [
            {
                key: deepcopy(run[key])
                for key in (
                    "candidate_id",
                    "run_index",
                    "execution_error",
                    "quality_passed",
                    "passed",
                    "safety_passed",
                    "categories",
                    "score_ranges",
                    "quality_gates",
                    "latency",
                    "stage_latency",
                    "real_model_calls",
                    "real_reranker_calls",
                )
                if key in run
            }
            for run in rows(report["runs"])
        ],
    }
