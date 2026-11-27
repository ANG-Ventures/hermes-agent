"""``hermes cron run --wait`` killed by SIGTERM records its in-flight cron turn.

digest-job-runner wraps ``cron run --wait`` in ``timeout(1)``; its SIGTERM used to
kill the CLI with Python's default action, so the cron turn's ``turn_api_calls``
rows had no ``turns`` row and blackbox-orphan-guard paged (t_4e9dd8d5).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_CHILD = textwrap.dedent(
    """
    import json, sys, threading, time, types
    out = sys.argv[1]

    import hermes_cli.lifecycle as lifecycle
    calls = []

    hook_delay = float(dict(a.split("=", 1) for a in sys.argv[2:] if "=" in a).get("delay", 0))

    def _record(name, **kw):
        time.sleep(hook_delay)  # a slow blackbox write (contended SQLite, busy disk)
        calls.append({"hook": name, "turn_id": kw.get("turn_id"), "reason": kw.get("reason")})
        with open(out, "w") as fh:
            json.dump(calls, fh)
        return []

    lifecycle.invoke_hook = _record

    import cron.scheduler as sched

    agent = types.SimpleNamespace(
        _current_turn_id="cron_x:turn1", session_id="cron_x", model="m", provider="p",
        platform="cron", _blackbox_turn_calls=("cron_x:turn1", []),
    )

    hold_lock = "hold-lock" in sys.argv[2:]

    def _fake_run_job_now(job_id, verbose=True):
        with sched._live_cron_agents_lock:
            sched._live_cron_agents[id(agent)] = agent
            if hold_lock:  # signal lands while the main thread holds the registry lock
                print("READY", flush=True)
                time.sleep(60)
        print("READY", flush=True)
        time.sleep(60)
        return {"success": True, "job_id": job_id}

    sched.run_job_now = _fake_run_job_now

    from hermes_cli.cron import _abandon_record_wait_secs, cron_run_now
    print("WAIT", _abandon_record_wait_secs(), flush=True)  # the handler's own bound, for the parent
    sys.exit(cron_run_now("job1"))
    """
)


# Slack past the handler's own bound (``_abandon_record_wait_secs``, printed by the child) for
# the flush and ``os._exit`` after the join. The wait follows the bound, so a slow record path
# under load fails here only when it overruns the bound the product itself promises.
_EXIT_MARGIN_S = 10.0


def _capture_hung_child(proc, tmp_path):
    """Keep evidence of a child still alive past its bound (t_32c2a03c): thread list and, on macOS,
    ``sample`` stacks. On CI into ``test-results/`` (gitignored; the macOS job uploads it on failure),
    else ``tmp_path``."""
    base = REPO_ROOT / "test-results" if os.environ.get("CI") else tmp_path
    diag = base / f"cron-wait-hung-{proc.pid}"
    diag.mkdir(parents=True, exist_ok=True)
    pid = str(proc.pid)
    cmds = [("ps.txt", ["ps", "-M", "-p", pid] if sys.platform == "darwin" else ["ps", "-L", "-p", pid])]
    if sys.platform == "darwin":
        cmds.append(("sample.txt", ["sample", pid, "3"]))
    for name, cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            (diag / name).write_text(res.stdout + res.stderr)
        except (OSError, subprocess.SubprocessError) as exc:
            (diag / name).write_text(f"{cmd}: {exc!r}\n")
    return diag


def _run_and_sigterm(tmp_path, *extra, hook_callback_timeout=None):
    out = tmp_path / "hooks.json"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if hook_callback_timeout is not None:
        (home / "config.yaml").write_text(f"plugins:\n  hook_callback_timeout: {hook_callback_timeout}\n")
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(out), *extra],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 60
        line = ""
        record_wait = None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if line.startswith("WAIT "):
                record_wait = float(line.split()[1])
            if "READY" in line or not line:
                break
        assert "READY" in line, proc.stderr.read()
        assert record_wait is not None, "child did not report the handler's record wait"
        sent = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=record_wait + _EXIT_MARGIN_S)
        except subprocess.TimeoutExpired:
            diag = _capture_hung_child(proc, tmp_path)
            pytest.fail(f"child alive {record_wait + _EXIT_MARGIN_S:.0f}s after SIGTERM "
                        f"(handler bound {record_wait:.0f}s); stacks in {diag}")
        elapsed = time.monotonic() - sent
    finally:
        if proc.poll() is None:
            proc.kill()
    return rc, out, elapsed


@pytest.mark.platforms("posix")
def test_sigterm_during_cron_run_wait_records_abandoned_turn(tmp_path):
    rc, out, _ = _run_and_sigterm(tmp_path)
    assert rc == 128 + signal.SIGTERM
    calls = json.loads(out.read_text())
    assert calls == [{"hook": "on_turn_abandoned", "turn_id": "cron_x:turn1", "reason": "signal_15"}]


@pytest.mark.platforms("posix")
def test_slow_record_within_hook_budget_lands_before_exit(tmp_path):
    """A recorder that takes 15 s (inside the 30 s default hook budget) still writes its row
    before the process exits (Prism 33a31a0e74cc: a literal 10 s wait killed it first)."""
    rc, out, elapsed = _run_and_sigterm(tmp_path, "delay=15")
    assert rc == 128 + signal.SIGTERM
    assert out.exists(), f"record cut before it landed (exit after {elapsed:.1f}s)"
    calls = json.loads(out.read_text())
    assert calls == [{"hook": "on_turn_abandoned", "turn_id": "cron_x:turn1", "reason": "signal_15"}]


@pytest.mark.platforms("posix")
def test_record_wait_follows_configured_hook_budget(tmp_path):
    """The wait is the configured hook budget plus a margin, not a constant: with a 1 s budget a
    15 s recorder is abandoned and the process exits well before the record would land."""
    rc, out, elapsed = _run_and_sigterm(tmp_path, "delay=15", hook_callback_timeout=1)
    assert rc == 128 + signal.SIGTERM
    assert not out.exists()
    assert elapsed < 15


@pytest.mark.platforms("posix")
def test_sigterm_while_main_thread_holds_registry_lock_still_exits(tmp_path):
    """The handler must not deadlock on a lock the interrupted main thread holds (Prism
    08326117fdf2): the record is abandoned after its bounded wait and the process exits."""
    rc, out, _ = _run_and_sigterm(tmp_path, "hold-lock", hook_callback_timeout=1)
    assert rc == 128 + signal.SIGTERM
    assert not out.exists()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("budget", [".nan", ".inf"])
def test_non_finite_hook_budget_still_waits_for_record(tmp_path, budget):
    """A non-finite ``plugins.hook_callback_timeout`` must not reach ``Thread.join`` (Prism
    f094fa505f02): ``join(timeout=nan)`` raises ValueError inside the handler, the process dies
    without ``os._exit(143)`` and the slow record is lost. Fall back to the finite default."""
    rc, out, elapsed = _run_and_sigterm(tmp_path, "delay=3", hook_callback_timeout=budget)
    assert rc == 128 + signal.SIGTERM
    assert out.exists(), f"record cut before it landed (exit after {elapsed:.1f}s)"
    calls = json.loads(out.read_text())
    assert calls == [{"hook": "on_turn_abandoned", "turn_id": "cron_x:turn1", "reason": "signal_15"}]


@pytest.mark.platforms("posix")
def test_record_path_import_runs_no_subprocess(tmp_path):
    """The record path's modules are imported when the handler is installed, and importing them
    must start no process (t_32c2a03c): ``agent.auxiliary_client`` read the version at import, 8
    ``git`` calls that took ~3 s each on a loaded Mac and ran the handler past its bound."""
    probe = textwrap.dedent(
        """
        import subprocess
        spawned = []
        _init = subprocess.Popen.__init__
        def _spy(self, args, *a, **k):
            spawned.append(args)
            return _init(self, args, *a, **k)
        subprocess.Popen.__init__ = _spy
        import agent.turn_finalizer, cron.scheduler
        print(spawned)
        """
    )
    env = dict(os.environ, HERMES_HOME=str(tmp_path))
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    res = subprocess.run([sys.executable, "-c", probe], cwd=str(REPO_ROOT), env=env,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().splitlines()[-1] == "[]"
