"""Explicitly selected llama.cpp integration with approved models and synthetic Japanese."""

from collections.abc import Iterator
from contextlib import ExitStack
from math import sqrt

import pytest

from digital_souls_memory.adapters.recall_models import (
    LlamaCppEmbedding,
    LlamaCppReranker,
    LlamaCppTokenCounter,
)
from digital_souls_memory.recall.domain import CandidateRef, RerankDocument, RerankInput
from digital_souls_memory.recall.inputs import InputLabel, LocalOnly
from digital_souls_memory.recall.settings import EMBEDDING_MODEL, RERANKER_MODEL

pytestmark = pytest.mark.it2

QUERY = "十一月三日に青い駅で待ち合わせる約束はいつ、どこですか。"
RELATED = "十一月三日、青い駅で待ち合わせましょう。"
UNRELATED = "歯車の潤滑油を交換してから旋盤の回転速度を調整しました。"


def model_input(*texts: str) -> LocalOnly:
    return LocalOnly(
        texts=texts,
        labels=(InputLabel("synthetic-sensitive", "synthetic-v1"),),
    )


@pytest.fixture
def real_models() -> Iterator[tuple[LlamaCppEmbedding, LlamaCppReranker, LlamaCppTokenCounter]]:
    from tests.it2_support import load_model_endpoints

    embedding_endpoint, reranker_endpoint = load_model_endpoints()
    with ExitStack() as stack:
        embedding = stack.enter_context(
            LlamaCppEmbedding(EMBEDDING_MODEL, endpoint=embedding_endpoint)
        )
        reranker = stack.enter_context(LlamaCppReranker(RERANKER_MODEL, endpoint=reranker_endpoint))
        counter = stack.enter_context(
            LlamaCppTokenCounter(EMBEDDING_MODEL, endpoint=embedding_endpoint)
        )
        yield embedding, reranker, counter


def test_real_embedding_dimensions_norm_determinism_and_japanese_similarity(
    real_models: tuple[LlamaCppEmbedding, LlamaCppReranker, LlamaCppTokenCounter],
) -> None:
    embedding, _, _ = real_models
    first = embedding.embed(model_input(QUERY, RELATED, UNRELATED), purpose="query")
    repeated = embedding.embed(model_input(QUERY), purpose="query")
    assert len(first.vectors) == 3
    for vector in first.vectors:
        assert len(vector) == 1024
        assert sqrt(sum(value * value for value in vector)) == pytest.approx(1, abs=1e-4)
    assert repeated.vectors[0] == pytest.approx(first.vectors[0], abs=1e-6)
    query, related, unrelated = first.vectors
    assert sum(a * b for a, b in zip(query, related, strict=True)) > sum(
        a * b for a, b in zip(query, unrelated, strict=True)
    )


def test_real_reranker_ranks_related_above_unrelated_with_raw_threshold_negative(
    real_models: tuple[LlamaCppEmbedding, LlamaCppReranker, LlamaCppTokenCounter],
) -> None:
    _, reranker, _ = real_models
    refs = tuple(
        CandidateRef(owner_id="synthetic", memory_id=str(i), anchor_id=None) for i in range(2)
    )
    scores = reranker.rerank(
        RerankInput(
            query=model_input(QUERY),
            documents=tuple(
                RerankDocument(ref=ref, input=model_input(text))
                for ref, text in zip(refs, (RELATED, UNRELATED), strict=True)
            ),
        )
    )
    by_ref = {value.ref: value.relevance for value in scores}
    assert by_ref[refs[0]] > by_ref[refs[1]]
    assert by_ref[refs[0]] >= -2
    assert by_ref[refs[1]] < -2
