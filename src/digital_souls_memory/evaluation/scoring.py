"""Independent scoring of observations against separate gold."""

import math
from typing import NotRequired, TypedDict, cast

from .models import integer, number, rows, string, strings, unique, unit
from .retrieval import bounded_results, eligible_memories, source_spans


class CategorySummary(TypedDict):
    case_count: int
    passed_count: int
    recall_at_5: float | None
    precision: float
    no_match_correct_rate: float | None
    false_positive_count: NotRequired[int]


class RunSummary(TypedDict):
    case_count: int
    passed: bool
    categories: dict[str, CategorySummary]
    safety_passed: NotRequired[bool]
    score_ranges: NotRequired[dict[str, object]]
    quality_gates: NotRequired[dict[str, object]]


def score_case(
    case: dict[str, object],
    gold: dict[str, object],
    hits: list[dict[str, object]],
    *,
    threshold: object,
    protocol: dict[str, object] | None = None,
) -> dict[str, object]:
    v2 = protocol is not None and protocol.get("comparison_version") == "memory-v2"
    minimum = unit(threshold)
    known = unique(rows(case["memories"]), "id")
    ids = [string(hit["memory_id"]) for hit in hits]
    if len(set(ids)) != len(ids) or not set(ids) <= known:
        raise ValueError("Unknown or duplicate observation")
    for hit in hits:
        unit(hit["score"])
    top = hits[:5]
    returned = {string(hit["memory_id"]) for hit in top}
    required, forbidden = set(strings(gold["required_ids"])), set(strings(gold["forbidden_ids"]))
    recall = len(required & returned) / len(required) if required else None
    precision = len(required & returned) / len(top) if top else 0.0
    eligible = unique(eligible_memories(case), "id")
    sources_valid = True
    observed_spans: list[dict[str, object]] = []
    for hit in hits:
        identifier = string(hit["memory_id"])
        spans = rows(hit["source_spans"])
        if identifier not in eligible:
            sources_valid = False
            continue
        originals = source_spans(case, memory_id=identifier)
        for span in spans:
            original = next((s for s in originals if s["turn_id"] == span["turn_id"]), None)
            start, end = span["start"], span["end"]
            if (
                original is None
                or type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(string(original["text"]))
                or span["text"] != string(original["text"])[start:end]
                or span["actor"] != original["actor"]
                or span["language"] != original["language"]
            ):
                sources_valid = False
        if not spans:
            sources_valid = False
        observed_spans.extend(spans)
    required_sources_valid = True
    for needed in rows(gold.get("required_source_spans", [])):
        if not any(
            span["turn_id"] == needed["turn_id"] and string(needed["text"]) in string(span["text"])
            for span in observed_spans
        ):
            required_sources_valid = False
    if not v2:
        sources_valid = sources_valid and required_sources_valid
    threshold_passed = all(number(hit["score"]) >= minimum for hit in hits)
    no_match_correct = not returned if not required else None
    order_passed = v2 or "required_order" not in gold or ids == strings(gold["required_order"])
    passed = (
        (recall == 1.0 if required else no_match_correct is True)
        and not forbidden.intersection(ids)
        and sources_valid
        and threshold_passed
        and order_passed
    )
    result: dict[str, object] = {
        "case_id": case["id"],
        "category": case["category"],
        "hits": hits,
        "passed": passed,
        "threshold_passed": threshold_passed,
        "sources_valid": sources_valid,
        "forbidden_returned": sorted(forbidden.intersection(ids)),
        "recall_at_5": recall,
        "precision": precision,
        "no_match_correct": no_match_correct,
        "order_passed": order_passed,
    }
    if v2:
        assert protocol is not None
        bounded, _ = bounded_results(
            hits, limit=integer(protocol["top_k"]), budget=integer(protocol["token_budget"])
        )
        safety = (
            sources_valid
            and not set(strings(gold["safety_forbidden_ids"])).intersection(ids)
            and not set(strings(gold["safety_forbidden_ids"])).intersection(eligible)
            and bounded == hits
        )
        quality = recall == 1.0 if required else no_match_correct is True
        result.update(
            safety_passed=safety,
            quality_passed=quality,
            passed=safety and quality,
            required_ids=sorted(required),
            false_positive_count=len(returned - required),
        )
    return result


def evaluate_case(
    case: dict[str, object],
    gold: dict[str, object],
    hits: list[dict[str, object]],
    protocol: dict[str, object],
) -> dict[str, object]:
    selected, budget = bounded_results(
        hits, limit=integer(protocol["top_k"]), budget=integer(protocol["token_budget"])
    )
    return {
        **score_case(case, gold, selected, threshold=protocol["threshold"], protocol=protocol),
        "retrieved_hits": hits,
        "return_budget": budget,
    }


