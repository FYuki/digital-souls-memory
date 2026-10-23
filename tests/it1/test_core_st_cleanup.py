"""Exercise the shell owner's terminal paths without Docker or network access."""

import os
import pty
import signal
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("ending,expected", [("success", 0), ("failure", 17), ("interrupt", 130)])
def test_runner_preserves_status_and_cleans_resources(
    tmp_path: Path, ending: str, expected: int
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

root = Path(os.environ['MEMORY_CORE_ST_EVIDENCE_DIR'])
args = sys.argv[1:]
if args[0] == 'compose':
    operation = args[5]
    with (root / 'operations.txt').open('a') as out:
        out.write(operation + '\\n')
    if operation == 'up':
        (root / 'resource').touch()
    elif operation == 'run':
        (root / 'ready').touch()
        if os.environ['TEST_ENDING'] == 'interrupt':
            while True:
                time.sleep(0.01)
        if os.environ['TEST_ENDING'] == 'failure':
            sys.exit(17)
    elif operation == 'down':
        (root / 'resource').unlink()
elif (root / 'resource').exists():
    print('synthetic-resource')
"""
    )
    docker.chmod(0o755)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    pid, terminal = pty.fork()
    if pid == 0:
        os.environ.update(
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            MEMORY_CORE_ST_EVIDENCE_DIR=str(evidence),
            TEST_ENDING=ending,
        )
        os.execvp("bash", ["bash", "tools/with-core-st.sh"])
    exited = False
    try:
        deadline = time.monotonic() + 10
        interrupted = False
        while time.monotonic() < deadline:
            if ending == "interrupt" and (evidence / "ready").exists() and not interrupted:
                os.write(terminal, b"\x03")
                interrupted = True
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                exited = True
                break
            time.sleep(0.01)
        else:
            pytest.fail("runner did not finish its terminal path")
        assert os.waitstatus_to_exitcode(status) == expected
        result = dict(
            line.split("=", 1) for line in (evidence / "result.txt").read_text().splitlines()
        )
        assert int(result["exit_status"]) == expected
        operations = (evidence / "operations.txt").read_text().splitlines()
        assert operations == ["config", "build", "up", "run", "logs", "down"]
        assert not (evidence / "resource").exists()
        assert (evidence / "tests.log").exists()
        assert (evidence / "cleanup.log").exists()
    finally:
        if not exited:
            os.killpg(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(terminal)
