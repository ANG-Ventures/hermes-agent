"""t_45a4bee2: the coalesced jobs.json mirror flush is single-flight per process and
double-checked under the global jobs lock.

Measured on a live gateway: once the 30 s window elapsed, every thread exiting a ``_jobs_lock``
section passed the unlocked "flush due?" test and queued on the lock; each then re-loaded and
re-parsed the whole jobs.json under the lock just to find nothing to write (15-19 waiters per
dump, cron 57 % of the GIL). Real store under a temp HERMES_HOME; only call counters and a
slow save are wrapped.
"""

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
N_THREADS = 12


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CRON_RUNTIME_STATE", raising=False)
    monkeypatch.delenv("HERMES_CRON_MIRROR_INTERVAL_S", raising=False)
    (tmp_path / "cron").mkdir()
    return tmp_path


@pytest.fixture
def clock(monkeypatch):
    from cron import runtime_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs, "_now", lambda: now[0])
    return now


@pytest.fixture(autouse=True)
def _clean_running_state():
    import cron.scheduler as sched

    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()
    yield
    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()


def _dirty(cron_dir):
    from cron import runtime_state as rs

    with rs.connect(cron_dir, create=False) as conn:
        return rs.dirty_since(conn)


def _claimed_job_with_clean_mirror(cj):
    created = cj.create_job(prompt="x", schedule="every 5m", name="herd")
    assert cj.claim_job_for_fire(created["id"]) is True
    owner = cj.get_job(created["id"])["fire_claim"]["by"]
    assert cj.maybe_flush_runtime_mirror(force=True) is True  # start from a clean mirror
    return created["id"], owner


def _instrument(monkeypatch, cj, *, on_mirror_save=None, save_delay=0.3):
    """Count load_jobs/_parse_jobs_file/mirror saves; the mirror save is slow so racers pile up."""
    counts = {"load": 0, "parse": 0, "save": 0}
    guard = threading.Lock()
    real_load, real_parse, real_save = cj.load_jobs, cj._parse_jobs_file, cj._save_jobs_unlocked

    def load(*a, **kw):
        with guard:
            counts["load"] += 1
        return real_load(*a, **kw)

    def parse(*a, **kw):
        with guard:
            counts["parse"] += 1
        return real_parse(*a, **kw)

    def save(*a, **kw):
        if kw.get("mirror_only"):
            with guard:
                counts["save"] += 1
                first = counts["save"] == 1
            time.sleep(save_delay)
            if first and on_mirror_save is not None:
                on_mirror_save()
        return real_save(*a, **kw)

    monkeypatch.setattr(cj, "load_jobs", load)
    monkeypatch.setattr(cj, "_parse_jobs_file", parse)
    monkeypatch.setattr(cj, "_save_jobs_unlocked", save)
    return counts


def _race(fn):
    barrier = threading.Barrier(N_THREADS)
    errors = []

    def run():
        try:
            barrier.wait(timeout=10)
            fn()
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert not any(t.is_alive() for t in threads)


def test_due_flush_herd_coalesces_to_one_write_and_no_reread_chain(temp_home, clock, monkeypatch):
    """12 threads exit sections right after the window elapses with one dirty write: exactly one
    jobs.json write and a constant number of full loads/parses, not one per waiter."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    jid, owner = _claimed_job_with_clean_mirror(cj)
    clock[0] += 1
    assert cj.heartbeat_fire_claim(jid, expected_owner=owner) is True
    clock[0] += 31  # window elapsed
    writes = rs.MIRROR_WRITES
    counts = _instrument(monkeypatch, cj)

    _race(cj._mirror_after_section)

    assert counts["save"] == 1 and rs.MIRROR_WRITES == writes + 1
    assert counts["load"] <= 2, counts
    assert counts["parse"] <= 4, counts
    assert _dirty(temp_home / "cron") is None


def test_write_after_inflight_seq_read_stays_dirty_and_flushes_next_window(temp_home, clock,
                                                                          monkeypatch):
    """A row write that lands while the single in-flight flush is writing the file (after its seq
    read) is not lost: the race still writes once, the late write stays dirty, and the next due
    call publishes it."""
    from cron import jobs as cj
    from cron import runtime_state as rs

    jid, owner = _claimed_job_with_clean_mirror(cj)
    clock[0] += 1
    assert cj.heartbeat_fire_claim(jid, expected_owner=owner) is True
    clock[0] += 31

    def late_write():
        clock[0] += 1
        assert cj.heartbeat_fire_claim(jid, expected_owner=owner) is True

    writes = rs.MIRROR_WRITES
    counts = _instrument(monkeypatch, cj, on_mirror_save=late_write)

    _race(cj._mirror_after_section)

    assert counts["save"] == 1 and rs.MIRROR_WRITES == writes + 1, counts
    assert _dirty(temp_home / "cron") is not None  # the late write is still owed
    live = cj.get_job(jid)["fire_claim"]
    raw = next(j for j in json.loads((temp_home / "cron" / "jobs.json").read_text())["jobs"]
               if j["id"] == jid)
    assert raw.get("fire_claim") != live

    clock[0] += 31
    assert cj.maybe_flush_runtime_mirror() is True
    assert _dirty(temp_home / "cron") is None
    raw = next(j for j in json.loads((temp_home / "cron" / "jobs.json").read_text())["jobs"]
               if j["id"] == jid)
    assert raw.get("fire_claim") == live


def test_sibling_process_flushed_while_we_waited_skips_load_under_lock(temp_home, clock,
                                                                      monkeypatch):
    """A real second process publishes the mirror after our unlocked due test passed but before
    our locked section starts: the locked section sees clean rows and a known file and returns
    without load_jobs/_parse_jobs_file of the whole jobs.json."""
    from cron import jobs as cj

    jid, owner = _claimed_job_with_clean_mirror(cj)
    clock[0] += 1
    assert cj.heartbeat_fire_claim(jid, expected_owner=owner) is True
    clock[0] += 31
    assert _dirty(temp_home / "cron") is not None

    real_lock = cj._jobs_lock
    state = {"sibling_rc": None, "load_under_lock": 0, "parse_under_lock": 0, "in_lock": False}

    @contextlib.contextmanager
    def lock_after_sibling(*a, **kw):
        if state["sibling_rc"] is None:
            env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
            proc = subprocess.run(
                [sys.executable, "-c",
                 "from cron import jobs; jobs.maybe_flush_runtime_mirror(force=True)"],
                cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120)
            state["sibling_rc"] = proc.returncode
            state["sibling_err"] = proc.stderr[-2000:]
        with real_lock(*a, **kw):
            state["in_lock"] = True
            try:
                yield
            finally:
                state["in_lock"] = False

    real_load, real_parse = cj.load_jobs, cj._parse_jobs_file

    def load(*a, **kw):
        if state["in_lock"]:
            state["load_under_lock"] += 1
        return real_load(*a, **kw)

    def parse(*a, **kw):
        if state["in_lock"]:
            state["parse_under_lock"] += 1
        return real_parse(*a, **kw)

    monkeypatch.setattr(cj, "_jobs_lock", lock_after_sibling)
    monkeypatch.setattr(cj, "load_jobs", load)
    monkeypatch.setattr(cj, "_parse_jobs_file", parse)

    assert cj.maybe_flush_runtime_mirror() is False
    assert state["sibling_rc"] == 0, state.get("sibling_err")
    assert _dirty(temp_home / "cron") is None  # the sibling really published
    assert state["load_under_lock"] == 0 and state["parse_under_lock"] == 0, state
