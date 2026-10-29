from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.evidence.provision import SourceSpan
from digital_souls_memory.recall.context import window_candidate, window_span
from digital_souls_memory.recall.domain import (
    RecallCandidate,
    RerankInput,
    RerankScore,
    ResolvedCandidate,
)
from digital_souls_memory.recall.settings import RecallSettings
from digital_souls_memory.recall.use_cases import RecallService
from tests.evidence_support import turn
from tests.memory_support import memory
from tests.recall_fixtures import anchor_candidate, candidate_for
from tests.recall_support import RecallHarness, StepClock
from tests.voice_support import anchor

pytestmark = pytest.mark.ut


class ChunkReranker:
    def __init__(self, *, other_relevance: float) -> None:
        self.calls: list[RerankInput] = []
        self.other_relevance = other_relevance

    def rerank(self, value: RerankInput) -> tuple[RerankScore, ...]:
        self.calls.append(value)
        scores = {
            "composite leader": 1.0,
            "relevance leader": 9.0,
            "another memory": self.other_relevance,
        }
        return tuple(
            RerankScore(ref=v.ref, chunk_index=v.chunk_index, relevance=scores[v.input.texts[0]])
            for v in reversed(value.documents)
        )


@pytest.mark.parametrize("other_count", [1, 5])
@pytest.mark.parametrize("kind", ["memory", "anchor", "related-anchor"])
def test_rerank_sees_each_chunk_and_selects_one_parent_after_relevance_ordering(
    other_count: int, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = RecallHarness()
    source = turn()
    h.evidence.add_turn(source)
    base = (
        candidate_for(memory(), sources=(source,))
        if kind == "memory"
        else anchor_candidate(replace(anchor(), character_id="char-b"), source=source)
    )

    def chunk(index: int, text: str, semantic: float) -> RecallCandidate:
        return replace(
            base,
            search_text=text,
            semantic=semantic,
            document=replace(base.document, chunk_index=index, content_digest=str(index) * 64),
        )

    first = chunk(0, "composite leader", 1.0)
    second = chunk(1, "relevance leader", 0.1)
    others = [
        candidate_for(
            replace(memory(), id=str(UUID(int=i + 2)), content="another memory"), sources=(source,)
        )
        for i in range(other_count)
    ]
    h.retriever.values = [first, second, *others]
    monkeypatch.setattr(h.counter, "count", lambda value: 1)
    reranker = ChunkReranker(other_relevance=10 if kind == "related-anchor" else 5)
    if kind == "related-anchor":
        h.retriever.anchors = [first, second]
    # This test owns only chunk selection. Real source/snapshot checks have DB coverage.
    h.provisions.validate = lambda snapshot: set(snapshot.documents) == {
        v.document for v in h.retriever.values
    }
    service = RecallService(
        evidence=h.evidence,
        characters=IdentityCharacters(lambda: h.identity),
        retriever=h.retriever,
        embedding=h.embedding,
        reranker=reranker,
        counter=h.counter,
        provisions=h.provisions,
        selections=h.selections,
        clock=lambda: h.now,
        monotonic=StepClock(),
        id_factory=h.new_provision_id,
        delivery_timeout=timedelta(minutes=10),
        non_sensitive_labels=frozenset(),
        settings=RecallSettings(),
    )

    response = service.recall(
        character_id="char-b",
        session_id="session-1",
        format="materials",
        query="fact",
        turn_id=None,
        sensitivity="synthetic-sensitive",
        classifier_version="synthetic-v1",
    )

    expected_ids = (
        [memory().id, *(v.ref.memory_id for v in others[:4])]
        if kind == "memory"
        else [v.ref.memory_id for v in others]
    )
    assert [v.id for v in response.memories] == expected_ids
    assert {v.input.texts for v in reranker.calls[0].documents} == {
        ("composite leader",),
        ("relevance leader",),
        ("another memory",),
    }
    assert {(v.ref, v.chunk_index) for v in reranker.calls[0].documents} == {
        (first.ref, 0),
        (second.ref, 1),
        *((v.ref, 0) for v in others),
    }
    record = h.provisions.get(response.provision_id)
    assert record is not None
    assert list(record.memory_ids) == expected_ids
    assert [v.identifier for v in record.provided_refs if v.kind == "memory"] == expected_ids
    assert record.provided_refs[0].kind == ("memory" if kind == "related-anchor" else kind)
    representative_score = next(s for s in record.scores if s.ref == first.ref)
    assert representative_score.relevance == 9
    assert representative_score.semantic == second.semantic
    if kind == "related-anchor":
        single = replace(second, document=replace(second.document, chunk_index=0))
        h.retriever.values = [single, *others]
        h.retriever.anchors = [single]
        baseline = service.recall(
            character_id="char-b",
            session_id="session-1",
            format="materials",
            query="fact",
            turn_id=None,
            sensitivity="synthetic-sensitive",
            classifier_version="synthetic-v1",
        )
        baseline_record = h.provisions.get(baseline.provision_id)
        assert baseline_record is not None
        assert record.provided_refs == baseline_record.provided_refs
    else:
        assert (
            sum(
                v.identifier == (memory().id if kind == "memory" else anchor().anchor_id)
                for v in record.provided_refs
            )
            == 1
        )
    assert [v.anchor_id for v in response.verbatim_anchors] == (
        [] if kind == "memory" else [anchor().anchor_id]
    )
    assert h.evidence.get_turn(source.turn_id) == source


def test_chunk_window_uses_original_coordinates_and_preserves_verbatim_unicode() -> None:
    matched_chunk = "Ａ🙂e\u0301Straßeｶﾞ: 合言葉は瑠璃色。"
    original = "原文だけの前置き。" * 100 + matched_chunk + "原文だけの後置き。" * 100
    span = SourceSpan(
        turn_id="source",
        start=7,
        end=7 + len(original),
        text=original,
        actor="char-c",
        language="ja",
        truncated=False,
        original_start=7,
        original_end=7 + len(original),
    )

    window = window_span(span, query=matched_chunk, width=100)

    assert matched_chunk in window.text
    assert window.text == original[window.start - 7 : window.end - 7]
    assert window.start <= 7 + original.index(matched_chunk)
    assert window.end >= 7 + original.index(matched_chunk) + len(matched_chunk)
    assert window.truncated is True
    assert (window.original_start, window.original_end) == (7, 7 + len(original))


def test_anchor_window_uses_selected_occurrence_even_when_chunk_text_is_repeated() -> None:
    source = turn(text="繰り返し🙂" * 500)
    base = anchor_candidate(replace(anchor(), character_id="char-b"), source=source)
    candidate = replace(
        base,
        search_text=source.text[1000:1020],
        chunk_start=1000,
        chunk_end=1020,
        document=replace(base.document, chunk_index=2),
    )
    span = SourceSpan(
        turn_id=source.turn_id,
        start=0,
        end=len(source.text),
        text=source.text,
        actor=source.actor,
        language=source.language,
        truncated=False,
        original_start=0,
        original_end=len(source.text),
    )
    value = ResolvedCandidate(candidate=candidate, spans=(span,), sources=base.dependencies)

    result = window_candidate(value, query="同じ語のどの位置か", width=40)

    (window,) = result.spans
    assert window.start <= 1000 < 1020 <= window.end
    assert window.text == source.text[window.start : window.end]
    assert (window.original_start, window.original_end) == (0, len(source.text))
    assert result.candidate.document == candidate.document
