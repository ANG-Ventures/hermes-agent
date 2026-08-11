"""Unit tests for the Chronos NAS-mediated cron provider (Phase 4D).

All NAS calls are mocked — ZERO live network. These prove:
  - is_available is config-only (no network), false without config.
  - one-shot arming sends the right provision payload (incl. sub-minute fires —
    the agent owns the time, so there's no 1-minute floor).
  - reconcile arms missing, cancels orphaned, skips paused.
  - fire_due re-arms the next one-shot after a successful run, and repeat-N
    (job gone) stops re-arming.
"""

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _future(**kw):
    """A timestamp safely in the future.

    These tests used to hardcode 2026-06-18 fire times. Arming now (correctly)
    refuses a fire_at in the past, so a frozen date would make them fail purely
    with the passage of time. The property under test is "a FUTURE fire is
    armed", so derive it rather than freezing it.
    """
    from datetime import datetime, timedelta, timezone

    kw = kw or {"hours": 6}
    return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat()


FUTURE_A = _future(hours=6)
FUTURE_B = _future(hours=7)


@pytest.fixture
def chronos(monkeypatch):
    """A ChronosCronScheduler with a fake NAS client capturing calls."""
    from plugins.cron_providers.chronos import ChronosCronScheduler

    class FakeClient:
        def __init__(self):
            self.provisions = []
            self.cancels = []
            self._armed = []

        def provision(self, *, job_id, fire_at, agent_callback_url, dedup_key):
            self.provisions.append({
                "job_id": job_id, "fire_at": fire_at,
                "agent_callback_url": agent_callback_url, "dedup_key": dedup_key,
            })
            return {"schedule_id": f"sched-{job_id}"}

        def cancel(self, *, job_id):
            self.cancels.append(job_id)
            return {}

        def list_armed(self):
            return list(self._armed)

    prov = ChronosCronScheduler()
    fake = FakeClient()
    prov._client = fake
    # callback_url is read via _cfg; patch the module helper to avoid config.
    monkeypatch.setattr("plugins.cron_providers.chronos._cfg",
                        lambda *k, default="": "https://agent.example/" if k[-1] == "callback_url" else "https://portal.test")
    return prov, fake


# -- is_available -------------------------------------------------------------

def test_is_available_false_without_config(temp_home, monkeypatch):
    from plugins.cron_providers.chronos import ChronosCronScheduler

    monkeypatch.setattr("plugins.cron_providers.chronos._cfg", lambda *k, default="": "")
    assert ChronosCronScheduler().is_available() is False


# -- arming -------------------------------------------------------------------

def test_arm_one_shot_sends_provision(chronos):
    prov, fake = chronos
    prov._arm_one_shot({"id": "j1", "next_run_at": FUTURE_A})

    assert len(fake.provisions) == 1
    p = fake.provisions[0]
    assert p["job_id"] == "j1"
    assert p["fire_at"] == FUTURE_A
    assert p["dedup_key"] == "j1:" + FUTURE_A
    assert p["agent_callback_url"] == "https://agent.example/"


# -- reconcile ----------------------------------------------------------------

def test_reconcile_arms_all_enabled(temp_home, chronos, monkeypatch):
    prov, fake = chronos
    jobs = [
        {"id": "a", "enabled": True, "next_run_at": FUTURE_A, "state": "scheduled"},
        {"id": "b", "enabled": True, "next_run_at": FUTURE_B, "state": "scheduled"},
    ]
    monkeypatch.setattr("cron.jobs.load_jobs", lambda: jobs)
    monkeypatch.setattr("cron.jobs.get_job", lambda jid: next(j for j in jobs if j["id"] == jid))

    prov.reconcile()
    assert {p["job_id"] for p in fake.provisions} == {"a", "b"}
    assert fake.cancels == []


