import json
from collections.abc import Callable
from contextlib import nullcontext
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Self, cast

import pytest

from digital_souls_memory.evaluation.adapters.fixture import FixtureEmbedding, FixtureSearch
from digital_souls_memory.evaluation.cli import main
from digital_souls_memory.evaluation.comparison import parameter_grid
from digital_souls_memory.evaluation.models import string
from digital_souls_memory.evaluation.ports import Search
from digital_souls_memory.evaluation.report import public_report, validate_public_report
from digital_souls_memory.evaluation.retrieval import bounded_results, rank_results, source_spans
from digital_souls_memory.evaluation.runner import run_evaluation

pytestmark = pytest.mark.it1


class ObservedSearch:
    def __init__(self, observations: list[dict[str, object]]) -> None:
        self.observations = deepcopy(observations)
        self.indexed: list[dict[str, object]] = []
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def index(self, case: dict[str, object], vectors: tuple[tuple[float, ...], ...]) -> None:
        assert len(vectors) == len(cast(list[dict[str, object]], case["memories"]))
        self.indexed.append(deepcopy(case))

    def observe(self, query_vector: tuple[float, ...]) -> list[dict[str, object]]:
        case = self.indexed[-1]
        return [
            {**row, "source_spans": source_spans(case, memory_id=string(row["memory_id"]))}
            for row in self.observations
        ]

    def search(self, query_vector: tuple[float, ...], *, limit: int) -> list[dict[str, object]]:
        return rank_results(
            self.observe(query_vector), cast(dict[str, object], self.indexed[-1]["protocol"])
        )


class FakeReranker:
    def __init__(self, scores: list[dict[str, object]]) -> None:
        self.scores = deepcopy(scores)
        self.calls: list[tuple[str, tuple[dict[str, object], ...]]] = []
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def rerank(
        self, query: str, documents: tuple[dict[str, object], ...]
    ) -> list[dict[str, object]]:
        self.calls.append((query, deepcopy(documents)))
        return deepcopy(self.scores)


def test_v2_rejects_search_ports_without_all_candidate_observation(
    v2_suite: dict[str, object],
    v2_fixture: dict[str, object],
    candidate: dict[str, object],
) -> None:
    report = run_evaluation(
        v2_suite,
        [candidate],
        mode="fixture",
        embedding_factory=lambda c: FixtureEmbedding(c, v2_fixture),
        search_factory=lambda c: nullcontext(cast(Search, object())),
    )
    for run in cast(list[dict[str, object]], report["runs"]):
        assert run["execution_error"] is not None
        assert run["case_results"] == []
        assert run["quality_passed"] is None


def test_public_evidence_preserves_valid_partial_source_spans(
    v2_suite: dict[str, object],
    v2_fixture: dict[str, object],
    candidate: dict[str, object],
) -> None:
    class PartialSearch(FixtureSearch):
        def observe(self, query_vector: tuple[float, ...]) -> list[dict[str, object]]:
            observations = super().observe(query_vector)
            for row in observations:
                row["source_spans"] = [
                    {**span, "end": 2, "text": str(span["text"])[:2]}
                    for span in cast(list[dict[str, object]], row["source_spans"])
                ]
            return observations

    report = run_evaluation(
        v2_suite,
        [candidate],
        mode="fixture",
        embedding_factory=lambda c: FixtureEmbedding(c, v2_fixture),
        search_factory=lambda c: PartialSearch(v2_fixture),
    )
    published = public_report(report)
    validate_public_report(published, suite=v2_suite, candidates=[candidate])
    for run in cast(list[dict[str, object]], published["runs"]):
        result = cast(list[dict[str, object]], run["case_results"])[0]
        assert result["safety_passed"] is True
        hit = cast(list[dict[str, object]], result["hits"])[0]
        assert cast(list[dict[str, object]], hit["source_spans"])[0]["end"] == 2


