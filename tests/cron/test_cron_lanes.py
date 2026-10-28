"""S1 of the cron script runner spec: lanes in the scheduler core.

Spec: Obsidian ``AI/Hermes Agent/Cron Script Runner Out Of Gateway — Spec (2026-10-27).md`` v1.0,
§5.1 (lane-scoped due scan), §5.2 (locks + ``_jobs_lock`` wait probe), §5.2a (cross-process
reap exemption), §5.4 ``host_gateways()`` (S1 (k)), §7 "S1: lanes in the scheduler core".

AC -> test:
  S1 (a) / I2   test_i2_partition_total_and_disjoint, test_i2_due_scan_partition
  S1 (b) / AC-1 test_ac1_script_lane_scan_leaves_agent_records_identical
  S1 (c) / AC-2 test_ac2_default_tick_equals_main_golden
  S1 (d)        test_lane_locks_are_independent, test_quiescent_home_holds_both_tick_locks
  Prism P1 8b668b9ca91b test_missed_oneshot_notices_follow_the_scanned_lane,
                test_script_lane_skips_bot_chat_drain_and_dead_owner_reap
  Prism P0 5ad49755d8a2 test_module_imports_without_fcntl
  S1 (e)        test_tick_reports_one_outcome_per_return_path
  S1 (f) / AC-12 test_ac12_cross_process_reap_exemption
  S1 (g) / AC-14 test_ac14_knob_reads_process_home
  S1 (h)        test_no_overdue_warning_across_lanes
  S1 (i) / AC-16 test_ac16_rewound_burst_across_lanes
  S1 (j) / I1   test_i1_lane_membership_has_one_source
  S1 (k)        test_k_host_gateways, test_k_one_start_time_comparator
  S1 E2E        test_e2e_agent_and_script_callers_share_one_store
  AC-3 (S1 E2E) test_ac3_no_double_run_across_processes
  Negative      test_negative_string_true_is_agent_lane, test_negative_no_agent_without_script_pauses
  §5.2 probe    test_jobs_lock_wait_probe_records_sections_and_ticks

Run as a script, this file is the child tick caller of the two-process tests:
``python tests/cron/test_cron_lanes.py <control dir> <role> <lane>``.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

if __name__ != "__main__":
    import pytest

    pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock-based lane locks")

REPO_ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
T0 = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC).timestamp()


# --- shared fixtures ----------------------------------------------------------------------------

def _home_fixture(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.endswith("_API_KEY") or key in ("INVOCATION_ID", "HERMES_TIMEZONE"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home" / ".hermes"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HOME", str(home.parent))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    (home / "config.yaml").write_text("cron:\n  oneshot_catchup_s: 0\n", encoding="utf-8")
    script = home / "scripts" / "ok.sh"
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    script.chmod(0o755)
    return home


if __name__ != "__main__":
    @pytest.fixture
    def home(tmp_path, monkeypatch):
        yield _home_fixture(tmp_path, monkeypatch)
        import hermes_time

        hermes_time.reset_cache()

    @pytest.fixture
    def vclock(home, tmp_path, monkeypatch):
        """Virtual clock + fake agent/sink from the C13 harness, installed in this process."""
        from tests.e2e.core.delivery import _cron_clock as H
        import cron.jobs as jobs

        control = H.Control(tmp_path / "control").ensure()
        clock = H.VirtualClock(control.clock_file)
        clock.set(T0)
        monkeypatch.setattr(jobs, "record_ticker_heartbeat", lambda **_kw: None)
        H.install(clock, control, monkeypatch.setattr)
        return clock, control


def _script_job(name, schedule="every 5m"):
    from cron.jobs import create_job

    return create_job(prompt=None, schedule=schedule, name=name, script="ok.sh",
                      no_agent=True, deliver="local")


def _agent_job(name, schedule="every 5m"):
    from cron.jobs import create_job

    return create_job(prompt=f"agent {name}", schedule=schedule, name=name, deliver="local")


def _store(home) -> List[dict]:
    from cron.runtime_state import merged_jobs

    return merged_jobs(Path(home) / "cron" / "jobs.json")


def _by_name(records) -> Dict[str, dict]:
    return {r["name"]: r for r in records}


def _ledger(home, where="") -> List[dict]:
    path = Path(home) / "cron" / "executions.db"
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM executions" + (f" WHERE {where}" if where else ""))]
    finally:
        conn.close()


AC3_QUERY = ("SELECT job_id, scheduled_instant, COUNT(*) c FROM executions "
             "WHERE scheduled_instant IS NOT NULL AND started_at IS NOT NULL "
             "GROUP BY 1,2 HAVING c > 1")


def _ac3_rows(home):
    path = Path(home) / "cron" / "executions.db"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        return conn.execute(AC3_QUERY).fetchall()
    finally:
        conn.close()


def _set_due(job_id, when_iso):
    from cron.jobs import update_job

    update_job(job_id, {"next_run_at": when_iso})


def _hold_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl  # POSIX-only; a top-level import breaks Windows collection (Prism P0 5ad49755d8a2)

    fd = open(path, "w", encoding="utf-8")  # noqa: SIM115
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


# --- S1 (a) / I2 --------------------------------------------------------------------------------

# Sanitized live root store: ANG-Ventures/hermes-home d961526fcc cron/jobs.json (749 rows),
# projected to the two fields lane membership can depend on: (repr(no_agent), script present).
LIVE_ROOT_SHAPES = {("True", True): 696, ("False", False): 52, ("False", True): 1}


def _live_root_records():
    out = []
    for (no_agent, has_script), count in LIVE_ROOT_SHAPES.items():
        for i in range(count):
            rec = {"id": f"live-{no_agent}-{has_script}-{i}", "no_agent": no_agent == "True"}
            if has_script:
                rec["script"] = "s.sh"
            out.append(rec)
    return out


def _generated_records(n=200, seed=20261027):
    rng = random.Random(seed)
    values = [True, False, None, "true", "True", 1, 0, "yes", [], {}, "_absent"]
    out = []
    for i in range(n):
        rec = {"id": f"gen-{i}"}
        v = rng.choice(values)
        if v != "_absent":
            rec["no_agent"] = v
        if rng.random() < 0.7:
            rec["script"] = rng.choice(["s.sh", "", None])
        out.append(rec)
    return out


def test_i2_partition_total_and_disjoint():
    from cron.lanes import job_lane

    records = _live_root_records() + _generated_records()
    assert len(records) == 949
    script = {r["id"] for r in records if job_lane(r) == "script"}
    agent = {r["id"] for r in records if job_lane(r) == "agent"}
    assert script | agent == {r["id"] for r in records}
    assert script & agent == set()
    assert script == {r["id"] for r in records if r.get("no_agent") is True}
    assert len([r for r in _live_root_records() if job_lane(r) == "script"]) == 696


def test_i2_due_scan_partition(home, vclock):
    """Union of the two lanes' due sets is the default scan's due set; intersection is empty."""
    import shutil

    from cron.jobs import get_due_jobs, use_cron_store

    clock, _ = vclock
    for i in range(6):
        _script_job(f"s{i}")
        _agent_job(f"a{i}")
    clock.advance(301)
    results = {}
    for lane in ("all", "agent", "script"):
        copy = home.parent / f"copy-{lane}"
        shutil.copytree(home, copy)
        with use_cron_store(copy):
            due = get_due_jobs() if lane == "all" else get_due_jobs(lane=lane)
        results[lane] = {j["name"] for j in due}
    assert len(results["all"]) == 12
    assert results["agent"] | results["script"] == results["all"]
    assert results["agent"] & results["script"] == set()
    assert results["script"] == {f"s{i}" for i in range(6)}


# --- S1 (b) / AC-1 ------------------------------------------------------------------------------

def test_ac1_script_lane_scan_leaves_agent_records_identical(home, vclock):
    from cron.jobs import RESTART_REQUEUE_KEY, create_job, update_job
    from cron.scheduler import tick

    clock, control = vclock
    for i in range(3):
        _script_job(f"s{i}")
    _agent_job("a-interval")
    _agent_job("a-cron", schedule="*/5 * * * *")
    once = create_job(prompt="once", schedule="2026-03-01T00:02:00+00:00", name="a-once",
                      deliver="local")
    requeue = _agent_job("a-requeue")
    clock.advance(301)
    update_job(requeue["id"], {RESTART_REQUEUE_KEY: {"at": datetime.fromtimestamp(
        clock.now_ts(), UTC).isoformat(), "reason": "test"}})
    assert once["id"]
    before = {n: json.dumps(r, sort_keys=True) for n, r in _by_name(_store(home)).items()
              if n.startswith("a-")}
    assert tick(verbose=False, sync=True, lane="script") == 3
    after = {n: json.dumps(r, sort_keys=True) for n, r in _by_name(_store(home)).items()
             if n.startswith("a-")}
    assert after == before
    started = [r["name"] for r in (json.loads(x) for x in control.runs.read_text().splitlines())
               if r["event"] == "start"]
    assert sorted(started) == ["s0", "s1", "s2"]


# --- S1 (c) / AC-2 ------------------------------------------------------------------------------

def _golden_scenario(home, clock, control):
    """Default ``tick()`` over a mixed store; the normalized result must equal main's."""
    from cron.jobs import create_job, update_job
    from cron.scheduler import tick

    names = {}
    for i in range(3):
        names[_script_job(f"s{i}")["id"]] = f"s{i}"
        names[_agent_job(f"a{i}")["id"]] = f"a{i}"
    names[create_job(prompt="o", schedule="2026-03-01T00:03:00+00:00", name="once",
                     deliver="local")["id"]] = "once"
    paused = _agent_job("paused")
    names[paused["id"]] = "paused"
    update_job(paused["id"], {"paused_at": datetime.fromtimestamp(T0, UTC).isoformat()})
    results = []
    for _ in range(7):
        clock.advance(60)
        results.append(tick(verbose=False, sync=True))

    def norm(value):
        if isinstance(value, str):
            for jid, name in names.items():
                value = value.replace(jid, f"<{name}>")
            return value
        if isinstance(value, dict):
            return {k: norm(v) for k, v in sorted(value.items())
                    if k not in ("created_at", "by", "pid") and "execution" not in k}
        if isinstance(value, list):
            return [norm(v) for v in value]
        return value

    store = sorted((norm(r) for r in _store(home)), key=lambda r: r["name"])
    rows = sorted((norm({k: r[k] for k in ("job_id", "status", "scheduled_instant",
                                            "delivery_outcome", "source")})
                   for r in _ledger(home)), key=lambda r: json.dumps(r, sort_keys=True))
    runs = sorted((norm({k: r[k] for k in ("event", "name", "instant", "fired_at")})
                   for r in (json.loads(x) for x in control.runs.read_text().splitlines())),
                  key=lambda r: json.dumps(r, sort_keys=True))
    return {"results": results, "store": store, "ledger": rows, "runs": runs}