def aggregate_run(
    results: list[dict[str, object]], *, protocol: dict[str, object] | None = None
) -> RunSummary:
    if not results:
        raise ValueError("Empty run")
    unique(results, "case_id")
    grouped: dict[str, list[dict[str, object]]] = {}
    for result in results:
        if type(result["passed"]) is not bool:
            raise ValueError("Invalid verdict")
        category = string(result["category"])
        grouped.setdefault(category, []).append(result)
    categories: dict[str, CategorySummary] = {}
    for category, members in grouped.items():
        recalls = [unit(row["recall_at_5"]) for row in members if row["recall_at_5"] is not None]
        no_match = [
            row["no_match_correct"] for row in members if row["no_match_correct"] is not None
        ]
        if any(type(value) is not bool for value in no_match):
            raise ValueError("Invalid no-match verdict")
        categories[category] = {
            "case_count": len(members),
            "passed_count": sum(row["passed"] is True for row in members),
            "recall_at_5": sum(recalls) / len(recalls) if recalls else None,
            "precision": sum(unit(row["precision"]) for row in members) / len(members),
            "no_match_correct_rate": sum(value is True for value in no_match) / len(no_match)
            if no_match
            else None,
        }
    summary: RunSummary = {
        "case_count": len(results),
        "passed": all(result["passed"] is True for result in results),
        "categories": categories,
    }
    if protocol is not None and protocol.get("comparison_version") == "memory-v2":
        for category, members in grouped.items():
            categories[category]["false_positive_count"] = sum(
                cast(int, row["false_positive_count"]) for row in members
            )
        safety = all(r["safety_passed"] is True for r in results)
        quality_groups: dict[tuple[str, bool], list[dict[str, object]]] = {}
        for result in results:
            quality_groups.setdefault(
                (string(result["category"]), result["recall_at_5"] is not None), []
            ).append(result)
        summary["safety_passed"] = safety
        summary["passed"] = safety and all(
            sum(r["quality_passed"] is True for r in members) / len(members)
            >= (0.9 if positive else 0.7)
            for (_, positive), members in quality_groups.items()
        )
        summary["score_ranges"] = score_ranges(results)
        gates: dict[str, object] = {}
        for category in grouped:
            sides: dict[str, object] = {}
            for positive, name in ((True, "recall"), (False, "no_match")):
                members = quality_groups.get((category, positive), [])
                if not members:
                    continue
                count = sum(r["quality_passed"] is True for r in members)
                required_rate = 0.9 if positive else 0.7
                sides[name] = {
                    "case_count": len(members),
                    "passed_count": count,
                    "pass_rate": count / len(members),
                    "required_rate": required_rate,
                    "passed": count / len(members) >= required_rate,
                }
            gates[category] = sides
        summary["quality_gates"] = gates
    return summary


def score_ranges(results: list[dict[str, object]]) -> dict[str, object]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for result in results:
        grouped.setdefault(string(result["category"]), []).append(result)
    output: dict[str, object] = {}
    for category, members in grouped.items():
        scales: dict[str, object] = {}
        for scale, field in (("composite", "score"), ("reranker", "reranker_score")):
            correct: list[float] = []
            incorrect: list[float] = []
            for result in members:
                required = set(strings(result["required_ids"]))
                other = []
                for observation in rows(result.get("candidate_observations", [])):
                    if observation[field] is None:
                        continue
                    value = number(observation[field])
                    if observation["memory_id"] in required:
                        correct.append(value)
                    else:
                        other.append(value)
                if other:
                    incorrect.append(max(other))
            scales[scale] = (
                {"correct": sample_range(correct), "highest_incorrect": sample_range(incorrect)}
                if correct or incorrect or scale == "composite"
                else None
            )
        output[category] = scales
    return output


def sample_range(samples: list[float]) -> dict[str, object] | None:
    if not samples:
        return None
    # Reranker scores may be negative; shift only for the duration percentile helper.
    offset = min(samples)
    shifted = [value - offset for value in samples]
    return {
        "count": len(samples),
        "min": min(samples),
        "max": max(samples),
        **{
            key: percentile(shifted, q, method="linear") + offset
            for key, q in (("p05", 0.05), ("p50", 0.5), ("p95", 0.95))
        },
    }


def percentile(values: list[float], quantile: float, *, method: str) -> float:
    if not values or method != "linear":
        raise ValueError("Nonempty samples and linear interpolation required")
    fraction = unit(quantile)
    samples = sorted(number(value) for value in values)
    if samples[0] < 0:
        raise ValueError("Negative duration")
    position = (len(samples) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return samples[lower] + (samples[upper] - samples[lower]) * (position - lower)


def stage_latency(results: list[dict[str, object]]) -> dict[str, object]:
    return {
        stage: {
            "p50_ms": percentile([number(r[stage + "_ms"]) for r in results], 0.5, method="linear"),
            "p95_ms": percentile(
                [number(r[stage + "_ms"]) for r in results], 0.95, method="linear"
            ),
        }
        for stage in (
            "passage_embedding",
            "index",
            "query_embedding",
            "search",
            "rerank",
            "result_selection",
            "scoring",
        )
    }
