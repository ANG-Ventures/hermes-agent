"""A job dispatched in the last tick before the drain, refused at script start because the
scheduler is shutting down, is a skip — not a job failure (t_5b550b0e).

Incident 2026-10-19 14:15: five jobs posted "Cronjob Failed" with
``Skipped: cron scheduler is shutting down`` to #alerts, recorded last_status=error and lost
their occurrence a whole period. The refused run must leave the occurrence (and any restart
re-queue fire) for the next process, and page nobody.

Real path: real jobs.json store, real due scan + tick advance + fire claim, real
``_run_job_script`` refusal; only the outbound delivery is captured.
"""

from __future__ import annotations

from datetime import timedelta

import pytest


@pytest.fixture
def env(hermes_env, monkeypatch):
    import cron.scheduler as sched

    sched.clear_shutdown()
    (hermes_env / "scripts" / "w.sh").write_text("#!/usr/bin/env bash\necho hi\n")
    sent = []
    monkeypatch.setattr(
        sched, "_deliver_result",
        lambda job, content, **kw: sent.append((content, kw.get("success"))))
    yield sent
    sched.clear_shutdown()


def _dispatch_then_drain(job_id):
    """What the ticker does for a due job, with the drain flag flipping between the tick's
    dispatch and the worker reaching run_script."""
    import cron.jobs as cj
    import cron.scheduler as sched
    from cron.executions import create_execution

    due = [j for j in cj.get_due_jobs() if j["id"] == job_id]
    assert due, "job not due"
    cj.advance_next_runs([job_id])
    execution = create_execution(
        job_id, source="builtin", scheduled_instant=due[0].get("_scheduled_instant"))
    dispatched = dict(due[0], execution_id=execution["id"])
    sched.signal_shutdown("gateway shutdown drain")
    assert sched._process_due_job(dispatched, None, None, False) is True
    sched.clear_shutdown()  # next process


def test_shutdown_refused_job_is_skip_and_keeps_its_occurrence(env):
    import cron.jobs as cj

    job = cj.create_job(prompt=None, schedule="every 15m", name="mirrors-refresh",
                        script="w.sh", no_agent=True, deliver="origin")
    slot = (cj._hermes_now() - timedelta(seconds=20)).isoformat()
    cj.update_job(job["id"], {"next_run_at": slot})

    _dispatch_then_drain(job["id"])

    row = cj.get_job(job["id"])
    assert env == [], f"shutdown refusal was paged: {env!r}"
    assert row.get("last_status") != "error", row.get("last_error")
    assert "shutting down" not in (row.get("last_error") or "")
    assert row["next_run_at"] == slot, "occurrence advanced past the refused slot"
    assert not row.get("fire_claim"), "claim must be released for the next process"
    assert [j for j in cj.get_due_jobs() if j["id"] == job["id"]], "next process must run it"


def test_shutdown_refused_restart_refire_is_kept(env):
    """A restart re-queue fire refused by a second drain keeps its run-now stamp."""
    import cron.jobs as cj

    job = cj.create_job(prompt=None, schedule="0 3 * * *", name="nightly",
                        script="w.sh", no_agent=True, deliver="origin")
    assert cj.request_restart_requeue(job["id"], "script killed by gateway shutdown drain")

    _dispatch_then_drain(job["id"])

    row = cj.get_job(job["id"])
    assert env == []
    assert row.get("last_status") != "error", row.get("last_error")
    assert row.get("manual_run_at") and row["next_run_at"] == row["manual_run_at"]
    assert [j for j in cj.get_due_jobs() if j["id"] == job["id"]], "re-fire lost"
