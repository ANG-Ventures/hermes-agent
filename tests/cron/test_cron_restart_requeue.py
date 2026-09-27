"""Restart re-queue for cron scripts killed by the gateway shutdown drain (t_1f4598ad).

Incident 2026-09-27 03:12: a safe-restart --now drain timed out and
terminate_running_scripts SIGTERMed two in-flight daily no_agent scripts
(media-brain-bridge, sub-vps-backup). Both recorded ``Script exited with
code -15`` and stayed last_status=error until the next day's fire.

These tests drive the real path: a live script subprocess, the exact drain
calls the gateway makes (signal_shutdown + terminate_running_scripts), the
real jobs.json store, then a fresh-boot due scan and a second run_one_job.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs/scripts don't leak."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    # Reload modules that cache get_hermes_home() at import time.
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


SCRIPT = """#!/bin/bash
# First fire: long-running (killed by the drain). Re-fire: finishes fast.
if [ -f "$(dirname "$0")/first-ran" ]; then
  echo second-run-ok
  exit 0
fi
touch "$(dirname "$0")/first-ran"
sleep 30
echo never
"""


@pytest.fixture
def env(hermes_env):
    import cron.scheduler as sched
    sched.clear_shutdown()
    (hermes_env / "scripts" / "long.sh").write_text(SCRIPT, encoding="utf-8")
    yield hermes_env
    sched.clear_shutdown()
    with sched._script_procs_lock:
        sched._restart_killed_job_ids.clear()


def _make_job():
    from cron.jobs import create_job, get_job
    job = create_job(
        prompt=None, schedule="0 3 * * *", name="nightly-backup",
        script="long.sh", no_agent=True, deliver="local",
    )
    return get_job(job["id"])


def _run_and_kill_by_drain(job):
    """Run the job in a thread; once its script is live, do what the gateway
    drain timeout does, then boot state is 'next process'."""
    import cron.scheduler as sched
    t = threading.Thread(target=sched.run_one_job, args=(job,), daemon=True)
    t.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with sched._script_procs_lock:
            if sched._active_script_procs:
                break
        time.sleep(0.05)
    else:
        pytest.fail("script never started")
    sched.signal_shutdown("gateway shutdown drain")
    assert sched.terminate_running_scripts("gateway drain timeout") == 1
    t.join(20)
    assert not t.is_alive()
    # New gateway process: fresh shutdown flag.
    sched.clear_shutdown()


def test_drain_killed_script_refires_once_and_ends_ok(env):
    from cron.jobs import get_job, get_due_jobs, RESTART_REQUEUE_KEY
    import cron.scheduler as sched

    job = _make_job()
    _run_and_kill_by_drain(job)

    killed = get_job(job["id"])
    assert killed["last_status"] == "error"
    assert "exited with code -15" in (killed["last_error"] or "")
    assert killed.get(RESTART_REQUEUE_KEY), "restart re-queue marker not persisted"

    due = [j for j in get_due_jobs() if j["id"] == job["id"]]
    assert due, "re-queued job not due on the next boot scan"
    assert not get_job(job["id"]).get(RESTART_REQUEUE_KEY), "marker must be consumed"

    assert sched.run_one_job(due[0]) is True
    final = get_job(job["id"])
    assert final["last_status"] == "ok", final.get("last_error")
    assert "restart_requeue_count" not in final
    assert not final.get("manual_run_at")
    # Back on the regular schedule: not due again.
    assert not [j for j in get_due_jobs() if j["id"] == job["id"]]


def test_refire_killed_again_is_not_requeued_twice(env):
    from cron.jobs import get_job, get_due_jobs, RESTART_REQUEUE_KEY

    job = _make_job()
    _run_and_kill_by_drain(job)
    due = [j for j in get_due_jobs() if j["id"] == job["id"]]
    assert due
    (env / "scripts" / "first-ran").unlink()  # re-fire is long again
    _run_and_kill_by_drain(due[0])
    again = get_job(job["id"])
    assert again["last_status"] == "error"
    assert not again.get(RESTART_REQUEUE_KEY), "re-queue must be bounded to one fire"
    assert not [j for j in get_due_jobs() if j["id"] == job["id"]]


def test_sigterm_outside_shutdown_is_not_requeued(env):
    """A script killed while the scheduler is NOT draining is a real failure."""
    from cron.jobs import get_job, RESTART_REQUEUE_KEY
    import cron.scheduler as sched

    job = _make_job()
    t = threading.Thread(target=sched.run_one_job, args=(job,), daemon=True)
    t.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with sched._script_procs_lock:
            if sched._active_script_procs:
                break
        time.sleep(0.05)
    sched.terminate_running_scripts("external kill")
    t.join(20)
    row = get_job(job["id"])
    assert row["last_status"] == "error"
    assert not row.get(RESTART_REQUEUE_KEY)


def test_stale_marker_is_dropped_not_fired(env):
    from cron import jobs as cj

    job = _make_job()
    assert cj.request_restart_requeue(job["id"], "test") is True
    old = (cj._hermes_now() - timedelta(
        seconds=cj.RESTART_REQUEUE_MAX_AGE_SECONDS + 60)).isoformat()
    cj.update_job(job["id"], {cj.RESTART_REQUEUE_KEY: {"at": old, "reason": "x"}})
    assert not [j for j in cj.get_due_jobs() if j["id"] == job["id"]]
    assert not cj.get_job(job["id"]).get(cj.RESTART_REQUEUE_KEY)


def test_oneshot_jobs_are_not_requeued(env):
    from cron import jobs as cj
    at = (cj._hermes_now() + timedelta(hours=1)).isoformat()
    job = cj.create_job(prompt=None, schedule=at, script="long.sh",
                        no_agent=True, deliver="local")
    assert job["schedule"]["kind"] == "once"
    assert cj.request_restart_requeue(job["id"], "test") is False
