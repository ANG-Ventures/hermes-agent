"""Regression coverage for ``hermes cron`` process exit status propagation.

``cmd_cron`` used to call ``cron_command(args)`` without returning its result,
so ``main()`` saw ``None`` and exited 0 for every failure: a missing job and a
``no_agent`` script that exited nonzero both printed ``✗ … failed: …`` and then
handed the shell ``EXIT=0``. Callers (deploy gates, acceptance scripts) read
that as success.

These drive the REAL CLI in a subprocess against an isolated ``HERMES_HOME`` so
the assertion is on the process exit status a caller actually observes — an
in-process ``cron_command()`` call returns the right code today and would not
have caught the defect, which lived in the dispatch wiring.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]


def _run_hermes(home: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Keep the sandbox off any ambient board/session wiring.
    for name in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


@pytest.fixture()
def cron_home(tmp_path):
    """An isolated Hermes home with a passing and a failing no_agent script."""
    home = tmp_path / "hermes"
    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "ok.sh").write_text("#!/bin/bash\necho hello-ok\n")
    (scripts / "bad.sh").write_text("#!/bin/bash\necho boom >&2\nexit 3\n")
    for name in ("ok.sh", "bad.sh"):
        (scripts / name).chmod(0o755)
    return home


def _create_script_job(home: Path, name: str, script: str) -> None:
    created = _run_hermes(
        home,
        "cron",
        "create",
        "every 1d",
        "--name",
        name,
        "--script",
        script,
        "--no-agent",
        "--deliver",
        "local",
    )
    assert created.returncode == 0, created.stdout + created.stderr
    assert "Mode: no-agent" in created.stdout, created.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="bash no_agent scripts are POSIX-only")
def test_cron_run_wait_missing_job_exits_nonzero(cron_home):
    """An unknown job reference must not hand the caller a zero exit status."""
    result = _run_hermes(cron_home, "cron", "run", "definitely-no-such-job", "--wait")

    assert "Job not found: definitely-no-such-job" in result.stdout, result.stdout
    assert result.returncode != 0, (
        f"missing job exited {result.returncode} (false green)\n{result.stdout}"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="bash no_agent scripts are POSIX-only")
def test_cron_run_wait_failed_script_exits_nonzero(cron_home):
    """A no_agent script exiting nonzero must fail the CLI, not just print ✗."""
    _create_script_job(cron_home, "badjob", "bad.sh")

    result = _run_hermes(cron_home, "cron", "run", "badjob", "--wait")

    assert "Script exited with code 3" in result.stdout, result.stdout
    assert result.returncode != 0, (
        f"failed script exited {result.returncode} (false green)\n{result.stdout}"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="bash no_agent scripts are POSIX-only")
def test_cron_run_wait_successful_script_exits_zero(cron_home):
    """The fix must not turn healthy runs red — success still exits 0."""
    _create_script_job(cron_home, "okjob", "ok.sh")

    result = _run_hermes(cron_home, "cron", "run", "okjob", "--wait")

    assert "completed" in result.stdout, result.stdout
    assert "hello-ok" in result.stdout, result.stdout
    assert result.returncode == 0, (
        f"successful script exited {result.returncode}\n{result.stdout}{result.stderr}"
    )
