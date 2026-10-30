"""Host-owned restart control for the dedicated synthetic performance deployment."""

import json
import os
import platform
import shlex
import subprocess
import sys
from functools import partial
from pathlib import Path

from tests.st.performance import ColdCondition, Manifest, run


def restart_memory_service(project: str) -> None:
    if not project.startswith("memory-core-st-"):
        raise ValueError("A dedicated core ST project is required")
    subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            "compose.core-st.yml",
            "-f",
            "compose.core-st.performance.yml",
            "restart",
            "--no-deps",
            "memory",
        ],
        check=True,
    )


def main() -> None:
    manifest_path, output_path, project = sys.argv[1:]
    manifest = Manifest.model_validate_json(Path(manifest_path).read_text())
    if not isinstance(manifest.cold_condition, ColdCondition):
        raise ValueError("performance-cold requires a structured cold condition")
    output = Path(output_path)
    output.with_name("host-control.json").write_text(
        json.dumps(
            {
                "python": platform.python_version(),
                "executable": sys.executable,
                "uv": subprocess.run(
                    ["uv", "--version"], check=True, capture_output=True, text=True
                ).stdout.strip(),
                "command": shlex.join(
                    [sys.executable, "-m", "tests.st.performance_control", *sys.argv[1:]]
                ),
                "project": project,
            },
            indent=2,
        )
    )
    os.environ["MEMORY_CORE_ST_URL"] = "http://127.0.0.1:18039"
    os.environ["MEMORY_CORE_ST_AUDIT_URL"] = (
        "postgresql://memory_st_admin:synthetic-only@127.0.0.1:55439/memory_core_st"
    )
    raise SystemExit(
        0 if run(manifest, output, restart_memory=partial(restart_memory_service, project)) else 1
    )


if __name__ == "__main__":
    main()
