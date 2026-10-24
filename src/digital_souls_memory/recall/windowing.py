"""Shared verbatim window boundaries in original Unicode code points."""

import re


def window_bounds(text: str, query: str, width: int) -> tuple[int, int]:
    if width >= len(text):
        return 0, len(text)
    folded_query = query.casefold()
    folded_match = text.casefold().find(folded_query)
    position, length = 0, 1
    if folded_match >= 0:
        offset = 0
        for index, character in enumerate(text):
            next_offset = offset + len(character.casefold())
            if offset <= folded_match < next_offset:
                position = index
            if next_offset >= folded_match + len(folded_query):
                length = index + 1 - position
                break
            offset = next_offset
    else:
        terms = {query[i : i + 2] for i in range(len(query) - 1)}
        sentences = list(re.finditer(r"[^。.!?\n]+[。.!?\n]?", text))
        if sentences:
            sentence = max(sentences, key=lambda s: sum(term in s.group() for term in terms))
            positions = sorted(
                sentence.group().find(term) for term in terms if term in sentence.group()
            )
            if positions:
                position, length = sentence.start() + positions[len(positions) // 2], 2
    size = min(len(text), max(width, length))
    start = max(0, min(position - (size - length) // 2, len(text) - size))
    return start, start + size
