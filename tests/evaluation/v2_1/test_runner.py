import json
from collections.abc import Callable
from contextlib import nullcontext
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from digital_souls_memory.evaluation.adapters.fixture import FixtureEmbedding, FixtureSearch
from digital_souls_memory.evaluation.cli import main
from digital_souls_memory.evaluation.context import context_payload
from digital_souls_memory.evaluation.models import record, rows, string, strings
from digital_souls_memory.evaluation.report import public_report, validate_public_report
from digital_souls_memory.evaluation.runner import run_evaluation

pytestmark = pytest.mark.it1


class Counter:
    method = "synthetic-character-count"

    def count(self, text: str) -> int:
        return len(text)


class Reranker:
    def __init__(
        self, scores: dict[str, float], calls: list[tuple[dict[str, object], ...]]
    ) -> None:
        self.scores = scores
        self.calls = calls

    def rerank(
        self, query: str, documents: tuple[dict[str, object], ...]
    ) -> list[dict[str, object]]:
        self.calls.append(deepcopy(documents))
        return [
            {"memory_id": d["memory_id"], "score": self.scores[string(d["memory_id"])]}
            for d in documents
        ]


def fixture_data(suite: dict[str, object], scores: dict[str, float]) -> dict[str, object]:
    case = rows(suite["cases"])[0]
    weights = record(record(suite["protocol"])["weights"])
    return {
        "vectors": {
            "passage": {string(m["content"]): [1, 0] for m in rows(case["memories"])},
            "query": {string(case["query"]): [1, 0]},
        },
        "observations": {
            string(case["id"]): [
                {
                    "memory_id": identifier,
                    "cosine": score,
                    "score": score,
                    "components": {key: score for key in weights},
                    "reranker_score": None,
                }
                for identifier, score in scores.items()
            ]
        },
    }


@pytest.mark.parametrize(
    ("shortened_turn", "shortened_end"),
    [(None, None), ("episode-source:1:0", 18), ("fact-source:1:0", 20)],
    ids=["complete", "missing-reason", "missing-dates"],
)
def test_fixed_fact_attachment_returns_required_sources_through_public_roundtrip(
    candidate: dict[str, object],
    shortened_turn: str | None,
    shortened_end: int | None,
) -> None:
    root = Path(__file__).resolve().parents[3] / "evals/retrieval/memory-v2.1/acceptance"
    suite = record(json.loads((root / "suite.json").read_bytes()))
    assert suite["cases"] == json.loads((root / "cases.json").read_bytes())
    assert suite["gold"] == json.loads((root / "gold.json").read_bytes())
    case = next(c for c in rows(suite["cases"]) if c["id"] == "valid-fact-attachment")
    gold = next(g for g in rows(suite["gold"]) if g["case_id"] == case["id"])
    if shortened_turn is not None:
        memories = rows(case["memories"])
        for memory in memories:
            memory["source_spans"] = [
                {**span, "end": shortened_end} if span["turn_id"] == shortened_turn else span
                for span in rows(memory["source_spans"])
            ]
        case["memories"] = memories
    suite.update(cases=[case], gold=[gold])
    expected_quality = shortened_turn is None
    fixture = fixture_data(suite, {"trip-episode": 0.9, "destination-fact": 0.9})

    def factory(c: dict[str, object]) -> nullcontext[Counter]:
        return nullcontext(Counter())

    report = run_evaluation(
        suite,
        [candidate],
        mode="fixture",
        embedding_factory=lambda c: FixtureEmbedding(c, fixture),
        search_factory=lambda c: FixtureSearch(fixture),
        token_counter_factory=factory,
    )
    assert report["quality_passed"] is None
    for run in rows(report["runs"]):
        assert run["execution_error"] is None
        assert run["quality_passed"] is None
        result = rows(run["case_results"])[0]
        hits = rows(result["hits"])
        assert {h["memory_id"] for h in hits} == set(strings(gold["required_ids"]))
        assert len(hits) <= 5
        assert result["safety_passed"] is True
        spans = [span for hit in hits for span in rows(hit["source_spans"])]
        for needed in rows(gold["required_source_spans"]):
            included = any(
                span["turn_id"] == needed["turn_id"]
                and string(needed["text"]) in string(span["text"])
                for span in spans
            )
            assert included is (needed["turn_id"] != shortened_turn)
        assert result["quality_passed"] is expected_quality
        budget = record(result["return_budget"])
        assert budget["token_count"] == Counter().count(context_payload(hits))
        assert int(str(budget["token_count"])) <= 1500
        assert budget["token_counter"] == Counter.method
        assert budget["excluded"] == []
        gate = record(record(record(run["quality_gates"])["fact_attachment"])["recall"])
        assert gate["passed"] is expected_quality
    published = public_report(report, token_counter_factory=factory)
    validate_public_report(
        published, suite=suite, candidates=[candidate], token_counter_factory=factory
    )
    for original, public in zip(rows(report["runs"]), rows(published["runs"]), strict=True):
        result = rows(original["case_results"])[0]
        public_result = rows(public["case_results"])[0]
        assert public_result["quality_passed"] == result["quality_passed"]
        assert public_result["return_budget"] == result["return_budget"]
        assert public["quality_gates"] == original["quality_gates"]
        for hit, public_hit in zip(rows(result["hits"]), rows(public_result["hits"]), strict=True):
            assert public_hit["memory_id"] == hit["memory_id"]
            for span, public_span in zip(
                rows(hit["source_spans"]), rows(public_hit["source_spans"]), strict=True
            ):
                assert "text" not in public_span
                assert public_span["turn_id"] == span["turn_id"]
                assert (public_span["start"], public_span["end"]) == (span["start"], span["end"])
                assert public_span["sha256"] == sha256(string(span["text"]).encode()).hexdigest()


