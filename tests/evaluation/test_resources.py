import httpx
import pytest

from digital_souls_memory.evaluation.adapters.resources import ResourceMonitor, gpu_used_mib

pytestmark = pytest.mark.it1


def test_resource_samples_keep_container_cpu_ram_and_gpu_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = {"db": 0, "model": 0}

    def stats(request: httpx.Request) -> httpx.Response:
        container = request.url.path.split("/")[2]
        ticks[container] += 1
        return httpx.Response(
            200,
            json={
                "cpu_stats": {"cpu_usage": {"total_usage": ticks[container] * 1000000}},
                "memory_stats": {"usage": 4096},
            },
        )

    monkeypatch.setattr(
        "digital_souls_memory.evaluation.adapters.resources.gpu_used_mib", lambda device: 123.0
    )
    with ResourceMonitor(
        db_container="db",
        model_container="model",
        gpu_device="0",
        transport=httpx.MockTransport(stats),
    ) as monitor:
        monitor.sample()
    evidence = monitor.observation
    assert evidence["db_cpu_ms"] == len(monitor.samples) - 1
    assert evidence["model_cpu_ms"] == len(monitor.samples) - 1
    assert evidence["sample_interval_ms"] == 100
    assert len(monitor.samples) >= 3
    assert evidence["samples"][0]["gpu_total_used_mib"] == 123  # type: ignore[index]
    assert evidence["samples"][0]["db_ram_bytes"] == 4096  # type: ignore[index]


def test_resource_failure_is_not_replaced_by_zero() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(httpx.HTTPStatusError):
        with ResourceMonitor(
            db_container="db",
            model_container="model",
            gpu_device="0",
            transport=httpx.MockTransport(broken),
        ):
            pytest.fail("Missing measurements must fail before evaluation")


def test_gpu_reader_uses_the_requested_device_and_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    outputs = iter(["2048\n", "NaN\n"])
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(command, 0, stdout=next(outputs), stderr="")

    monkeypatch.setattr("digital_souls_memory.evaluation.adapters.resources.subprocess.run", run)
    assert gpu_used_mib("0") == 2048
    with pytest.raises(ValueError):
        gpu_used_mib("0")
    assert all("--id=0" in command for command in commands)


def test_background_sample_failure_closes_sampler_and_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Event

    failed = Event()
    calls = 0

    def stats(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 2:
            failed.set()
            return httpx.Response(404)
        return httpx.Response(
            200, json={"cpu_stats": {"cpu_usage": {"total_usage": 1}}, "memory_stats": {"usage": 1}}
        )

    monkeypatch.setattr(
        "digital_souls_memory.evaluation.adapters.resources.gpu_used_mib", lambda device: 0.0
    )
    monitor = ResourceMonitor(
        db_container="db",
        model_container="model",
        gpu_device="0",
        transport=httpx.MockTransport(stats),
    )
    with pytest.raises(httpx.HTTPStatusError):
        with monitor:
            assert failed.wait(2)
    assert not monitor.thread.is_alive()
    assert monitor.client.is_closed
