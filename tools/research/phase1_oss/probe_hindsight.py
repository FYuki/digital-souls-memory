"""Hindsight native-engine probes; synthetic DB and deterministic embeddings only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

ROOT = Path(os.environ.get("OSS_PROBE_ROOT", "/tmp/phase1-oss-reassessment"))
os.environ.update(
    HINDSIGHT_API_EMBEDDINGS_PROVIDER="openai",
    HINDSIGHT_API_RERANKER_PROVIDER="none",
    HINDSIGHT_API_LLM_PROVIDER="none",
    HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION="false",
    HINDSIGHT_API_RETAIN_EXTRACTION_MODE="chunks",
    HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS="384",
    HF_HUB_OFFLINE="1",
    TRANSFORMERS_OFFLINE="1",
)

# A hard guard: neither real models nor external network services can be used.
_socket_connect = socket.socket.connect


def unix_only_connect(sock, address):
    if sock.family != socket.AF_UNIX:
        raise RuntimeError(f"Network disabled by probe: {sock.family}")
    return _socket_connect(sock, address)


socket.socket.connect = unix_only_connect

from hindsight_api import RequestContext  # noqa: E402
from hindsight_api.engine.consolidation.consolidator import (  # noqa: E402
    _apply_dedup_create_fold,
    _dedup_adjudicate,
    _TemporalBounds,
)
from hindsight_api.engine.cross_encoder import RRFPassthroughCrossEncoder  # noqa: E402
from hindsight_api.engine.db_utils import acquire_with_retry  # noqa: E402
from hindsight_api.engine.embeddings import Embeddings  # noqa: E402
from hindsight_api.engine.memory_engine import MemoryEngine  # noqa: E402
from hindsight_api.engine.query_analyzer import QueryAnalysis, QueryAnalyzer  # noqa: E402
from hindsight_api.engine.response_models import LLMCallResult, TokenUsage  # noqa: E402
from hindsight_api.engine.task_backend import SyncTaskBackend  # noqa: E402


class FixtureEmbeddings(Embeddings):
    provider_name = "synthetic-fixture"
    dimension = 384

    async def initialize(self):
        pass

    async def encode(self, texts):
        # Deterministic vectors test storage/plumbing, never semantic quality.
        return [
            [(b - 127.5) / 127.5 for b in hashlib.sha384(text.encode()).digest()] * 8
            for text in texts
        ]


class FixtureQueryAnalyzer(QueryAnalyzer):
    def load(self):
        pass

    def analyze(self, query, reference_date=None):
        return QueryAnalysis()


async def main():
    socket_dir = os.environ.get("OSS_PROBE_SOCKET", str(ROOT / "pgsocket"))
    db_url = "postgresql://postgres@/oss_probe?host=" + socket_dir
    engine = MemoryEngine(
        db_url=db_url,
        memory_llm_provider="none",
        memory_llm_api_key="",
        memory_llm_model="none",
        embeddings=FixtureEmbeddings(),
        cross_encoder=RRFPassthroughCrossEncoder(),
        query_analyzer=FixtureQueryAnalyzer(),
        pool_min_size=1,
        pool_max_size=5,
        task_backend=SyncTaskBackend(),
        skip_llm_verification=True,
    )
    await engine.initialize()
    ctx = RequestContext()
    bank = "phase1-synthetic-" + uuid4().hex[:12]
    observations = []

    def check(name, result, requirement):
        assert result, name
        observations.append({"test": name, "execution": "PASS", "phase1": requirement})

    async def retain(document, text, tags=None, **extra):
        result = await engine.retain_batch_async(
            bank,
            [{"content": text, "document_id": document, "tags": tags or [], **extra}],
            request_context=ctx,
        )
        return result[0]

    try:
        await engine.update_bank(bank, name="Synthetic probe", request_context=ctx)
        text = "合成記録：アオは『青い星』と呼ぶ。"
        await retain("turn-1", text, ["character:a"])
        doc = await engine.get_document("turn-1", bank, request_context=ctx)
        check(
            "chunks_mode_stores_original_japanese",
            doc["original_text"] == text,
            "MATCH: source document retained",
        )
        await retain("turn-1", text, ["character:a"])
        doc = await engine.get_document("turn-1", bank, request_context=ctx)
        check(
            "same_document_same_text_idempotent",
            doc["memory_unit_count"] == 1,
            "MATCH: document-level deduplication",
        )

        await retain("global", "合成GLOBAL_MARKER", [])
        recall = await engine.recall_async(
            bank,
            "青い星",
            tags=["character:a"],
            tags_match="any_strict",
            include_chunks=True,
            request_context=ctx,
        )
        serialized = json.dumps(recall.model_dump(), ensure_ascii=False, default=str)
        check(
            "recall_can_return_original_chunks",
            text in serialized,
            "MATCH: explicit source-chunk retrieval",
        )
        check(
            "strict_tag_excludes_untagged",
            "GLOBAL_MARKER" not in serialized,
            "MATCH with adapter: use strict scope",
        )
        broad = await engine.recall_async(
            bank, "合成GLOBAL_MARKER", tags=["character:a"], request_context=ctx
        )
        check(
            "default_any_includes_untagged",
            "GLOBAL_MARKER" in json.dumps(broad.model_dump(), ensure_ascii=False, default=str),
            "GAP: default tags are not a perception boundary",
        )

        await retain("turn-1", "合成記録：別の本文へ置換", ["character:a"])
        replaced = await engine.get_document("turn-1", bank, request_context=ctx)
        check(
            "same_document_replaces_original_by_default",
            replaced["original_text"] != text,
            "GAP: append-only Turn protection must wrap retain",
        )

        # Two distinct mentions may remain as raw facts. The requirement is a
        # unified fact-facing representation, not a reinforcement score/time.
        mention1 = await retain("mention-1", "Aoi likes tea.", ["character:a"])
        mention2 = await retain("mention-2", "Tea is Aoi's favorite drink.", ["character:a"])
        backend = await engine._get_backend()
        unified_id = uuid4()
        unified_text = "Aoi likes tea."
        vector = (await engine.embeddings.encode([unified_text]))[0]
        async with acquire_with_retry(backend) as conn:
            await conn.execute(
                "INSERT INTO memory_units"
                "(id,bank_id,text,fact_type,event_date,source_memory_ids,"
                "proof_count,embedding,tags) "
                "VALUES($1,$2,$3,'observation',NOW(),$4,1,$5::vector,$6)",
                unified_id,
                bank,
                unified_text,
                [UUID(mention1[0])],
                str(vector),
                ["character:a"],
            )
        # Candidate lookup is native SQL. The supplied identical anchor vector
        # and merge verdict are fixtures: this tests consolidation mechanics,
        # not an actual model's semantic equivalence accuracy.
        judge = SimpleNamespace(
            call=AsyncMock(
                return_value=LLMCallResult(
                    content=json.dumps(
                        {"action": "merge", "text": unified_text, "reason": "same fact"}
                    ),
                    usage=TokenUsage(),
                )
            )
        )
        config = await engine._config_resolver.resolve_full_config(bank, ctx)
        outcome = await _dedup_adjudicate(
            backend,
            engine,
            bank,
            config,
            judge,
            "Aoi prefers tea.",
            str(vector),
            ["character:a"],
            exclude_id=None,
        )
        async with acquire_with_retry(backend) as conn:
            async with conn.transaction():
                merged_id = await _apply_dedup_create_fold(
                    conn,
                    engine,
                    bank,
                    outcome,
                    [UUID(mention2[0])],
                    _TemporalBounds(),
                )
            unified = await conn.fetchrow(
                "SELECT source_memory_ids FROM memory_units WHERE id=$1",
                unified_id,
            )
            n = await conn.fetchval(
                "SELECT count(*) FROM memory_units WHERE bank_id=$1 AND fact_type='observation'",
                bank,
            )
            raw_n = await conn.fetchval(
                "SELECT count(*) FROM memory_units WHERE bank_id=$1 "
                "AND document_id IN ('mention-1','mention-2')",
                bank,
            )
        check(
            "multiple_mentions_share_one_observation",
            merged_id == str(unified_id) and n == 1,
            "MATCH mechanism: native dedup folds a new mention into one existing observation",
        )
        check(
            "unified_fact_keeps_both_sources_and_raw_mentions",
            set(unified["source_memory_ids"]) == {UUID(mention1[0]), UUID(mention2[0])}
            and raw_n == 2,
            "MATCH mechanism: raw mentions may coexist while the fact view is unified",
        )

        doomed = await retain("doomed", "合成SECRET_MARKER", ["character:a"])
        kept = await retain("kept", "合成：アオはお茶を好む", ["character:a"])
        backend = await engine._get_backend()
        obs_id = uuid4()
        async with acquire_with_retry(backend) as conn:
            await conn.execute(
                "UPDATE memory_units SET consolidated_at=NOW() WHERE bank_id=$1", bank
            )
            # Seed a fixture observation, not an LLM-generated claim. Delete must
            # invalidate this stale text and requeue the surviving source.
            await conn.execute(
                "INSERT INTO memory_units"
                "(id,bank_id,text,fact_type,event_date,source_memory_ids,proof_count) "
                "VALUES($1,$2,$3,'observation',NOW(),$4,2)",
                obs_id,
                bank,
                "合成派生SECRET_MARKER",
                [UUID(doomed[0]), UUID(kept[0])],
            )
        deleted = await engine.delete_document("doomed", bank, request_context=ctx)
        check(
            "delete_document_removes_source",
            deleted["document_deleted"] == 1,
            "MATCH: document and native fact removal",
        )
        async with acquire_with_retry(backend) as conn:
            obs = await conn.fetchval("SELECT text FROM memory_units WHERE id=$1", obs_id)
            surviving = await conn.fetchrow(
                "SELECT text,consolidated_at FROM memory_units WHERE id=$1", UUID(kept[0])
            )
            residual = await conn.fetchval(
                "SELECT count(*) FROM memory_units "
                "WHERE bank_id=$1 AND text LIKE '%SECRET_MARKER%'",
                bank,
            )
        check(
            "source_delete_invalidates_shared_observation",
            obs is None and residual == 0,
            "MATCH: stale shared observation removed synchronously",
        )
        check(
            "source_delete_keeps_and_requeues_other_fact",
            surviving is not None and surviving["consolidated_at"] is None,
            "PARTIAL: surviving fact preserved; observation recomputation queued, not completed",
        )
        await retain("doomed", "合成SECRET_MARKER", ["character:a"])
        resurrected = await engine.get_document("doomed", bank, request_context=ctx)
        check(
            "deleted_document_id_can_be_recreated",
            resurrected is not None,
            "GAP: application tombstone/retry guard required",
        )
    finally:
        await engine.close()
    result = {
        "candidate": "hindsight",
        "bank": bank,
        "models": "fixture embeddings; chunks mode; real PostgreSQL",
        "observations": observations,
    }
    (ROOT / "hindsight-results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
