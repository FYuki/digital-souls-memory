"""Pure eligibility, source recovery and scoring rules for comparison fixtures."""

from digital_souls_memory.recall.lexical import lexical_terms as lexical_terms
from digital_souls_memory.recall.lexical import normalized_key as normalized_key

from .context import context_payload
from .models import (
    COMPONENTS,
    number,
    record,
    rows,
    string,
    strings,
    timestamp,
    unit,
    validated_weights,
)


def eligible_memories(case: dict[str, object]) -> list[dict[str, object]]:
    turns = {string(turn["turn_id"]): turn for turn in rows(case["turns"])}
    sessions = {string(s["session_id"]): s for s in rows(case["sessions"])}
    character = string(case["character_id"])
    as_of = timestamp(case["as_of"])
    result = []
    for memory in rows(case["memories"]):
        sources = strings(memory["source_turn_ids"])
        if not sources or any(identifier not in turns for identifier in sources):
            continue
        if timestamp(memory["valid_from"]) > as_of:
            continue
        if memory["valid_to"] is not None and timestamp(memory["valid_to"]) <= as_of:
            continue
        visible = True
        for identifier in sources:
            turn = turns[identifier]
            participants = strings(sessions[string(turn["session_id"])]["participants"])
            if character not in participants and character not in strings(turn["audience"]):
                visible = False
            if timestamp(turn["occurred_at"]) > as_of:
                visible = False
        if visible:
            result.append(memory)
    return result


def source_spans(case: dict[str, object], *, memory_id: str) -> list[dict[str, object]]:
    memories = {string(memory["id"]): memory for memory in eligible_memories(case)}
    memory = memories[memory_id]
    turns = {string(turn["turn_id"]): turn for turn in rows(case["turns"])}
    ranges = (
        rows(memory["source_spans"])
        if "source_spans" in memory
        else [
            {"turn_id": identifier, "start": 0, "end": len(string(turns[identifier]["text"]))}
            for identifier in strings(memory["source_turn_ids"])
        ]
    )
    return [
        {
            "turn_id": span["turn_id"],
            "start": span["start"],
            "end": span["end"],
            "text": string(turns[string(span["turn_id"])]["text"])[
                int(str(span["start"])) : int(str(span["end"]))
            ],
            "actor": turns[string(span["turn_id"])]["actor"],
            "language": turns[string(span["turn_id"])]["language"],
        }
        for span in ranges
    ]


def composite_score(components: object, weights: object) -> float:
    values, coefficients = record(components), validated_weights(weights)
    if set(values) != COMPONENTS:
        raise ValueError("Six score components required")
    return sum(unit(values[key]) * unit(coefficients[key]) for key in sorted(COMPONENTS))


def score_components(
    case: dict[str, object],
    memory: dict[str, object],
    *,
    semantic_distance: object,
    lexical: object,
) -> dict[str, float]:
    turns = {string(t["turn_id"]): t for t in rows(case["turns"])}
    origin = max(
        timestamp(memory["last_reinforced_at"]),
        *(timestamp(turns[i]["occurred_at"]) for i in strings(memory["source_turn_ids"])),
    )
    days = max(0.0, (timestamp(case["as_of"]) - origin).total_seconds() / 86400)
    return {
        "semantic": max(0.0, min(1.0, 1 - number(semantic_distance))),
        "lexical": unit(lexical),
        "recency": 1.0 if memory["kind"] in ("Commitment", "VerbatimAnchor") else 2 ** (-days / 30),
        **{key: unit(memory[key]) for key in ("importance", "self_relevance", "trust")},
    }


def rank_results(
    observations: list[dict[str, object]], protocol: dict[str, object]
) -> list[dict[str, object]]:
    scored = [
        {**row, "score": composite_score(row["components"], protocol["weights"])}
        for row in observations
    ]
    return sorted(
        (row for row in scored if number(row["score"]) >= unit(protocol["threshold"])),
        key=lambda row: (-number(row["score"]), string(row["memory_id"])),
    )


def validate_observations(
    observations: list[dict[str, object]],
    case: dict[str, object],
    protocol: dict[str, object],
    *,
    allow_unmeasured_cosine: bool = False,
) -> None:
    from .models import unique

    if unique(observations, "memory_id") != {m["id"] for m in eligible_memories(case)}:
        raise ValueError("Complete eligible candidate evidence required")
    for observation in observations:
        score = composite_score(observation["components"], protocol["weights"])
        if not abs(number(observation["score"]) - score) <= 1e-12:
            raise ValueError("Candidate component score mismatch")
        cosine = observation["cosine"]
        if cosine is None:
            if not allow_unmeasured_cosine:
                raise ValueError("Measured cosine required")
        else:
            value = number(cosine)
            if (
                not -1.000001 <= value <= 1.000001
                or abs(
                    unit(record(observation["components"])["semantic"]) - max(0.0, min(1.0, value))
                )
                > 1e-12
            ):
                raise ValueError("Cosine and semantic component mismatch")
        if observation["reranker_score"] is not None:
            number(observation["reranker_score"])


def select_observations(
    observations: list[dict[str, object]], protocol: dict[str, object]
) -> list[dict[str, object]]:
    reranked = [o["reranker_score"] is not None for o in observations]
    if protocol.get("comparison_version") == "memory-v2.1" and any(reranked):
        return sorted(
            (
                o
                for o in observations
                if o["reranker_score"] is not None
                and number(o["reranker_score"]) >= number(protocol["threshold"])
            ),
            key=lambda o: (-number(o["reranker_score"]), string(o["memory_id"])),
        )
    if any(reranked) and not all(reranked):
        raise ValueError("Incomplete reranker evidence")
    return sorted(
        (o for o in observations if number(o["score"]) >= unit(protocol["threshold"])),
        key=lambda o: (
            -number(o["reranker_score"] if any(reranked) else o["score"]),
            string(o["memory_id"]),
        ),
    )


def rerank_candidates(observations: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(observations, key=lambda o: (-number(o["score"]), string(o["memory_id"])))[:20]


def bounded_results(
    hits: list[dict[str, object]], *, limit: int, budget: int
) -> tuple[list[dict[str, object]], dict[str, object]]:
    selected: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []

    def encoded_size(items: list[dict[str, object]]) -> int:
        # Scores are observations, not part of the context offered to a caller.
        return len(context_payload(items).encode("utf-8"))

    for hit in hits:
        if len(selected) >= limit:
            reason = "top_k"
        elif encoded_size([*selected, hit]) > budget:
            reason = "token_budget"
        else:
            selected.append(hit)
            continue
        excluded.append({"memory_id": hit["memory_id"], "reason": reason})
    return selected, {"utf8_bytes": encoded_size(selected), "excluded": excluded}
