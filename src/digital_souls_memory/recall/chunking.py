"""Overlapping verbatim slices in Unicode codepoint coordinates."""

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class TextChunk:
    index: int
    start: int
    end: int
    text: str


def split_chunks(text: str, *, length: int, overlap: int) -> tuple[TextChunk, ...]:
    if length <= 0 or not 0 <= overlap < length:
        raise ValueError("Invalid chunk ranges")
    chunks: list[TextChunk] = []
    start = 0
    while True:
        end = min(start + length, len(text))
        chunks.append(TextChunk(index=len(chunks), start=start, end=end, text=text[start:end]))
        if end == len(text):
            return tuple(chunks)
        start = end - overlap