@pytest.fixture
def many_case(
    v2_suite: dict[str, object],
    memory: dict[str, object],
) -> dict[str, object]:
    case = cast(list[dict[str, object]], v2_suite["cases"])[0]
    case["memories"] = [{**memory, "id": f"m{i}"} for i in range(1, 8)]
    # Short original spans keep five whole results inside the real return budget.
    cast(list[dict[str, object]], case["turns"])[0]["text"] = "合成原文"
    return case


def test_fixture_port_exposes_unfiltered_observations(
    v2_suite: dict[str, object],
    v2_fixture: dict[str, object],
) -> None:
    case = {
        **cast(list[dict[str, object]], v2_suite["cases"])[0],
        "protocol": {**cast(dict[str, object], v2_suite["protocol"]), "threshold": 0.9},
    }
    with FixtureSearch(v2_fixture) as search:
        search.index(case, ((1.0, 0.0),))
        observed = search.observe((1.0, 0.0))
    assert len(observed) == 1
    assert observed[0]["memory_id"] == "m1"
    assert observed[0]["score"] == pytest.approx(0.55)


def test_disabled_reranker_keeps_results_and_records_candidates_below_threshold_and_top_five(
    v2_suite: dict[str, object],
    many_case: dict[str, object],
    candidate: dict[str, object],
    observation_factory: Callable[[str, float], dict[str, object]],
) -> None:
    protocol = cast(dict[str, object], v2_suite["protocol"])
    protocol["threshold"] = 0.5
    observations = [observation_factory(f"m{i}", 0.9 - i * 0.1) for i in range(1, 8)]
    searches: list[ObservedSearch] = []

    def search_factory(settings: dict[str, object]) -> ObservedSearch:
        search = ObservedSearch(observations)
        searches.append(search)
        return search

    fixture: dict[str, object] = {
        "vectors": {
            "passage": {
                string(m["content"]): [1, 0]
                for m in cast(list[dict[str, object]], many_case["memories"])
            },
            "query": {string(many_case["query"]): [1, 0]},
        }
    }

    def embedding(settings: dict[str, object]) -> FixtureEmbedding:
        return FixtureEmbedding(settings, fixture)

    report = run_evaluation(
        v2_suite,
        [candidate],
        mode="fixture",
        embedding_factory=embedding,
        search_factory=search_factory,
    )
    expected_hits = [
        {**o, "source_spans": source_spans(many_case, memory_id=string(o["memory_id"]))}
        for o in rank_results(observations, protocol)
    ]
    expected, budget = bounded_results(expected_hits, limit=5, budget=1500)
    for run in cast(list[dict[str, object]], report["runs"]):
        assert run["execution_error"] is None
        result = cast(list[dict[str, object]], run["case_results"])[0]
        assert [
            {key: h[key] for key in ("memory_id", "score", "source_spans")}
            for h in cast(list[dict[str, object]], result["hits"])
        ] == [{key: h[key] for key in ("memory_id", "score", "source_spans")} for h in expected]
        assert result["return_budget"] == budget
        recorded = cast(list[dict[str, object]], result["candidate_observations"])
        assert {o["memory_id"] for o in recorded} == {f"m{i}" for i in range(1, 8)}
        for actual in recorded:
            original = next(o for o in observations if o["memory_id"] == actual["memory_id"])
            assert actual["components"] == original["components"]
            assert actual["cosine"] == original["cosine"]
            assert actual["score"] == pytest.approx(original["score"])
            assert actual["reranker_score"] is None
    assert len(searches) == 3
    assert all(search.closed for search in searches)


