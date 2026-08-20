"""`hermes doctor` must surface the ABSOLUTE runtime interpreter path.

Regression coverage for papercut pc-bf39c22e: reproducing a default-profile cron bug,
an agent guessed `~/.hermes/venv/bin/python` from an old layout; that path does not
exist on this fleet. Doctor reported the Python VERSION and that a venv entry point
existed, but never the interpreter path itself — which is the thing a cron/launchd job
actually needs to be written correctly.

`sys.executable` is authoritative: it is the interpreter doctor is running under, which
is by construction the one the CLI uses.
"""
from __future__ import annotations

import re
import sys


def _doctor_output() -> str:
    """Run the REAL doctor and capture its output.

    Calls `hermes_cli.doctor.run_doctor` directly rather than shelling out — doctor is
    a long, network-touching command and the subprocess forms (`-m hermes_cli`,
    `from hermes_cli import main`) are not valid entry points. Direct invocation also
    guarantees we exercise THIS worktree's code.
    """
    import argparse
    import contextlib
    import io

    from hermes_cli.doctor import run_doctor

    args = argparse.Namespace(fix=False, verbose=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        with contextlib.suppress(SystemExit):
            run_doctor(args)
    return buf.getvalue()


def test_doctor_prints_the_interpreter_path():
    out = _doctor_output()
    assert "Interpreter:" in out, "doctor no longer reports the interpreter path"


def test_the_reported_path_is_absolute_and_real():
    """A relative or nonexistent path would be worse than printing nothing."""
    import os

    out = _doctor_output()
    m = re.search(r"Interpreter:\s*(\S+)", out)
    assert m, "could not parse the interpreter line"
    path = m.group(1)
    assert os.path.isabs(path), f"interpreter path is not absolute: {path}"
    assert os.path.exists(path), f"interpreter path does not exist: {path}"


def test_the_reported_path_is_a_working_python():
    """Prove the surfaced path is actually runnable — the whole point is that a
    caller can paste it into a cron/launchd job."""
    import os
    import subprocess

    out = _doctor_output()
    path = re.search(r"Interpreter:\s*(\S+)", out).group(1)
    assert os.access(path, os.X_OK), f"interpreter is not executable: {path}"
    probe = subprocess.run(
        [path, "-c", "import sys; print(sys.version_info[:2])"],
        capture_output=True, text=True, timeout=120,
    )
    assert probe.returncode == 0, f"interpreter failed to run: {probe.stderr[:200]}"


def test_version_line_is_still_present():
    """Regression guard: adding the path must not displace the version check."""
    out = _doctor_output()
    assert re.search(r"Python \d+\.\d+\.\d+", out), "the Python version line disappeared"