@pytest.mark.parametrize("enabled", [False, True])
def test_runner_uses_relevance_for_no_match_and_preserves_disabled_results(
    new_suite: dict[str, object], candidate: dict[str, object], enabled: bool
) -> None:
    case = rows(new_suite["cases"])[0]
    new_suite["cases"] = [{**case, "category": "unrelated"}]
    new_suite["gold"] = [{**rows(new_suite["gold"])[0], "required_ids": []}]
    fixture = fixture_data(new_suite, {"m1": 0.9})
    calls: list[tuple[dict[str, object], ...]] = []
    report = run_evaluation(
        new_suite,
        [candidate],
        mode="fixture",
        embedding_factory=lambda c: FixtureEmbedding(c, fixture),
        search_factory=lambda c: FixtureSearch(fixture),
        reranker_factory=(lambda c: nullcontext(Reranker({"m1": -4.0}, calls)))
        if enabled
        else None,
        token_counter_factory=lambda c: nullcontext(Counter()),
    )
    for run in rows(report["runs"]):
        assert run["execution_error"] is None
        result = rows(run["case_results"])[0]
        assert [h["memory_id"] for h in rows(result["hits"])] == ([] if enabled else ["m1"])
        assert result["no_match_correct"] is enabled
        assert result["safety_passed"] is True


