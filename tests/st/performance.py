"""Explicit real-model HTTP measurements with a predeclared manifest."""

import json
import os
import sys
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
    concurrency: Literal[1]
    cold_condition: str = Field(min_length=1)
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


def run(manifest: Manifest, output: Path) -> bool:
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
        with new_client() as client:
            with httpx.Client(
                base_url=os.environ["MEMORY_CORE_ST_URL"],
                timeout=30,
                trust_env=False,
            ) as preflight:
                config_response = preflight.get("/_st/config")
                config_response.raise_for_status()
                configuration = config_response.json()
                if configuration != {
                    "mode": "performance",
                    "real_models": True,
                    "embedding": manifest.embedding,
                    "reranker": manifest.reranker,
                }:
                    raise ValueError("Real-model ST deployment required")
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
                warm = [measure(client, character, request) for _ in range(manifest.warm_up)]
                trials = [measure(client, character, request) for _ in range(manifest.repetitions)]
                summary = summarize_latency(
                    [s for s, _ in trials],
                    expected_count=manifest.repetitions,
                    real_models=True,
                )
                success = success and summary.required_pass and all(s.success for s, _ in warm)
                runs.append(
                    {
                        "data_count": count,
                        "summary": asdict(summary),
                        "warm_up": [asdict(s) | {"error": e} for s, e in warm],
                        "trials": [asdict(s) | {"error": e} for s, e in trials],
                    }
                )
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
