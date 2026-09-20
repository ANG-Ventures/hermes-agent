"""A foreground terminal command must die on a thread-scoped interrupt.

Incident 2026-09-20 (Apollo gateway, session 20260920_053351_7c1cde9c): a
``/stop`` at 07:18:57 could not reach a turn parked in foreground ``terminal``
tool calls (a 420s sequential timeout, a 120s pip timeout). The turn's thread
ran until its tools timed out and only then unwound — holding the per-session
turn lease for ~30 minutes and silently blocking the user's next message.

The mechanism that MUST work for ``/stop`` to be honest: ``AIAgent.interrupt``
fans ``tools.interrupt.set_interrupt(True, tid)`` out to every tool-worker
thread, and the foreground execution wait loop
(``BaseEnvironment._wait_for_process``, the local backend's shared path for
``env.execute``) polls ``is_interrupted()``, kills the child process group,
and returns rc=130 with the documented ``[Command interrupted]`` marker.

These tests pin that contract on the REAL LocalEnvironment against a real
``sleep`` child, on the CALLING thread and — the shape the gateway actually
uses — via a cross-thread fan-out onto a worker tid.
"""

import os
import threading
import time

import pytest

from tools.environments.local import LocalEnvironment
from tools.interrupt import set_interrupt


# The environment pays a one-time login-shell snapshot cost on its first
# execute; every timing assertion below is made against a WARMED env so the
# measurement is of the interrupt path, not of shell startup.
_WARMUP_TIMEOUT = 60
# Generous vs the ~1s arming delay: the poll loop backs off to 200ms and the
# kill escalation has a short grace, so a correct implementation lands well
# under this while a non-polling one would need the full command duration.
_INTERRUPT_BUDGET_SECONDS = 8.0
_SLEEP_SECONDS = 30
_COMMAND_TIMEOUT = 600  # far beyond the budget: only the interrupt can end it


@pytest.fixture
def warm_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    os.makedirs(tmp_path / "home", exist_ok=True)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=_WARMUP_TIMEOUT)
    env.execute("echo warm", timeout=_WARMUP_TIMEOUT)
    try:
        yield env
    finally:
        set_interrupt(False, threading.current_thread().ident)
        try:
            env.cleanup()
        except Exception:
            pass


def test_foreground_sleep_returns_130_when_interrupted_on_its_own_thread(warm_env):
    """set_interrupt on the executing thread aborts a foreground command fast."""
    tid = threading.current_thread().ident

    def arm():
        time.sleep(1.0)
        set_interrupt(True, tid, reason="user stop")

    armer = threading.Thread(target=arm, daemon=True)
    armer.start()

    started = time.monotonic()
    result = warm_env.execute(f"sleep {_SLEEP_SECONDS}", timeout=_COMMAND_TIMEOUT)
    elapsed = time.monotonic() - started
    armer.join(timeout=5)

    assert result["returncode"] == 130, (
        "a foreground command interrupted mid-run must report the documented "
        f"interrupt exit code, got {result['returncode']!r}"
    )
    assert "[Command interrupted]" in result["output"]
    assert elapsed < _INTERRUPT_BUDGET_SECONDS, (
        f"interrupt took {elapsed:.1f}s; a foreground command must not run to "
        f"its own {_SLEEP_SECONDS}s duration after /stop"
    )


def test_foreground_sleep_returns_130_via_cross_thread_fanout(warm_env):
    """The gateway shape: the interrupt is set on the tool WORKER's tid.

    ``AIAgent.interrupt`` runs on the event loop thread and fans the per-thread
    interrupt bit out to ``_tool_worker_threads``. The tool itself is blocked
    in ``env.execute`` on that worker, so the poll must observe a bit set by a
    DIFFERENT thread.
    """
    box: dict = {}
    ready = threading.Event()

    def worker():
        box["tid"] = threading.current_thread().ident
        ready.set()
        started = time.monotonic()
        result = warm_env.execute(f"sleep {_SLEEP_SECONDS}", timeout=_COMMAND_TIMEOUT)
        box["elapsed"] = time.monotonic() - started
        box["result"] = result

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert ready.wait(timeout=10), "worker thread never started"
    time.sleep(1.0)  # let the child actually be running

    set_interrupt(True, box["tid"], reason="user stop")
    thread.join(timeout=_INTERRUPT_BUDGET_SECONDS + 10)
    set_interrupt(False, box["tid"])

    assert not thread.is_alive(), "worker never unwound after the interrupt"
    assert box["result"]["returncode"] == 130
    assert "[Command interrupted]" in box["result"]["output"]
    assert box["elapsed"] < _INTERRUPT_BUDGET_SECONDS, (
        f"cross-thread interrupt took {box['elapsed']:.1f}s — the zombie-turn "
        "class is back"
    )


def test_uninterrupted_command_keeps_normal_semantics(warm_env):
    """No interrupt set → the command completes normally with its own rc."""
    result = warm_env.execute("echo hello && exit 3", timeout=_WARMUP_TIMEOUT)
    assert result["returncode"] == 3
    assert "hello" in result["output"]
    assert "[Command interrupted]" not in result["output"]