def test_only_top_twenty_composite_candidates_are_reranked_and_unmeasured_stay_null(
    new_suite: dict[str, object], candidate: dict[str, object]
) -> None:
    case = rows(new_suite["cases"])[0]
    memory = rows(case["memories"])[0]
    # Input order intentionally disagrees with composite rank; the last score is below threshold.
    memories = [
        {**memory, "id": f"m{i:02d}", "content": f"合成候補{i}"} for i in reversed(range(21))
    ]
    blocked = {**memory, "id": "blocked", "content": "禁止合成候補", "valid_to": case["as_of"]}
    new_suite["cases"] = [{**case, "memories": [blocked, *memories]}]
    new_suite["gold"] = [
        {
            **rows(new_suite["gold"])[0],
            "required_ids": ["m19"],
            "forbidden_ids": ["blocked"],
            "safety_forbidden_ids": ["blocked"],
        }
    ]
    composite = {f"m{i:02d}": 0.9 - i * 0.04 for i in reversed(range(21))}
    fixture = fixture_data(new_suite, composite)
    calls: list[tuple[dict[str, object], ...]] = []
    passages: list[str] = []
    indexed_ids: list[list[object]] = []

    class RecordingEmbedding(FixtureEmbedding):
        def embed(self, texts: tuple[str, ...], *, purpose: str) -> tuple[tuple[float, ...], ...]:
            if purpose == "passage":
                passages.extend(texts)
            return super().embed(texts, purpose=purpose)

    class RecordingSearch(FixtureSearch):
        def index(self, case: dict[str, object], vectors: tuple[tuple[float, ...], ...]) -> None:
            indexed_ids.append([m["id"] for m in rows(case["memories"])])
            super().index(case, vectors)

    report = run_evaluation(
        new_suite,
        [candidate],
        mode="fixture",
        embedding_factory=lambda c: RecordingEmbedding(c, fixture),
        search_factory=lambda c: RecordingSearch(fixture),
        reranker_factory=lambda c: nullcontext(
            Reranker({f"m{i:02d}": float(i) for i in range(21)}, calls)
        ),
        token_counter_factory=lambda c: nullcontext(Counter()),
    )
    assert len(calls) == 3
    assert set(passages) == {f"合成候補{i}" for i in range(21)}
    assert len(indexed_ids) == 3
    assert all(set(ids) == set(composite) for ids in indexed_ids)
    for documents in calls:
        assert [d["memory_id"] for d in documents] == [f"m{i:02d}" for i in range(20)]
        assert [d["content"] for d in documents] == [f"合成候補{i}" for i in range(20)]
        assert all(set(d) == {"memory_id", "content"} for d in documents)
    for run in rows(report["runs"]):
        assert run["execution_error"] is None
        result = rows(run["case_results"])[0]
        assert [h["memory_id"] for h in rows(result["hits"])] == [
            f"m{i:02d}" for i in range(19, 14, -1)
        ]
        observed = {string(o["memory_id"]): o for o in rows(result["candidate_observations"])}
        assert set(observed) == set(composite)
        assert observed["m20"]["reranker_score"] is None
        assert observed["m19"]["reranker_score"] == 19.0
        assert observed["m19"]["score"] == pytest.approx(composite["m19"])
        ranges = record(record(run["score_ranges"])["detail"])
        assert record(record(ranges["reranker"])["highest_incorrect"])["max"] == 18.0
    published = public_report(report, token_counter_factory=lambda c: nullcontext(Counter()))
    validate_public_report(
        published,
        suite=new_suite,
        candidates=[candidate],
        token_counter_factory=lambda c: nullcontext(Counter()),
    )