GOLDEN_FILE = Path(__file__).with_name("fixtures") / "cron_lanes_tick_all_golden.json"


def test_ac2_default_tick_equals_main_golden(home, vclock):
    """The golden was produced by THIS scenario on origin/main 881b7d5601 (pre-S1):
    ``CRON_LANES_GOLDEN_WRITE=1 pytest tests/cron/test_cron_lanes.py -k ac2 -s`` in a main
    worktree with this file copied in. S1 must reproduce it byte for byte."""
    clock, control = vclock
    got = json.dumps(_golden_scenario(home, clock, control), sort_keys=True, indent=1)
    out = os.environ.get("CRON_LANES_GOLDEN_WRITE")
    if out:
        print("GOLDEN-BEGIN\n" + got + "\nGOLDEN-END")
        pytest.skip("golden printed")
    assert got + "\n" == GOLDEN_FILE.read_text(encoding="utf-8")


# --- S1 (d) -------------------------------------------------------------------------------------

def test_lane_locks_are_independent(home, vclock):
    from cron.lanes import TickOutcome
    from cron.scheduler import _get_lock_paths, tick

    clock, control = vclock
    _script_job("s")
    _agent_job("a")
    clock.advance(301)
    agent_lock = _get_lock_paths("agent")[1]
    script_lock = _get_lock_paths("script")[1]
    assert agent_lock.name == ".tick.lock" and script_lock.name == ".tick-script.lock"
    assert _get_lock_paths()[1] == agent_lock
    out: list = []
    held = _hold_lock(agent_lock)
    try:
        assert tick(verbose=False, sync=True, lane="agent", report=out.append) == 0
        assert out == [TickOutcome.LOCK_BUSY]
        out.clear()
        # Prism P1 ef5bf5094f61: a .tick.lock holder (quiesce, pre-S1 code) excludes the whole
        # home from an "all" tick, as on main. "all" never narrows to the script lane.
        assert tick(verbose=False, sync=True, report=out.append) == 0
        assert out == [TickOutcome.LOCK_BUSY]
    finally:
        held.close()
    assert not control.runs.exists() or not [
        x for x in control.runs.read_text().splitlines() if json.loads(x)["event"] == "start"]
    held = _hold_lock(script_lock)
    try:
        out.clear()
        assert tick(verbose=False, sync=True, lane="script", report=out.append) == 0
        assert out == [TickOutcome.LOCK_BUSY]
        out.clear()
        # "all" with only the script lock busy (a runner mid-tick) ticks the agent lane alone.
        assert tick(verbose=False, sync=True, report=out.append) == 1
        assert out == [TickOutcome.ADMITTED]
    finally:
        held.close()
    started = [json.loads(x)["name"] for x in control.runs.read_text().splitlines()
               if json.loads(x)["event"] == "start"]
    assert started == ["a"]


