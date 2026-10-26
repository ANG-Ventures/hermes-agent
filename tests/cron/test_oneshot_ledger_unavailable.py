"""An unreadable executions ledger never licenses retiring a one-shot (t_9b3b6e4b).

2026-10-23 12:30-12:38 PT the gateway hit EMFILE; every ``sqlite3.connect`` on
``cron/executions.db`` raised ``unable to open database file`` and the dedup gate
(``completed_occurrence``) swallowed it to False. Nine one-shots that had already
completed were then retired as MISSED and paged their deliver targets.

Pins:
  - ledger unreadable + one-shot  -> not fired, not retired, no MISSED notice, one throttled WARNING
  - ledger unreadable + recurring -> still fires (a duplicate beats a silent skip)
  - ledger readable, slot completed -> the one-shot record settles terminal
  - ledger readable, slot not completed, past catch-up -> still retired with MISSED
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

import cron.jobs as jobs_mod
from cron.jobs import drain_missed_oneshot_notices, get_due_jobs, is_terminal_job, load_jobs, save_jobs

FIXED_NOW = datetime(2026, 10, 23, 19, 35, 0, tzinfo=timezone.utc)


@pytest.fixture()
def cron_store(tmp_path, monkeypatch):
    from cron import executions

    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: FIXED_NOW)
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {"cron": {}})
    monkeypatch.setattr(jobs_mod, "_ledger_unavailable_warned", {}, raising=False)
    drain_missed_oneshot_notices()
    yield tmp_path
    drain_missed_oneshot_notices()


@pytest.fixture()
def ledger_down(monkeypatch):
    from cron import executions

    @contextmanager
    def _broken():
        raise sqlite3.OperationalError("unable to open database file")
        yield  # pragma: no cover

    monkeypatch.setattr(executions, "_transaction", _broken)


def _oneshot(jid, run_at_dt):
    return {
        "id": jid, "name": jid, "prompt": "remind me",
        "schedule": {"kind": "once", "run_at": run_at_dt.isoformat()},
        "next_run_at": run_at_dt.isoformat(), "last_run_at": None,
        "enabled": True, "state": "scheduled",
        "repeat": {"times": 1, "completed": 0}, "deliver": "local",
    }


def _interval(jid, next_run_dt):
    return {
        "id": jid, "name": jid, "prompt": "x",
        "schedule": {"kind": "interval", "minutes": 60},
        "next_run_at": next_run_dt.isoformat(), "last_run_at": None,
        "enabled": True, "state": "scheduled",
        "repeat": {"times": None, "completed": 0}, "deliver": "local",
    }


def _ledger_warnings(caplog):
    return [r for r in caplog.records if "cron.oneshot.ledger_unavailable" in r.getMessage()]


def test_oneshot_ledger_unavailable_is_not_retired(cron_store, ledger_down, caplog):
    # 7 h past due: outside the default 6 h catch-up window, the exact MISSED branch.
    record = _oneshot("cbc", FIXED_NOW - timedelta(hours=7))
    save_jobs([record])
    before = load_jobs()

    with caplog.at_level(logging.WARNING):
        assert get_due_jobs() == []

    assert load_jobs() == before
    assert drain_missed_oneshot_notices() == []
    assert not (cron_store / "cron" / "output" / "cbc").exists()
    assert len(_ledger_warnings(caplog)) == 1
    assert "id=cbc" in _ledger_warnings(caplog)[0].getMessage()
    # The ledger read failure itself still logs at its raise site (load-bearing for operators).
    assert any("Cannot check completed occurrence" in r.getMessage() for r in caplog.records)


def test_oneshot_ledger_unavailable_inside_catchup_does_not_fire(cron_store, ledger_down):
    save_jobs([_oneshot("soon", FIXED_NOW - timedelta(minutes=3))])
    before = load_jobs()
    assert get_due_jobs() == []
    assert load_jobs() == before


def test_recurring_ledger_unavailable_still_fires(cron_store, ledger_down):
    save_jobs([_interval("hourly", FIXED_NOW - timedelta(minutes=1))])
    assert [d["id"] for d in get_due_jobs()] == ["hourly"]


def test_ledger_unavailable_warning_throttled_across_ticks(cron_store, ledger_down, caplog):
    save_jobs([_oneshot("cbc", FIXED_NOW - timedelta(hours=7))])
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert get_due_jobs() == []
    assert len(_ledger_warnings(caplog)) == 1
    assert [j["id"] for j in load_jobs()] == ["cbc"]


def test_ledger_proven_oneshot_settles_terminal(cron_store, caplog):
    from cron import executions

    run_at = FIXED_NOW - timedelta(hours=7)
    save_jobs([_oneshot("cbc", run_at)])
    row = executions.create_execution(
        "cbc", source="builtin", scheduled_instant=run_at.isoformat())
    executions.finish_execution(row["id"], success=True)

    with caplog.at_level(logging.WARNING, logger="cron.occurrences"):
        assert get_due_jobs() == []
        stored = load_jobs()
        assert len(stored) == 1
        job = stored[0]
        assert is_terminal_job(job)
        assert job["repeat"]["completed"] == job["repeat"]["times"] == 1
        assert job["last_run_at"]
        assert drain_missed_oneshot_notices() == []
        assert get_due_jobs() == []  # second tick: terminal, not re-evaluated
    skips = [r for r in caplog.records if "already completed by execution" in r.getMessage()]
    assert len(skips) == 1, caplog.text
    assert load_jobs() == stored


def test_settle_does_not_double_count(cron_store):
    from cron import executions

    run_at = FIXED_NOW - timedelta(minutes=5)
    record = _oneshot("once", run_at)
    record["repeat"] = {"times": 1, "completed": 1}  # claim_dispatch already counted it
    save_jobs([record])
    row = executions.create_execution(
        "once", source="builtin", scheduled_instant=run_at.isoformat())
    executions.finish_execution(row["id"], success=True)

    assert get_due_jobs() == []
    assert load_jobs()[0]["repeat"]["completed"] == 1
    assert is_terminal_job(load_jobs()[0])


def test_readable_ledger_unrun_oneshot_still_retired_missed(cron_store):
    save_jobs([_oneshot("gone", FIXED_NOW - timedelta(hours=7))])
    assert get_due_jobs() == []
    assert load_jobs() == []
    notices = drain_missed_oneshot_notices()
    assert [n["job"]["id"] for n in notices] == ["gone"]


def test_fire_claim_refuses_oneshot_when_ledger_unavailable(cron_store, ledger_down):
    from cron.jobs import claim_job_for_fire

    save_jobs([_oneshot("cbc", FIXED_NOW - timedelta(seconds=30))])
    before = load_jobs()
    assert claim_job_for_fire("cbc") is False
    assert load_jobs() == before


def test_fire_claim_recurring_proceeds_when_ledger_unavailable(cron_store, ledger_down):
    from cron.jobs import claim_job_for_fire

    save_jobs([_interval("hourly", FIXED_NOW - timedelta(seconds=30))])
    assert claim_job_for_fire("hourly") is True
