"""Detached delegation spawn on macOS: the child is its own responsible process (t_45fef017).

A Popen child of the gateway is charged to the gateway pid; after a gateway restart macOS Local
Network Privacy refuses the orphan's LAN connections (errno 65, t_f8b497c9). ``spawn_child`` spawns
it disclaimed instead. These tests run the real ``spawn_child`` path with a probe argv in place of
the agent worker and need no network: they read the child's responsible pid from the OS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools import delegate_detached as dd
from tools import macos_spawn
from tools import process_registry as pr

# conftest's autouse guard replaces spawn_child (a real one would boot an agent). These tests spawn
# a probe argv, not the agent worker, so they call the function captured at import.
_REAL_SPAWN_CHILD = dd.spawn_child

# The probe reports what the spawned process sees, then exits 0.
_PROBE = r"""
import ctypes, json, os, sys
resp = None
if sys.platform == "darwin":
    f = ctypes.CDLL(None).responsibility_get_pid_responsible_for_pid
    f.restype, f.argtypes = ctypes.c_int, [ctypes.c_int]
    resp = f(os.getpid())
print("probe-stdout")
sys.stderr.write("probe-stderr\n")
json.dump({"pid": os.getpid(), "sid": os.getsid(0), "pgid": os.getpgid(0), "resp": resp,
           "cwd": os.getcwd(), "stdin": os.fstat(0).st_rdev,
           "devnull": os.stat(os.devnull).st_rdev, "marker": os.environ.get(sys.argv[2])},
          open(sys.argv[1], "w"))
"""


def _spawn_probe(tmp_path: Path, monkeypatch) -> tuple:
    out = tmp_path / "probe.json"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    argv = [sys.executable, "-c", _PROBE, str(out), dd.WORKER_BOOT_MARKER]
    monkeypatch.setattr(pr, "restart_safe_gateway_child_argv",
                        lambda command, **_: pr.GatewayChildDispatch("in_process", argv))
    record = {"execution": {"workspace_hint": str(workspace)}}
    spawned = _REAL_SPAWN_CHILD(record=record, attempt_id="t45:g0:a", profile_home=tmp_path / "home",
                                continuation=None, live_delegation_id=None)
    assert spawned is not None
    deadline = time.time() + 30
    while spawned.popen.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert spawned.popen.returncode == 0, (spawned.job_dir / "child.log").read_text()
    return spawned, json.loads(out.read_text()), workspace


@pytest.mark.skipif(not macos_spawn.available(), reason="macOS responsibility disclaim API")
def test_detached_child_is_its_own_responsible_process(tmp_path, monkeypatch):
    spawned, seen, workspace = _spawn_probe(tmp_path, monkeypatch)
    assert isinstance(spawned.popen, macos_spawn.SpawnedProcess)
    assert seen["pid"] == spawned.pid
    # The point of the fix: not charged to this (gateway) process, so its restart cannot strand it.
    assert seen["resp"] == spawned.pid
    assert macos_spawn.responsible_pid(os.getpid()) != spawned.pid
    # Same process shape Popen(start_new_session=True, stdin=DEVNULL, stdout=log, stderr=STDOUT) gave.
    assert seen["sid"] == seen["pgid"] == spawned.pid
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(workspace)
    assert seen["stdin"] == seen["devnull"]
    assert seen["marker"] == "1"  # env reaches the child
    log = (spawned.job_dir / "child.log").read_text()
    assert "probe-stdout" in log and "probe-stderr" in log


@pytest.mark.skipif(sys.platform == "win32", reason="detached spawn is POSIX-only")
def test_without_the_disclaim_api_spawn_falls_back_to_popen(tmp_path, monkeypatch):
    monkeypatch.setattr(macos_spawn, "available", lambda: False)
    spawned, seen, workspace = _spawn_probe(tmp_path, monkeypatch)
    assert isinstance(spawned.popen, subprocess.Popen)
    assert seen["sid"] == spawned.pid
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(workspace)


@pytest.mark.skipif(not macos_spawn.available(), reason="macOS responsibility disclaim API")
def test_failed_disclaim_spawn_falls_back_to_popen(tmp_path, monkeypatch):
    def _refuse(*_a, **_k):
        raise OSError(1, "refused")

    monkeypatch.setattr(macos_spawn, "spawn_disclaimed", _refuse)
    spawned, seen, _ = _spawn_probe(tmp_path, monkeypatch)
    assert isinstance(spawned.popen, subprocess.Popen)
    # Popen child: charged to whatever this process is charged to (the bug's precondition).
    assert seen["resp"] == macos_spawn.responsible_pid(os.getpid()) != spawned.pid


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_errno_65_after_the_responsible_gateway_exited_is_reported_as_lan_privacy():
    dead = _dead_pid()
    result = {"status": "error", "summary": None,
              "error": "ConnectionError: [Errno 65] No route to host (192.168.1.5:8123)"}
    dd._annotate_lan_privacy(result, dead)
    assert "Local Network Privacy, not a network outage" in result["error"]
    assert f"pid {dead}" in result["error"]


@pytest.mark.parametrize("responsible", ["self", "alive", None])
def test_errno_65_is_left_alone_when_the_responsible_process_is_not_a_dead_gateway(responsible):
    pid = {"self": os.getpid(), "alive": os.getppid(), None: None}[responsible]
    result = {"status": "completed", "summary": "curl: (7) Failed to connect: No route to host"}
    dd._annotate_lan_privacy(result, pid)
    assert result["summary"] == "curl: (7) Failed to connect: No route to host"


def test_other_errors_are_not_annotated():
    result = {"status": "error", "summary": None, "error": "Connection refused"}
    dd._annotate_lan_privacy(result, _dead_pid())
    assert result["error"] == "Connection refused"