def test_quiescent_home_holds_both_tick_locks(home, vclock, monkeypatch):
    """Prism P1 ef5bf5094f61: the data-wipe quiesce excludes every lane's tick, and the plan
    never deletes either lane's lock inode."""
    import hermes_cli.data_cleanup_holders as holders
    from cron.lanes import TickOutcome
    from cron.scheduler import _get_lock_paths, tick
    from hermes_cli.data_cleanup import plan_data_removal

    clock, control = vclock
    _script_job("s")
    _agent_job("a")
    clock.advance(301)
    for name in ("_refuse_backend_writers", "_refuse_multiplexer", "_drain_manual_gateway"):
        monkeypatch.setattr(holders, name, lambda _home: None)
    out: list = []
    with holders.quiescent_home(home):
        for lane in ("all", "agent", "script"):
            out.clear()
            assert tick(verbose=False, sync=True, lane=lane, report=out.append) == 0, lane
            assert out == [TickOutcome.LOCK_BUSY], lane
    assert not control.runs.exists() or not control.runs.read_text().strip()
    for lane in ("agent", "script"):
        _get_lock_paths(lane)[1].touch()
    plan = plan_data_removal(home, home.parent / "project")
    assert home / "cron" / ".tick-script.lock" not in plan.remove
    assert home / "cron" / ".tick.lock" not in plan.remove


def test_missed_oneshot_notices_follow_the_scanned_lane(home, vclock, monkeypatch):
    """Prism P1 8b668b9ca91b: a script-lane tick must not send or consume an AGENT job's queued
    MISSED notice (and vice versa); "all" delivers both."""
    import cron.jobs as jobs
    import cron.scheduler as sched

    sent: list = []
    monkeypatch.setattr(sched, "_deliver_result",
                        lambda job, _text, **_kw: sent.append(job["id"]) and None)

    def queue():
        jobs.drain_missed_oneshot_notices()
        with jobs._missed_oneshot_notices_lock:
            jobs._missed_oneshot_notices.extend([
                {"job": {"id": "agent-1", "prompt": "p"}, "text": "MISSED a"},
                {"job": {"id": "script-1", "no_agent": True, "script": "ok.sh"}, "text": "MISSED s"},
            ])

    queue()
    sched.tick(verbose=False, sync=True, lane="script")
    assert sent == ["script-1"]
    sched.tick(verbose=False, sync=True, lane="agent")
    assert sent == ["script-1", "agent-1"]
    assert jobs.drain_missed_oneshot_notices() == []
    sent.clear()
    queue()
    sched.tick(verbose=False, sync=True)
    assert sorted(sent) == ["agent-1", "script-1"]


