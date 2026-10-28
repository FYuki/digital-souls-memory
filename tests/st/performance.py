"""Explicit real-model HTTP measurements with a predeclared manifest."""

import json
import os
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import uuid4

import httpx
import psycopg
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from digital_souls_memory.client import (
    MemoryClient,
    MemoryHTTPError,
    MemoryResponseError,
    MemoryTransportError,
)
from digital_souls_memory.client.models import RecallRequest
from digital_souls_memory.recall.settings import EMBEDDING_MODEL, RERANKER_MODEL
from tests.st.latency import LatencySample, summarize_latency
from tests.st.support import add_session, add_turn, eventually, new_client, register


class ColdCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    restart_memory_service: Literal[True]
    restart_postgres: Literal[False]
    restart_model_servers: Literal[False]
    flush_os_cache: Literal[False]


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    environment: str = Field(min_length=1)
    hardware: str = Field(min_length=1)
    db_image: str = Field(min_length=1)
    client_image: str = Field(min_length=1)
    db_version: str = Field(min_length=1)
    embedding: dict[str, object]
    reranker: dict[str, object]
    dimensions: Literal[1024]
    data_counts: tuple[StrictInt, ...]
    input_text: str = Field(min_length=1)
    input_length: StrictInt
    candidate_limit: Literal[200]
    rerank_limit: Literal[20]
    result_limit: Literal[5]
    token_limit: Literal[1500]
    relevance_threshold: Literal[-2]
    repetitions: int = Field(ge=100, strict=True)
    warm_up: int = Field(ge=0, strict=True)
    concurrency: int = Field(default=1, ge=1, strict=True)
    cold_condition: str | ColdCondition
    period: str = Field(min_length=1)
    aggregation: Literal["nearest-rank"]

    @model_validator(mode="after")
    def fixed_conditions(self) -> "Manifest":
        if self.embedding != EMBEDDING_MODEL or self.reranker != RERANKER_MODEL:
            raise ValueError("Approved model identities and digests required")
        if not self.data_counts or any(n <= 0 for n in self.data_counts):
            raise ValueError("Positive dataset sizes required")
        if tuple(sorted(set(self.data_counts))) != self.data_counts:
            raise ValueError("Dataset sizes must be unique and increasing")
        if self.input_length != len(self.input_text):
            raise ValueError("Input length does not match the frozen text")
        if isinstance(self.cold_condition, str) and not self.cold_condition:
            raise ValueError("A nonempty cold condition is required")
        return self


def measure(
    client: MemoryClient,
    character: str,
    request: RecallRequest,
) -> tuple[LatencySample, str | None]:
    started = perf_counter()
    try:
        response = client.recall(character, request)
    except (MemoryHTTPError, MemoryTransportError, MemoryResponseError) as error:
        return LatencySample(
            elapsed_ms=(perf_counter() - started) * 1000,
            success=False,
            provision_id=None,
        ), type(error).__name__
    return LatencySample(
        elapsed_ms=(perf_counter() - started) * 1000,
        success=True,
        provision_id=response.provision_id,
    ), None


def check_deployment(manifest: Manifest, *, wait_for_health: bool) -> None:
    with httpx.Client(
        base_url=os.environ["MEMORY_CORE_ST_URL"], timeout=30, trust_env=False
    ) as preflight:
        if wait_for_health:

            def healthy() -> bool:
                try:
                    return preflight.get("/health").is_success
                except httpx.TransportError:
                    return False

            eventually(healthy, timeout=180)
        config_response = preflight.get("/_st/config")
        config_response.raise_for_status()
        if config_response.json() != {
            "mode": "performance",
            "real_models": True,
            "embedding": manifest.embedding,
            "reranker": manifest.reranker,
        }:
            raise ValueError("Real-model ST deployment required")


def parallel_trials(
    manifest: Manifest, character: str, request: RecallRequest
) -> list[tuple[LatencySample, str | None]]:
    def worker(index: int) -> list[tuple[LatencySample, str | None]]:
        with new_client() as client:
            return [
                measure(client, character, request)
                for _ in range(index, manifest.repetitions, manifest.concurrency)
            ]

    with ThreadPoolExecutor(max_workers=manifest.concurrency) as executor:
        batches = executor.map(worker, range(min(manifest.concurrency, manifest.repetitions)))
        return [trial for batch in batches for trial in batch]


