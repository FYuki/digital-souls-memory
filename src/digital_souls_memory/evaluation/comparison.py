"""Comparison settings fixed before tuning; no product defaults."""

from copy import deepcopy

from .models import (
    COMPARISON_ATTRIBUTE_WEIGHT,
    COMPARISON_VERSION,
    SEMANTIC_LEXICAL_PERCENTAGE,
    SEMANTIC_PERCENTAGES,
    THRESHOLD_PERCENTAGES,
    record,
    rows,
    string,
    unit,
    validate_protocol,
)


def parameter_grid() -> list[dict[str, object]]:
    return [
        {
            "threshold": threshold / 100,
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
        for threshold in THRESHOLD_PERCENTAGES
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
        if parameters not in parameter_grid():
            raise ValueError("Parameters must belong to the fixed tuning grid")
        resolved.update(parameters)
    validate_protocol(resolved)
    return resolved


def tuning_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {**deepcopy(candidate), "id": f"{candidate['id']}.p{index:02d}", "parameters": parameters}
        for candidate in candidates
        for index, parameters in enumerate(parameter_grid())
    ]


def selected_candidates(
    report: dict[str, object], candidates: list[dict[str, object]]
) -> list[dict[str, object]]:
    if record(report["suite"])["split"] != "tuning":
        raise ValueError("Selection may only consume tuning observations")
    expanded = tuning_candidates(candidates)
    by_id = {string(c["id"]): c for c in expanded}
    if rows(report["candidates"]) != expanded:
        raise ValueError("Complete fixed grid required for selection")
    runs = rows(report["runs"])
    if any(run["execution_error"] is not None for run in runs):
        raise ValueError("Incomplete tuning cannot select parameters")

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
