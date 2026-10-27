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

    def _record(name, **kw):
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

    def _fake_run_job_now(job_id, verbose=True):
        with sched._live_cron_agents_lock:
            sched._live_cron_agents[id(agent)] = agent
        print("READY", flush=True)
        time.sleep(60)
        return {"success": True, "job_id": job_id}

    sched.run_job_now = _fake_run_job_now

    from hermes_cli.cron import cron_run_now
    sys.exit(cron_run_now("job1"))
    """
)


@pytest.mark.platforms("posix")
def test_sigterm_during_cron_run_wait_records_abandoned_turn(tmp_path):
    out = tmp_path / "hooks.json"
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path / "home")
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(out)],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 60
        line = ""
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if "READY" in line or not line:
                break
        assert "READY" in line, proc.stderr.read()
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert rc == 128 + signal.SIGTERM
    calls = json.loads(out.read_text())
    assert calls == [{"hook": "on_turn_abandoned", "turn_id": "cron_x:turn1", "reason": "signal_15"}]
