"""固定Hindsightの自動worker経路を調べる合成probe。品質・性能試験ではない。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

# 既存probeのAF_UNIX限定guardと初期化部品だけを流用。mainは呼ばない。
# isort: off
from probe_hindsight import (
    ROOT,
    FixtureEmbeddings,
    FixtureQueryAnalyzer,
    MemoryEngine,
    RequestContext,
    RRFPassthroughCrossEncoder,
    acquire_with_retry,
)

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.response_models import LLMCallResult, TokenUsage
from hindsight_api.engine.search.tags import TagGroupLeaf
from hindsight_api.engine.task_backend import WorkerTaskBackend
from hindsight_api.extensions.operation_validator import (
    OperationValidationError,
    OperationValidatorExtension,
    ValidationResult,
)
from hindsight_api.worker.poller import WorkerPoller
# isort: on


class Gate:
    def __init__(self):
        self.marker = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def arm(self, marker):
        self.marker = marker
        self.entered.clear()
        self.release.clear()

    async def stop(self, text):
        if self.marker and self.marker in text:
            self.marker = None
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), 30)


class ConstantEmbeddings(FixtureEmbeddings):
    """全候補を近似重複判定へ通す。類似度・日本語品質を一切評価しない。"""

    def __init__(self):
        self.gate = Gate()

    async def encode(self, texts):
        await self.gate.stop("\n".join(texts))
        return [[1.0] + [0.0] * 383 for _ in texts]


ALIASES = {"青は茶を好む。": "青はお茶が好き。", "青の好物はお茶。": "青はお茶が好き。"}


def canonical(text):
    return ALIASES.get(text, text)


class FixtureLLM:
    """LLM境界のみを置換。候補検索・検証・DB書込・workerは上流のまま。"""

    provider = "mock"

    def __init__(self):
        self.calls = []
        self.gate = Gate()

    def with_config(self, *args, **kwargs):
        return self

    async def cleanup(self):
        pass

    async def call(self, **kwargs):
        prompt = "\n".join(m["content"] for m in kwargs["messages"] if m["role"] == "user")
        scope = kwargs["scope"]
        self.calls.append({"scope": scope, "prompt": prompt})
        await self.gate.stop(prompt)
        if scope == "consolidation_dedup":
            new = prompt.split("[NEW] ", 1)[1].split("\n[EXISTING]", 1)[0].strip()
            old = prompt.split("[EXISTING] ", 1)[1].split("\n\nRespond", 1)[0].strip()
            same_topic = new.startswith("青はお茶が好き。") and old.startswith("青はお茶が好き。")
            same = canonical(new) == canonical(old) or same_topic
            text = old if len(old) >= len(new) else new
            payload = {
                "action": "merge" if same else "keep",
                "text": text if same else "",
                "reason": "fixture",
            }
        elif scope == "consolidation":
            facts = re.findall(
                r"^\[([0-9a-f-]{36})\] (.*?)"
                r"(?: \((?:occurred_start|occurred_end|mentioned_at)=.*)?$",
                prompt,
                re.M,
            )
            if not facts:
                raise AssertionError("fixture did not parse native fact prompt")
            # 意図的にCREATEを返す。後段のnative reconciliationが重複を解消する。
            grouped = {}
            for mid, text in facts:
                normalized = canonical(text)
                key = "tea" if normalized.startswith("青はお茶が好き。") else normalized
                action = grouped.setdefault(
                    key, {"text": text, "source_fact_ids": [], "reason": "fixture"}
                )
                action["source_fact_ids"].append(mid)
                if len(text) > len(action["text"]):
                    action["text"] = text
            payload = {"creates": list(grouped.values()), "updates": [], "deletes": []}
        else:
            raise AssertionError(f"unexpected model call: {scope}")
        return LLMCallResult(
            content=kwargs["response_format"].model_validate(payload), usage=TokenUsage()
        )


class CharacterScope(OperationValidatorExtension):
    def __init__(self):
        super().__init__({})

    async def validate_retain(self, ctx):
        return ValidationResult.accept()

    async def validate_recall(self, ctx):
        return ValidationResult.accept()

    async def validate_reflect(self, ctx):
        return ValidationResult.accept()

    async def resolve_tag_scope(self, ctx):
        if ctx.request_context.api_key in {"a", "b"}:
            return [
                TagGroupLeaf(tags=["character:" + ctx.request_context.api_key], match="any_strict")
            ]
        return None

    async def resolve_write_tag_scope(self, ctx):
        if ctx.request_context.api_key in {"a", "b"}:
            return ["character:" + ctx.request_context.api_key]
        return None


async def main():
    os.environ["HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION"] = "true"
    # noneは上流でobservationsを強制OFFにするため、fixtureの存在を明示する。
    raw = _get_raw_config()
    raw.llm_provider = "mock"
    raw.enable_observations = True
    raw.enable_auto_consolidation = True
    raw.retain_extraction_mode = "chunks"
    embeddings, fixture = ConstantEmbeddings(), FixtureLLM()
    engine = MemoryEngine(
        db_url="postgresql://postgres@/oss_probe?host=" + os.environ["OSS_PROBE_SOCKET"],
        memory_llm_provider="none",
        memory_llm_api_key="",
        memory_llm_model="none",
        embeddings=embeddings,
        cross_encoder=RRFPassthroughCrossEncoder(),
        query_analyzer=FixtureQueryAnalyzer(),
        pool_min_size=1,
        pool_max_size=10,
        task_backend=WorkerTaskBackend(),
        skip_llm_verification=True,
    )
    results, snapshots, environment = [], {}, {}
    output = Path(os.environ.get("OSS_DOMAIN_OUTPUT", str(ROOT / "hindsight-domain-results.json")))

    def check(name, ok, details=None, criterion="native mechanism"):
        results.append(
            {
                "test": name,
                "execution": "PASS",
                "assertion": "PASS" if ok else "FAIL",
                "criterion": criterion,
                "details": details,
            }
        )

    async def rows(bank):
        async with acquire_with_retry(engine._backend) as conn:
            data = await conn.fetch(
                "SELECT id,text,fact_type,tags,document_id,source_memory_ids,consolidated_at "
                "FROM memory_units WHERE bank_id=$1 ORDER BY created_at,id",
                bank,
            )
            return [dict(r) for r in data]

    async def ops():
        async with acquire_with_retry(engine._backend) as conn:
            return [
                dict(r)
                for r in await conn.fetch(
                    "SELECT operation_id,bank_id,operation_type,status,error_message "
                    "FROM async_operations ORDER BY created_at"
                )
            ]

    async def drain():
        # 本番workerのclaim→実行→完了記録。pollerの常駐loopの代わりに有限回drain。
        for _ in range(80):
            batch = await poller.claim_batch()
            if not batch:
                pending = [r for r in await ops() if r["status"] not in {"completed", "cancelled"}]
                if pending:
                    raise AssertionError(f"worker did not drain: {pending}")
                return
            await asyncio.wait_for(asyncio.gather(*(poller.execute_task(t) for t in batch)), 45)
            if not await poller.wait_for_active_tasks(timeout=40):
                raise AssertionError("worker tasks did not complete")
        raise AssertionError("worker drain exceeded bounded rounds")

    async def bank(label):
        name = "domain-" + label + "-" + uuid4().hex[:8]
        await engine.update_bank(name, name="合成ドメイン検証", request_context=ctx)
        return name

    async def retain(bank, doc, text, tags=None, scopes="per_tag", **extra):
        return await engine.retain_batch_async(
            bank,
            [
                {
                    "content": text,
                    "document_id": doc,
                    "tags": tags or ["character:a"],
                    "observation_scopes": scopes,
                    **extra,
                }
            ],
            request_context=ctx,
        )

    async def recall(bank, key="character:a", **extra):
        res = await engine.recall_async(
            bank,
            "合成",
            tags=[key],
            tags_match="any_strict",
            include_chunks=True,
            max_tokens=12000,
            request_context=ctx,
            **extra,
        )
        return res.model_dump(mode="json")

    try:
        await engine.initialize()
        async with acquire_with_retry(engine._backend) as conn:
            environment["postgres_version"] = await conn.fetchval("SELECT version()")
            environment["extensions"] = [
                dict(r)
                for r in await conn.fetch(
                    "SELECT extname,extversion FROM pg_extension ORDER BY extname"
                )
            ]
        environment["python"] = os.sys.version
        environment["search_backend"] = raw.text_search_extension
        environment["postgres_image"] = os.environ.get("OSS_PROBE_POSTGRES_IMAGE")
        engine._llm_config = fixture
        engine._consolidation_llm_config = fixture
        ctx = RequestContext()
        poller = WorkerPoller(
            engine._backend,
            "domain-fixture-worker",
            engine.execute_task,
            max_slots=4,
            max_retries=0,
        )
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as guard_socket:
                guard_socket.connect(("203.0.113.1", 443))
        except (RuntimeError, OSError):
            check(
                "external_network_guard",
                True,
                "AF_INET/AF_INET6 connect rejected; Docker --network none",
            )
        else:
            check("external_network_guard", False)

        # d: seedなし。retainの自動submitからnative workerがobservationを作る。
        b = await bank("dedup")
        for i, text in enumerate(
            [
                "青はお茶が好き。",
                "青は茶を好む。",
                "青の好物はお茶。",
                "青は猫を2匹飼う。",
                "青は猫を3匹飼う。",
                "青はお茶が嫌い。",
                "赤はお茶が好き。",
            ]
        ):
            await retain(b, f"turn-{i}", text)
            await drain()
        r = await rows(b)
        obs = [x for x in r if x["fact_type"] == "observation"]
        tea = [x for x in obs if x["text"] == "青はお茶が好き。"]
        check(
            "auto_consolidation_equivalent_mentions",
            len(tea) == 1 and len(tea[0]["source_memory_ids"]) == 3,
            {"observations": obs, "raw_count": len(r) - len(obs)},
        )
        check(
            "auto_consolidation_distinct_number_negation_subject",
            len(obs) == 5,
            [x["text"] for x in obs],
            "fixture意味判定が異なるFactを区別する経路。実モデル精度はNOT RUN",
        )
        fact_view = await engine.recall_async(
            b,
            "合成",
            fact_type=["observation"],
            tags=["character:a"],
            tags_match="any_strict",
            max_tokens=12000,
            request_context=ctx,
        )
        check(
            "observation_only_recall_unifies_fact_view",
            len(fact_view.results) == 5,
            fact_view.model_dump(mode="json"),
            "raw factを同時返却せず統合済みFactを利用する",
        )
        check(
            "native_reconciliation_called",
            any(c["scope"] == "consolidation_dedup" for c in fixture.calls),
        )
        snapshots["dedup"] = r

        # a: 2出典のobservationを自動生成し削除→再統合が完了するまで実行。
        b = await bank("delete")
        await retain(b, "doomed", "青はお茶が好き。秘密符号=SECRET_MARKER")
        await drain()
        await retain(b, "kept", "青はお茶が好き。")
        await drain()
        before = await rows(b)
        check(
            "delete_fixture_has_shared_observation",
            any(
                x["fact_type"] == "observation"
                and len(x["source_memory_ids"]) == 2
                and "SECRET_MARKER" in x["text"]
                for x in before
            ),
        )
        await engine.delete_document("doomed", b, request_context=ctx)
        requeued = await rows(b)
        check(
            "delete_invalidates_and_requeues",
            not any(x["fact_type"] == "observation" for x in requeued)
            and any(x["document_id"] == "kept" and x["consolidated_at"] is None for x in requeued),
        )
        await drain()
        after, retrieved = await rows(b), await recall(b)
        check(
            "delete_reconsolidation_completes",
            len([x for x in after if x["fact_type"] == "observation"]) == 1
            and all(x["consolidated_at"] for x in after if x["fact_type"] != "observation"),
            after,
        )
        check(
            "delete_secret_absent_from_observation_and_recall",
            "SECRET_MARKER" not in json.dumps([after, retrieved], default=str),
            retrieved,
        )
        snapshots["deletion"] = {
            "before": before,
            "requeued": requeued,
            "after": after,
            "recall": retrieved,
        }

        # b1: consolidateが読んだ削除対象のLLM応答を後から返す競合。
        b = await bank("race-consolidation")
        await retain(b, "doomed", "RACE_SECRET")
        fixture.gate.arm("RACE_SECRET")
        running = asyncio.create_task(drain())
        await asyncio.wait_for(fixture.gate.entered.wait(), 20)
        await engine.delete_document("doomed", b, request_context=ctx)
        fixture.gate.release.set()
        await running
        check(
            "delete_during_consolidation_no_resurrection",
            "RACE_SECRET" not in json.dumps([await rows(b), await recall(b)], default=str),
        )

        # b2: 生存出典も持つ応答が削除後に到着。本文だけが残る危険も調べる。
        b = await bank("race-mixed")
        await retain(b, "doomed", "青はお茶が好き。秘密符号=MIXED_SECRET")
        await retain(b, "kept", "青はお茶が好き。")
        fixture.gate.arm("MIXED_SECRET")
        running = asyncio.create_task(drain())
        await asyncio.wait_for(fixture.gate.entered.wait(), 20)
        await engine.delete_document("doomed", b, request_context=ctx)
        fixture.gate.release.set()
        await running
        await drain()
        mixed = {"rows": await rows(b), "recall": await recall(b)}
        check(
            "delete_during_mixed_consolidation_no_stale_text",
            "MIXED_SECRET" not in json.dumps(mixed, default=str),
            mixed,
        )

        # b3: 新たなretainがembedding待ちの間に同IDを削除。tombstoneの有無。
        b = await bank("race-retain")
        await retain(b, "doomed", "RETAIN_SECRET")
        await drain()
        embeddings.gate.arm("RETAIN_SECRET")
        task = asyncio.create_task(retain(b, "doomed", "RETAIN_SECRET", force_reextract=True))
        await asyncio.wait_for(embeddings.gate.entered.wait(), 20)
        await engine.delete_document("doomed", b, request_context=ctx)
        embeddings.gate.release.set()
        retain_error = None
        try:
            await task
        except Exception as exc:
            retain_error = {"type": type(exc).__name__, "message": str(exc)}
        await drain()
        raced = {"rows": await rows(b), "error": retain_error}
        check(
            "delete_during_retain_no_resurrection",
            "RETAIN_SECRET" not in json.dumps(raced["rows"], default=str),
            raced,
            "削除した内容の復活を防ぐ",
        )
        await engine.delete_document("doomed", b, request_context=ctx)
        await drain()
        await retain(b, "doomed", "RETAIN_SECRET")
        await drain()
        replay = await rows(b)
        check(
            "delete_blocks_late_retry",
            "RETAIN_SECRET" not in json.dumps(replay, default=str),
            replay,
            "削除後の同ID・同内容の再送を拒否する",
        )

        # c: observationのscopeとdocument chunkのscopeをそれぞれ確認。
        for mode in ("per_tag", "combined", "shared"):
            b = await bank("scope-" + mode)
            await retain(b, "only-a", "A_PRIVATE", ["character:a"], mode)
            await retain(b, "only-b", "B_PRIVATE", ["character:b"], mode)
            await retain(b, "both", "SHARED_PUBLIC", ["character:a", "character:b"], mode)
            await drain()
            ar, br = await recall(b), await recall(b, "character:b")
            check(
                "strict_scope_no_cross_character_" + mode,
                "B_PRIVATE" not in json.dumps(ar) and "A_PRIVATE" not in json.dumps(br),
                {"a": ar, "b": br, "rows": await rows(b)},
            )
            if mode == "per_tag":
                ar_text = json.dumps(ar.get("chunks", {}))
                br_text = json.dumps(br.get("chunks", {}))
                check(
                    "strict_scope_returns_allowed_chunks",
                    "A_PRIVATE" in ar_text
                    and "SHARED_PUBLIC" in ar_text
                    and "B_PRIVATE" in br_text
                    and "SHARED_PUBLIC" in br_text,
                    {"a_chunks": ar.get("chunks"), "b_chunks": br.get("chunks")},
                )
            observations_a = json.dumps(ar.get("results", []), ensure_ascii=False)
            check(
                "shared_fact_visible_as_observation_" + mode,
                any(
                    x.get("fact_type") == "observation" and "SHARED_PUBLIC" in x.get("text", "")
                    for x in ar.get("results", [])
                ),
                observations_a,
            )
            if mode == "shared":
                default_result = await engine.recall_async(
                    b, "合成", tags=["character:a"], max_tokens=12000, request_context=ctx
                )
                check(
                    "shared_scope_default_any_exposes_other_character",
                    "B_PRIVATE" in default_result.model_dump_json(),
                    default_result.model_dump(mode="json"),
                    "危険設定の陽性対照。PASSはM6適合を意味しない",
                )
            if mode == "per_tag":
                scoped_bank = b
                scoped_doc = await engine.get_document("only-b", b, request_context=ctx)
                check("scope_fixture_has_b_document", scoped_doc is not None)
        # c追加: 共有観測を先に作り、Aだけが知覚した同じ主題の詳細を後から統合する。
        # 一意なmarkerの別Factだけではcross-scope foldを検出できない。
        for mode in ("per_tag", "combined", "custom"):
            b = await bank("topic-scope-" + mode)
            shared_scopes = [["character:a"], ["character:b"]] if mode == "custom" else mode
            private_scopes = [["character:a"]] if mode == "custom" else mode
            await retain(
                b, "shared", "青はお茶が好き。", ["character:a", "character:b"], shared_scopes
            )
            await drain()
            await retain(
                b,
                "private",
                "青はお茶が好き。秘密符号=A_ONLY_DETAIL",
                ["character:a"],
                private_scopes,
            )
            await drain()
            a_result, b_result = await recall(b), await recall(b, "character:b")
            check(
                "same_topic_scope_blocks_private_detail_" + mode,
                "A_ONLY_DETAIL" in json.dumps(a_result)
                and "A_ONLY_DETAIL" not in json.dumps(b_result),
                {"a": a_result, "b": b_result, "rows": await rows(b)},
                "同主題の共有Factと私的詳細を統合してもBに私的詳細を返さない",
            )
        engine._operation_validator = CharacterScope()
        for who, forbidden in (("a", "B_PRIVATE"), ("b", "A_PRIVATE")):
            scoped_ctx = RequestContext(api_key=who)
            returned = await engine.recall_async(
                scoped_bank,
                "合成",
                include_chunks=True,
                max_tokens=12000,
                request_context=scoped_ctx,
            )
            hidden = await engine.get_document(
                "only-" + ("b" if who == "a" else "a"), scoped_bank, request_context=scoped_ctx
            )
            check(
                "forced_scope_without_caller_tags_" + who,
                forbidden not in returned.model_dump_json() and hidden is None,
                returned.model_dump(mode="json"),
            )
        try:
            await engine.retain_batch_async(
                scoped_bank,
                [{"document_id": "forged", "content": "FORGED", "tags": ["character:b"]}],
                request_context=RequestContext(api_key="a"),
            )
        except OperationValidationError as exc:
            check(
                "forced_write_scope_rejects_forged_tags",
                True,
                {"type": type(exc).__name__, "message": str(exc)},
            )
        else:
            check("forced_write_scope_rejects_forged_tags", False)
        engine._operation_validator = None

        # e: 原文の不変性と派生再処理を別々に検査する。
        b = await bank("documents")
        await retain(b, "session", "TURN_ONE")
        await drain()
        await retain(b, "session", "TURN_TWO", update_mode="append")
        await drain()
        appended = await engine.get_document("session", b, request_context=ctx)
        check(
            "append_preserves_prior_text",
            "TURN_ONE" in appended["original_text"] and "TURN_TWO" in appended["original_text"],
            appended,
        )
        await retain(b, "session", "TURN_TWO", update_mode="append")
        await drain()
        retried = await engine.get_document("session", b, request_context=ctx)
        check(
            "append_retry_idempotent",
            retried["original_text"] == appended["original_text"],
            retried,
            "Turnの再送による二重追記を防ぐ",
        )
        await retain(b, "session", "TURN_THREE")
        await drain()
        replaced = await engine.get_document("session", b, request_context=ctx)
        check(
            "replace_removes_old_text_and_derived",
            replaced["original_text"] == "TURN_THREE"
            and "TURN_ONE" not in json.dumps(await rows(b), default=str),
            replaced,
        )
        await retain(b, "turn-immutable", "逐語の合成Turn")
        await drain()
        before = await rows(b)
        await engine.reprocess_document(b, "turn-immutable", request_context=ctx)
        await drain()
        after = await rows(b)
        doc = await engine.get_document("turn-immutable", b, request_context=ctx)

        def ids(records):
            return {str(x["id"]) for x in records if x["document_id"] == "turn-immutable"}

        check(
            "reprocess_preserves_original_and_rebuilds_units",
            doc["original_text"] == "逐語の合成Turn" and ids(before).isdisjoint(ids(after)),
            {"before": before, "after": after, "document": doc},
        )
        check(
            "all_native_operations_completed",
            all(x["status"] == "completed" for x in await ops()),
            await ops(),
        )
    except Exception as exc:
        results.append(
            {
                "test": "probe_aborted",
                "execution": "FAIL",
                "assertion": "NOT RUN",
                "details": {"type": type(exc).__name__, "message": str(exc)},
            }
        )
        raise
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "candidate": "hindsight",
                    "source_commit": "f29ba70c825094c839daaf27b2b5a564de46fb4e",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "environment": environment,
                    "models": (
                        "fixture LLM at provider boundary; constant 384d embeddings; "
                        "chunks retain; native PostgreSQL worker claim/execute; RRF"
                    ),
                    "execution_counts": dict(Counter(x["execution"] for x in results)),
                    "assertion_counts": dict(Counter(x["assertion"] for x in results)),
                    "results": results,
                    "snapshots": snapshots,
                    "fixture_call_counts": dict(Counter(x["scope"] for x in fixture.calls)),
                    "not_run": [
                        "実LLM/embeddingの日本語品質",
                        "実モデル込みp95",
                        "全競合scheduleの網羅",
                        "Reflection/Belief/人格の実装",
                        "本番常駐workerの障害復旧",
                    ],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            + "\n"
        )
        await engine.close()
        print(
            json.dumps(
                {
                    "output": str(output),
                    "execution": dict(Counter(x["execution"] for x in results)),
                    "assertions": dict(Counter(x["assertion"] for x in results)),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