@pytest.mark.parametrize("fault", [None, "missing", "duplicate", "unknown", "nan", "exception"])
def test_reranker_precedes_threshold_and_budget_and_never_falls_back(
    v2_suite: dict[str, object],
    many_case: dict[str, object],
    candidate: dict[str, object],
    observation_factory: Callable[[str, float], dict[str, object]],
    fault: str | None,
) -> None:
    cast(dict[str, object], v2_suite["protocol"])["threshold"] = 0.5
    observations = [observation_factory(f"m{i}", 0.6 if i < 7 else 0.1) for i in range(1, 8)]
    scores: list[dict[str, object]] = [
        {"memory_id": f"m{i}", "score": float(i)} for i in range(1, 8)
    ]
    if fault == "missing":
        scores.pop()
    elif fault == "duplicate":
        scores[-1] = scores[0]
    elif fault == "unknown":
        scores[-1] = {"memory_id": "unsent", "score": 99.0}
    elif fault == "nan":
        scores[-1] = {"memory_id": "m7", "score": float("nan")}

    class Reranker(FakeReranker):
        def rerank(
            self, query: str, documents: tuple[dict[str, object], ...]
        ) -> list[dict[str, object]]:
            result = super().rerank(query, documents)
            if fault == "exception":
                raise RuntimeError("synthetic private error")
            return result

    rerankers: list[Reranker] = []
    searches: list[ObservedSearch] = []

    def reranker_factory(settings: dict[str, object]) -> Reranker:
        port = Reranker(scores)
        rerankers.append(port)
        return port

    def search_factory(settings: dict[str, object]) -> ObservedSearch:
        port = ObservedSearch(observations)
        searches.append(port)
        return port

    fixture: dict[str, object] = {
        "vectors": {
            "passage": {
                string(cast(list[dict[str, object]], many_case["memories"])[0]["content"]): [1, 0]
            },
            "query": {string(many_case["query"]): [1, 0]},
        }
    }

    def embedding(settings: dict[str, object]) -> FixtureEmbedding:
        return FixtureEmbedding(settings, fixture)

    report = run_evaluation(
        v2_suite,
        [candidate],
        mode="fixture",
        embedding_factory=embedding,
        search_factory=search_factory,
        reranker_factory=reranker_factory,
    )
    assert len(rerankers) == 3
    assert all(r.closed for r in rerankers)
    assert all(s.closed for s in searches)
    for port, run in zip(rerankers, cast(list[dict[str, object]], report["runs"]), strict=True):
        query, documents = port.calls[0]
        assert query == many_case["query"]
        assert {d["memory_id"] for d in documents} == {f"m{i}" for i in range(1, 8)}
        assert all(
            d["content"] == cast(list[dict[str, object]], many_case["memories"])[0]["content"]
            for d in documents
        )
        assert all(not {"gold", "required_ids", "forbidden_ids"} & set(d) for d in documents)
        if fault is None:
            assert run["execution_error"] is None
            result = cast(list[dict[str, object]], run["case_results"])[0]
            assert [h["memory_id"] for h in cast(list[dict[str, object]], result["hits"])] == [
                "m6",
                "m5",
                "m4",
                "m3",
                "m2",
            ]
            assert all(
                h["score"] == pytest.approx(0.6)
                for h in cast(list[dict[str, object]], result["hits"])
            )
            assert {
                o["memory_id"]: o["reranker_score"]
                for o in cast(list[dict[str, object]], result["candidate_observations"])
            } == {f"m{i}": float(i) for i in range(1, 8)}
        else:
            assert run["execution_error"] is not None
            assert run["quality_passed"] is None


