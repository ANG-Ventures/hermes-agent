"""kanban-video-orchestrator monitor.py: names its board, and a refused kanban call fails the run.

Prism 4a99c57c9f17: a bare ``hermes kanban list --tenant`` is refused (rc 2) once there is no
global board pointer; the monitor read the empty stdout as "0 tasks" and exited 0. A fake
``hermes`` on PATH records argv and plays the CLI's exit code.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2]
          / "optional-skills/creative/kanban-video-orchestrator/scripts/monitor.py")

pytestmark = pytest.mark.platforms("posix")


def _fake_hermes(tmp_path: Path, rc: int, stdout: str = "") -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "hermes"
    lines = ["#!/bin/sh", f'echo "$*" >> "{tmp_path / "argv.log"}"', f"printf '%s' '{stdout}'"]
    if rc:
        lines.append("echo 'kanban: list needs a board' >&2")
    lines.append(f"exit {rc}")
    fake.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fake.chmod(0o755)
    return bindir


def _run(tmp_path: Path, bindir: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}
    return subprocess.run([sys.executable, str(SCRIPT), "--tenant", "vid", "--once", *extra],
                          capture_output=True, text=True, env=env, timeout=60)


def test_refused_kanban_call_is_a_nonzero_monitor_exit(tmp_path):
    bindir = _fake_hermes(tmp_path, rc=2)
    out = _run(tmp_path, bindir)
    assert out.returncode != 0, out.stdout
    assert "Total: 0" not in out.stdout
    assert "exited 2" in out.stderr


def test_every_kanban_call_names_the_board(tmp_path):
    bindir = _fake_hermes(tmp_path, rc=0, stdout="[]")
    out = _run(tmp_path, bindir, "--board", "vids")
    assert out.returncode == 0, out.stderr
    calls = (tmp_path / "argv.log").read_text().splitlines()
    assert calls and all(c.startswith("kanban --board vids ") for c in calls), calls