def run(
    manifest: Manifest, output: Path, *, restart_memory: Callable[[], None] | None = None
) -> bool:
    results: dict[str, object] = {
        "manifest": manifest.model_dump(mode="json"),
        "started_at": datetime.now(UTC).isoformat(),
        "command": (
            "python -m tests.st.performance /evidence/manifest.json /evidence/performance.json"
        ),
        "mode": "real_models",
        "status": "FAIL",
    }
    runs: list[dict[str, object]] = []
    results["runs"] = runs
    success = True
    try:
        cold = isinstance(manifest.cold_condition, ColdCondition)
        if cold and restart_memory is None:
            raise ValueError("Cold measurement requires a Memory restart operation")
        with ExitStack() as clients:
            client = clients.enter_context(new_client())
            check_deployment(manifest, wait_for_health=False)
            character = "perf-" + uuid4().hex
            register(client, character)
            session = add_session(client, delivery=False, started_at=datetime.now(UTC))
            inserted = 0
            for count in manifest.data_counts:
                for index in range(inserted, count):
                    add_turn(
                        client,
                        session,
                        actor=f"synthetic-perf-user-{index}",
                        readers=[character],
                        text=manifest.input_text,
                    )
                inserted = count

                def indexed(expected: int = count) -> bool:
                    with psycopg.connect(os.environ["MEMORY_CORE_ST_AUDIT_URL"]) as conn:
                        return conn.execute(
                            "SELECT count(*) FROM recall_documents WHERE ready AND owner_id=%s",
                            (character,),
                        ).fetchone() == (expected,)

                eventually(indexed, timeout=1800)
                assert len(client.list_memories(character)) == count
                request = RecallRequest(
                    session_id=session,
                    format="materials",
                    query=manifest.input_text,
                    sensitivity="synthetic-sensitive",
                    classifier_version="synthetic-v1",
                )
                cold_trial = None
                if cold:
                    clients.close()
                    assert restart_memory is not None
                    restart_memory()
                    check_deployment(manifest, wait_for_health=True)
                    client = clients.enter_context(new_client())
                    cold_trial = measure(client, character, request)
                warm = [measure(client, character, request) for _ in range(manifest.warm_up)]
                started = perf_counter()
                trials = (
                    [measure(client, character, request) for _ in range(manifest.repetitions)]
                    if manifest.concurrency == 1
                    else parallel_trials(manifest, character, request)
                )
                elapsed = perf_counter() - started
                summary = summarize_latency(
                    [s for s, _ in trials],
                    expected_count=manifest.repetitions,
                    real_models=True,
                )
                success = success and summary.required_pass and all(s.success for s, _ in warm)
                if cold_trial is not None:
                    success = success and cold_trial[0].success
                measurement: dict[str, object] = {
                    "data_count": count,
                    "summary": asdict(summary),
                    "warm_up": [asdict(s) | {"error": e} for s, e in warm],
                    "trials": [asdict(s) | {"error": e} for s, e in trials],
                    "load": {
                        "failure_rate": sum(not s.success for s, _ in trials) / len(trials),
                        "elapsed_seconds": elapsed,
                        "throughput_rps": len(trials) / elapsed if elapsed > 0 else None,
                    },
                }
                if cold_trial is not None:
                    sample, error = cold_trial
                    measurement["cold_start"] = {
                        "sample": asdict(sample) | {"error": error},
                        "delta_p50_ms": (
                            sample.elapsed_ms - summary.p50_ms
                            if summary.p50_ms is not None
                            else None
                        ),
                    }
                runs.append(measurement)
                output.write_text(
                    json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False)
                )
        results["status"] = "PASS" if success else "FAIL"
    except Exception as error:
        results["error"] = type(error).__name__
        raise
    finally:
        results["finished_at"] = datetime.now(UTC).isoformat()
        output.write_text(json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False))
    return success


def main() -> None:
    manifest = Manifest.model_validate_json(Path(sys.argv[1]).read_text())
    raise SystemExit(0 if run(manifest, Path(sys.argv[2])) else 1)


if __name__ == "__main__":
    main()
