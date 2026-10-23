"""Reference rescoring of complete saved v1 tuning observations; no model calls."""

import argparse
import hashlib
import json
from pathlib import Path

from .comparison import resolved_protocol, selected_candidates
from .models import load_suite, record, rows, string, unique, validate_suite
from .report import _public_hits, _restore_hit, validate_public_report
from .retrieval import composite_score, eligible_memories, rank_results, validate_observations
from .scoring import aggregate_run, evaluate_case


def rescore_report(
    payload: bytes,
    *,
    expected_sha256: str,
    source_suite: dict[str, object],
    target_suite: dict[str, object],
    candidates: list[dict[str, object]],
) -> dict[str, object]:
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("Source digest mismatch")
    if (
        source_suite["split"] != "tuning"
        or target_suite["split"] != "tuning"
        or record(source_suite["protocol"]).get("comparison_version") != "memory-v1"
        or record(target_suite["protocol"]).get("comparison_version") != "memory-v2"
        or source_suite["cases"] != target_suite["cases"]
    ):
        raise ValueError("Same cases and explicit tuning versions required")
    validate_suite(target_suite, tuning_case_ids=set())
    source = record(json.loads(payload))
    validate_public_report(source, suite=source_suite, candidates=candidates)
    old_gold = {g["case_id"]: g for g in rows(source_suite["gold"])}
    gold = {g["case_id"]: g for g in rows(target_suite["gold"])}
    for identifier, expected in gold.items():
        for key in ("required_ids", "forbidden_ids", "required_source_spans", "required_order"):
            if expected.get(key) != old_gold[identifier].get(key):
                raise ValueError("Gold meaning must be retained")
    by_id = {c["id"]: c for c in candidates}
    source_runs = rows(source["runs"])
    cases = {c["id"]: c for c in rows(target_suite["cases"])}
    runs: list[dict[str, object]] = []
    for candidate in candidates:
        target_protocol = resolved_protocol(record(target_suite["protocol"]), candidate)
        # A low-threshold observation is usable only after checking every eligible ID.
        options = [
            c
            for c in candidates
            if {k: v for k, v in c.items() if k not in ("id", "parameters")}
            == {k: v for k, v in candidate.items() if k not in ("id", "parameters")}
            and resolved_protocol(record(source_suite["protocol"]), c)["threshold"] == 0.2
            and record(resolved_protocol(record(source_suite["protocol"]), c)["weights"])
            == record(target_protocol["weights"])
        ]
        if not options:
            raise ValueError("Complete low-threshold source required")
        source_candidate = options[0]
        for index in range(1, 4):
            source_run = next(
                r
                for r in source_runs
                if r["candidate_id"] == source_candidate["id"] and r["run_index"] == index
            )
            if source_run["execution_error"] is not None:
                raise ValueError("Failed source run cannot be rescored")
            results = []
            for saved in rows(source_run["case_results"]):
                case = cases[saved["case_id"]]
                turns = {string(t["turn_id"]): t for t in rows(case["turns"])}
                restored = [_restore_hit(h, turns) for h in rows(saved["retrieved_hits"])]
                if unique(restored, "memory_id") != {m["id"] for m in eligible_memories(case)}:
                    raise ValueError("Source omitted eligible candidates")
                observations = [
                    {
                        "memory_id": h["memory_id"],
                        "cosine": None,
                        "components": h["components"],
                        "score": composite_score(h["components"], target_protocol["weights"]),
                        "reranker_score": None,
                    }
                    for h in restored
                ]
                validate_observations(
                    observations, case, target_protocol, allow_unmeasured_cosine=True
                )
                hits = rank_results(restored, target_protocol)
                result = evaluate_case(case, gold[case["id"]], hits, target_protocol)
                result["candidate_observations"] = observations
                result["hits"] = _public_hits(rows(result["hits"]))
                result["retrieved_hits"] = _public_hits(rows(result["retrieved_hits"]))
                results.append(result)
            runs.append(
                {
                    "candidate_id": candidate["id"],
                    "run_index": index,
                    "real_model_calls": 0,
                    "execution_error": None,
                    "case_results": results,
                    **aggregate_run(results, protocol=target_protocol),
                }
            )
    return {
        "source_sha256": expected_sha256,
        "new_measurement": False,
        "acceptance_measured": False,
        "protocol": target_suite["protocol"],
        "suite": {"split": "tuning", "protocol": target_suite["protocol"]},
        "candidates": list(by_id.values()),
        "runs": runs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="保存済みv1 tuningの参考再採点")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--source-suite", type=Path, required=True)
    parser.add_argument("--source-suite-sha256", required=True)
    parser.add_argument("--target-suite", type=Path, required=True)
    parser.add_argument("--target-suite-sha256", required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = rescore_report(
        args.source.read_bytes(),
        expected_sha256=args.source_sha256,
        source_suite=load_suite(args.source_suite, expected_sha256=args.source_suite_sha256),
        target_suite=load_suite(args.target_suite, expected_sha256=args.target_suite_sha256),
        candidates=rows(json.loads(args.candidates.read_bytes())),
    )
    bases = [
        {k: v for k, v in c.items() if k != "parameters"}
        for c in rows(result["candidates"])
        if string(c["id"]).endswith(".p00")
    ]
    if bases:
        bases = [{**c, "id": string(c["id"])[:-4]} for c in bases]
        result["selected_candidates"] = selected_candidates(result, bases)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