def test_script_lane_skips_bot_chat_drain_and_dead_owner_reap(home, vclock, monkeypatch):
    """Prism P1 8b668b9ca91b: the bot-chat drain and the dead-owner reap were serialized by the
    single .tick.lock; they run only on ticks that took it (agent, all)."""
    import cron.bot_chat_delivery as bcd
    import cron.scheduler as sched

    calls: list = []
    monkeypatch.setattr(bcd, "drain", lambda *a, **k: calls.append("drain"))
    monkeypatch.setattr(bcd, "drain_in_background", lambda *a, **k: calls.append("drain_bg"))
    monkeypatch.setattr(sched, "_maybe_reap_dead_owners", lambda: calls.append("reap"))
    for lane, sync in (("script", True), ("script", False)):
        sched.tick(verbose=False, sync=sync, lane=lane)
    assert calls == []
    sched.tick(verbose=False, sync=True, lane="agent")
    assert calls == ["drain", "reap"]
    calls.clear()
    sched.tick(verbose=False, sync=False)
    assert calls == ["drain_bg", "reap"]
    calls.clear()
    held = _hold_lock(sched._get_lock_paths("script")[1])
    try:  # "all" narrowed to agent by a busy script lock still owns .tick.lock
        sched.tick(verbose=False, sync=True)
    finally:
        held.close()
    assert calls == ["drain", "reap"]


def test_module_imports_without_fcntl():
    """Prism P0 5ad49755d8a2: on Windows ``fcntl`` is absent; importing this module (pytest
    collection) must not raise before the skipif mark applies."""
    code = ("import sys, importlib.util; sys.modules['fcntl'] = None; "
            "s = importlib.util.spec_from_file_location('lanes_probe', sys.argv[1]); "
            "s.loader.exec_module(importlib.util.module_from_spec(s))")
    proc = subprocess.run([sys.executable, "-c", code, __file__], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]


# --- S1 (e) -------------------------------------------------------------------------------------

def test_tick_reports_one_outcome_per_return_path(home, vclock, monkeypatch):
    import agent.estop as estop
    import cron.scheduler as sched
    from cron.lanes import TickOutcome
    from hermes_cli.backend_retirement import retirement

    def run(lane="script", **kw):
        out: list = []
        sched.tick(verbose=False, sync=True, lane=lane, report=out.append, **kw)
        return out

    assert run() == [TickOutcome.ADMITTED]

    @contextlib.contextmanager
    def refused():
        yield False

    with monkeypatch.context() as m:
        m.setattr(retirement, "work", refused)
        assert run() == [TickOutcome.REFUSED_RETIREMENT]
    held = _hold_lock(sched._get_lock_paths("script")[1])
    try:
        assert run() == [TickOutcome.LOCK_BUSY]
    finally:
        held.close()
    with monkeypatch.context() as m:
        m.setattr(estop, "check_paused", lambda *_a, **_k: True)
        assert run() == [TickOutcome.ESTOP]
    assert run(can_dispatch=lambda: False) == [TickOutcome.DISPATCH_PAUSED]
    with monkeypatch.context() as m:
        m.setattr(sched, "is_shutting_down", lambda: True)
        assert run() == [TickOutcome.SHUTTING_DOWN]

    class Hold:
        def __call__(self):
            return True

        def admit(self):
            return None

    assert run(can_dispatch=Hold()) == [TickOutcome.ADMISSION_HOLD]
    assert run(lane="all") == [TickOutcome.ADMITTED]
    # report is optional and the int return is unchanged
    assert sched.tick(verbose=False, sync=True, lane="agent") == 0


# --- S1 (f) / AC-12 -----------------------------------------------------------------------------

def _insert_row(home, *, pid, pid_start, age_s, process_id=None):
    from cron.executions import _transaction

    now = datetime.now(UTC)
    rid = uuid.uuid4().hex
    with _transaction() as conn:
        conn.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, process_started_at, "
            "status, claimed_at, started_at) VALUES (?, 'j', 'builtin', ?, ?, ?, 'running', ?, ?)",
            (rid, process_id or uuid.uuid4().hex, pid, pid_start,
             (now - timedelta(seconds=age_s)).isoformat(),
             (now - timedelta(seconds=age_s)).isoformat()))
    return rid


def _status(home, rid):
    return {r["id"]: r for r in _ledger(home)}[rid]


@pytest.fixture if __name__ != "__main__" else (lambda f: f)
def live_proc():
    procs = []

    def spawn():
        p = subprocess.Popen(["sleep", "120"])
        procs.append(p)
        from gateway.status import get_process_start_time

        for _ in range(100):
            st = get_process_start_time(p.pid)
            if st is not None:
                return p.pid, st
            time.sleep(0.01)
        raise AssertionError("no start time for spawned process")

    yield spawn
    for p in procs:
        p.kill()
        p.wait(timeout=10)


def _now_iso(offset_s=0.0):
    return (datetime.now(UTC) + timedelta(seconds=offset_s)).isoformat()


