"""memory-v2.2: 旧比較器の実DB/モデルportと採点器を使う固定チャンク比較。

CLI: python -m tests.chunk_evaluation CONFIG OUTPUT_DIR
製品HTTPの代替ではない。内部採点protocolは互換性のためv2.1。
"""

import copy
import hashlib
import json
import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from digital_souls_memory.evaluation.adapters.real import (
    real_factories,
    real_reranker_factory,
    real_token_counter_factory,
)
from digital_souls_memory.evaluation.context import bounded_results
from digital_souls_memory.evaluation.models import rows, validate_suite
from digital_souls_memory.evaluation.ports import ObservedSearch
from digital_souls_memory.evaluation.retrieval import eligible_memories
from digital_souls_memory.evaluation.scoring import aggregate_run, score_case
from digital_souls_memory.recall.chunking import TextChunk, split_chunks


def chunk_case(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, tuple[str, TextChunk]]]:
    expanded = copy.deepcopy(case)
    expanded["memories"] = []
    mapping = {}
    for memory in case["memories"]:
        for chunk in split_chunks(memory["content"], length=512, overlap=64):
            identifier = f"{memory['id']}#{chunk.index}"
            expanded["memories"].append({**memory, "id": identifier, "content": chunk.text})
            mapping[identifier] = (memory["id"], chunk)
    return expanded, mapping


def collapse_hits(
    hits: list[dict[str, Any]], mapping: dict[str, tuple[str, TextChunk]]
) -> list[dict[str, Any]]:
    result, seen = [], set()
    for hit in sorted(
        hits,
        key=lambda h: (
            -h["reranker_score"],
            mapping[h["memory_id"]][0],
            mapping[h["memory_id"]][1].index,
        ),
    ):
        identifier, chunk = mapping[hit["memory_id"]]
        if identifier not in seen:
            seen.add(identifier)
            result.append(
                {
                    **hit,
                    "memory_id": identifier,
                    "matched_chunk": {"start": chunk.start, "end": chunk.end},
                }
            )
    return result


def main() -> None:
    config = json.loads(Path(sys.argv[1]).read_text())
    output = Path(sys.argv[2])
    output.mkdir(parents=True, exist_ok=True)
    candidate = json.loads(
        Path("evals/retrieval/memory-v2.1/candidates-bge-m3-rerank.json").read_text()
    )[0]
    ef, sf = real_factories(config, [candidate])
    rf = real_reranker_factory(config, [candidate])
    assert rf is not None
    tf = real_token_counter_factory(config, [candidate])
    frozen = {
        s: Path(f"evals/retrieval/memory-v2.2/{s}/suite.json").read_bytes()
        for s in ("tuning", "acceptance")
    }
    receipt = {
        "comparison_version": "memory-v2.2",
        "chunk_length": 512,
        "chunk_overlap": 64,
        "threshold": -2,
        "tuning": "固定採用値の確認のみ・探索なし",
        "suite_sha256": {s: hashlib.sha256(b).hexdigest() for s, b in frozen.items()},
        "started_at": datetime.now(UTC).isoformat(),
    }
    (output / "frozen.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    for split, raw in frozen.items():
        suite = json.loads(raw)
        # 既存validator/採点器の版識別子を変更せず、新版は外側manifestで識別する。
        validate_suite(suite, tuning_case_ids=set())
        protocol = suite["protocol"]
        gold = {g["case_id"]: g for g in suite["gold"]}
        runs: list[dict[str, Any]] = []
        for index in range(1, 4):
            results: list[dict[str, Any]] = []
            with ExitStack() as stack:
                embedding = stack.enter_context(ef(candidate))
                search = stack.enter_context(sf(candidate))
                assert isinstance(search, ObservedSearch)
                reranker = stack.enter_context(rf(candidate))
                counter = stack.enter_context(tf(candidate))
                for case in suite["cases"]:
                    filtered = {**case, "memories": eligible_memories(case), "protocol": protocol}
                    expanded, mapping = chunk_case(filtered)
                    memories = expanded["memories"]
                    vectors = (
                        embedding.embed(tuple(m["content"] for m in memories), purpose="passage")
                        if memories
                        else ()
                    )
                    search.index(expanded, vectors)
                    query = embedding.embed((case["query"],), purpose="query")[0]
                    observations: list[dict[str, Any]] = [dict(o) for o in search.observe(query)]
                    sending = sorted(
                        observations,
                        key=lambda h: (
                            -h["score"],
                            mapping[h["memory_id"]][0],
                            mapping[h["memory_id"]][1].index,
                        ),
                    )[:20]
                    by_id = {m["id"]: m for m in memories}
                    scores = (
                        reranker.rerank(
                            case["query"],
                            tuple(
                                {
                                    "memory_id": h["memory_id"],
                                    "content": by_id[h["memory_id"]]["content"],
                                }
                                for h in sending
                            ),
                        )
                        if sending
                        else []
                    )
                    relevance = {s["memory_id"]: s["score"] for s in scores}
                    observations = [
                        {**h, "reranker_score": relevance.get(h["memory_id"])} for h in observations
                    ]
                    passing = [
                        h
                        for h in observations
                        if h["reranker_score"] is not None and h["reranker_score"] >= -2
                    ]
                    hits = collapse_hits(passing, mapping)
                    # 窓の検索中心は選択チャンク。原文span自体は変更しない。
                    selected: list[dict[str, Any]] = []
                    excluded: list[dict[str, object]] = []
                    for hit in hits:
                        chunk = hit["matched_chunk"]
                        memory = next(
                            m for m in filtered["memories"] if m["id"] == hit["memory_id"]
                        )
                        window_query = memory["content"][chunk["start"] : chunk["end"]]
                        chosen, budget = bounded_results(
                            [*selected, hit],
                            case={**case, "query": window_query},
                            limit=5,
                            budget=1500,
                            token_counter=counter,
                        )
                        selected = chosen
                        excluded.extend(rows(budget["excluded"]))
                    result = score_case(
                        case,
                        gold[case["id"]],
                        selected,
                        threshold=-2,
                        protocol=protocol,
                        token_counter=counter,
                    )
                    result["candidate_observations"] = [
                        {**h, "memory_id": mapping[h["memory_id"]][0]} for h in observations
                    ]
                    result["excluded"] = excluded
                    results.append(result)
            runs.append(
                {
                    "run_index": index,
                    "summary": aggregate_run(results, protocol=protocol),
                    "case_results": results,
                }
            )
            (output / f"{split}.json").write_text(
                json.dumps(
                    {"comparison_version": "memory-v2.2", "runs": runs},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            print(split, index, runs[-1]["summary"]["passed"], flush=True)
        if split == "tuning":
            receipt["tuning_result_sha256"] = hashlib.sha256(
                (output / "tuning.json").read_bytes()
            ).hexdigest()
            receipt["acceptance_started_at"] = datetime.now(UTC).isoformat()
            (output / "frozen.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
