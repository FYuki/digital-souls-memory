"""Shared verbatim window boundaries in original Unicode code points."""

import re


def window_bounds(text: str, query: str, width: int) -> tuple[int, int]:
    if width >= len(text):
        return 0, len(text)
    folded_query = query.casefold()
    folded_match = text.casefold().find(folded_query)
    folded_length = len(folded_query)
    position, length = 0, 1
    if folded_match < 0:
        terms = {folded_query[i : i + 2] for i in range(len(folded_query) - 1)}
        sentences = list(re.finditer(r"[^。.!?\n]+[。.!?\n]?", text))
        if sentences:
            sentence = max(
                sentences, key=lambda s: sum(term in s.group().casefold() for term in terms)
            )
            folded_sentence = sentence.group().casefold()
            positions = sorted(
                folded_sentence.find(term) for term in terms if term in folded_sentence
            )
            if positions:
                folded_match = (
                    len(text[: sentence.start()].casefold()) + positions[len(positions) // 2]
                )
                folded_length = 2
    if folded_match >= 0:
        offset = 0
        for index, character in enumerate(text):
            next_offset = offset + len(character.casefold())
            if offset <= folded_match < next_offset:
                position = index
            if next_offset >= folded_match + folded_length:
                length = index + 1 - position
                break
            offset = next_offset
    size = min(len(text), max(width, length))
    start = max(0, min(position - (size - length) // 2, len(text) - size))
    return start, start + size
