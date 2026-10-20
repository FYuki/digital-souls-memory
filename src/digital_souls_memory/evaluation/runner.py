"""Execute independent runs; preserve errors without fake fallback."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime
from resource import RUSAGE_SELF, getrusage
from time import perf_counter, process_time

from .comparison import resolved_protocol
from .models import (
    integer,
    number,
    record,
    rows,
    string,
    strings,
    unique,
    validate_candidate,
    validate_suite,
    validate_vectors,
)
from .ports import (
    Embedding,
    EmbeddingFactory,
    MeasuredEmbedding,
    MeasuredReranker,
    MeasuredSearch,
    ObservedSearch,
    RerankerFactory,
    ResourceFactory,
    ResourceObserver,
    SearchFactory,
)
from .retrieval import (
    bounded_results,
    eligible_memories,
    select_observations,
    validate_observations,
)
from .scoring import aggregate_run, percentile, score_case, stage_latency


def run_evaluation(
    suite: dict[str, object],
    candidates: list[dict[str, object]],
    *,
    mode: str,
    embedding_factory: EmbeddingFactory,
    search_factory: SearchFactory,
    resource_factory: ResourceFactory | None = None,
    reranker_factory: RerankerFactory | None = None,
) -> dict[str, object]:
    validate_suite(suite, tuning_case_ids=set())
    if mode not in ("fixture", "real") or not candidates:
        raise ValueError("Explicit mode and candidates required")
    unique(candidates, "id")
    for candidate in candidates:
        validate_candidate(
            candidate,
            require_context=record(suite["protocol"]).get("comparison_version") == "memory-v2",
        )
        resolved_protocol(record(suite["protocol"]), candidate)
    snapshot = deepcopy(suite)
    protocol = record(snapshot["protocol"])
    v2 = protocol.get("comparison_version") == "memory-v2"
    if reranker_factory is not None and not v2:
        raise ValueError("Reranker requires memory-v2")
    if any("reranker" in c for c in candidates) and reranker_factory is None:
        raise ValueError("Configured reranker requires an explicit port")
    gold = {string(item["case_id"]): item for item in rows(snapshot["gold"])}
    runs: list[dict[str, object]] = []
    for candidate in candidates:
        resolved = resolved_protocol(protocol, candidate)
        for index in range(1, integer(protocol["repetitions"]) + 1):
            results: list[dict[str, object]] = []
            run: dict[str, object] = {
                "candidate_id": candidate["id"],
                "run_index": index,
                "real_model_calls": 0,
                "execution_error": None,
                "quality_passed": None,
                "case_results": results,
            }
            if v2:
                run["reranker_enabled"] = reranker_factory is not None
            embedding: Embedding | None = None
            observer: ResourceObserver | None = None
            reranker = None
            cpu_started = process_time()
            try:
                with ExitStack() as resources:
                    if resource_factory is not None:
                        observer = resources.enter_context(resource_factory(deepcopy(candidate)))
                    embedding = resources.enter_context(embedding_factory(deepcopy(candidate)))
                    search = resources.enter_context(search_factory(deepcopy(candidate)))
                    reranker = (
                        resources.enter_context(reranker_factory(deepcopy(candidate)))
                        if reranker_factory is not None
                        else None
                    )
                    if v2 and not isinstance(search, ObservedSearch):
                        raise ValueError("All-candidate observation port required")
                    dimensions = integer(candidate["dimensions"])
                    for case in rows(snapshot["cases"]):
                        memories = eligible_memories(case)
                        if v2 and set(strings(gold[string(case["id"])]["safety_forbidden_ids"])) & {
                            m["id"] for m in memories
                        }:
                            raise ValueError("Safety-forbidden candidate passed eligibility")
                        source_ids = {
                            identifier
                            for memory in memories
                            for identifier in strings(memory["source_turn_ids"])
                        }
                        filtered = {
                            **deepcopy(case),
                            "memories": deepcopy(memories),
                            "turns": [t for t in rows(case["turns"]) if t["turn_id"] in source_ids],
                            "protocol": deepcopy(resolved),
                        }
                        started = perf_counter()
                        if memories:
                            vectors = validate_vectors(
                                embedding.embed(
                                    tuple(string(m["content"]) for m in memories), purpose="passage"
                                ),
                                count=len(memories),
                                dimensions=dimensions,
                            )
                        else:
                            vectors = ()
                        passage_embedding_ms = (perf_counter() - started) * 1000
                        started = perf_counter()
                        search.index(filtered, vectors)
                        index_ms = (perf_counter() - started) * 1000
                        started = perf_counter()
                        query_started = started
                        query_vector = validate_vectors(
                            embedding.embed((string(case["query"]),), purpose="query"),
                            count=1,
                            dimensions=dimensions,
                        )[0]
                        embedding_ms = (perf_counter() - started) * 1000
                        started = perf_counter()
                        observations: list[dict[str, object]] = []
                        rerank_ms = 0.0
                        if v2:
                            assert isinstance(search, ObservedSearch)
                            observations = search.observe(query_vector)
                            validate_observations(observations, case, resolved)
                            if any(o["reranker_score"] is not None for o in observations):
                                raise ValueError("Search observations must precede reranking")
                            search_finished = perf_counter()
                            rerank_started = search_finished
                            if reranker is not None and observations:
                                scores = reranker.rerank(
                                    string(case["query"]),
                                    tuple(
                                        {"memory_id": m["id"], "content": m["content"]}
                                        for m in memories
                                    ),
                                )
                                if unique(scores, "memory_id") != {
                                    o["memory_id"] for o in observations
                                }:
                                    raise ValueError("Reranker response IDs mismatch")
                                by_id = {string(s["memory_id"]): number(s["score"]) for s in scores}
                                observations = [
                                    {**o, "reranker_score": by_id[string(o["memory_id"])]}
                                    for o in observations
                                ]
                            rerank_ms = (perf_counter() - rerank_started) * 1000
                            selection_started = perf_counter()
                            hits = select_observations(observations, resolved)
                        else:
                            hits = search.search(query_vector, limit=integer(protocol["top_k"]))
                        selected, budget = bounded_results(
                            hits,
                            limit=integer(resolved["top_k"]),
                            budget=integer(resolved["token_budget"]),
                        )
                        finished = perf_counter()
                        search_ms = ((search_finished if v2 else finished) - started) * 1000
                        started = perf_counter()
                        result = score_case(
                            case,
                            gold[string(case["id"])],
                            selected,
                            threshold=resolved["threshold"],
                            protocol=resolved,
                        )
                        if v2:
                            result["candidate_observations"] = [
                                {
                                    key: deepcopy(o[key])
                                    for key in (
                                        "memory_id",
                                        "cosine",
                                        "components",
                                        "score",
                                        "reranker_score",
                                    )
                                }
                                for o in observations
                            ]
                            result["eligible_ids"] = sorted(string(m["id"]) for m in memories)
                            result["rerank_ms"] = rerank_ms
                            result["result_selection_ms"] = (finished - selection_started) * 1000
                        result["scoring_ms"] = (perf_counter() - started) * 1000
                        result["passage_embedding_ms"] = passage_embedding_ms
                        result["index_ms"] = index_ms
                        result["query_embedding_ms"] = embedding_ms
                        result["search_ms"] = search_ms
                        result["recall_ms"] = (finished - query_started) * 1000
                        result["retrieved_hits"] = hits
                        result["return_budget"] = budget
                        if isinstance(search, MeasuredSearch):
                            result["database"] = deepcopy(search.measurements)
                        results.append(result)
                    if mode == "real":
                        if not isinstance(embedding, MeasuredEmbedding):
                            raise ValueError("Real call evidence required")
                        integer(embedding.real_model_calls)
                        if reranker is not None and not isinstance(reranker, MeasuredReranker):
                            raise ValueError("Real reranker call evidence required")
                run.update(aggregate_run(results, protocol=resolved))
                if v2:
                    run["stage_latency"] = stage_latency(results)
                samples = [float(str(result["recall_ms"])) for result in results]
                p95 = percentile(samples, 0.95, method="linear")
                run["latency"] = {
                    "p50_ms": percentile(samples, 0.5, method="linear"),
                    "p95_ms": p95,
                    "target_p95_met": p95 <= 300,
                    "required_p95_met": p95 <= 600,
                }
                run["quality_passed"] = run["passed"] if mode == "real" else None
            except Exception:
                # External exception messages can contain request bodies or private paths.
                run["execution_error"] = "execution_error"
                run["case_count"] = len(results)
            finally:
                run["harness_cpu_ms"] = (process_time() - cpu_started) * 1000
                run["harness_process_peak_rss_kib"] = getrusage(RUSAGE_SELF).ru_maxrss
                if mode == "real" and isinstance(embedding, MeasuredEmbedding):
                    run["real_model_calls"] = embedding.real_model_calls
                if v2:
                    run["real_reranker_calls"] = (
                        reranker.real_model_calls
                        if mode == "real" and isinstance(reranker, MeasuredReranker)
                        else 0
                    )
                if observer is not None and observer.observation:
                    run["resources"] = deepcopy(observer.observation)
            runs.append(run)
    quality = (
        all(run["quality_passed"] is True for run in runs)
        if mode == "real" and all(run["execution_error"] is None for run in runs)
        else None
    )
    return {
        "mode": mode,
        "created_at": datetime.now(UTC).isoformat(),
        "candidates": deepcopy(candidates),
        "suite": snapshot,
        "runs": runs,
        "quality_passed": quality,
    }