@pytest.mark.parametrize(
    "category",
    [
        "multisource_revocation",
        "deleted_source",
        "deleted_memory",
        "binding_character",
        "update",
        "fact_version",
        "fact_revocation",
    ],
)
def test_safety_ids_are_excluded_before_embedding_index_and_reranking(
    v2_suite: dict[str, object],
    candidate: dict[str, object],
    memory: dict[str, object],
    observation_factory: Callable[[str, float], dict[str, object]],
    category: str,
) -> None:
    case = cast(list[dict[str, object]], v2_suite["cases"])[0]
    case["category"] = category
    blocked = {**memory, "id": "blocked", "content": "禁止された合成内容"}
    if category == "binding_character":
        hidden_turn = {
            **cast(list[dict[str, object]], case["turns"])[0],
            "turn_id": "hidden",
            "session_id": "hidden-session",
        }
        case["turns"] = [*cast(list[dict[str, object]], case["turns"]), hidden_turn]
        case["sessions"] = [
            *cast(list[dict[str, object]], case["sessions"]),
            {"session_id": "hidden-session", "participants": ["other"]},
        ]
        blocked["source_turn_ids"] = ["hidden"]
    elif category in ("deleted_source", "deleted_memory", "multisource_revocation"):
        blocked["source_turn_ids"] = (
            ["t1", "missing"] if category == "multisource_revocation" else ["missing"]
        )
    else:
        blocked["valid_to"] = case["as_of"]
    case["memories"] = [memory, blocked]
    gold = cast(list[dict[str, object]], v2_suite["gold"])[0]
    gold.update(forbidden_ids=["blocked"], safety_forbidden_ids=["blocked"])
    passages: list[str] = []

    class Embedding:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def embed(self, texts: tuple[str, ...], *, purpose: str) -> tuple[tuple[float, ...], ...]:
            if purpose == "passage":
                passages.extend(texts)
            return tuple((1.0, 0.0) for _ in texts)

    searches: list[ObservedSearch] = []
    rerankers: list[FakeReranker] = []

    def search_factory(settings: dict[str, object]) -> ObservedSearch:
        port = ObservedSearch([observation_factory("m1", 0.6)])
        searches.append(port)
        return port

    def reranker_factory(settings: dict[str, object]) -> FakeReranker:
        port = FakeReranker([{"memory_id": "m1", "score": 1.0}])
        rerankers.append(port)
        return port

    report = run_evaluation(
        v2_suite,
        [candidate],
        mode="fixture",
        embedding_factory=lambda settings: Embedding(),
        search_factory=search_factory,
        reranker_factory=reranker_factory,
    )
    assert all(
        run["execution_error"] is None for run in cast(list[dict[str, object]], report["runs"])
    )
    assert passages == [memory["content"]] * 3
    for search in searches:
        assert [m["id"] for m in cast(list[dict[str, object]], search.indexed[0]["memories"])] == [
            "m1"
        ]
        assert [
            t["turn_id"] for t in cast(list[dict[str, object]], search.indexed[0]["turns"])
        ] == ["t1"]
    for reranker in rerankers:
        assert [d["memory_id"] for d in reranker.calls[0][1]] == ["m1"]


