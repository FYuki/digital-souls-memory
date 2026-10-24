"""Verbatim context windows and whole-payload token accounting."""

import json
from copy import deepcopy

from digital_souls_memory.recall.windowing import window_bounds

from .models import rows, string
from .ports import TokenCounter


def context_payload(hits: list[dict[str, object]]) -> str:
    return json.dumps(
        [{"memory_id": h["memory_id"], "source_spans": h["source_spans"]} for h in hits],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def token_count(hits: list[dict[str, object]], counter: TokenCounter) -> int:
    count = counter.count(context_payload(hits))
    if type(count) is not int or count < 0:
        raise ValueError("Nonnegative token count required")
    return count


def _window(hit: dict[str, object], query: str, width: int) -> dict[str, object]:
    spans = rows(hit["source_spans"])
    total = sum(len(string(s["text"])) for s in spans)
    windows: list[dict[str, object]] = []
    for span in spans:
        text = string(span["text"])
        size = min(len(text), max(1, width * len(text) // total))
        left, right = window_bounds(text, query, size)
        start, end = int(str(span["start"])), int(str(span["end"]))
        windows.append(
            {
                **span,
                "start": start + left,
                "end": start + right,
                "text": text[left:right],
                "truncated": left != 0 or right != len(text),
                "original_start": start,
                "original_end": end,
            }
        )
    return {**hit, "source_spans": windows}


def bounded_results(
    hits: list[dict[str, object]],
    *,
    case: dict[str, object],
    limit: int,
    budget: int,
    token_counter: TokenCounter,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    selected: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    for original in hits:
        hit = deepcopy(original)
        if len(selected) >= min(limit, 5):
            excluded.append({"memory_id": hit["memory_id"], "reason": "top_k"})
            continue
        if token_count([hit], token_counter) > budget:
            size = sum(len(string(s["text"])) for s in rows(hit["source_spans"]))
            low, high = 1, size - 1
            fitting = None
            while low <= high:
                width = (low + high) // 2
                window = _window(hit, string(case["query"]), width)
                if token_count([window], token_counter) <= budget:
                    fitting = window
                    low = width + 1
                else:
                    high = width - 1
            if fitting is None:
                excluded.append({"memory_id": hit["memory_id"], "reason": "token_budget"})
                continue
            hit = fitting
        if token_count([*selected, hit], token_counter) > budget:
            excluded.append({"memory_id": hit["memory_id"], "reason": "token_budget"})
        else:
            selected.append(hit)
    return selected, {
        "token_count": token_count(selected, token_counter),
        "token_counter": string(token_counter.method),
        "excluded": excluded,
    }