@pytest.mark.parametrize(
    "fault", [None, "truncated", "start", "method", "count", "missing-factory"]
)
def test_public_roundtrip_keeps_window_metadata_and_rejects_tampering(
    new_suite: dict[str, object],
    candidate: dict[str, object],
    fault: str | None,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    case = rows(new_suite["cases"])[0]
    text = "前文。" + "あ" * 2200 + "橙色のリボン" + "い" * 2200
    case["query"] = "橙色のリボン"
    case["turns"] = [{**rows(case["turns"])[0], "text": text}]
    case["memories"] = [
        {
            **rows(case["memories"])[0],
            "source_spans": [{"turn_id": "t1", "start": 4, "end": len(text)}],
        }
    ]
    new_suite["cases"] = [case]
    new_suite["gold"] = [
        {
            **rows(new_suite["gold"])[0],
            "required_source_spans": [{"turn_id": "t1", "text": "橙色のリボン"}],
        }
    ]
    fixture = fixture_data(new_suite, {"m1": 0.9})

    def factory(c: dict[str, object]) -> nullcontext[Counter]:
        return nullcontext(Counter())

    report = run_evaluation(
        new_suite,
        [candidate],
        mode="fixture",
        embedding_factory=lambda c: FixtureEmbedding(c, fixture),
        search_factory=lambda c: FixtureSearch(fixture),
        token_counter_factory=factory,
    )
    published = public_report(report, token_counter_factory=factory)
    validate_public_report(
        published, suite=new_suite, candidates=[candidate], token_counter_factory=factory
    )
    if fault == "missing-factory":
        with rejected():
            validate_public_report(published, suite=new_suite, candidates=[candidate])
        return
    for run in rows(published["runs"]):
        assert run["execution_error"] is None
        result = rows(run["case_results"])[0]
        assert result["quality_passed"] is True
        span = rows(rows(result["hits"])[0]["source_spans"])[0]
        assert span["truncated"] is True
        assert (span["original_start"], span["original_end"]) == (4, len(text))
        assert (
            span["sha256"]
            == sha256(text[int(str(span["start"])) : int(str(span["end"]))].encode()).hexdigest()
        )
        assert "text" not in span
        assert record(result["return_budget"])["token_counter"] == Counter.method
    first = rows(rows(published["runs"])[0]["case_results"])[0]
    span = rows(rows(first["hits"])[0]["source_spans"])[0]
    if fault == "truncated":
        span["truncated"] = False
    elif fault == "start":
        span["original_start"] = 0
    elif fault == "method":
        record_budget = record(first["return_budget"])
        record_budget["token_counter"] = "different-counter"
        first["return_budget"] = record_budget
    elif fault == "count":
        first["return_budget"] = {**record(first["return_budget"]), "token_count": 1}
    if fault is not None:
        # rows() returns new dictionaries; put the altered observation back into the report.
        hit = rows(first["hits"])[0]
        hit["source_spans"] = [span]
        first["hits"] = [hit]
        run = rows(published["runs"])[0]
        run["case_results"] = [first]
        published["runs"] = [run, *rows(published["runs"])[1:]]
        with rejected():
            validate_public_report(
                published, suite=new_suite, candidates=[candidate], token_counter_factory=factory
            )


@pytest.mark.parametrize("fails", [False, True])
def test_counter_is_closed_on_success_and_failure_without_fallback(
    new_suite: dict[str, object],
    candidate: dict[str, object],
    fails: bool,
) -> None:
    fixture = fixture_data(new_suite, {"m1": 0.9})
    closed: list[bool] = []

    from collections.abc import Iterator
    from contextlib import contextmanager

    class FailingCounter(Counter):
        def count(self, text: str) -> int:
            if fails:
                raise RuntimeError("synthetic-tokenizer-failure")
            return super().count(text)

    @contextmanager
    def factory(c: dict[str, object]) -> Iterator[FailingCounter]:
        try:
            yield FailingCounter()
        finally:
            closed.append(True)

    report = run_evaluation(
        new_suite,
        [candidate],
        mode="fixture",
        embedding_factory=lambda c: FixtureEmbedding(c, fixture),
        search_factory=lambda c: FixtureSearch(fixture),
        token_counter_factory=factory,
    )
    assert closed == [True, True, True]
    for run in rows(report["runs"]):
        assert (run["execution_error"] is not None) is fails
        if fails:
            assert run["case_results"] == []
            assert run["quality_passed"] is None


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_pins_tuning_selection_and_counter_through_acceptance(
    new_suite: dict[str, object],
    candidate: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("MEMORY_DATABASE_URL", raising=False)
    if enabled:
        candidate["reranker"] = {
            "model_alias": "synthetic-reranker",
            "revision": "synthetic",
            "sha256": "b" * 64,
            "quantization": "synthetic",
            "max_context_length": 8192,
        }
    fixture = fixture_data(new_suite, {"m1": 0.9})
    queries = record(record(fixture["vectors"])["query"])
    fixture["vectors"] = {**record(fixture["vectors"]), "query": {**queries, "unrelated": [1, 0]}}
    fixture["reranker_scores"] = {
        rows(new_suite["cases"])[0]["query"]: [{"memory_id": "m1", "score": 2.0}],
        "unrelated": [{"memory_id": "m1", "score": -4.0}],
    }
    suites = {}
    for split in ("tuning", "acceptance"):
        suite = deepcopy(new_suite)
        positive = {**rows(suite["cases"])[0], "id": split + "-positive"}
        negative = {
            **positive,
            "id": split + "-negative",
            "category": "unrelated",
            "query": "unrelated",
        }
        suite.update(
            split=split,
            cases=[positive, negative],
            gold=[
                {**rows(new_suite["gold"])[0], "case_id": positive["id"]},
                {**rows(new_suite["gold"])[0], "case_id": negative["id"], "required_ids": []},
            ],
        )
        observations = record(fixture["observations"])
        observations[string(positive["id"])] = deepcopy(observations["case-a"])
        observations[string(negative["id"])] = [
            {
                **row,
                "score": 0.1,
                "cosine": 0.1,
                "components": {k: 0.1 for k in record(row["components"])},
            }
            for row in rows(observations["case-a"])
        ]
        fixture["observations"] = observations
        suites[split] = suite
        (tmp_path / f"{split}.json").write_text(json.dumps(suite))
    for name, value in (("fixture", fixture), ("candidates", [candidate])):
        (tmp_path / f"{name}.json").write_text(json.dumps(value))

    def arguments(split: str) -> list[str]:
        return [
            "--suite",
            str(tmp_path / f"{split}.json"),
            "--fixture",
            str(tmp_path / "fixture.json"),
            "--candidates",
            str(tmp_path / "candidates.json"),
            "--mode",
            "fixture",
            "--output",
            str(tmp_path / f"{split}-report.json"),
        ]

    def factory(c: dict[str, object]) -> nullcontext[Counter]:
        return nullcontext(Counter())

    assert main([*arguments("tuning"), "--tune"], token_counter_factory=factory) == 0
    pinned = tmp_path / "tuning-report.json"
    digest = sha256(pinned.read_bytes()).hexdigest()
    assert (
        main(
            [
                *arguments("acceptance"),
                "--tuning-suite",
                str(tmp_path / "tuning.json"),
                "--tuning-sha256",
                sha256((tmp_path / "tuning.json").read_bytes()).hexdigest(),
                "--tuning-report",
                str(pinned),
                "--tuning-report-sha256",
                digest,
            ],
            token_counter_factory=factory,
        )
        == 0
    )
    published = record(json.loads((tmp_path / "acceptance-report.json").read_bytes()))
    selected = rows(published["candidates"])
    if enabled:
        assert -4 < float(str(record(selected[0]["parameters"])["threshold"])) <= 2
    assert sha256(pinned.read_bytes()).hexdigest() == digest
    assert len(rows(published["runs"])) == 3
    for run in rows(published["runs"]):
        assert run["execution_error"] is None
        positive, negative = rows(run["case_results"])
        assert [h["memory_id"] for h in rows(positive["hits"])] == ["m1"]
        assert negative["hits"] == []
        assert record(positive["return_budget"])["token_counter"] == Counter.method
    validate_public_report(
        published, suite=suites["acceptance"], candidates=selected, token_counter_factory=factory
    )


def test_real_cli_constructs_token_counter_from_explicit_model_config(
    new_suite: dict[str, object],
    candidate: dict[str, object],
    manifest: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("MEMORY_DATABASE_URL", raising=False)
    new_suite["split"] = "tuning"
    fixture = fixture_data(new_suite, {"m1": 0.9})
    constructed: list[dict[str, object]] = []

    class MeasuredEmbedding(FixtureEmbedding):
        real_model_calls = 0

        def embed(self, texts: tuple[str, ...], *, purpose: str) -> tuple[tuple[float, ...], ...]:
            self.real_model_calls += 1
            return super().embed(texts, purpose=purpose)

    def factories(
        config: dict[str, object], candidates: list[dict[str, object]]
    ) -> tuple[
        Callable[[dict[str, object]], MeasuredEmbedding],
        Callable[[dict[str, object]], FixtureSearch],
    ]:
        return lambda c: MeasuredEmbedding(c, fixture), lambda c: FixtureSearch(fixture)

    def counter_factory(
        config: dict[str, object], candidates: list[dict[str, object]]
    ) -> Callable[[dict[str, object]], nullcontext[Counter]]:
        constructed.append(deepcopy(config))
        return lambda c: nullcontext(Counter())

    class Observer:
        observation: dict[str, object] = {}

    monkeypatch.setattr("digital_souls_memory.evaluation.cli.real_factories", factories)
    monkeypatch.setattr(
        "digital_souls_memory.evaluation.cli.real_token_counter_factory", counter_factory
    )
    monkeypatch.setattr(
        "digital_souls_memory.evaluation.cli.ResourceMonitor",
        lambda **kwargs: nullcontext(Observer()),
    )
    config: dict[str, object] = {
        "endpoints": {str(candidate["model_alias"]): "http://127.0.0.1:18080"},
        "model_identities": {str(candidate["model_alias"]): candidate},
        "resources": {
            "db_container": "synthetic-db",
            "gpu_device": "0",
            "model_containers": {str(candidate["model_alias"]): "synthetic-model"},
        },
    }
    for name, value in (
        ("suite", new_suite),
        ("candidates", [candidate]),
        ("manifest", manifest),
        ("config", config),
    ):
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    output = tmp_path / "report.json"
    assert (
        main(
            [
                "--suite",
                str(tmp_path / "suite.json"),
                "--suite-sha256",
                sha256((tmp_path / "suite.json").read_bytes()).hexdigest(),
                "--candidates",
                str(tmp_path / "candidates.json"),
                "--manifest",
                str(tmp_path / "manifest.json"),
                "--real-config",
                str(tmp_path / "config.json"),
                "--mode",
                "real",
                "--tune",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert constructed == [config]
    published = record(json.loads(output.read_bytes()))
    for run in rows(published["runs"]):
        assert run["execution_error"] is None
        assert (
            record(rows(run["case_results"])[0]["return_budget"])["token_counter"] == Counter.method
        )


def test_acceptance_rejects_missing_manifest_before_tokenizing_pinned_report(
    new_suite: dict[str, object],
    candidate: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.evaluation.comparison import tuning_candidates

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    tuning = {**deepcopy(new_suite), "split": "tuning"}
    fixture = fixture_data(tuning, {"m1": 0.9})
    counted: list[str] = []

    class RecordingCounter(Counter):
        def count(self, text: str) -> int:
            counted.append(text)
            return super().count(text)

    class MeasuredEmbedding(FixtureEmbedding):
        real_model_calls = 0

        def embed(self, texts: tuple[str, ...], *, purpose: str) -> tuple[tuple[float, ...], ...]:
            self.real_model_calls += 1
            return super().embed(texts, purpose=purpose)

    def factory(c: dict[str, object]) -> nullcontext[RecordingCounter]:
        return nullcontext(RecordingCounter())

    report = run_evaluation(
        tuning,
        tuning_candidates([candidate], protocol=record(tuning["protocol"])),
        mode="real",
        embedding_factory=lambda c: MeasuredEmbedding(c, fixture),
        search_factory=lambda c: FixtureSearch(fixture),
        token_counter_factory=factory,
    )
    published = public_report(report, token_counter_factory=factory)
    counted.clear()
    acceptance = deepcopy(new_suite)
    case = rows(acceptance["cases"])[0]
    acceptance["cases"] = [{**case, "id": "acceptance-only"}]
    acceptance["gold"] = [{**rows(acceptance["gold"])[0], "case_id": "acceptance-only"}]
    for name, value in (
        ("tuning", tuning),
        ("acceptance", acceptance),
        ("tuning-report", published),
        ("candidates", [candidate]),
    ):
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    args = ["--mode", "real", "--output", str(tmp_path / "output.json")]
    for flag, name in (
        ("suite", "acceptance"),
        ("tuning-suite", "tuning"),
        ("tuning-report", "tuning-report"),
        ("candidates", "candidates"),
    ):
        path = tmp_path / f"{name}.json"
        args.extend([f"--{flag}", str(path)])
        digest_flag = {
            "suite": "suite-sha256",
            "tuning-suite": "tuning-sha256",
            "tuning-report": "tuning-report-sha256",
        }.get(flag)
        if digest_flag is not None:
            args.extend([f"--{digest_flag}", sha256(path.read_bytes()).hexdigest()])
    assert (
        main(
            args,
            embedding_factory=lambda c: MeasuredEmbedding(c, fixture),
            search_factory=lambda c: FixtureSearch(fixture),
            token_counter_factory=factory,
        )
        == 2
    )
    assert counted == []
    assert not (tmp_path / "output.json").exists()
