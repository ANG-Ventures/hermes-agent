"""One-shot due during a slow tick fires on the next tick (t_d238204c).

2026-09-30: an absolute one-shot ("2026-09-30T09:45:00", 28a5a721bb20) never
fired while ticks ran every ~60s under Studio load 44-51/32. These pin:
  - a tick whose scan started before run_at does not lose the job: the next
    scan (on time, or minutes late) returns it exactly once;
  - a past-due one-shot the scan leaves out is named in a WARNING with the
    fields that decided the skip (it used to be silent).
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

import cron.jobs as jobs_mod
from cron.jobs import LATE_FIRE_KEY, drain_missed_oneshot_notices, get_due_jobs, save_jobs

PT = timezone(timedelta(hours=-7))
RUN_AT = datetime(2026, 9, 30, 9, 45, 0, tzinfo=PT)


@pytest.fixture()
def clock(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {"cron": {}})
    state = {"now": RUN_AT - timedelta(seconds=8)}
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: state["now"])
    monkeypatch.setattr(jobs_mod, "_overdue_oneshot_warned", {})
    drain_missed_oneshot_notices()
    yield state
    drain_missed_oneshot_notices()


def _oneshot(jid="cut", **extra):
    row = {
        "id": jid,
        "name": jid,
        "prompt": "",
        "no_agent": True,
        "script": "oneshot/cut.sh",
        "schedule": {"kind": "once", "run_at": RUN_AT.isoformat()},
        "next_run_at": RUN_AT.isoformat(),
        "last_run_at": None,
        "enabled": True,
        "state": "scheduled",
        "repeat": {"times": 1, "completed": 0},
        "deliver": "local",
    }
    row.update(extra)
    return row


def _recurring(n):
    # Load-shaped store: hundreds of recurring rows, none due at 09:45.
    nxt = (RUN_AT + timedelta(minutes=30)).isoformat()
    return [
        {
            "id": f"r{i:04d}",
            "name": f"r{i:04d}",
            "prompt": "x",
            "schedule": {"kind": "interval", "minutes": 60},
            "next_run_at": nxt,
            "last_run_at": None,
            "enabled": True,
            "state": "scheduled",
            "repeat": {"times": None, "completed": 0},
            "deliver": "local",
        }
        for i in range(n)
    ]


@pytest.mark.parametrize("next_tick_late_s", [52, 65, 300])
def test_oneshot_due_during_slow_tick_fires_on_next_tick(clock, next_tick_late_s):
    save_jobs(_recurring(588) + [_oneshot()])

    # Slow tick: its scan snapshot is taken 8s BEFORE run_at, the tick then
    # takes past run_at to finish. The one-shot is correctly not due yet.
    assert [j["id"] for j in get_due_jobs() if j["id"] == "cut"] == []

    # Next tick, on time or several minutes late under load: fires once.
    clock["now"] = RUN_AT + timedelta(seconds=next_tick_late_s)
    due = [j for j in get_due_jobs() if j["id"] == "cut"]
    assert len(due) == 1
    if next_tick_late_s > jobs_mod.ONESHOT_GRACE_SECONDS:
        assert due[0][LATE_FIRE_KEY] == next_tick_late_s

    # The run claim holds it: a third tick while it runs does not re-dispatch.
    clock["now"] += timedelta(seconds=60)
    assert [j for j in get_due_jobs() if j["id"] == "cut"] == []


def test_skipped_overdue_oneshot_is_named(clock, caplog):
    # A never-run one-shot 5 min past due that the scan skips (here: a fresh
    # fire_claim left by a claim that never ran) must be logged with the
    # deciding fields, once per interval, not silently every tick.
    clock["now"] = RUN_AT + timedelta(minutes=5)
    claim = {"at": (clock["now"] - timedelta(seconds=30)).isoformat(), "by": "other"}
    save_jobs([_oneshot(run_claim=claim)])

    with caplog.at_level(logging.WARNING, logger="cron.jobs"):
        assert get_due_jobs() == []
        clock["now"] += timedelta(seconds=60)
        assert get_due_jobs() == []

    hits = [r.getMessage() for r in caplog.records if "cron.oneshot.overdue_not_due" in r.getMessage()]
    assert len(hits) == 1, hits
    assert "id=cut" in hits[0] and "overdue=300s" in hits[0] and "run_claim=" in hits[0]


def test_paused_overdue_oneshot_is_not_named(clock, caplog):
    clock["now"] = RUN_AT + timedelta(minutes=5)
    save_jobs([_oneshot(enabled=False, state="paused", paused_at=RUN_AT.isoformat())])
    with caplog.at_level(logging.WARNING, logger="cron.jobs"):
        assert get_due_jobs() == []
    assert not [r for r in caplog.records if "overdue_not_due" in r.getMessage()]
