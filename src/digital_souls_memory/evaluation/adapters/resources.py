"""Sample dedicated container counters and whole-GPU memory; never infer missing values."""

import re
import subprocess
from threading import Event, Thread
from time import monotonic
from typing import Self

import httpx

from ..models import number, record


def gpu_used_mib(device: str) -> float:
    output = subprocess.run(
        [
            "nvidia-smi",
            "--id=" + device,
            "--query-gpu=memory.used",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    return number(float(output.stdout.strip()))


class ResourceMonitor:
    def __init__(
        self,
        *,
        db_container: str,
        model_container: str,
        gpu_device: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if (
            any(
                not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value)
                for value in (db_container, model_container, gpu_device)
            )
            or db_container == model_container
        ):
            raise ValueError("Distinct dedicated containers and explicit GPU required")
        self.containers = {"db": db_container, "model": model_container}
        self.gpu_device = gpu_device
        self.client = httpx.Client(
            base_url="http://docker",
            trust_env=False,
            timeout=5,
            transport=httpx.HTTPTransport(uds="/var/run/docker.sock")
            if transport is None
            else transport,
        )
        self.stopped = Event()
        self.samples: list[dict[str, object]] = []
        self.error: Exception | None = None
        self.started = monotonic()
        self.thread = Thread(target=self._observe, daemon=True)
        self.observation: dict[str, object] = {}

    def sample(self) -> None:
        sample: dict[str, object] = {"elapsed_ms": (monotonic() - self.started) * 1000}
        for role, container in self.containers.items():
            response = self.client.get(
                f"/containers/{container}/stats", params={"stream": "false", "one-shot": "true"}
            )
            response.raise_for_status()
            stats = record(response.json())
            sample[role + "_cpu_ns"] = number(
                record(record(stats["cpu_stats"])["cpu_usage"])["total_usage"]
            )
            sample[role + "_ram_bytes"] = number(record(stats["memory_stats"])["usage"])
        sample["gpu_total_used_mib"] = gpu_used_mib(self.gpu_device)
        if any(number(value) < 0 for value in sample.values()):
            raise ValueError("Negative resource observation")
        self.samples.append(sample)

    def _observe(self) -> None:
        while not self.stopped.wait(0.1):
            try:
                self.sample()
            except Exception as error:
                self.error = error
                return

    def __enter__(self) -> Self:
        try:
            self.sample()
            self.thread.start()
            return self
        except BaseException:
            self.client.close()
            raise

    def __exit__(self, *exc: object) -> None:
        self.stopped.set()
        self.thread.join()
        try:
            if self.error is not None:
                raise self.error
            self.sample()
            self.observation = {
                "sample_interval_ms": 100,
                "samples": self.samples,
                **{
                    role + "_cpu_ms": (
                        number(self.samples[-1][role + "_cpu_ns"])
                        - number(self.samples[0][role + "_cpu_ns"])
                    )
                    / 1e6
                    for role in self.containers
                },
            }
            if any(number(self.observation[role + "_cpu_ms"]) < 0 for role in self.containers):
                raise ValueError("Container counter reset during evaluation")
        finally:
            self.client.close()
