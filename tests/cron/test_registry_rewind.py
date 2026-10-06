"""A jobs.json rewound to an older snapshot must not re-fire occurrences that already ran.

2026-10-05 22:44 an operator "levelled" a merged PR's files into the live home with
``git show origin/main:$p > $p``; one of them was ``cron/jobs.json``, so the scheduler store went
back to a committed snapshot whose ``next_run_at`` values were hours (or a day) stale. The next
due scan logged 402 ``missed its scheduled time ... Running now`` lines: every daily job that had
already run that day ran a second time and paged off schedule.

The executions ledger is the authority for "this occurrence already ran" (``completed_occurrence``),
but it kept only the newest ``MAX_TERMINAL_EXECUTIONS`` rows fleet-wide (~1.5 h of history on a
busy host), so the 09:12 completion of a 09:00 daily job was gone by 22:44.
"""
from datetime import timedelta

from cron import executions, jobs


def _job(prompt):
    return jobs.create_job(prompt=prompt, schedule="every 1h", model="fixture", deliver="local")


def _complete(job_id, instant):
    row = executions.create_execution(job_id, source="builtin", scheduled_instant=instant)
    executions.mark_execution_running(row["id"])
    executions.finish_execution(row["id"], success=True)


def _rewind(job_id, instant):
    stored = jobs.load_jobs()
    next(r for r in stored if r["id"] == job_id)["next_run_at"] = instant
    jobs.save_jobs(stored)


def _next_run(job_id):
    return next(r for r in jobs.load_jobs() if r["id"] == job_id)["next_run_at"]


def test_rewound_slot_completed_long_ago_does_not_refire(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 3)
    with jobs.use_cron_store(tmp_path / "cron"):
        now = jobs._hermes_now()
        job = _job("daily")
        slot = (now - timedelta(hours=13)).replace(microsecond=0).isoformat()
        _complete(job["id"], slot)
        # The rest of the fleet keeps finishing runs; the global cap would prune the row above.
        for index in range(10):
            _complete(f"other-{index}", (now - timedelta(minutes=index)).isoformat())
        _rewind(job["id"], slot)

        assert [j["id"] for j in jobs.get_due_jobs()] == []
        assert jobs._ensure_aware(jobs.datetime.fromisoformat(_next_run(job["id"]))) > now