@pytest.mark.parametrize(
    "fault", [None, "missing", "duplicate", "component", "cosine", "range", "rerank-latency"]
)
def test_candidate_evidence_survives_public_roundtrip_and_detects_tampering(
    v2_suite: dict[str, object],
    v2_fixture: dict[str, object],
    candidate: dict[str, object],
    fault: str | None,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    def embedding(settings: dict[str, object]) -> FixtureEmbedding:
        return FixtureEmbedding(settings, v2_fixture)

    report = run_evaluation(
        v2_suite,
        [candidate],
        mode="fixture",
        embedding_factory=embedding,
        search_factory=lambda settings: FixtureSearch(v2_fixture),
    )
    assert all(r["execution_error"] is None for r in cast(list[dict[str, object]], report["runs"]))
    published = public_report(report)
    validate_public_report(published, suite=v2_suite, candidates=[candidate])
    run = cast(list[dict[str, object]], published["runs"])[0]
    result = cast(list[dict[str, object]], run["case_results"])[0]
    observations = cast(list[dict[str, object]], result["candidate_observations"])
    assert len(observations) == 1
    assert observations[0]["memory_id"] == "m1"
    assert all(not {"content", "text", "query", "vectors"} & set(o) for o in observations)
    if fault is None:
        return
    if fault == "missing":
        result["candidate_observations"] = []
    elif fault == "duplicate":
        result["candidate_observations"] = [observations[0], observations[0]]
    elif fault == "component":
        cast(dict[str, object], observations[0]["components"])["semantic"] = 0.99
    elif fault == "cosine":
        observations[0]["cosine"] = -0.9
    elif fault == "range":
        cast(
            dict[str, object],
            cast(dict[str, object], cast(dict[str, object], run["score_ranges"])["detail"])[
                "composite"
            ],
        )["correct"] = {"min": 0.0}
    else:
        result["rerank_ms"] = 999.0
    with rejected():
        validate_public_report(published, suite=v2_suite, candidates=[candidate])


@pytest.mark.parametrize("reranked", [False, True])
def test_cli_v2_fixture_pins_inputs_and_publishes_candidate_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    v2_suite: dict[str, object],
    v2_fixture: dict[str, object],
    candidate: dict[str, object],
    reranked: bool,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("MEMORY_DATABASE_URL", raising=False)
    if reranked:
        candidate["reranker"] = {
            "model_alias": "synthetic-reranker",
            "revision": "synthetic",
            "sha256": "b" * 64,
            "quantization": "synthetic",
            "max_context_length": 8192,
        }
        v2_fixture["reranker_scores"] = {
            str(cast(list[dict[str, object]], v2_suite["cases"])[0]["query"]): [
                {"memory_id": "m1", "score": -2.0}
            ]
        }
    for name, value in (("suite", v2_suite), ("fixture", v2_fixture), ("candidates", [candidate])):
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    output = tmp_path / "report.json"
    status = main(
        [
            "--suite",
            str(tmp_path / "suite.json"),
            "--fixture",
            str(tmp_path / "fixture.json"),
            "--candidates",
            str(tmp_path / "candidates.json"),
            "--mode",
            "fixture",
            "--output",
            str(output),
            "--summary-output",
            str(tmp_path / "summary.json"),
            "--artifact-reference",
            "eval-artifacts/2026-10-20/report.json",
        ]
    )
    assert status == 0
    published = cast(dict[str, object], json.loads(output.read_text()))
    validate_public_report(published, suite=v2_suite, candidates=[candidate])
    assert (
        cast(dict[str, object], published["input_files_sha256"])["suite"]
        == sha256((tmp_path / "suite.json").read_bytes()).hexdigest()
    )
    assert published["quality_passed"] is None
    summary = cast(dict[str, object], json.loads((tmp_path / "summary.json").read_bytes()))
    assert (
        cast(dict[str, object], summary["artifact"])["sha256"]
        == sha256(output.read_bytes()).hexdigest()
    )
    assert all("case_results" not in row for row in cast(list[dict[str, object]], summary["runs"]))
    for run in cast(list[dict[str, object]], published["runs"]):
        assert run["execution_error"] is None
        assert run["real_model_calls"] == 0
        result = cast(list[dict[str, object]], run["case_results"])[0]
        assert result["safety_passed"] is True
        assert (
            cast(list[dict[str, object]], result["candidate_observations"])[0]["memory_id"] == "m1"
        )
        assert cast(list[dict[str, object]], result["candidate_observations"])[0][
            "reranker_score"
        ] == (-2.0 if reranked else None)


def test_rerank_time_is_recorded_per_stage_and_included_in_total_recall(
    v2_suite: dict[str, object],
    v2_fixture: dict[str, object],
    candidate: dict[str, object],
    observation_factory: Callable[[str, float], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"seconds": 0.0}
    monkeypatch.setattr(
        "digital_souls_memory.evaluation.runner.perf_counter", lambda: clock["seconds"]
    )

    class TimedEmbedding(FixtureEmbedding):
        def embed(self, texts: tuple[str, ...], *, purpose: str) -> tuple[tuple[float, ...], ...]:
            clock["seconds"] += 0.01
            return super().embed(texts, purpose=purpose)

    class TimedSearch(ObservedSearch):
        def index(self, case: dict[str, object], vectors: tuple[tuple[float, ...], ...]) -> None:
            clock["seconds"] += 0.04
            super().index(case, vectors)

        def observe(self, query_vector: tuple[float, ...]) -> list[dict[str, object]]:
            clock["seconds"] += 0.02
            return super().observe(query_vector)

    class TimedReranker(FakeReranker):
        def rerank(
            self, query: str, documents: tuple[dict[str, object], ...]
        ) -> list[dict[str, object]]:
            clock["seconds"] += 0.03
            return super().rerank(query, documents)

    def embedding(settings: dict[str, object]) -> TimedEmbedding:
        return TimedEmbedding(settings, v2_fixture)

    def search(settings: dict[str, object]) -> TimedSearch:
        return TimedSearch([observation_factory("m1", 0.55)])

    def reranker(settings: dict[str, object]) -> TimedReranker:
        return TimedReranker([{"memory_id": "m1", "score": -2.0}])

    report = run_evaluation(
        v2_suite,
        [candidate],
        mode="fixture",
        embedding_factory=embedding,
        search_factory=search,
        reranker_factory=reranker,
    )
    for run in cast(list[dict[str, object]], report["runs"]):
        assert run["execution_error"] is None
        result = cast(list[dict[str, object]], run["case_results"])[0]
        assert result["passage_embedding_ms"] == pytest.approx(10)
        assert result["index_ms"] == pytest.approx(40)
        assert result["query_embedding_ms"] == pytest.approx(10)
        assert result["search_ms"] == pytest.approx(20)
        assert result["rerank_ms"] == pytest.approx(30)
        assert result["recall_ms"] == pytest.approx(60)
        for stage, expected in (("query_embedding", 10), ("search", 20), ("rerank", 30)):
            summary = cast(dict[str, object], cast(dict[str, object], run["stage_latency"])[stage])
            assert summary["p50_ms"] == pytest.approx(expected)
            assert summary["p95_ms"] == pytest.approx(expected)
        assert cast(dict[str, object], run["latency"])["p95_ms"] == pytest.approx(60)
        assert cast(dict[str, object], run["latency"])["target_p95_met"] is True
        assert cast(dict[str, object], run["latency"])["required_p95_met"] is True
    published = public_report(report)
    validate_public_report(published, suite=v2_suite, candidates=[candidate])
    cast(
        dict[str, object],
        cast(
            dict[str, object], cast(list[dict[str, object]], published["runs"])[0]["stage_latency"]
        )["rerank"],
    )["p95_ms"] = 0.0
    with pytest.raises(ValueError):
        validate_public_report(published, suite=v2_suite, candidates=[candidate])


def test_v2_cli_uses_pinned_tuning_selection_for_acceptance_without_retuning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    v2_suite: dict[str, object],
    v2_fixture: dict[str, object],
    candidate: dict[str, object],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("MEMORY_DATABASE_URL", raising=False)
    tuning = deepcopy(v2_suite)
    tuning["split"] = "tuning"
    cast(list[dict[str, object]], tuning["cases"])[0]["id"] = "tuning-only"
    cast(list[dict[str, object]], tuning["gold"])[0]["case_id"] = "tuning-only"
    fixture = deepcopy(v2_fixture)
    observations = cast(dict[str, object], fixture["observations"])
    observations["tuning-only"] = deepcopy(observations["case-a"])
    for name, value in (
        ("acceptance", v2_suite),
        ("tuning", tuning),
        ("fixture", fixture),
        ("candidates", [candidate]),
    ):
        (tmp_path / f"{name}.json").write_text(json.dumps(value))

    def arguments(split: str) -> list[str]:
        return [
            "--suite",
            str(tmp_path / f"{split}.json"),
            "--candidates",
            str(tmp_path / "candidates.json"),
            "--fixture",
            str(tmp_path / "fixture.json"),
            "--mode",
            "fixture",
            "--output",
            str(tmp_path / f"{split}-report.json"),
        ]

    assert main([*arguments("tuning"), "--tune"]) == 0
    tuning_report = tmp_path / "tuning-report.json"
    digest = sha256(tuning_report.read_bytes()).hexdigest()
    assert (
        main(
            [
                *arguments("acceptance"),
                "--tuning-suite",
                str(tmp_path / "tuning.json"),
                "--tuning-sha256",
                sha256((tmp_path / "tuning.json").read_bytes()).hexdigest(),
                "--tuning-report",
                str(tuning_report),
                "--tuning-report-sha256",
                digest,
            ]
        )
        == 0
    )
    report = cast(dict[str, object], json.loads((tmp_path / "acceptance-report.json").read_text()))
    selected = cast(list[dict[str, object]], report["candidates"])
    assert selected[0]["parameters"] == parameter_grid()[0]
    assert len(cast(list[dict[str, object]], report["runs"])) == 3
    validate_public_report(report, suite=v2_suite, candidates=selected)
    assert sha256(tuning_report.read_bytes()).hexdigest() == digest