def test_reconcile_advances_a_missed_recurring_job_before_arming(temp_home, chronos):
    """Skipping a stale fire must not permanently unschedule the recurring job."""
    from datetime import datetime, timedelta, timezone

    from cron.jobs import create_job, get_job, load_jobs, save_jobs

    prov, fake = chronos
    job = create_job(prompt="Recurring check", schedule="every 8h")
    jobs = load_jobs()
    jobs[0]["next_run_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=3)
    ).isoformat()
    save_jobs(jobs)

    before = datetime.now(timezone.utc)
    prov.reconcile()

    assert len(fake.provisions) == 1
    fire_at = fake.provisions[0]["fire_at"]
    assert datetime.fromisoformat(fire_at) > before
    stored = get_job(job["id"])
    assert stored is not None
    assert stored["next_run_at"] == fire_at


# -- fire_due re-arm ----------------------------------------------------------

def test_fire_due_rearms_next_oneshot(chronos, monkeypatch):
    prov, fake = chronos
    # super().fire_due runs the job; stub the ABC default to "ran".
    monkeypatch.setattr("cron.scheduler_provider.CronScheduler.fire_due",
                        lambda self, jid, **kw: True)
    monkeypatch.setattr("cron.jobs.get_job",
                        lambda jid: {"id": jid, "enabled": True, "next_run_at": FUTURE_B})

    assert prov.fire_due("j1") is True
    assert [p["job_id"] for p in fake.provisions] == ["j1"]
    assert fake.provisions[0]["fire_at"] == FUTURE_B


# -- past fire_at must never be armed (re-fire-on-restart guard) --------------


def test_arm_one_shot_refuses_a_fire_at_in_the_past(chronos):
    """A next_run_at already in the PAST must not be armed.

    ROOT CAUSE (2026-08-11): reconcile() runs on gateway boot and arms every
    enabled job at its stored next_run_at, with no check that the time is still
    in the future. A job whose next_run_at had already passed got armed with a
    past fire_at, and the external scheduler fired it IMMEDIATELY -- so a
    scheduled job re-ran minutes after a restart. Observed live: a `0 */8` job
    delivered 7 messages in ~17h, 3 of them re-fires whose own "in the last
    0.1h/0.4h" delta proved they followed a real run.

    A past one-shot is a MISSED fire, not a due one. The next legitimate fire is
    already represented by the schedule; arming the stale one only duplicates it.
    """
    from datetime import datetime, timedelta, timezone

    prov, fake = chronos
    past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    prov._arm_one_shot({"id": "stale", "next_run_at": past})

    assert fake.provisions == [], (
        "armed a one-shot whose fire_at is in the past -- the external scheduler "
        "fires those immediately, which is the re-fire-on-restart bug"
    )
    assert "stale" not in prov._armed


def test_arm_one_shot_still_arms_a_future_fire_at(chronos):
    """The guard must not break normal arming."""
    from datetime import datetime, timedelta, timezone

    prov, fake = chronos
    future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    prov._arm_one_shot({"id": "ok", "next_run_at": future})

    assert [p["job_id"] for p in fake.provisions] == ["ok"]
    assert prov._armed["ok"] == future


def test_arm_one_shot_arms_a_fire_at_inside_the_grace_window(chronos):
    """A fire_at a few seconds in the past is CLOCK SKEW, not a missed fire.

    Arming must stay tolerant there, or a job whose fire_at passed while the
    provision request was in flight would be silently dropped.
    """
    from datetime import datetime, timedelta, timezone

    prov, fake = chronos
    just_now = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    prov._arm_one_shot({"id": "skew", "next_run_at": just_now})

    assert [p["job_id"] for p in fake.provisions] == ["skew"]


def test_arm_one_shot_tolerates_an_unparseable_fire_at(chronos):
    """A malformed timestamp must ARM (fail-open), never crash reconcile.

    Dropping it would silently unschedule the job; reconcile is the only thing
    that re-arms, so a raised exception there stops every later job in the loop.
    """
    prov, fake = chronos
    prov._arm_one_shot({"id": "weird", "next_run_at": "not-a-timestamp"})
    assert [p["job_id"] for p in fake.provisions] == ["weird"]
