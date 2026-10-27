"""Verbatim response assembly and whole-JSON token budgeting."""

import json
from dataclasses import asdict, dataclass, replace

from digital_souls_memory.evidence.provision import SourceSpan
from digital_souls_memory.identity.sessions import SessionIdentity
from digital_souls_memory.recall.domain import ResolvedCandidate
from digital_souls_memory.recall.windowing import window_bounds


@dataclass(frozen=True, kw_only=True)
class ResponseMemory:
    id: str
    kind: str
    content: str
    source_turn_ids: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ResponseAnchor:
    anchor_id: str
    turn_id: str
    text: str


@dataclass(frozen=True, kw_only=True)
class RecallResponse:
    provision_id: str
    temperament_version: int | None
    personality_version: int | None
    memories: tuple[ResponseMemory, ...]
    source_spans: tuple[SourceSpan, ...]
    verbatim_anchors: tuple[ResponseAnchor, ...]
    context: str | None


def window_span(span: SourceSpan, *, query: str, width: int) -> SourceSpan:
    if width >= len(span.text):
        return span
    start, end = window_bounds(span.text, query, width)
    return replace(
        span,
        start=span.start + start,
        end=span.start + end,
        text=span.text[start:end],
        truncated=True,
    )


def assemble_response(
    *,
    provision_id: str,
    selection: SessionIdentity,
    candidates: tuple[ResolvedCandidate, ...],
    format: str,
) -> RecallResponse:
    memories: list[ResponseMemory] = []
    spans: dict[tuple[str, int, int], SourceSpan] = {}
    anchors: dict[str, ResponseAnchor] = {}
    for resolved in candidates:
        value = resolved.candidate
        if value.memory is not None:
            memories.append(
                ResponseMemory(
                    id=value.memory.id,
                    kind=value.memory.kind,
                    content=value.memory.content,
                    source_turn_ids=value.memory.source_turn_ids,
                )
            )
        for span in resolved.spans:
            spans[(span.turn_id, span.start, span.end)] = span
        if value.anchor is not None:
            span = next(s for s in resolved.spans if s.turn_id == value.anchor.turn_id)
            anchors[value.anchor.anchor_id] = ResponseAnchor(
                anchor_id=value.anchor.anchor_id, turn_id=span.turn_id, text=span.text
            )
    originals = tuple(spans.values())
    return RecallResponse(
        provision_id=provision_id,
        temperament_version=selection.temperament_version,
        personality_version=selection.personality_version,
        memories=tuple(memories),
        source_spans=originals,
        verbatim_anchors=tuple(anchors.values()),
        context="\n".join(s.text for s in originals) if format == "assembled" else None,
    )


def serialize_response(value: RecallResponse) -> str:
    return json.dumps(asdict(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
