"""Claim heartbeats and the fire-claim fence's ownership check stay off the global ``_jobs_lock``.

Live 2026-10-26 (Apollo, after #2287 moved hot fields into ``cron/runtime_state.db``): py-spy
showed 14-21 threads waiting on ``_jobs_lock``; the callers were ``fire_claim_fence`` (6x),
``heartbeat_fire_claim`` from ``fence.lost()`` (3x), ``claim_job_for_fire`` and ``claim_dispatch``.
In sqlite mode a heartbeat is one owner-guarded UPDATE in the row's own ``BEGIN IMMEDIATE``
transaction and the fence check is one committed row read, so neither may queue behind an
unrelated ``_jobs_lock`` holder (a scheduler tick, a definition save).
"""

import threading
import time

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CRON_RUNTIME_STATE", raising=False)
    monkeypatch.delenv("HERMES_CRON_MIRROR_INTERVAL_S", raising=False)
    (tmp_path / "cron").mkdir()
    return tmp_path


def _claimed_jobs(cj, n):
    out = []
    for i in range(n):
        created = cj.create_job(prompt="x", schedule="every 5m", name=f"hb{i}")
        assert cj.claim_job_for_fire(created["id"]) is True
        out.append((created["id"], cj.get_job(created["id"])["fire_claim"]["by"]))
    return out


def _run_while_jobs_lock_held(cj, work, n_threads):
    """Hold ``_jobs_lock`` in one thread, run ``work(i)`` in ``n_threads`` others, and return
    (results, finished-while-held count). The holder releases only after the workers had 5 s."""
    held, release = threading.Event(), threading.Event()

    def holder():
        with cj._jobs_lock():
            held.set()
            release.wait(30)

    h = threading.Thread(target=holder, daemon=True)
    h.start()
    assert held.wait(10)
    results = [None] * n_threads
    done = [threading.Event() for _ in range(n_threads)]

    def run(i):
        try:
            results[i] = work(i)
        finally:
            done[i].set()

    workers = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(n_threads)]
    for w in workers:
        w.start()
    deadline = time.monotonic() + 5.0
    for d in done:
        d.wait(max(deadline - time.monotonic(), 0))
    finished_while_held = sum(d.is_set() for d in done)
    release.set()
    for w in workers:
        w.join(30)
    h.join(30)
    return results, finished_while_held


def test_heartbeats_and_fence_do_not_wait_on_jobs_lock(temp_home):
    from cron import jobs as cj

    claims = _claimed_jobs(cj, 6)

    def work(i):
        jid, owner = claims[i % len(claims)]
        if i % 2:
            with cj.fire_claim_fence(jid, expected_owner=owner) as owns:
                return ("fence", owns)
        return ("beat", cj.heartbeat_fire_claim(jid, expected_owner=owner))

    results, finished = _run_while_jobs_lock_held(cj, work, 12)
    assert finished == 12, f"{12 - finished}/12 heartbeat/fence calls blocked on _jobs_lock"
    assert all(ok for _kind, ok in results), results


def test_unlocked_heartbeat_keeps_owner_guard(temp_home):
    """Off the lock the compare-and-set still holds: a stale owner never refreshes, and the
    fence reports a re-owned claim as lost."""
    from cron import jobs as cj

    (jid, owner_a), = _claimed_jobs(cj, 1)
    assert cj.claim_job_for_fire(jid, claim_ttl_seconds=0) is True  # owner B takes over
    owner_b = cj.get_job(jid)["fire_claim"]["by"]
    assert owner_b != owner_a
    assert cj.heartbeat_fire_claim(jid, expected_owner=owner_a) is False
    assert cj.heartbeat_fire_claim(jid, expected_owner=owner_b) is True
    with cj.fire_claim_fence(jid, expected_owner=owner_a) as owns:
        assert owns is False
    with cj.fire_claim_fence(jid, expected_owner=owner_b) as owns:
        assert owns is True
    assert cj.get_job(jid)["fire_claim"]["by"] == owner_b


def test_run_claim_heartbeat_does_not_wait_on_jobs_lock(temp_home):
    from datetime import datetime, timedelta, timezone

    from cron import jobs as cj

    run_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    cj.save_jobs([{
        "id": "oneshot", "name": "o", "prompt": "x",
        "schedule": {"kind": "once", "run_at": run_at}, "next_run_at": run_at,
        "enabled": True, "state": "scheduled",
        "run_claim": {"at": datetime.now(timezone.utc).isoformat(), "by": "host:1"},
    }])
    assert cj.heartbeat_run_claim("oneshot", expected_owner="host:1") is True  # row adopted

    results, finished = _run_while_jobs_lock_held(
        cj, lambda _i: cj.heartbeat_run_claim("oneshot", expected_owner="host:1"), 4)
    assert finished == 4, f"{4 - finished}/4 run-claim heartbeats blocked on _jobs_lock"
    assert results == [True] * 4
    assert cj.heartbeat_run_claim("oneshot", expected_owner="intruder") is False


def test_json_mode_heartbeat_still_takes_the_locked_path(temp_home, monkeypatch):
    """The kill switch (no runtime_state.db) keeps the jobs.json compare-and-refresh under the
    lock: the unlocked path is sqlite-only."""
    from cron import jobs as cj

    monkeypatch.setenv("HERMES_CRON_RUNTIME_STATE", "json")
    created = cj.create_job(prompt="x", schedule="every 5m", name="json")
    assert cj.claim_job_for_fire(created["id"]) is True
    owner = cj.get_job(created["id"])["fire_claim"]["by"]
    assert not (temp_home / "cron" / "runtime_state.db").exists()
    assert cj._refresh_claim_unlocked(created["id"], "fire_claim", owner, once_only=False) is None
    assert cj.heartbeat_fire_claim(created["id"], expected_owner=owner) is True
