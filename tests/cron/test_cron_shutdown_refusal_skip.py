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


def test_provider_fire_refused_at_shutdown_stays_loud(env):
    """claim_fire -> fire_claimed (webhook/provider fires) carries no due slot this process can put
    back, so the refusal must not be silenced: the failure notice is still delivered."""
    import cron.jobs as cj
    import cron.scheduler as sched
    from cron.scheduler_provider import InProcessCronScheduler

    job = cj.create_job(prompt=None, schedule="every 15m", name="dpx-store-lock-watch",
                        script="w.sh", no_agent=True, deliver="origin")
    slot = (cj._hermes_now() - timedelta(seconds=20)).isoformat()
    cj.update_job(job["id"], {"next_run_at": slot})
    provider = InProcessCronScheduler()
    claimed = provider.claim_fire(job["id"])
    assert claimed is not None
    sched.signal_shutdown("gateway shutdown drain")
    provider.fire_claimed(claimed)
    sched.clear_shutdown()

    assert [ok for _c, ok in env] == [False], f"unrestored refusal was silenced: {env!r}"


def test_shutdown_skip_after_claim_taken_by_another_tick_touches_nothing(env, monkeypatch):
    """The refused run's claim was cleared/re-taken before cleanup: its stale owner must neither
    rewind next_run_at nor clear the newer run_claim."""
    import cron.jobs as cj
    import cron.scheduler_script as ss

    job = cj.create_job(prompt=None, schedule="every 15m", name="studio-ceiling-watch",
                        script="w.sh", no_agent=True, deliver="origin")
    slot = (cj._hermes_now() - timedelta(seconds=20)).isoformat()
    cj.update_job(job["id"], {"next_run_at": slot})
    newer = {"next_run_at": "2099-01-01T00:00:00+00:00", "fire_claim": None,
             "run_claim": {"at": cj._hermes_now().isoformat(), "by": "worker-B:xyz"}}
    real = ss._run_job_script

    def refuse_after_another_tick(*a, **kw):
        out = real(*a, **kw)
        cj.update_job(job["id"], newer)
        return out

    monkeypatch.setattr(ss, "_run_job_script", refuse_after_another_tick)
    _dispatch_then_drain(job["id"])

    row = cj.get_job(job["id"])
    assert row["next_run_at"] == newer["next_run_at"], "stale owner rewound next_run_at"
    assert row.get("run_claim") == newer["run_claim"], "stale owner cleared a newer run_claim"


def test_shutdown_skip_never_releases_another_owners_claim(env):
    import cron.jobs as cj
    from cron.scheduler_shutdown_skip import _release_fire

    job = cj.create_job(prompt=None, schedule="every 15m", name="x",
                        script="w.sh", no_agent=True, deliver="origin")
    foreign = {"at": cj._hermes_now().isoformat(), "by": "worker-B:abc"}
    cj.update_job(job["id"], {"fire_claim": foreign})
    for owner in (None, "worker-A:def"):
        assert _release_fire(job["id"], owner, "2026-01-01T00:00:00+00:00") is False
        row = cj.get_job(job["id"])
        assert row["fire_claim"] == foreign
        assert row["next_run_at"] != "2026-01-01T00:00:00+00:00"


def test_shutdown_refusal_does_not_consume_a_monitor_change(env, monkeypatch):
    """monitor_url + pre-run script: the URL change detected before the refused script must
    still be a change for the next process."""
    import cron.jobs as cj
    import cron.monitor as mon

    monkeypatch.setattr(mon, "_fetch_monitor_url", lambda url: (True, "v2"))
    job = cj.create_job(prompt="report the change", schedule="every 15m", name="status-watch",
                        script="w.sh", monitor_url="https://example.com/s", deliver="origin")
    cj.update_job(job["id"], {"monitor_state": {"last_output_hash": mon.hash_monitor_output("v1")}})
    mon._write_last_output(job["id"], "v1")
    cj.update_job(job["id"], {"next_run_at": (cj._hermes_now() - timedelta(seconds=20)).isoformat()})

    _dispatch_then_drain(job["id"])

    assert env == []
    row = cj.get_job(job["id"])
    assert row["monitor_state"]["last_output_hash"] == mon.hash_monitor_output("v1")
    assert mon.check_monitor(row).changed, "monitor change consumed by the refused run"


def test_monitor_rollback_never_overwrites_a_run_that_took_the_claim(env, monkeypatch):
    """Prism daa521022dcb (reviewer): worker B takes over the claim and stores v3 before A's refused
    script returns; A's rollback must leave B's monitor state and last output alone."""
    import cron.jobs as cj
    import cron.monitor as mon
    import cron.scheduler_script as ss

    monkeypatch.setattr(mon, "_fetch_monitor_url", lambda url: (True, "v2"))
    job = cj.create_job(prompt="report the change", schedule="every 15m", name="status-watch",
                        script="w.sh", monitor_url="https://example.com/s", deliver="origin")
    cj.update_job(job["id"], {"monitor_state": {"last_output_hash": mon.hash_monitor_output("v1")}})
    mon._write_last_output(job["id"], "v1")
    cj.update_job(job["id"], {"next_run_at": (cj._hermes_now() - timedelta(seconds=20)).isoformat()})
    v3 = {"last_output_hash": mon.hash_monitor_output("v3")}
    real = ss._run_job_script

    def refuse_after_takeover(*a, **kw):
        out = real(*a, **kw)
        cj.update_job(job["id"], {"fire_claim": {"at": cj._hermes_now().isoformat(), "by": "worker-B:xyz"},
                                  "monitor_state": v3})
        mon._write_last_output(job["id"], "v3")
        return out

    monkeypatch.setattr(ss, "_run_job_script", refuse_after_takeover)
    _dispatch_then_drain(job["id"])

    row = cj.get_job(job["id"])
    assert row["monitor_state"] == v3, "A's rollback overwrote B's monitor state"
    assert mon._read_last_output(job["id"]) == "v3"


def test_schedule_edited_during_refused_run_keeps_its_new_next_run(env, monkeypatch):
    """Prism 6d15940f4ae6 (reviewer): an every-15m job edited to every-1d mid-run must not get the
    old 15m slot back (it would fire immediately after the restart)."""
    import cron.jobs as cj
    import cron.scheduler_script as ss

    job = cj.create_job(prompt=None, schedule="every 15m", name="mirrors-refresh",
                        script="w.sh", no_agent=True, deliver="origin")
    slot = (cj._hermes_now() - timedelta(seconds=20)).isoformat()
    cj.update_job(job["id"], {"next_run_at": slot})
    real = ss._run_job_script
    edited = {}

    def refuse_after_edit(*a, **kw):
        out = real(*a, **kw)
        edited.update(cj.update_job(job["id"], {"schedule": "every 1d"}) or {})
        return out

    monkeypatch.setattr(ss, "_run_job_script", refuse_after_edit)
    _dispatch_then_drain(job["id"])

    row = cj.get_job(job["id"])
    assert row["next_run_at"] != slot, "old-schedule occurrence restored over the edit"
    assert row["next_run_at"] == edited.get("next_run_at")
    assert not row.get("fire_claim"), "claim still released"
