"""Comparison settings fixed before tuning; no product defaults."""

from copy import deepcopy

from .models import (
    COMPARISON_ATTRIBUTE_WEIGHT,
    COMPARISON_VERSION,
    SEMANTIC_LEXICAL_PERCENTAGE,
    SEMANTIC_PERCENTAGES,
    THRESHOLD_PERCENTAGES,
    number,
    record,
    rows,
    string,
    unit,
    validate_protocol,
)


def parameter_grid(
    *, protocol: dict[str, object] | None = None, reranker_enabled: bool = False
) -> list[dict[str, object]]:
    relevance = (
        protocol is not None
        and protocol.get("comparison_version") == "memory-v2.1"
        and reranker_enabled
    )
    return [
        {
            "threshold": threshold if relevance else threshold / 100,
            "weights": {
                "semantic": semantic / 100,
                "lexical": (SEMANTIC_LEXICAL_PERCENTAGE - semantic) / 100,
                "recency": COMPARISON_ATTRIBUTE_WEIGHT,
                "importance": COMPARISON_ATTRIBUTE_WEIGHT,
                "self_relevance": COMPARISON_ATTRIBUTE_WEIGHT,
                "trust": COMPARISON_ATTRIBUTE_WEIGHT,
            },
        }
        for semantic in SEMANTIC_PERCENTAGES
        for threshold in (tuple(range(-12, 13)) if relevance else THRESHOLD_PERCENTAGES)
    ]


def comparison_protocol() -> dict[str, object]:
    return {
        "comparison_version": COMPARISON_VERSION,
        "top_k": 5,
        "token_budget": 1500,
        "repetitions": 3,
        "percentile_method": "linear",
        **parameter_grid()[0],
    }


def resolved_protocol(
    protocol: dict[str, object], candidate: dict[str, object]
) -> dict[str, object]:
    resolved = deepcopy(protocol)
    if "parameters" in candidate:
        parameters = record(candidate["parameters"])
        if parameters not in parameter_grid(
            protocol=protocol, reranker_enabled="reranker" in candidate
        ):
            raise ValueError("Parameters must belong to the fixed tuning grid")
        resolved.update(parameters)
    validate_protocol(resolved)
    return resolved


def tuning_candidates(
    candidates: list[dict[str, object]], *, protocol: dict[str, object] | None = None
) -> list[dict[str, object]]:
    return [
        {**deepcopy(candidate), "id": f"{candidate['id']}.p{index:02d}", "parameters": parameters}
        for candidate in candidates
        for index, parameters in enumerate(
            parameter_grid(protocol=protocol, reranker_enabled="reranker" in candidate)
        )
    ]


def selected_candidates(
    report: dict[str, object], candidates: list[dict[str, object]]
) -> list[dict[str, object]]:
    if record(report["suite"])["split"] != "tuning":
        raise ValueError("Selection may only consume tuning observations")
    protocol = record(record(report["suite"]).get("protocol", {}))
    expanded = tuning_candidates(candidates, protocol=protocol)
    by_id = {string(c["id"]): c for c in expanded}
    if rows(report["candidates"]) != expanded:
        raise ValueError("Complete fixed grid required for selection")
    runs = rows(report["runs"])
    if any(run["execution_error"] is not None for run in runs):
        raise ValueError("Incomplete tuning cannot select parameters")
    if protocol.get("comparison_version") in ("memory-v2", "memory-v2.1"):
        selected = []
        for candidate in candidates:
            options = [
                f"{candidate['id']}.p{i:02d}"
                for i in range(
                    len(parameter_grid(protocol=protocol, reranker_enabled="reranker" in candidate))
                )
            ]
            safe = []
            for identifier in options:
                members = [run for run in runs if run["candidate_id"] == identifier]
                if len(members) != 3 or {r["run_index"] for r in members} != {1, 2, 3}:
                    raise ValueError("Three independent runs required")
                if all(r["safety_passed"] is True for r in members):
                    safe.append(identifier)
            if not safe:
                raise ValueError("No safe tuning setting")

            def quality_key(identifier: str) -> tuple[float, float, float]:
                members = [run for run in runs if run["candidate_id"] == identifier]
                positive_rates: list[float] = []
                negative_rates: list[float] = []
                recalls = []
                for run in members:
                    groups: dict[tuple[str, bool], list[dict[str, object]]] = {}
                    for result in rows(run["case_results"]):
                        positive = result["recall_at_5"] is not None
                        groups.setdefault((string(result["category"]), positive), []).append(result)
                        if positive:
                            recalls.append(number(result["recall_at_5"]))
                    for (_, positive), results in groups.items():
                        rate = sum(r["quality_passed"] is True for r in results) / len(results)
                        (positive_rates if positive else negative_rates).append(rate)
                return (
                    min(positive_rates) if positive_rates else 1.0,
                    min(negative_rates) if negative_rates else 1.0,
                    sum(recalls) / len(recalls) if recalls else 0.0,
                )

            selected.append(
                {
                    **deepcopy(candidate),
                    "parameters": deepcopy(by_id[max(safe, key=quality_key)]["parameters"]),
                }
            )
        return selected

    def selection_key(identifier: str) -> tuple[float, ...]:
        members = [run for run in runs if run["candidate_id"] == identifier]
        if len(members) != 3:
            raise ValueError("Three tuning runs required")
        results = [result for run in members for result in rows(run["case_results"])]
        violations = sum(
            sum(
                (
                    result["sources_valid"] is not True,
                    result["threshold_passed"] is not True,
                    bool(result["forbidden_returned"]),
                    result["order_passed"] is not True,
                    result["recall_at_5"] is not None and result["recall_at_5"] != 1.0,
                    result["no_match_correct"] is False,
                )
            )
            for result in results
        )
        categories = [
            record(value) for run in members for value in record(run["categories"]).values()
        ]
        minimum = min(
            float(str(c["passed_count"])) / float(str(c["case_count"])) for c in categories
        )
        recalls = [unit(r["recall_at_5"]) for r in results if r["recall_at_5"] is not None]
        parameters = record(by_id[identifier]["parameters"])
        return (
            -float(violations),
            minimum,
            sum(recalls) / len(recalls) if recalls else 0.0,
            sum(unit(r["precision"]) for r in results) / len(results),
            unit(parameters["threshold"]),
            unit(record(parameters["weights"])["semantic"]),
        )

    return [
        {
            **deepcopy(candidate),
            "parameters": deepcopy(
                by_id[
                    max(
                        (f"{candidate['id']}.p{i:02d}" for i in range(len(parameter_grid()))),
                        key=selection_key,
                    )
                ]["parameters"]
            ),
        }
        for candidate in candidates
    ]