def test_ac12_cross_process_reap_exemption(home, live_proc):
    from cron.executions import _OWNER_WEDGED_REASON, recover_interrupted_executions
    from cron.lanes import write_runner_heartbeat

    runner_pid, runner_start = live_proc()
    gw_pid, gw_start = live_proc()

    def hb(**fields):
        payload = {"at": _now_iso(), "pid": runner_pid, "pid_start": runner_start,
                   "state": "active", "last_ok_at": _now_iso(-5)}
        payload.update(fields)
        assert write_runner_heartbeat(home, payload)

    cases = {}

    def case(name, heartbeat, *, pid=runner_pid, pid_start=runner_start):
        hb(**heartbeat)
        rid = _insert_row(home, pid=pid, pid_start=pid_start, age_s=7300)
        recover_interrupted_executions()
        cases[name] = _status(home, rid)

    case("i_active_fresh", {})
    case("ii_active_stale", {"last_ok_at": _now_iso(-181)})
    case("iii_draining_in_bound", {"state": "draining", "draining_since": _now_iso(-3600)})
    case("iv_draining_past_bound", {"state": "draining", "draining_since": _now_iso(-3700)})
    case("v_pid_start_mismatch", {"pid_start": runner_start + 100000})
    case("vi_gateway_owner", {}, pid=gw_pid, pid_start=gw_start)
    statuses = {k: v["status"] for k, v in cases.items()}
    assert statuses == {
        "i_active_fresh": "running", "ii_active_stale": "unknown",
        "iii_draining_in_bound": "running", "iv_draining_past_bound": "unknown",
        "v_pid_start_mismatch": "unknown", "vi_gateway_owner": "unknown"}, statuses
    for k in ("ii_active_stale", "iv_draining_past_bound", "v_pid_start_mismatch",
              "vi_gateway_owner"):
        assert cases[k]["error"] == _OWNER_WEDGED_REASON


def test_lane_peer_read_failure_is_false(home, live_proc, monkeypatch):
    from cron import lanes

    pid, st = live_proc()
    (home / "cron" / lanes.HEARTBEAT_NAME).write_text("{not json", encoding="utf-8")
    assert lanes.lane_peer(pid, st, home) is False
    assert lanes.lane_peer(pid, st, home / "missing") is False
    assert lanes.read_runner_heartbeat(home) is None


# --- S1 (g) / AC-14 -----------------------------------------------------------------------------

def test_ac14_knob_reads_process_home(home, tmp_path):
    from cron.lanes import knob_enabled
    from cron.scheduler_provider import _profile_cron_scope

    profile = home / "profiles" / "p1"
    (profile / "cron").mkdir(parents=True)

    def write(path, value):
        path.write_text(f"cron:\n  script_runner:\n    enabled: {value}\n", encoding="utf-8")

    write(home / "config.yaml", "true")
    write(profile / "config.yaml", "false")
    assert knob_enabled() is True
    with _profile_cron_scope(profile):
        assert knob_enabled() is True
    write(home / "config.yaml", "false")
    write(profile / "config.yaml", "true")
    with _profile_cron_scope(profile):
        assert knob_enabled() is False
    assert knob_enabled() is False
    # A YAML string "true", a number or null never enable. (PyYAML parses YAML 1.1 ``yes``/``on``
    # to the boolean True, so those enable like ``true``; spec ambiguity flagged on the card.)
    for literal in ('"true"', "'true'", "1", "~", "enabled"):
        write(home / "config.yaml", literal)
        assert knob_enabled() is False, literal
    write(home / "config.yaml", "true")
    assert knob_enabled() is True
    (home / "config.yaml").write_text("cron: {}\n", encoding="utf-8")
    assert knob_enabled() is False
    (home / "config.yaml").unlink()
    assert knob_enabled() is False


# --- S1 (h) -------------------------------------------------------------------------------------

def test_no_overdue_warning_across_lanes(home, vclock, caplog):
    import logging

    from cron.jobs import create_job, get_due_jobs, update_job

    clock, _ = vclock
    once = create_job(prompt="o", schedule="2026-03-01T00:02:00+00:00", name="agent-once",
                      deliver="local")
    update_job(once["id"], {"enabled": False})  # left out of every scan: the warning's case
    clock.advance(3600)
    with caplog.at_level(logging.WARNING, logger="cron.jobs"):
        get_due_jobs(lane="script")
    assert "cron.oneshot.overdue_not_due" not in caplog.text
    with caplog.at_level(logging.WARNING, logger="cron.jobs"):
        get_due_jobs(lane="agent")
    assert "cron.oneshot.overdue_not_due" in caplog.text


# --- S1 (i) / AC-16 -----------------------------------------------------------------------------

def test_ac16_rewound_burst_across_lanes(home, vclock):
    from cron.executions import _transaction
    from cron.jobs import get_due_jobs
    from cron.lanes import REWOUND_LANES_NAME
    from cron.occurrences import REWOUND_BURST_MARKER

    clock, _ = vclock
    ids = [_script_job(f"s{i}")["id"] for i in range(15)]
    ids += [_agent_job(f"a{i}")["id"] for i in range(15)]
    stored = {r["id"]: r["next_run_at"] for r in _store(home)}
    with _transaction() as conn:
        for jid in ids:
            later = datetime.fromisoformat(stored[jid]) + timedelta(hours=1)
            conn.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at,"
                " started_at, finished_at, scheduled_instant) VALUES (?, ?, 'builtin', 'x', 1,"
                " 'completed', ?, ?, ?, ?)",
                (uuid.uuid4().hex, jid, later.isoformat(), later.isoformat(),
                 (later + timedelta(seconds=1)).isoformat(),
                 later.astimezone(UTC).isoformat()))
    marker = home / "cron" / REWOUND_BURST_MARKER
    get_due_jobs(lane="script")
    assert not marker.exists()  # 15 < threshold on its own
    clock.advance(30)
    get_due_jobs(lane="agent")
    body = json.loads(marker.read_text(encoding="utf-8"))
    assert body["count"] == 30
    lanes_file = json.loads((home / "cron" / REWOUND_LANES_NAME).read_text(encoding="utf-8"))
    assert sorted(lanes_file) == ["agent", "script"]
    assert len(lanes_file["script"]["ids"]) == 15


