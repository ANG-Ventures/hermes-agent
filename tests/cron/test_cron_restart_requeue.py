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


def _run_and_kill_by_drain(job, first_ran):
    """Run the job in a thread; once its script is live, do what the gateway
    drain timeout does, then boot state is 'next process'.

    "Live" means the script reached its long phase (``first_ran`` exists), not
    merely that the Popen is registered: killing bash before its ``touch``
    leaves no marker and the re-fire takes the long branch again."""
    import cron.scheduler as sched
    t = threading.Thread(target=sched.run_one_job, args=(job,), daemon=True)
    t.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with sched._script_procs_lock:
            if sched._active_script_procs and first_ran.exists():
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
    _run_and_kill_by_drain(job, env / "scripts" / "first-ran")

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
    first_ran = env / "scripts" / "first-ran"
    _run_and_kill_by_drain(job, first_ran)
    due = [j for j in get_due_jobs() if j["id"] == job["id"]]
    assert due
    first_ran.unlink()  # re-fire is long again
    _run_and_kill_by_drain(due[0], first_ran)
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


def test_dying_process_tick_leaves_requeue_marker_for_next_boot(env):
    """Reviewer race on #1301: the dying gateway's ticker keeps running from
    drain timeout until runner.stop() returns. A tick in that window must not
    consume the marker (the dispatch would be refused as 'Skipped: shutting
    down', record an error and use up the one re-fire)."""
    from cron.jobs import get_job, get_due_jobs, RESTART_REQUEUE_KEY
    import cron.scheduler as sched

    job = _make_job()
    t = threading.Thread(target=sched.run_one_job, args=(job,), daemon=True)
    t.start()
    deadline = time.monotonic() + 15
    first_ran = env / "scripts" / "first-ran"
    while time.monotonic() < deadline:
        with sched._script_procs_lock:
            if sched._active_script_procs and first_ran.exists():
                break
        time.sleep(0.05)
    else:
        pytest.fail("script never started")
    sched.signal_shutdown("gateway shutdown drain")
    assert sched.terminate_running_scripts("gateway drain timeout") == 1
    t.join(20)
    assert not t.is_alive()
    before = get_job(job["id"])
    assert before.get(RESTART_REQUEUE_KEY)

    # Still the dying process: its ticker fires once more.
    assert sched.tick(verbose=False) == 0
    after = get_job(job["id"])
    assert after.get(RESTART_REQUEUE_KEY) == before.get(RESTART_REQUEUE_KEY)
    assert after.get("next_run_at") == before.get("next_run_at")
    assert "shutting down" not in (after.get("last_error") or "")

    # Next process: the re-fire is still there and ends ok.
    sched.clear_shutdown()
    due = [j for j in get_due_jobs() if j["id"] == job["id"]]
    assert due, "re-fire lost when the dying process's ticker scans after the marker"
    assert sched.run_one_job(due[0]) is True
    assert get_job(job["id"])["last_status"] == "ok"


def test_dying_process_tick_does_not_skip_normally_due_job(env):
    """A job that simply comes due during the drain is left for the next
    process, not recorded as a 'Skipped: shutting down' error."""
    from cron.jobs import get_job, update_job
    import cron.jobs as cj
    import cron.scheduler as sched

    job = _make_job()
    past = (cj._hermes_now() - timedelta(seconds=30)).isoformat()
    update_job(job["id"], {"next_run_at": past})
    sched.signal_shutdown("gateway shutdown drain")
    assert sched.tick(verbose=False) == 0
    row = get_job(job["id"])
    assert row.get("next_run_at") == past
    assert row.get("last_status") in (None, "")


def _capture_deliveries(monkeypatch):
    import cron.scheduler as sched
    sent = []

    def fake_deliver(job, content, **kw):
        sent.append((content, kw.get("success")))
        return None

    monkeypatch.setattr(sched, "_deliver_result", fake_deliver)
    return sent


def test_drain_killed_script_is_not_paged(env, monkeypatch):
    """t_e0aa9875: a restart kill is not a failure -> no 'Cronjob Failed' page.
    The run is still recorded and the one re-fire is still requested."""
    from cron.jobs import get_job, RESTART_REQUEUE_KEY

    sent = _capture_deliveries(monkeypatch)
    job = _make_job()
    _run_and_kill_by_drain(job, env / "scripts" / "first-ran")
    row = get_job(job["id"])
    assert sent == [], f"restart kill was paged: {sent!r}"
    assert row["last_status"] == "error"
    assert row.get(RESTART_REQUEUE_KEY), "re-fire must still be requested"


def test_sigterm_outside_shutdown_still_pages(env, monkeypatch):
    """Other direction: a kill while NOT draining is a real failure and pages."""
    import cron.scheduler as sched

    sent = _capture_deliveries(monkeypatch)
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
    assert len(sent) == 1, sent
    content, success = sent[0]
    assert success is False
    assert "-15" in content or "exited with code" in content


def _make_named_job(name):
    from cron.jobs import create_job, get_job
    job = create_job(
        prompt=None, schedule="0 3 * * *", name=name,
        script="long.sh", no_agent=True, deliver="local",
    )
    return get_job(job["id"])


def test_single_requeue_fires_now(env):
    """One marker in a scan keeps the run-now shape: due on the same scan."""
    from cron import jobs as cj

    job = _make_named_job("solo")
    assert cj.request_restart_requeue(job["id"], "restart") is True
    due = [j for j in cj.get_due_jobs() if j["id"] == job["id"]]
    assert due, "single re-queue must fire on the first scan"
    row = cj.get_job(job["id"])
    assert row["next_run_at"] == row["manual_run_at"]


def test_requeues_in_one_scan_are_staggered(env, monkeypatch):
    """2026-09-30 09:28:43: one restart killed two scripts and both re-fired
    71 ms apart. The n-th re-fire in one scan is deferred n * STAGGER."""
    from datetime import datetime
    from cron import jobs as cj

    a = _make_named_job("fleet-config-lint")
    b = _make_named_job("fleet-upstream-leak-lint")
    assert cj.request_restart_requeue(a["id"], "restart") is True
    assert cj.request_restart_requeue(b["id"], "restart") is True

    t0 = cj._hermes_now()
    monkeypatch.setattr(cj, "_hermes_now", lambda: t0)
    due_ids = {j["id"] for j in cj.get_due_jobs()}
    rows = [cj.get_job(a["id"]), cj.get_job(b["id"])]
    for row in rows:
        assert not row.get(cj.RESTART_REQUEUE_KEY), "marker must be consumed"
        assert row["next_run_at"] == row["manual_run_at"], "run-now shape kept"
    fire_at = sorted(datetime.fromisoformat(r["next_run_at"]) for r in rows)
    gap = (fire_at[1] - fire_at[0]).total_seconds()
    assert gap >= 60, f"re-fires {gap}s apart: same-second stampede"
    assert gap == cj.RESTART_REQUEUE_STAGGER_S
    assert len(due_ids & {a["id"], b["id"]}) == 1, "only the first fires now"

    # The deferred one is not due before its slot, and IS due at it.
    later = t0 + (fire_at[1] - fire_at[0]) - timedelta(seconds=1)
    monkeypatch.setattr(cj, "_hermes_now", lambda: later)
    pending = ({a["id"], b["id"]} - due_ids).pop()
    assert pending not in {j["id"] for j in cj.get_due_jobs()}
    at_slot = later + timedelta(seconds=1)
    monkeypatch.setattr(cj, "_hermes_now", lambda: at_slot)
    assert pending in {j["id"] for j in cj.get_due_jobs()}, "staggered re-fire lost"
