"""Interval jobs re-arm off their fire instant, not their finish (t_b2f58c48).

``mark_job_run`` used to re-arm ``every N m`` jobs at ``finish + N``. The
ticker scans every 60 s, so ``finish + N`` always landed a few seconds after
tick k+N and the job waited for tick k+N+1: every interval job ran every N+1
minutes (measured live: 1m -> 121 s, 2m -> 3.03 min, 5m -> 5.06-6.05 min).

These drive the real store (temp HERMES_HOME) through the same calls the
ticker makes: get_due_jobs -> advance_next_runs -> claim_job_for_fire ->
mark_job_run, with a fake clock.
"""
from datetime import datetime, timedelta, timezone

import pytest


T0 = datetime(2026, 10, 3, 10, 52, 24, 850000, tzinfo=timezone.utc)


@pytest.fixture
def clock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = {"now": T0}
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: state["now"])
    return state


def _make_interval_job(minutes, next_run_at):
    from cron.jobs import create_job, load_jobs, save_jobs

    job = create_job(prompt="x", schedule=f"every {minutes}m", name="iv")
    jobs = load_jobs()
    jobs[0]["next_run_at"] = next_run_at.isoformat()
    save_jobs(jobs)
    return job["id"]


def _fire(clock, jid, tick_at, run_seconds):
    """One ticker fire: scan, advance, claim, run, mark (as cron.scheduler.tick)."""
    from cron.jobs import (
        advance_next_runs, claim_job_for_fire, get_due_jobs, mark_job_run,
    )

    clock["now"] = tick_at
    due = [j["id"] for j in get_due_jobs()]
    assert jid in due, f"job not due at tick {tick_at}"
    advance_next_runs(due)
    claimed = claim_job_for_fire(jid, return_job=True)
    assert claimed
    clock["now"] = tick_at + timedelta(seconds=run_seconds)
    assert mark_job_run(jid, True, expected_fire_owner=claimed["fire_claim"]["by"])


def _due_ids(clock, at):
    from cron.jobs import get_due_jobs

    clock["now"] = at
    return [j["id"] for j in get_due_jobs()]


def test_1m_job_finishing_2s_after_its_tick_is_due_on_the_next_tick(clock):
    """The card's RED case: a 1m job whose run ends 2 s after its tick."""
    jid = _make_interval_job(1, T0 - timedelta(seconds=1))
    _fire(clock, jid, T0, run_seconds=2)
    # Next tick: 60 s after the previous tick plus a little scan jitter.
    assert jid in _due_ids(clock, T0 + timedelta(seconds=60, milliseconds=5))


@pytest.mark.parametrize("minutes", [1, 2, 5, 15])
def test_interval_job_fires_every_n_ticks_over_many_cycles(clock, minutes):
    """Over 6 cycles of 60 s ticks the job fires exactly every N ticks."""
    jid = _make_interval_job(minutes, T0 - timedelta(seconds=1))
    tick = T0
    fires = []
    for i in range(minutes * 6 + 1):
        tick_at = T0 + timedelta(seconds=60 * i, milliseconds=3 * i)
        if jid in _due_ids(clock, tick_at):
            fires.append(i)
            _fire(clock, jid, tick_at, run_seconds=7)
    assert fires == list(range(0, minutes * 6 + 1, minutes))


def test_overlong_run_skips_missed_slots_without_double_fire(clock):
    """A 1m job whose run takes 150 s is re-armed in the future, once."""
    from cron.jobs import get_job

    jid = _make_interval_job(1, T0 - timedelta(seconds=1))
    _fire(clock, jid, T0, run_seconds=150)
    finish = T0 + timedelta(seconds=150)
    nxt = datetime.fromisoformat(get_job(jid)["next_run_at"])
    assert finish < nxt <= finish + timedelta(minutes=1)
    assert nxt == T0 + timedelta(minutes=3)  # stays on the fire-time grid
    # Not due again until that slot (no catch-up burst for skipped slots).
    assert jid not in _due_ids(clock, finish + timedelta(seconds=1))
    assert jid in _due_ids(clock, nxt)


def test_fire_stamp_is_cleared_and_absent_stamp_falls_back_to_finish(clock):
    """mark_job_run consumes the stamp; with no stamp it keeps finish + N."""
    from cron.jobs import INTERVAL_FIRE_AT_KEY, get_job, mark_job_run

    jid = _make_interval_job(5, T0 - timedelta(seconds=1))
    _fire(clock, jid, T0, run_seconds=2)
    assert INTERVAL_FIRE_AT_KEY not in get_job(jid)

    # A run that bypassed the fire paths (e.g. `hermes cron run --wait`).
    clock["now"] = T0 + timedelta(minutes=2)
    mark_job_run(jid, True)
    assert get_job(jid)["next_run_at"] == (clock["now"] + timedelta(minutes=5)).isoformat()


def test_cron_kind_unaffected(clock):
    """cron-kind jobs never get the stamp (croniter already aligns to slots)."""
    pytest.importorskip("croniter")
    from cron.jobs import (
        INTERVAL_FIRE_AT_KEY, advance_next_runs, create_job, get_job, load_jobs,
        save_jobs,
    )

    job = create_job(prompt="x", schedule="* * * * *", name="c")
    jobs = load_jobs()
    jobs[0]["next_run_at"] = (T0 - timedelta(seconds=1)).isoformat()
    save_jobs(jobs)
    advance_next_runs([job["id"]])
    assert INTERVAL_FIRE_AT_KEY not in get_job(job["id"])