def test_rewound_entries_older_than_a_cycle_do_not_count(home, vclock):
    from cron.lanes import RUNNER_CYCLE_S, merge_rewound_lanes

    clock, _ = vclock
    cron_dir = home / "cron"
    now = datetime.fromtimestamp(clock.now_ts(), UTC)
    assert merge_rewound_lanes(cron_dir, "script", [f"s{i}" for i in range(15)], now) == [
        f"s{i}" for i in range(15)]
    later = now + timedelta(seconds=RUNNER_CYCLE_S + 1)
    assert merge_rewound_lanes(cron_dir, "agent", ["a1"], later) == ["a1"]


# --- S1 (j) / I1 --------------------------------------------------------------------------------

def _lane_decision_violations(root: Path):
    """``no_agent`` reads inside lane-scoped code (a function that names a ``lane``) or anywhere in
    ``cron/scheduler_tick.py``, outside ``cron/lanes.py``."""
    out = []
    for sub in ("cron", "gateway", "hermes_cli"):
        for path in sorted((root / sub).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel == "cron/lanes.py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            scopes = [tree] if rel == "cron/scheduler_tick.py" else [
                n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any((isinstance(x, ast.Name) and x.id == "lane")
                        or (isinstance(x, ast.arg) and x.arg == "lane") for x in ast.walk(n))]
            for scope in scopes:
                for node in ast.walk(scope):
                    if isinstance(node, ast.Constant) and node.value == "no_agent":
                        out.append(f"{rel}:{node.lineno}")
    return sorted(set(out))


def test_i1_lane_membership_has_one_source():
    assert _lane_decision_violations(REPO_ROOT) == []


def test_i1_lint_catches_an_inlined_read(tmp_path):
    import shutil

    for sub in ("cron", "gateway", "hermes_cli"):
        (tmp_path / sub).mkdir()
    shutil.copy(REPO_ROOT / "cron" / "scheduler_tick.py", tmp_path / "cron" / "scheduler_tick.py")
    src = (tmp_path / "cron" / "scheduler_tick.py").read_text(encoding="utf-8")
    (tmp_path / "cron" / "scheduler_tick.py").write_text(
        src + "\n\ndef _inlined(job):\n    return job.get(\"no_agent\") is True\n", encoding="utf-8")
    assert _lane_decision_violations(tmp_path) != []


# --- S1 (k): host_gateways ----------------------------------------------------------------------

def _gateway_record(path: Path, *, pid, start_time, updated_at, code_sha="a" * 40):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "start_time": start_time, "updated_at": updated_at,
                                "code_sha": code_sha}), encoding="utf-8")


def test_k_host_gateways(tmp_path, live_proc, monkeypatch):
    import gateway.status as status
    from cron import lanes

    root = tmp_path / ".hermes"
    root_pid, root_st = live_proc()
    live_pid, live_st = live_proc()
    step_pid, step_st = live_proc()
    unread_pid, unread_st = live_proc()
    stale_pid, stale_st = live_proc()
    dead = subprocess.Popen(["true"])
    dead.wait(timeout=10)
    fresh, old = _now_iso(), _now_iso(-600)
    _gateway_record(root / "gateway_state.json", pid=root_pid, start_time=root_st,
                    updated_at=fresh)
    prof = root / "profiles"
    _gateway_record(prof / "live" / "gateway_state.json", pid=live_pid, start_time=live_st,
                    updated_at=fresh)
    _gateway_record(prof / "dead" / "gateway_state.json", pid=dead.pid, start_time=12345,
                    updated_at=fresh)
    _gateway_record(prof / "stale" / "gateway_state.json", pid=stale_pid,
                    start_time=stale_st + 360000, updated_at=old)
    _gateway_record(prof / "clockstep" / "gateway_state.json", pid=step_pid,
                    start_time=step_st + 360000, updated_at=fresh)
    _gateway_record(prof / "unreadable" / "gateway_state.json", pid=unread_pid,
                    start_time=unread_st, updated_at=old)
    real = status.get_process_start_time
    monkeypatch.setattr(status, "get_process_start_time",
                        lambda pid: None if pid == unread_pid else real(pid))
    entries = lanes.host_gateways(root)
    assert sorted(e.home.name for e in entries) == sorted(
        [".hermes", "live", "clockstep", "unreadable"])
    by_name = {e.home.name: e for e in entries}
    assert by_name["live"].pid == live_pid and by_name["live"].code_sha == "a" * 40
    assert by_name["unreadable"].record_stale is True
    assert by_name["clockstep"].record_stale is False


def test_k_one_start_time_comparator():
    """D14: every start-time comparison in cron/lanes.py goes through
    ``start_time_fingerprints_match``; a hand-rolled ``==``/``!=`` on a start time is refused."""
    src = (REPO_ROOT / "cron" / "lanes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)) for op in node.ops):
            text = ast.get_source_segment(src, node) or ""
            if "start" in text:
                bad.append(f"lanes.py:{node.lineno}: {text}")
    assert bad == []
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for name in ("runner_pid_alive", "host_gateways"):
        calls = {getattr(c.func, "attr", getattr(c.func, "id", None))
                 for c in ast.walk(funcs[name]) if isinstance(c, ast.Call)}
        assert "start_time_fingerprints_match" in calls or "_start_matches" in calls, name


def test_runner_pid_alive_fails_toward_false(live_proc, monkeypatch):
    import gateway.status as status
    from cron.lanes import runner_pid_alive

    pid, st = live_proc()
    assert runner_pid_alive(pid, st) is True
    assert runner_pid_alive(pid, st + 100000) is False
    assert runner_pid_alive(pid, None) is False
    assert runner_pid_alive("x", st) is False
    monkeypatch.setattr(status, "get_process_start_time", lambda _pid: None)
    assert runner_pid_alive(pid, st) is False


def test_code_contains_s1(tmp_path):
    from cron import lanes

    repo = tmp_path / "tree"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "old")
    old = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "s1")
    s1 = git("rev-parse", "HEAD")
    assert lanes.code_contains_s1(s1, tree=repo, s1_sha=s1) is True
    assert lanes.code_contains_s1(old, tree=repo, s1_sha=s1) is False
    assert lanes.code_contains_s1(None, tree=repo, s1_sha=s1) is False
    assert lanes.code_contains_s1("abc", tree=repo, s1_sha=s1) is False
    assert lanes.code_contains_s1("f" * 40, tree=repo, s1_sha=s1) is False
    assert lanes.code_contains_s1(s1, tree=repo, s1_sha=None) is False


