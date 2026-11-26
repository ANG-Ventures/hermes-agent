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


def _fire_mid_tick(clock, jid, tick_at, claim_after, run_seconds):
    """Rest of one fire after the due scan found the job: the advance + claim (which stamp the
    fire instant) run ``claim_after`` into the tick, as on the live ticker."""
    from cron.jobs import advance_next_runs, claim_job_for_fire, mark_job_run

    clock["now"] = tick_at + claim_after
    advance_next_runs([jid])
    claimed = claim_job_for_fire(jid, return_job=True)
    assert claimed
    clock["now"] = tick_at + claim_after + timedelta(seconds=run_seconds)
    assert mark_job_run(jid, True, expected_fire_owner=claimed["fire_claim"]["by"])


@pytest.mark.parametrize("minutes", [1, 2, 5])
def test_fixed_cadence_ticker_fires_every_n_ticks_when_claim_lags_the_scan(clock, minutes):
    """t_ed8ce7a2: the ticker runs on a FIXED 60 s cadence (``next_tick += interval``), so tick
    k+N starts exactly N minutes after tick k. The fire instant is stamped when the job is
    claimed, a moment AFTER that tick's due scan; ``fire_at + N`` then lands just after tick
    k+N's scan and the job waited for tick k+N+1 (live: 1m rows every 120 s, 2m every 180 s,
    5m every 360 s, claim exactly 60 s after scheduled_instant)."""
    jid = _make_interval_job(minutes, T0 - timedelta(seconds=1))
    scan_after, claim_after = timedelta(milliseconds=200), timedelta(milliseconds=1500)
    fires = []
    for i in range(minutes * 6 + 1):
        tick_at = T0 + timedelta(seconds=60 * i)  # fixed cadence: no per-tick drift
        if jid in _due_ids(clock, tick_at + scan_after):
            fires.append(i)
            _fire_mid_tick(clock, jid, tick_at, claim_after, run_seconds=7)
    assert fires == list(range(0, minutes * 6 + 1, minutes))


def test_external_provider_claim_rearms_at_exact_interval(clock):
    """Prism 589ad59a51f6 (t_ed8ce7a2): an external provider (Chronos) claims at the exact slot
    and re-arms its callback at the stored ``next_run_at``; no 60 s poll absorbs an offset, so
    the half-tick shift must not apply. Claim 12:00:00, finish 12:00:05 -> next 12:01:00."""
    from cron.jobs import claim_job_for_fire, get_job, mark_job_run

    t = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
    jid = _make_interval_job(1, t)
    for i in range(3):
        clock["now"] = t + timedelta(minutes=i)
        assert get_job(jid)["next_run_at"] == clock["now"].isoformat()
        claimed = claim_job_for_fire(jid, return_job=True)
        assert claimed
        clock["now"] += timedelta(seconds=5)
        assert mark_job_run(jid, True, expected_fire_owner=claimed["fire_claim"]["by"])
    assert get_job(jid)["next_run_at"] == (t + timedelta(minutes=3)).isoformat()


def test_overlong_run_skips_missed_slots_without_double_fire(clock):
    """A 1m job whose run takes 150 s is re-armed in the future, once."""
    from cron.jobs import get_job

    jid = _make_interval_job(1, T0 - timedelta(seconds=1))
    _fire(clock, jid, T0, run_seconds=150)
    finish = T0 + timedelta(seconds=150)
    nxt = datetime.fromisoformat(get_job(jid)["next_run_at"])
    # Stays on the fire-time grid, armed half a tick early (t_ed8ce7a2): due on tick T0+3m.
    assert nxt == T0 + timedelta(minutes=3) - timedelta(seconds=30)
    # The skipped slot (tick T0+2m) does not fire; the next one fires once, on tick T0+3m.
    assert jid not in _due_ids(clock, T0 + timedelta(minutes=2))
    assert jid in _due_ids(clock, T0 + timedelta(minutes=3))


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


def test_next_slot_steps_real_time_across_dst_fall_back(monkeypatch):
    """``fire_at + k*N`` is real elapsed time, not wall-clock arithmetic.

    America/New_York falls back at 2026-11-01 02:00 EDT -> 01:00 EST. A 2 h
    interval fired at 00:11 EDT (04:11Z) is next due at 06:11Z = 01:11 EST.
    Aware local + timedelta would give 02:11 EST (07:11Z), an hour late
    (CI e2e test_cron_virtual_clock_soak[newyork_on_shanghai_fall]).
    """
    from zoneinfo import ZoneInfo

    from cron.jobs import _next_interval_slot

    ny = ZoneInfo("America/New_York")
    fire_at = datetime(2026, 11, 1, 0, 11, tzinfo=ny)
    now = fire_at + timedelta(seconds=5)
    # _ensure_aware converts the anchor into the Hermes zone (a DST zone here).
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)
    slot = _next_interval_slot({"kind": "interval", "minutes": 120}, fire_at.isoformat(), now)
    assert datetime.fromisoformat(slot).astimezone(timezone.utc) == datetime(
        2026, 11, 1, 6, 11, tzinfo=timezone.utc
    )
