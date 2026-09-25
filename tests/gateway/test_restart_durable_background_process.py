"""A notify_on_complete background process survives a gateway RESTART and its
result reaches the next boot (t_1191e078), driven through the real
``GatewayRunner.stop()`` with a real child process (hermetic, no adapters).

Measured before the fix: ``stop(restart=True)`` SIGTERM'd the child via
``process_registry.kill_all`` and emptied the checkpoint, so the next boot
recovered nothing and the calling session never heard back.

Contract:
  1. RESTART + durable notify child with a routable origin -> child stays
     alive, checkpoint keeps it, the next boot re-adopts it and its completion
     carries the real output and exit code (also when it finished while no
     gateway was running).
  2. Plain stop() still kills it (the #8202 orphan contract).
  3. Restart does NOT exempt a child without a routable origin.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

import tools.process_registry as pr_mod
from tests.gateway.restart_test_helpers import make_restart_runner

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX spawn path")

CALLER = "agent:main:telegram:dm:CALLER"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(pr_mod, "CHECKPOINT_PATH", tmp_path / "processes.json")
    monkeypatch.setattr(pr_mod, "_process_output_dir", lambda: tmp_path / "process_output")
    reg = pr_mod.ProcessRegistry()
    monkeypatch.setattr(pr_mod, "process_registry", reg)
    yield reg
    for s in list(reg._running.values()):
        if s.pid and pr_mod.ProcessRegistry._host_pid_is_ours(s.pid, s.host_start_time):
            pr_mod.ProcessRegistry._terminate_host_pid(s.pid, s.host_start_time)


def _spawn(reg, command, *, routable=True):
    s = reg.spawn_local(command, cwd="/tmp", session_key=CALLER, durable_output=True)
    s.notify_on_complete = True
    if routable:
        s.watcher_platform = "telegram"
        s.watcher_chat_id = "CALLER"
        s.watcher_interval = 5
    reg._write_checkpoint()
    return s


def _alive(s) -> bool:
    return pr_mod.ProcessRegistry._host_pid_is_ours(s.pid, s.host_start_time)


def _wait(pred, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


async def _stop(**kw):
    runner, _adapter = make_restart_runner()
    await runner.stop(**kw)


def _checkpoint_ids():
    path = pr_mod.CHECKPOINT_PATH
    return {e["session_id"] for e in json.loads(path.read_text())} if path.exists() else set()


def _boot():
    """A fresh registry reading the same checkpoint = the next gateway boot."""
    reg = pr_mod.ProcessRegistry()
    recovered = reg.recover_from_checkpoint()
    return reg, recovered


def _completions(reg):
    items = []
    while not reg.completion_queue.empty():
        items.append(reg.completion_queue.get_nowait())
    return [i for i in items if i.get("type") == "completion"]


@pytest.mark.asyncio
async def test_restart_keeps_durable_child_and_boot_delivers_real_result(registry):
    s = _spawn(registry, "echo STARTED; sleep 4; echo DURABLE_DONE; exit 7")
    assert _wait(lambda: "STARTED" in s.output_buffer), s.output_buffer

    await _stop(restart=True)

    assert _alive(s), "restart must not kill a durable notify child"
    assert not s.exited and s.termination_source == ""
    assert s.id in _checkpoint_ids()

    boot, recovered = _boot()
    assert recovered == 1
    adopted = boot.get(s.id)
    assert adopted.detached and not adopted.exited
    assert boot.get(s.id).watcher_platform == "telegram"
    assert [w["session_id"] for w in boot.pending_watchers] == [s.id]

    assert _wait(lambda: boot.get(s.id).exited), "adopted child never finished"
    done = boot.get(s.id)
    assert done.exit_code == 7
    assert "DURABLE_DONE" in done.output_buffer
    (evt,) = _completions(boot)
    assert evt["session_id"] == s.id and evt["session_key"] == CALLER
    assert evt["exit_code"] == 7 and "DURABLE_DONE" in evt["output"]


@pytest.mark.asyncio
async def test_child_finishing_while_gateway_down_is_still_delivered(registry):
    s = _spawn(registry, "sleep 3; echo FINISHED_IN_GAP; exit 3")

    await _stop(restart=True)
    assert _alive(s)
    assert s.id in _checkpoint_ids()
    # The old process is gone in production; here its reader thread reaps the
    # child. Either way the next boot only has the pid, the log and exit file.
    assert _wait(lambda: not _alive(s)), "child did not finish"
    assert _wait(lambda: os.path.exists(s.exit_path))

    boot, recovered = _boot()
    assert recovered == 1
    done = boot.get(s.id)
    assert done.exited and done.exit_code == 3
    assert "FINISHED_IN_GAP" in done.output_buffer
    (evt,) = _completions(boot)
    assert evt["exit_code"] == 3 and "FINISHED_IN_GAP" in evt["output"]
    assert [w["session_id"] for w in boot.pending_watchers] == [s.id]


@pytest.mark.asyncio
async def test_plain_stop_still_kills_durable_child(registry):
    s = _spawn(registry, "sleep 60")

    await _stop()

    assert _wait(lambda: not _alive(s), timeout=5), "plain stop must kill (#8202)"
    assert s.termination_source == "kill_all"
    assert s.id not in _checkpoint_ids()
    _boot_reg, recovered = _boot()
    assert recovered == 0


@pytest.mark.asyncio
async def test_restart_kills_child_without_routable_origin(registry):
    s = _spawn(registry, "sleep 60", routable=False)

    await _stop(restart=True)

    assert _wait(lambda: not _alive(s), timeout=5)
    assert s.termination_source == "kill_all"
    assert s.id not in _checkpoint_ids()
