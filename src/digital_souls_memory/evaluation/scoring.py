"""Independent scoring of observations against separate gold."""

import math
from typing import TypedDict

from .models import integer, number, rows, string, strings, unique, unit
from .retrieval import bounded_results, eligible_memories, source_spans


class CategorySummary(TypedDict):
    case_count: int
    passed_count: int
    recall_at_5: float | None
    precision: float
    no_match_correct_rate: float | None


class RunSummary(TypedDict):
    case_count: int
    passed: bool
    categories: dict[str, CategorySummary]


def score_case(
    case: dict[str, object],
    gold: dict[str, object],
    hits: list[dict[str, object]],
    *,
    threshold: object,
) -> dict[str, object]:
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
    for needed in rows(gold.get("required_source_spans", [])):
        if not any(
            span["turn_id"] == needed["turn_id"] and string(needed["text"]) in string(span["text"])
            for span in observed_spans
        ):
            sources_valid = False
    threshold_passed = all(number(hit["score"]) >= minimum for hit in hits)
    no_match_correct = not returned if not required else None
    order_passed = "required_order" not in gold or ids == strings(gold["required_order"])
    passed = (
        (recall == 1.0 if required else no_match_correct is True)
        and not forbidden.intersection(ids)
        and sources_valid
        and threshold_passed
        and order_passed
    )
    return {
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
        **score_case(case, gold, selected, threshold=protocol["threshold"]),
        "retrieved_hits": hits,
        "return_budget": budget,
    }


def aggregate_run(results: list[dict[str, object]]) -> RunSummary:
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
    return {
        "case_count": len(results),
        "passed": all(result["passed"] is True for result in results),
        "categories": categories,
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
