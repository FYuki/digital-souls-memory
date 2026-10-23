"""Shared search-key normalization and distinct term matching."""

import unicodedata


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


def lexical_score(search_key: str, terms: tuple[str, ...]) -> float:
    return sum(term in search_key for term in terms) / len(terms) if terms else 0.0
