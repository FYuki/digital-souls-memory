"""チャンク比較アダプタの重要な契約。"""

import json
import sys
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path

import pytest

from digital_souls_memory.evaluation.comparison import comparison_protocol
from digital_souls_memory.evaluation.ports import (
    EmbeddingFactory,
    RerankerFactory,
    SearchFactory,
    TokenCounterFactory,
)
from tests import chunk_evaluation
from tests.chunk_evaluation import (
    chunk_case,
    collapse_hits,
    frozen_tuning_case_ids,
    validate_frozen_suite,
)

pytestmark = pytest.mark.ut


def test_main_rejects_overlap_before_acceptance_model_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: dict[str, object],
    memory: dict[str, object],
    gold: dict[str, object],
    candidate: dict[str, object],
) -> None:
    source_case = {
        **case,
        "memories": [{**memory, "source_spans": [{"turn_id": "t1", "start": 0, "end": 1}]}],
    }
    acceptance_query = "acceptance-only query"
    for split in ("tuning", "acceptance"):
        suite = {
            "split": split,
            "cases": [
                {**source_case, "query": acceptance_query} if split == "acceptance" else source_case
            ],
            "gold": [{**gold, "safety_forbidden_ids": [], "quality_forbidden_ids": []}],
            "protocol": {
                **comparison_protocol(),
                "comparison_version": "memory-v2.1",
                "threshold": -2,
            },
        }
        path = tmp_path / f"evals/retrieval/memory-v2.2/{split}/suite.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(suite))
    candidates = tmp_path / "evals/retrieval/memory-v2.1/candidates-bge-m3-rerank.json"
    candidates.parent.mkdir(parents=True)
    candidates.write_text(json.dumps([candidate]))
    config = tmp_path / "config.json"
    config.write_text("{}")
    output = tmp_path / "out"
    embed_calls: list[tuple[str, tuple[str, ...]]] = []
    embedding_factories: list[dict[str, object]] = []

    class FakeEmbedding:
        def embed(self, texts: tuple[str, ...], *, purpose: str) -> tuple[tuple[float, ...], ...]:
            embed_calls.append((purpose, texts))
            return tuple((0.0, 0.0) for _ in texts)

    class FakeSearch:
        def index(self, case: dict[str, object], vectors: tuple[tuple[float, ...], ...]) -> None:
            pass

        def search(self, query_vector: tuple[float, ...], *, limit: int) -> list[dict[str, object]]:
            return []

        def observe(self, query_vector: tuple[float, ...]) -> list[dict[str, object]]:
            return []

    class FakeReranker:
        def rerank(
            self, query: str, documents: tuple[dict[str, object], ...]
        ) -> list[dict[str, object]]:
            return []

    class FakeTokenCounter:
        method = "synthetic"

        def count(self, text: str) -> int:
            return len(text)

    def embedding_factory(candidate: dict[str, object]) -> nullcontext[FakeEmbedding]:
        embedding_factories.append(candidate)
        return nullcontext(FakeEmbedding())

    def factories(
        config: dict[str, object], candidates: list[dict[str, object]]
    ) -> tuple[EmbeddingFactory, SearchFactory]:
        return embedding_factory, lambda candidate: nullcontext(FakeSearch())

    def reranker_factory(
        config: dict[str, object], candidates: list[dict[str, object]]
    ) -> RerankerFactory:
        return lambda candidate: nullcontext(FakeReranker())

    def counter_factory(
        config: dict[str, object], candidates: list[dict[str, object]]
    ) -> TokenCounterFactory:
        return lambda candidate: nullcontext(FakeTokenCounter())

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["chunk_evaluation", str(config), str(output)])
    monkeypatch.setattr(chunk_evaluation, "real_factories", factories)
    monkeypatch.setattr(chunk_evaluation, "real_reranker_factory", reranker_factory)
    monkeypatch.setattr(chunk_evaluation, "real_token_counter_factory", counter_factory)

    with pytest.raises(ValueError, match="Empty, mixed, or incomplete evaluation set") as error:
        chunk_evaluation.main()

    frames = {entry.name for entry in error.traceback}
    assert {"validate_frozen_suite", "validate_suite"} <= frames
    assert len(embedding_factories) == 3
    assert all(acceptance_query not in texts for _, texts in embed_calls)
    assert (output / "tuning.json").is_file()
    assert not (output / "acceptance.json").exists()


def test_frozen_acceptance_rejects_tuning_case_overlap(
    make_suite: Callable[[], dict[str, object]],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    tuning = {**make_suite(), "split": "tuning"}
    tuning_ids = frozen_tuning_case_ids(json.dumps(tuning).encode())
    acceptance = make_suite()

    with rejected():
        validate_frozen_suite("acceptance", acceptance, tuning_ids)


def test_frozen_acceptance_allows_disjoint_tuning_cases(
    make_suite: Callable[[], dict[str, object]],
    case: dict[str, object],
    gold: dict[str, object],
) -> None:
    tuning = {
        **make_suite(),
        "split": "tuning",
        "cases": [{**case, "id": "other-case"}],
        "gold": [{**gold, "case_id": "other-case"}],
    }
    tuning_ids = frozen_tuning_case_ids(json.dumps(tuning).encode())

    validate_frozen_suite("acceptance", make_suite(), tuning_ids)


def test_frozen_tuning_does_not_reject_its_own_case_ids(
    make_suite: Callable[[], dict[str, object]],
) -> None:
    tuning = {**make_suite(), "split": "tuning"}
    tuning_ids = frozen_tuning_case_ids(json.dumps(tuning).encode())

    validate_frozen_suite("tuning", tuning, tuning_ids)


def test_chunk_text_retains_original_provenance() -> None:
    case = {
        "memories": [
            {
                "id": "m",
                "content": "あ" * 1000,
                "source_turn_ids": ["t"],
                "source_spans": [{"turn_id": "t", "start": 0, "end": 1000}],
            }
        ]
    }
    expanded, mapping = chunk_case(case)
    assert [len(m["content"]) for m in expanded["memories"]] == [512, 512, 104]
    assert mapping["m#1"][1].start == 448
    assert expanded["memories"][1]["source_spans"] == case["memories"][0]["source_spans"]
    assert case["memories"][0]["content"] == "あ" * 1000


def test_collapse_after_rerank_keeps_best_chunk_and_original_id() -> None:
    _, mapping = chunk_case({"memories": [{"id": "m", "content": "あ" * 1000}]})
    hits = [
        {"memory_id": "m#0", "reranker_score": 1.0},
        {"memory_id": "m#1", "reranker_score": 4.0},
    ]
    result = collapse_hits(hits, mapping)
    assert len(result) == 1
    assert result[0]["memory_id"] == "m"
    assert result[0]["matched_chunk"] == {"start": 448, "end": 960}