# --- negatives ----------------------------------------------------------------------------------

def test_negative_string_true_is_agent_lane(home, vclock):
    from cron.jobs import get_due_jobs, load_jobs, save_jobs
    from cron.lanes import job_lane

    clock, _ = vclock
    job = _agent_job("stringy")
    raw = load_jobs()
    for rj in raw:
        if rj["id"] == job["id"]:
            rj["no_agent"] = "true"
    save_jobs(raw)
    assert job_lane({"no_agent": "true"}) == "agent"
    clock.advance(301)
    assert [j["name"] for j in get_due_jobs(lane="agent")] == ["stringy"]
    assert get_due_jobs(lane="script") == []


def test_negative_no_agent_without_script_pauses(home, monkeypatch):
    """A hand-injected ``no_agent: true`` with no script lands in the script lane and is paused by
    the legacy path (``_run_no_agent_job`` → ``_block_and_pause_job``)."""
    from cron.jobs import load_jobs, save_jobs
    from cron.scheduler import tick

    job = _agent_job("legacy")
    raw = load_jobs()
    for rj in raw:
        if rj["id"] == job["id"]:
            rj["no_agent"] = True
            rj.pop("script", None)
            rj["prompt"] = ""
    save_jobs(raw)
    _set_due(job["id"], _now_iso(-30))
    assert tick(verbose=False, sync=True, lane="script") == 1
    rec = _by_name(_store(home))["legacy"]
    assert rec["enabled"] is False
    assert rec.get("paused_reason")


# --- §5.2 probe ---------------------------------------------------------------------------------

def test_jobs_lock_wait_probe_records_sections_and_ticks(home, vclock):
    from cron import lanes
    from cron.jobs import _jobs_lock, get_due_jobs
    from cron.scheduler import tick

    lanes.reset_lock_waits()
    with _jobs_lock():
        with _jobs_lock():  # nested: one acquisition
            pass
    get_due_jobs(lane="script")
    tick(verbose=False, sync=True, lane="script")
    waits = lanes.lock_waits()
    labels = [label for label, _ in waits]
    assert labels[0] == "test_jobs_lock_wait_probe_records_sections_and_ticks"
    assert "get_due_jobs" in labels
    assert "tick:script" in labels
    assert all(isinstance(w, float) and w >= 0 for _, w in waits)
    assert lanes._LOCK_WAITS.maxlen == 10_000


# --- two-process tick callers -------------------------------------------------------------------

class TickChild:
    """``tick(lane=...)`` in its own OS process, stepped one call per ``release()``."""

    def __init__(self, ctl: Path, role: str, lane: str, env):
        self.dir = ctl / role
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ctl, self.role, self.lane, self.env = ctl, role, lane, env
        self.go = 0
        log = open(self.dir / "stderr.log", "a", encoding="utf-8")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(ctl), role, lane],
            cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        log.close()
        self._wait(0, 120.0)

    def _idle(self):
        try:
            return int((self.dir / "idle").read_text(encoding="utf-8") or -1)
        except (OSError, ValueError):
            return -1

    def _wait(self, n, timeout=60.0):
        deadline = time.monotonic() + timeout
        while self._idle() < n:
            if self.proc.poll() is not None:
                tail = (self.dir / "stderr.log").read_text(encoding="utf-8")[-4000:]
                raise AssertionError(f"{self.role} exited rc={self.proc.returncode}:\n{tail}")
            if time.monotonic() >= deadline:
                raise AssertionError(f"{self.role} step {n} timed out")
            time.sleep(0.005)

    def release(self, arm=False):
        self.go += 1
        if arm:
            (self.dir / f"arm-{self.go}").touch()
        tmp = self.dir / ".go.tmp"
        tmp.write_text(str(self.go), encoding="utf-8")
        os.replace(tmp, self.dir / "go")

    def wait(self):
        self._wait(self.go)

    def results(self):
        p = self.dir / "results.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def stop(self):
        (self.dir / "stop").touch()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


