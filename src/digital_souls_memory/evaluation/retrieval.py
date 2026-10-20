"""Pure eligibility, source recovery and scoring rules for comparison fixtures."""

import json
import unicodedata

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
    return [
        {
            "turn_id": identifier,
            "start": 0,
            "end": len(string(turns[identifier]["text"])),
            "text": turns[identifier]["text"],
            "actor": turns[identifier]["actor"],
            "language": turns[identifier]["language"],
        }
        for identifier in strings(memory["source_turn_ids"])
    ]


def composite_score(components: object, weights: object) -> float:
    values, coefficients = record(components), validated_weights(weights)
    if set(values) != COMPONENTS:
        raise ValueError("Six score components required")
    return sum(unit(values[key]) * unit(coefficients[key]) for key in sorted(COMPONENTS))


def normalized_key(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def lexical_terms(text: str) -> tuple[str, ...]:
    terms: set[str] = set()
    sequence = ""
    for char in normalized_key(text) + " ":
        if char.isalnum():
            sequence += char
        elif sequence:
            terms.update(sequence[i : i + 2] for i in range(max(1, len(sequence) - 1)))
            sequence = ""
    return tuple(sorted(terms))


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


def bounded_results(
    hits: list[dict[str, object]], *, limit: int, budget: int
) -> tuple[list[dict[str, object]], dict[str, object]]:
    selected: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []

    def encoded_size(items: list[dict[str, object]]) -> int:
        # Scores are observations, not part of the context offered to a caller.
        fragments = [
            {"memory_id": h["memory_id"], "source_spans": h["source_spans"]} for h in items
        ]
        return len(
            json.dumps(
                fragments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )

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
