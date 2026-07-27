"""Shutdown-drain cancellation for in-flight cron SCRIPT jobs (`no_agent`).

Background — the asymmetry this closes:

``GatewayRunner._drain_active_agents`` WAITS on in-flight cron work
(``_active_cron_job_count()`` folds ``cron.scheduler.get_running_job_ids()``
into the same wait it applies to chat sessions, #60432). But the cancellation
half never existed: ``_interrupt_running_agents`` iterates ONLY
``self._running_agents``, and cron jobs run on the scheduler's own thread pool,
entirely outside that dict. ``mark_running_jobs_interrupted`` is pure
bookkeeping — its own docstring says it records status so a truncated job can't
report success; it cancels nothing.

Net effect measured live on the fleet (2026-07-20..27): the default profile
timed out its 180s drain on 14 of 18 shutdowns, and 4 of those made ZERO
progress (``cron_at_start == cron_now``) because a `no_agent` script had been
launched with a blocking ``subprocess.run(..., timeout=3600)`` that nothing
could interrupt. The drain waited the full 180s on work it had no mechanism to
signal, then force-killed it anyway.

These tests drive the REAL module functions (no mocks of the unit under test)
and assert the signal actually reaches a live subprocess.
"""

import subprocess
import threading
import time

import pytest

from cron import scheduler as sched


@pytest.fixture(autouse=True)
def _clean_shutdown_state():
    """Each test starts from a clean, un-signalled scheduler."""
    sched.clear_shutdown()
    yield
    sched.clear_shutdown()


@pytest.fixture
def make_script(tmp_path, monkeypatch):
    """Write a script into the sandboxed HERMES_HOME scripts dir.

    ``_run_job_script`` refuses any path outside ``$HERMES_HOME/scripts``
    (a real security guard, not a test obstacle) — so the fixture points
    HERMES_HOME at a temp dir and writes there. Honouring the guard keeps
    these tests on the production code path instead of bypassing it.
    """
    home = tmp_path / "hermes_home"
    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(sched, "_get_hermes_home", lambda: home)

    def _make(name: str, body: str):
        p = scripts / name
        p.write_text(body)
        p.chmod(0o755)
        return p

    return _make


def test_shutdown_signal_is_observable():
    """The drain needs a readable flag to gate on."""
    assert sched.is_shutting_down() is False
    sched.signal_shutdown("test")
    assert sched.is_shutting_down() is True
    sched.clear_shutdown()
    assert sched.is_shutting_down() is False


def test_script_refuses_to_start_once_shutdown_signalled(make_script, tmp_path):
    """A job dispatched into a draining gateway must not START new work.

    Without this, the ticker can submit a fresh 60-minute script one second
    before SIGTERM and hand the drain brand-new work to wait on.
    """
    marker = tmp_path / "RAN"
    script = make_script("canary.sh", f"#!/bin/bash\ntouch {marker}\n")

    sched.signal_shutdown("test")
    ok, output = sched._run_job_script(str(script))

    assert ok is False
    assert "shutting down" in output.lower()
    assert not marker.exists(), (
        "script executed despite the shutdown signal — the drain will now wait "
        "on work that was started AFTER the gateway began draining"
    )


def test_running_script_is_terminated_by_shutdown(make_script):
    """THE REGRESSION: a long script must actually die when the drain says so.

    This is the measured production failure — a `no_agent` script sleeping far
    past the drain deadline, with the gateway unable to do anything but wait.
    """
    script = make_script("slow.sh", "#!/bin/bash\nsleep 120\n")

    result = {}

    def _run():
        result["out"] = sched._run_job_script(str(script))

    t = threading.Thread(target=_run, daemon=True)
    started = time.monotonic()
    t.start()

    # Let the subprocess actually come up before signalling.
    deadline = time.monotonic() + 10
    while not sched._active_script_procs and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sched._active_script_procs, "script subprocess never registered"

    assert sched.terminate_running_scripts("gateway shutdown") == 1
    t.join(timeout=20)
    elapsed = time.monotonic() - started

    assert not t.is_alive(), (
        "script survived terminate_running_scripts() — this is the live bug: "
        "the drain waits its full timeout on a subprocess it cannot signal"
    )
    assert elapsed < 30, (
        f"script took {elapsed:.1f}s to die; it must terminate promptly, not "
        "run to its own 120s completion"
    )
    ok, _ = result["out"]
    assert ok is False, "a terminated script must not report success"


def test_terminate_is_safe_when_nothing_is_running():
    """The drain calls this unconditionally — it must never raise."""
    assert sched.terminate_running_scripts("noop") == 0


def test_completed_script_is_deregistered(make_script):
    """No leak: a finished script must not linger in the registry.

    A stale entry would make a later drain try to signal a dead pid, and
    (worse) could let a recycled pid be terminated.
    """
    script = make_script("quick.sh", "#!/bin/bash\necho hi\n")

    ok, out = sched._run_job_script(str(script))

    assert ok is True and "hi" in out
    assert not sched._active_script_procs, (
        "finished script left a stale Popen in the registry"
    )


def test_failing_script_is_also_deregistered(make_script):
    """The registry must drain on the ERROR path too, not just success."""
    script = make_script("boom.sh", "#!/bin/bash\nexit 3\n")

    ok, out = sched._run_job_script(str(script))

    assert ok is False
    assert "3" in out
    assert not sched._active_script_procs, (
        "failed script left a stale Popen in the registry"
    )


def test_normal_runs_are_unaffected_when_not_draining(make_script):
    """Negative control — the guard must not disturb ordinary operation."""
    script = make_script("ok.sh", "#!/bin/bash\necho normal\n")

    ok, out = sched._run_job_script(str(script))

    assert ok is True
    assert "normal" in out


def test_gateway_drain_calls_the_cron_terminator():
    """AST source-contract: the gateway shutdown path must WIRE the terminator.

    A perfect scheduler-side implementation is inert if nothing calls it — the
    original bug was precisely an unwired cancellation half. Asserting on the
    source keeps this honest without standing up a whole GatewayRunner.
    """
    import ast
    import inspect

    import gateway.run as run_mod

    src = inspect.getsource(run_mod)
    tree = ast.parse(src)

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "terminate_running_scripts" in called, (
        "gateway/run.py never calls terminate_running_scripts() — the drain "
        "still has no way to cancel an in-flight cron script"
    )
    assert "signal_shutdown" in called, (
        "gateway/run.py never calls signal_shutdown() — a job dispatched "
        "mid-drain can still start brand-new long-running work"
    )