def _child_env():
    from tests.e2e.environ_snapshot import environ_snapshot

    env = {k: v for k, v in environ_snapshot().items() if not k.startswith("PYTEST_")}
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _expected_interval_fires(minutes, steps, step_s=60):
    period = minutes * 60
    return {T0 + period * k for k in range(1, steps * step_s // period + 1)}


def _started_by_job(home):
    out: Dict[str, List[float]] = {}
    for r in _ledger(home, "started_at IS NOT NULL"):
        out.setdefault(r["job_id"], []).append(
            datetime.fromisoformat(r["scheduled_instant"]).timestamp()
            if r["scheduled_instant"] else None)
    return out


if __name__ != "__main__":
    @pytest.mark.slow_test(240, reason="two OS processes tick 120 virtual minutes")
    def test_e2e_agent_and_script_callers_share_one_store(home, vclock, tmp_path):
        clock, control = vclock
        periods = {}
        for i in range(50):
            m = (5, 10, 15, 30)[i % 4]
            periods[_script_job(f"s{i:02d}", schedule=f"every {m}m")["id"]] = m
        for i in range(5):
            periods[_agent_job(f"a{i}", schedule="every 15m")["id"]] = 15
        ctl = control.root
        env = _child_env()
        agent = TickChild(ctl, "agent", "agent", env)
        script = TickChild(ctl, "script", "script", env)
        try:
            for _ in range(120):
                clock.advance(60)
                agent.release()
                script.release()
                agent.wait()
                script.wait()
        finally:
            agent.stop()
            script.stop()
        started = _started_by_job(home)
        for jid, minutes in periods.items():
            assert sorted(started.get(jid, [])) == sorted(_expected_interval_fires(minutes, 120)), jid
        assert _ac3_rows(home) == []
        runs = [json.loads(x) for x in control.runs.read_text().splitlines()
                if json.loads(x)["event"] == "start"]
        by_pid = {r["name"][0]: set() for r in runs}
        for r in runs:
            by_pid[r["name"][0]].add(r["pid"])
        assert by_pid == {"s": {script.proc.pid}, "a": {agent.proc.pid}}
        assert {o["outcome"] for o in agent.results() + script.results()} == {"ADMITTED"}

    @pytest.mark.slow_test(300, reason="two OS processes tick 120 virtual minutes with barriers")
    def test_ac3_no_double_run_across_processes(home, vclock):
        clock, control = vclock
        j1 = _script_job("j1", schedule="every 5m")["id"]
        periods = {j1: 5}
        for i in range(8):
            periods[_script_job(f"s{i}", schedule=f"every {(5, 10)[i % 2]}m")["id"]] = (5, 10)[i % 2]
        agent_ids = [_agent_job(f"a{i}")["id"] for i in range(2)]
        ctl = control.root
        (ctl / "j1").write_text(j1, encoding="utf-8")
        env = _child_env()
        a = TickChild(ctl, "A", "script", env)
        b = TickChild(ctl, "B", "script", env)
        first_due_step = 5  # J1 is created at T0, every 5m
        try:
            for step in range(1, 121):
                clock.advance(60)
                arm = step == first_due_step
                a.release(arm=arm)
                b.release(arm=arm)
                a.wait()
                b.wait()
        finally:
            a.stop()
            b.stop()
        barriers = [json.loads(x) for x in (ctl / "barriers.jsonl").read_text().splitlines()]
        print("AC-3 barriers:", barriers)
        assert _ac3_rows(home) == []
        assert any(r["result"] == "timeout" for r in barriers), barriers
        started = _started_by_job(home)
        for jid, minutes in periods.items():
            assert sorted(started.get(jid, [])) == sorted(_expected_interval_fires(minutes, 120)), jid
        assert not any(started.get(jid) for jid in agent_ids)
        outcomes = {o["outcome"] for o in a.results() + b.results()}
        assert "LOCK_BUSY" in outcomes or len(barriers) >= 1


# --- child entry --------------------------------------------------------------------------------

def _child_main(ctl: Path, role: str, lane: str) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from tests.e2e.core.delivery import _cron_clock as H

    control = H.Control(ctl)
    H.install(H.VirtualClock(control.clock_file), control)
    import cron.jobs as jobs
    import cron.scheduler as sched

    jobs.record_ticker_heartbeat = lambda **_kw: None
    me = ctl / role
    j1_file = ctl / "j1"
    j1 = j1_file.read_text(encoding="utf-8") if j1_file.exists() else None
    real_due = sched.get_due_jobs
    state = {"step": 0}

    def _barrier(n, result):
        with open(ctl / "barriers.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"barrier": n, "role": role, "step": state["step"],
                                 "result": result}) + "\n")

    def _wait_for(pred, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return "released"
            time.sleep(0.01)
        return "timeout"

    def _a_completed():
        rows = [r for r in _ledger(os.environ["HERMES_HOME"],
                                   f"job_id='{j1}' AND status='completed'")
                if r["pid"] != os.getpid()]
        return bool(rows)

    def due(*args, **kwargs):
        arm = me / f"arm-{state['step']}"
        if role == "A" and arm.exists():
            arm.unlink()
            _barrier(2, _wait_for(lambda: (ctl / "b1_entered").exists(), 1.0))
        result = real_due(*args, **kwargs)
        if role == "B" and arm.exists() and any(j["id"] == j1 for j in result):
            arm.unlink()
            (ctl / "b1_entered").touch()
            _barrier(1, _wait_for(_a_completed, 3.0))
        return result

    if j1 is not None:
        sched.get_due_jobs = due
    (me / "idle").write_text("0", encoding="utf-8")
    n = 0
    while not (me / "stop").exists():
        try:
            go = int((me / "go").read_text(encoding="utf-8") or 0)
        except (OSError, ValueError):
            go = 0
        if go <= n:
            time.sleep(0.003)
            continue
        n += 1
        state["step"] = n
        out: list = []
        result = sched.tick(verbose=False, sync=True, lane=lane, report=out.append)
        with open(me / "results.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"step": n, "result": result,
                                 "outcome": out[0].name if out else None}) + "\n")
        tmp = me / ".idle.tmp"
        tmp.write_text(str(n), encoding="utf-8")
        os.replace(tmp, me / "idle")


if __name__ == "__main__":
    _child_main(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
