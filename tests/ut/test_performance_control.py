"""The host control boundary owns only its dedicated Compose Memory service."""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.ut


def test_restart_targets_only_memory_in_owned_compose_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.st import performance_control

    commands: list[list[str]] = []

    def completed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", completed)
    performance_control.restart_memory_service("memory-core-st-synthetic")
    restarts = [command for command in commands if "restart" in command]
    assert len(restarts) == 1
    (restart,) = restarts
    assert restart[:2] == ["docker", "compose"]
    assert restart[restart.index("-p") + 1] == "memory-core-st-synthetic"
    assert "compose.core-st.yml" in restart
    assert "compose.core-st.performance.yml" in restart
    assert restart[-1] == "memory"
    for command in commands:
        assert "down" not in command
        assert {"postgres", "init", "runner"}.isdisjoint(command)


def test_restart_command_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.st import performance_control

    commands: list[list[str]] = []

    def failed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        raise subprocess.CalledProcessError(17, command)

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        performance_control.restart_memory_service("memory-core-st-synthetic")
    assert len(commands) == 1


@pytest.mark.parametrize("passed", [True, False])
def test_host_entry_records_runtime_and_connects_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, passed: bool
) -> None:
    from tests.st import performance_control
    from tests.st.performance import Manifest
    from tests.ut.test_core_st_harness import manifest_payload
    from tests.ut.test_performance_conditions import cold_condition

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload() | {"cold_condition": cold_condition()}))
    output = tmp_path / "performance.json"
    project = "memory-core-st-synthetic"
    monkeypatch.setattr(sys, "argv", ["control", str(manifest_path), str(output), project])
    commands: list[list[str]] = []

    def completed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="uv 0.8.22\n", stderr="")

    def measured(manifest: Manifest, path: Path, *, restart_memory: Callable[[], None]) -> bool:
        assert manifest.model_dump(mode="json")["cold_condition"] == cold_condition()
        assert path == output
        assert os.environ["MEMORY_CORE_ST_URL"] == "http://127.0.0.1:18039"
        assert "127.0.0.1:55439/memory_core_st" in os.environ["MEMORY_CORE_ST_AUDIT_URL"]
        restart_memory()
        return passed

    monkeypatch.setattr(subprocess, "run", completed)
    monkeypatch.setattr(performance_control, "run", measured)
    # Restore environment writes performed by the host entry at teardown.
    monkeypatch.setenv("MEMORY_CORE_ST_URL", "synthetic-before")
    monkeypatch.setenv("MEMORY_CORE_ST_AUDIT_URL", "synthetic-before")
    with pytest.raises(SystemExit) as error:
        performance_control.main()
    assert error.value.code == (0 if passed else 1)
    metadata = json.loads((tmp_path / "host-control.json").read_text())
    assert metadata["python"] == "3.12.3"
    assert metadata["uv"] == "uv 0.8.22"
    assert metadata["executable"] == sys.executable
    assert metadata["project"] == project
    assert str(manifest_path) in metadata["command"]
    assert str(output) in metadata["command"]
    assert commands[0] == ["uv", "--version"]
    assert commands[1][-3:] == ["restart", "--no-deps", "memory"]
    assert project in commands[1]


def test_host_entry_rejects_legacy_condition_before_any_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.st import performance_control
    from tests.ut.test_core_st_harness import manifest_payload

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_payload()))
    monkeypatch.setattr(
        sys, "argv", ["control", str(path), str(tmp_path / "out.json"), "memory-core-st-synthetic"]
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: pytest.fail("No operation for legacy input")
    )
    with pytest.raises(ValueError):
        performance_control.main()
    assert not (tmp_path / "host-control.json").exists()