def test_rewound_slot_older_than_a_completed_later_slot_does_not_refire(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with jobs.use_cron_store(tmp_path / "cron"):
        now = jobs._hermes_now()
        job = _job("hourly")
        stale = (now - timedelta(hours=5)).replace(microsecond=0).isoformat()
        ran = (now - timedelta(minutes=20)).replace(microsecond=0).isoformat()
        _complete(job["id"], ran)
        _rewind(job["id"], stale)  # no ledger row for `stale` itself, but a LATER slot completed

        assert [j["id"] for j in jobs.get_due_jobs()] == []
        assert jobs._ensure_aware(jobs.datetime.fromisoformat(_next_run(job["id"]))) > now


def test_genuine_missed_slot_still_catches_up(tmp_path, monkeypatch):
    """Control: a slot newer than every completion is a real miss (gateway down) and fires once."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with jobs.use_cron_store(tmp_path / "cron"):
        now = jobs._hermes_now()
        job = _job("hourly")
        _complete(job["id"], (now - timedelta(hours=6)).replace(microsecond=0).isoformat())
        _rewind(job["id"], (now - timedelta(hours=4)).replace(microsecond=0).isoformat())

        assert [j["id"] for j in jobs.get_due_jobs()] == [job["id"]]


def test_retention_keeps_each_jobs_newest_completed_occurrence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 2)
    with jobs.use_cron_store(tmp_path / "cron"):
        now = jobs._hermes_now()
        old, newest = (now - timedelta(days=1)).isoformat(), (now - timedelta(hours=1)).isoformat()
        _complete("daily", old)
        _complete("daily", newest)
        for index in range(5):
            _complete(f"other-{index}", now.isoformat())

        kept = [r for r in executions.list_executions(job_id="daily", limit=10)]
        assert [r["scheduled_instant"] for r in kept] == [
            jobs.datetime.fromisoformat(newest).astimezone(jobs.timezone.utc).isoformat()]
        # The cap still bounds the ledger: past it, only per-job newest completions survive.
        assert len(executions.list_executions(limit=100)) == 6


def test_rewound_slot_still_catches_up_an_occurrence_missed_after_the_watermark(tmp_path, monkeypatch):
    """Rewound behind a completion, then a real miss after it: resume after the watermark, fire once.

    Prism #1795 2c48c2277a95: a daily job completed Monday, the snapshot points at Sunday, the
    scheduler was down for Tuesday's slot. Skipping straight to now would lose Tuesday."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with jobs.use_cron_store(tmp_path / "cron"):
        now = jobs._hermes_now()
        job = _job("hourly")
        ran = (now - timedelta(hours=5)).replace(microsecond=0).isoformat()
        _complete(job["id"], ran)
        _rewind(job["id"], (now - timedelta(hours=8)).replace(microsecond=0).isoformat())

        assert [j["id"] for j in jobs.get_due_jobs()] == [job["id"]]


def test_prematurely_completed_later_slot_is_not_rewind_proof(tmp_path, monkeypatch):
    """A completion recorded long before the slot it claims is poison, not proof (Prism abe5a49483e9)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with jobs.use_cron_store(tmp_path / "cron"):
        now = jobs._hermes_now()
        job = _job("hourly")
        _complete(job["id"], (now + timedelta(hours=2)).replace(microsecond=0).isoformat())
        _rewind(job["id"], (now - timedelta(hours=3)).replace(microsecond=0).isoformat())

        assert [j["id"] for j in jobs.get_due_jobs()] == [job["id"]]


def test_rewound_oneshot_completed_at_a_later_reschedule_does_not_refire(tmp_path, monkeypatch):
    """A one-shot rescheduled later and completed, then restored from an older snapshot, is done.

    Prism #1797 792e09957d5e: the restored ``next_run_at`` has no exact completion and the
    later-completion check was recurring-only, so the one-shot ran its side effects twice."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with jobs.use_cron_store(tmp_path / "cron"):
        now = jobs._hermes_now()
        job = jobs.create_job(prompt="once", schedule=(now + timedelta(hours=1)).isoformat(),
                              model="fixture", deliver="local")
        _complete(job["id"], (now - timedelta(minutes=10)).replace(microsecond=0).isoformat())
        restored = (now - timedelta(minutes=30)).replace(microsecond=0).isoformat()
        _rewind(job["id"], restored)

        assert [j["id"] for j in jobs.get_due_jobs()] == []
        assert jobs.claim_job_for_fire(job["id"]) is False
        assert _next_run(job["id"]) == restored  # no recurring successor computed for a one-shot


def test_retention_keeps_the_newest_valid_completion_not_a_poisoned_later_one(tmp_path, monkeypatch):
    """Prism #1797 f84fc879b9dd: a poisoned future-slot completion must not displace the valid
    watermark from the per-job retention exemption, or a rewind re-runs today's completed slot."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 2)
    with jobs.use_cron_store(tmp_path / "cron"):
        now = jobs._hermes_now()
        job = jobs.create_job(prompt="daily", schedule="every 24h", model="fixture", deliver="local")
        valid = (now - timedelta(hours=2)).replace(microsecond=0).isoformat()
        _complete(job["id"], valid)
        _complete(job["id"], (now + timedelta(hours=20)).replace(microsecond=0).isoformat())  # poison
        for index in range(5):
            _complete(f"other-{index}", now.isoformat())
        _rewind(job["id"], (now - timedelta(hours=5)).replace(microsecond=0).isoformat())

        assert [j["id"] for j in jobs.get_due_jobs()] == []
        assert jobs._ensure_aware(jobs.datetime.fromisoformat(_next_run(job["id"]))) > now
