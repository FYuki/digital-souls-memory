"""Run the measurement owner against synthetic HTTP and indexing boundaries."""

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from threading import Barrier, Lock, current_thread, local, main_thread
from unittest.mock import MagicMock

import httpx
import psycopg
import pytest

from digital_souls_memory.client import MemoryClient
from digital_souls_memory.client.models import MemoryResponse
from digital_souls_memory.recall.settings import EMBEDDING_MODEL, RERANKER_MODEL
from tests.memory_support import memory
from tests.st import performance
from tests.ut.test_core_st_harness import manifest_payload
from tests.ut.test_performance_conditions import cold_condition

pytestmark = pytest.mark.ut


class Deployment:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.texts: list[str] = []
        self.queries: list[str] = []
        self.events: list[str] = []
        self.clients: list[MemoryClient] = []
        self.open_clients: set[int] = set()
        self.active_clients: set[int] = set()
        self.generation = 0
        self.calls_in_generation = 0
        self.clock = 0.0
        self.lock = Lock()
        self.barrier: Barrier | None = None
        self.active = 0
        self.peak = 0
        self.local = local()
        self.parallel = False
        self.fail_last = False
        original_http_client = httpx.Client
        monkeypatch.setenv("MEMORY_CORE_ST_URL", "http://synthetic")
        monkeypatch.setenv("MEMORY_CORE_ST_AUDIT_URL", "synthetic-unused-db")
        monkeypatch.setattr(performance, "register", lambda *args: None)
        monkeypatch.setattr(performance, "add_session", lambda *args, **kwargs: "session")
        monkeypatch.setattr(performance, "add_turn", self.add_turn)
        monkeypatch.setattr(performance, "new_client", self.new_client)
        monkeypatch.setattr(performance, "perf_counter", self.counter)
        connection = MagicMock()

        def indexed() -> tuple[int]:
            self.events.append("indexed")
            return (len(self.texts),)

        connection.__enter__.return_value.execute.return_value.fetchone.side_effect = indexed
        monkeypatch.setattr(psycopg, "connect", lambda *args: connection)
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kwargs: original_http_client(
                **({"transport": httpx.MockTransport(self.preflight)} | kwargs)
            ),
        )

    def add_turn(self, *args: object, text: str, **kwargs: object) -> str:
        self.texts.append(text)
        self.events.append("insert")
        return f"turn-{len(self.texts)}"

    def preflight(self, request: httpx.Request) -> httpx.Response:
        self.events.append("ready")
        if request.url.path == "/_st/config":
            return httpx.Response(
                200,
                json={
                    "mode": "performance",
                    "real_models": True,
                    "embedding": EMBEDDING_MODEL,
                    "reranker": RERANKER_MODEL,
                },
            )
        assert request.url.path in ("/health", "/health/live", "/health/ready")
        return httpx.Response(200, json={"status": "ok"})

    def new_client(self) -> MemoryClient:
        identifier = len(self.clients)
        deployment = self

        class Transport(httpx.MockTransport):
            def close(self) -> None:
                deployment.open_clients.remove(identifier)

        client = MemoryClient(
            base_url="http://synthetic",
            timeout=5,
            transport=Transport(lambda request: self.handle(identifier, request)),
        )
        self.clients.append(client)
        self.open_clients.add(identifier)
        return client

    def handle(self, identifier: int, request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/_st/config", "/health", "/health/live", "/health/ready"):
            return self.preflight(request)
        if request.url.path.endswith("/memories"):
            body = MemoryResponse.model_validate(asdict(memory())).model_dump(mode="json")
            return httpx.Response(200, json=[body] * len(self.texts))
        assert request.url.path.endswith("/recall")
        body = json.loads(request.content)
        assert body["session_id"] == "session"
        with self.lock:
            assert identifier not in self.active_clients, "A client is shared across workers"
            self.active_clients.add(identifier)
            self.queries.append(body["query"])
            index = len(self.queries)
            self.calls_in_generation += 1
            self.events.append("recall")
            self.active += 1
            self.peak = max(self.peak, self.active)
            first_wave = self.barrier is not None and index <= self.barrier.parties
        try:
            if first_wave:
                assert self.barrier is not None
                self.barrier.wait(timeout=5)
            if self.parallel:
                self.local.elapsed = (900 if index == 100 else index) / 1000
            else:
                self.clock += 0.7 if self.generation and self.calls_in_generation == 1 else 0.01
            if self.fail_last and index == 100:
                raise httpx.ReadError("synthetic failed response")
            return httpx.Response(
                200,
                json={
                    "provision_id": f"p-{index}",
                    "temperament_version": None,
                    "personality_version": None,
                    "memories": [],
                    "source_spans": [],
                    "verbatim_anchors": [],
                    "context": None,
                },
            )
        finally:
            with self.lock:
                self.active -= 1
                self.active_clients.remove(identifier)

    def counter(self) -> float:
        if self.parallel:
            if current_thread() is main_thread():
                value = self.clock
                self.clock = 2.0
                return value
            ending = getattr(self.local, "ending", False)
            self.local.ending = not ending
            return float(self.local.elapsed) if ending else 0.0
        return self.clock

    def restart(self) -> None:
        assert self.events[-1] == "indexed"
        assert not self.open_clients, "Close pre-restart HTTP connections before restarting"
        self.events.append("restart")
        self.generation += 1
        self.calls_in_generation = 0


@pytest.fixture
def deployment(monkeypatch: pytest.MonkeyPatch) -> Deployment:
    return Deployment(monkeypatch)


def test_cold_first_recall_precedes_warmup_for_each_dataset(
    deployment: Deployment, tmp_path: Path
) -> None:
    manifest = performance.Manifest.model_validate(
        manifest_payload()
        | {"data_counts": [1, 2], "warm_up": 2, "cold_condition": cold_condition()}
    )
    output = tmp_path / "performance.json"
    performance.run(manifest, output, restart_memory=deployment.restart)
    result = json.loads(output.read_text())
    assert deployment.generation == 2
    assert deployment.texts == [manifest.input_text] * 2
    assert len(deployment.queries) == 206
    for run in result["runs"]:
        assert run["cold_start"]["sample"]["elapsed_ms"] == pytest.approx(700)
        assert run["cold_start"]["delta_p50_ms"] == pytest.approx(690)
        assert len(run["warm_up"]) == 2
        assert len(run["trials"]) == 100
        assert run["summary"]["p50_ms"] == pytest.approx(10)
    for index, event in enumerate(deployment.events):
        if event == "restart":
            following = deployment.events[index + 1 :]
            assert following.index("ready") < following.index("recall")
    assert result["manifest"]["cold_condition"] == cold_condition()
    assert not deployment.open_clients


def test_json_inside_legacy_string_remains_warm_and_preserves_output(
    deployment: Deployment, tmp_path: Path
) -> None:
    condition = '{"restart_memory_service":true}'
    manifest = performance.Manifest.model_validate(
        manifest_payload() | {"data_counts": [100, 1000], "cold_condition": condition}
    )
    output = tmp_path / "performance.json"
    assert performance.run(manifest, output)
    result = json.loads(output.read_text())
    assert deployment.generation == 0
    assert len(deployment.texts) == 1000
    assert len(deployment.queries) == 210
    assert result["manifest"]["cold_condition"] == condition
    assert result["status"] == "PASS"
    for count, run in zip((100, 1000), result["runs"], strict=True):
        assert run["data_count"] == count
        assert len(run["warm_up"]) == 5
        assert len(run["trials"]) == 100
        assert set(run["summary"]) == {
            "p50_ms",
            "p95_ms",
            "required_pass",
            "p95_target_pass",
            "p50_target_pass",
        }
        assert all(s["success"] and s["provision_id"] and s["error"] is None for s in run["trials"])
    assert not deployment.open_clients


@pytest.mark.parametrize("workers", [4, 8])
@pytest.mark.parametrize("failed_trial", [False, True])
def test_parallel_trials_use_all_samples_and_wall_time(
    deployment: Deployment, tmp_path: Path, workers: int, failed_trial: bool
) -> None:
    deployment.parallel = True
    deployment.fail_last = failed_trial
    deployment.barrier = Barrier(workers)
    manifest = performance.Manifest.model_validate(
        manifest_payload() | {"data_counts": [1], "concurrency": workers, "warm_up": 0}
    )
    output = tmp_path / "performance.json"
    assert performance.run(manifest, output) is not failed_trial
    result = json.loads(output.read_text())
    (run,) = result["runs"]
    assert len(deployment.queries) == len(run["trials"]) == 100
    assert deployment.peak == workers
    assert run["summary"]["p50_ms"] == pytest.approx(50)
    assert run["summary"]["p95_ms"] == pytest.approx(95)
    assert run["summary"]["required_pass"] is not failed_trial
    assert run["load"]["failure_rate"] == pytest.approx(0.01 if failed_trial else 0)
    assert run["load"]["elapsed_seconds"] == pytest.approx(2)
    assert run["load"]["throughput_rps"] == pytest.approx(50)
    failed = [sample for sample in run["trials"] if not sample["success"]]
    assert len(failed) == int(failed_trial)
    if failed_trial:
        assert failed[0]["error"] == "MemoryTransportError"
        assert failed[0]["elapsed_ms"] == pytest.approx(900)
    assert result["status"] == ("FAIL" if failed_trial else "PASS")
    assert not deployment.open_clients


@pytest.mark.parametrize("failure", [RuntimeError, TimeoutError])
def test_restart_failure_keeps_failed_evidence_and_closes_clients(
    deployment: Deployment, tmp_path: Path, failure: type[Exception]
) -> None:
    manifest = performance.Manifest.model_validate(
        manifest_payload() | {"data_counts": [1], "cold_condition": cold_condition()}
    )

    def unavailable() -> None:
        deployment.restart()
        raise failure("synthetic restart/readiness failure")

    output = tmp_path / "performance.json"
    with pytest.raises(failure):
        performance.run(manifest, output, restart_memory=unavailable)
    result = json.loads(output.read_text())
    assert result["status"] == "FAIL"
    assert result["error"] == failure.__name__
    assert result["finished_at"] >= result["started_at"]
    assert deployment.queries == []
    assert not deployment.open_clients


def test_cold_without_restart_operation_fails_before_recall(
    deployment: Deployment,
    tmp_path: Path,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    manifest = performance.Manifest.model_validate(
        manifest_payload() | {"data_counts": [1], "cold_condition": cold_condition()}
    )
    # Only refusal is contractual; neither its exception class nor wording is fixed.
    with rejected() as error:
        performance.run(manifest, tmp_path / "performance.json")
    assert isinstance(error.value, Exception)
    assert deployment.queries == []


def test_long_input_reaches_ingestion_and_every_recall_without_truncation(
    deployment: Deployment, tmp_path: Path
) -> None:
    text = "合成の約束です。十一月三日に青い駅で本を返します。" * 50
    manifest = performance.Manifest.model_validate(
        manifest_payload()
        | {"data_counts": [1], "input_text": text, "input_length": len(text), "warm_up": 1}
    )
    assert performance.run(manifest, tmp_path / "performance.json")
    assert deployment.texts == [text]
    assert deployment.queries == [text] * 101


@pytest.mark.parametrize("ready", [True, False])
def test_post_restart_readiness_retries_without_recall_and_preserves_timeout(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ready: bool
) -> None:
    manifest = performance.Manifest.model_validate(
        manifest_payload() | {"data_counts": [1], "cold_condition": cold_condition()}
    )
    observations: list[bool] = []
    original = deployment.preflight
    attempts = 0

    def preflight(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/health":
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("synthetic restarting")
            if not ready or attempts == 2:
                return httpx.Response(503)
        return original(request)

    def bounded(check: Callable[[], bool], *, timeout: float = 30) -> None:
        if timeout == 180:
            assert deployment.queries == []
            observations.extend(check() for _ in range(3))
            if not observations[-1]:
                raise TimeoutError("synthetic readiness timeout")
        else:
            assert check()

    monkeypatch.setattr(deployment, "preflight", preflight)
    monkeypatch.setattr(performance, "eventually", bounded)
    output = tmp_path / "performance.json"
    if ready:
        assert performance.run(manifest, output, restart_memory=deployment.restart)
        assert observations == [False, False, True]
        assert len(deployment.queries) == 106
    else:
        with pytest.raises(TimeoutError):
            performance.run(manifest, output, restart_memory=deployment.restart)
        result = json.loads(output.read_text())
        assert result["status"] == "FAIL"
        assert result["error"] == "TimeoutError"
        assert deployment.queries == []
    assert not deployment.open_clients
