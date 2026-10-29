"""S2 of the cron script runner spec: the runner process.

Spec: Obsidian ``AI/Hermes Agent/Cron Script Runner Out Of Gateway — Spec (2026-10-27).md`` v1.0,
§5.4 (the runner process), §5.5 (tick watchdog, one strike, code skew, drain), §7 "S2: the runner".
Depends on S1 (hermes-agent#2396, merge 410f6d65af5a).

AC -> test:
  S2 (a)         test_a_runner_may_tick_truth_table
  S2 (b)         test_b_last_ok_at_only_on_admitted_tick, test_b_deleted_home_gets_no_heartbeat
  S2 (c)         test_c_sigterm_terminates_script_requeues_and_exits_0
  S2 (d)         test_d_code_skew_drains_then_exits_0
  S2 (e) AC-15   test_e_ac15_config_refusals_exit_78
  S2 (f) AC-4d   test_f_ac4d_host_precondition, test_f_ac4d_precondition_cases
  S2 (g) AC-4e   test_g_ac4e_watchdog_tree_kill_one_strike,
                 test_g_watchdog_snapshot_survives_a_held_procs_lock
  S2 (h)         test_h_runner_writes_no_ticker_markers
  S2 (i)         test_i_rearm_and_knob_toggle_clear_the_trip
  AC-4c          test_ac4c_profile_holder_on_s1_that_does_not_delegate
  E2E            test_e2e_runner_fires_echo_job
  S1_MERGE_SHA   test_s1_merge_sha_is_the_merged_s1
  move           test_cron_tick_profile_homes_moved_verbatim
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock / process groups")

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).resolve().parent / "_runner_harness.py"
UTC = timezone.utc
S1_SHA = "410f6d65af5af9045ee1c0d24c56090cf715d4ff"


# --- fixtures -----------------------------------------------------------------------------------

def _scrub_env(monkeypatch):
    for key in list(os.environ):
        if key.endswith("_API_KEY") or key in (
                "INVOCATION_ID", "HERMES_TIMEZONE", "HERMES_MACHINE_ID",
                "_HERMES_CRON_EXTERNAL_WORKER"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A scratch host root (``<tmp>/home/.hermes``) with the knob on and no gateway records."""
    _scrub_env(monkeypatch)
    home = tmp_path / "home" / ".hermes"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HOME", str(home.parent))
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_config(home, knob=True)
    from cron import lanes

    lanes._knob_cache.clear()
    yield home
    lanes._knob_cache.clear()


def _write_config(home: Path, *, knob):
    text = "cron:\n  oneshot_catchup_s: 0\n"
    if knob is not None:
        text += f"  script_runner:\n    enabled: {knob}\n"
    (home / "config.yaml").write_text(text, encoding="utf-8")
    # A new (mtime, size) signature: never served from the knob cache.
    st = (home / "config.yaml").stat()
    os.utime(home / "config.yaml", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def _make_profile(root_home: Path, name: str) -> Path:
    prof = root_home / "profiles" / name
    (prof / "cron").mkdir(parents=True)
    (prof / "scripts").mkdir()
    (prof / "config.yaml").write_text("cron:\n  oneshot_catchup_s: 0\n", encoding="utf-8")
    return prof


def _script(home: Path, name: str, body: str) -> str:
    path = home / "scripts" / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return name


def _job(home: Path, name: str, script: str, *, schedule="every 1m", due_now=True):
    from cron.jobs import create_job, update_job
    from cron.scheduler_provider import _profile_cron_scope

    with _profile_cron_scope(home):
        job = create_job(prompt=None, schedule=schedule, name=name, script=script,
                         no_agent=True, deliver="local")
        if due_now:
            update_job(job["id"], {"next_run_at": (datetime.now(UTC) - timedelta(seconds=5)).isoformat()})
    return job


def _rows(home: Path, where="") -> list:
    path = Path(home) / "cron" / "executions.db"
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM executions" + (f" WHERE {where}" if where else ""))]
        except sqlite3.OperationalError as exc:
            # The runner creates the file before the table; that window reads as "no rows yet".
            if "no such table" in str(exc):
                return []
            raise
    finally:
        conn.close()


def _hb(home: Path):
    from cron.lanes import read_runner_heartbeat

    return read_runner_heartbeat(home)


def _now_iso(offset_s=0.0):
    return (datetime.now(UTC) + timedelta(seconds=offset_s)).isoformat()


def _wait_for(pred, timeout, step=0.05, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture
def procs():
    """Processes a test starts; each is killed with its whole group in teardown."""
    started: list = []
    yield started
    for p in started:
        # Only a child we have not reaped: a reaped pid may already name an unrelated process.
        if p.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)  # windows-footgun: ok — skipped on win32
            with contextlib.suppress(Exception):
                p.kill()
        with contextlib.suppress(Exception):
            p.wait(timeout=10)


def _fake_gateway(procs, tmp_path, profile: str):
    """A live process whose command line reads as ``hermes -p <profile> gateway run``."""
    from gateway.status import get_process_start_time

    fake = tmp_path / f"fakegw-{profile}" / "hermes_cli" / "main.py"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text("import time\ntime.sleep(600)\n", encoding="utf-8")
    argv = [sys.executable, str(fake)] + (["-p", profile] if profile != "default" else []) + [
        "gateway", "run"]
    p = subprocess.Popen(argv, start_new_session=True)
    procs.append(p)
    st = _wait_for(lambda: get_process_start_time(p.pid), 10, what="fake gateway start time")
    return p, st


def _gateway_record(home: Path, *, pid, start_time, code_sha=S1_SHA, delegation=None,
                    updated_at=None):
    record = {"pid": pid, "start_time": start_time, "kind": "hermes-gateway",
              "gateway_state": "running", "updated_at": updated_at or _now_iso(),
              "code_sha": code_sha, "code_version": "0.21.5"}
    if delegation is not None:
        record["cron_script_lane_delegation"] = delegation
    (home / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")


def _child_env(home: Path, **extra) -> dict:
    from tests.e2e.environ_snapshot import environ_snapshot

    env = {k: v for k, v in environ_snapshot().items() if not k.startswith("PYTEST_")}
    for key in ("HERMES_MACHINE_ID", "_HERMES_CRON_EXTERNAL_WORKER"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["HERMES_HOME"] = str(home)
    env["HOME"] = str(home.parent)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _spawn_runner(procs, home: Path, log: Path, **extra):
    fh = open(log, "a", encoding="utf-8")  # noqa: SIM115
    p = subprocess.Popen([sys.executable, str(HARNESS)], cwd=str(REPO_ROOT),
                         env=_child_env(home, **extra), stdin=subprocess.DEVNULL,
                         stdout=fh, stderr=fh, start_new_session=True)
    fh.close()
    procs.append(p)
    return p


def _lock_free(path: Path) -> bool:
    import fcntl

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return False
        raise
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def _group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


# --- S1_MERGE_SHA + the moved enumerator --------------------------------------------------------

def _cli_rc(cli_main):
    try:
        return cli_main.main()
    except SystemExit as exc:
        return exc.code


def test_cli_cron_runner_and_rearm_dispatch(root, monkeypatch, capsys):
    """``hermes cron runner`` runs ``script_runner.main``; ``hermes cron runner rearm`` deletes
    the trip marker and prints it."""
    import hermes_cli.main as cli_main
    from cron import script_runner

    called = []
    monkeypatch.setattr(script_runner, "main", lambda argv=None: called.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["hermes", "cron", "runner"])
    assert _cli_rc(cli_main) in (0, None) and called == [[]]
    script_runner.trip_marker_path().write_text('{"pid": 7}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["hermes", "cron", "runner", "rearm"])
    assert _cli_rc(cli_main) in (0, None)
    assert not script_runner.trip_marker_path().exists()
    assert '"pid": 7' in capsys.readouterr().out


def test_s1_merge_sha_is_the_merged_s1():
    from cron import lanes

    assert lanes.S1_MERGE_SHA == S1_SHA


def test_cron_tick_profile_homes_moved_verbatim(root):
    import gateway.run as gw_run
    from cron import scheduler_ownership

    _make_profile(root, "p1")
    homes = scheduler_ownership.cron_tick_profile_homes(None)
    assert [(n, Path(h)) for n, h in homes][:1] == [("default", root)]
    assert ("p1", root / "profiles" / "p1") in [(n, Path(h)) for n, h in homes]
    assert gw_run._cron_tick_profile_homes is scheduler_ownership.cron_tick_profile_homes


# --- S2 (a): runner_may_tick truth table ---------------------------------------------------------

@pytest.mark.spawns_gateway_lookalike
def test_a_runner_may_tick_truth_table(root, tmp_path, procs, monkeypatch):
    from cron import script_runner

    prof = _make_profile(root, "p1")
    # No gateway at all: tick.
    assert script_runner.runner_may_tick("p1", prof) is True
    # Knob off: no tick.
    _write_config(root, knob=False)
    assert script_runner.runner_may_tick("p1", prof) is False
    _write_config(root, knob="'true'")
    assert script_runner.runner_may_tick("p1", prof) is False
    _write_config(root, knob=True)
    # Trip marker present, no gateway: no tick.
    script_runner.trip_marker_path().write_text("{}", encoding="utf-8")
    assert script_runner.runner_may_tick("p1", prof) is False
    script_runner.trip_marker_path().unlink()

    gw, st = _fake_gateway(procs, tmp_path, "p1")
    # Field 1 + matching pid + fresh: tick.
    _gateway_record(prof, pid=gw.pid, start_time=st, delegation=1)
    assert script_runner.runner_may_tick("p1", prof) is True
    # Field absent.
    _gateway_record(prof, pid=gw.pid, start_time=st)
    assert script_runner.runner_may_tick("p1", prof) is False
    # Field 0.
    _gateway_record(prof, pid=gw.pid, start_time=st, delegation=0)
    assert script_runner.runner_may_tick("p1", prof) is False
    # Field 1 + stale record: the runtime-status rung no longer proves the holder either way,
    # so pin liveness to the holder and check the stale read itself.
    import gateway.status as status
    real_live = status.resolve_gateway_liveness
    _gateway_record(prof, pid=gw.pid, start_time=st, delegation=1, updated_at=_now_iso(-600))
    monkeypatch.setattr(status, "resolve_gateway_liveness", lambda **_kw: status.GatewayLiveness(
        running=True, pid=gw.pid, source="pid"))
    assert script_runner.runner_may_tick("p1", prof) is False
    # Field 1 + fresh record whose pid is not the liveness holder.
    _gateway_record(prof, pid=gw.pid, start_time=st, delegation=1)
    monkeypatch.setattr(status, "resolve_gateway_liveness", lambda **_kw: status.GatewayLiveness(
        running=True, pid=gw.pid + 100000, source="pid"))
    assert script_runner.runner_may_tick("p1", prof) is False
    # Probe raises.
    def boom(**_kw):
        raise RuntimeError("probe")
    monkeypatch.setattr(status, "resolve_gateway_liveness", boom)
    assert script_runner.runner_may_tick("p1", prof) is False
    monkeypatch.setattr(status, "resolve_gateway_liveness", real_live)
    _gateway_record(prof, pid=gw.pid, start_time=st, delegation=1)
    assert script_runner.runner_may_tick("p1", prof) is True


# --- S2 (b): heartbeat ---------------------------------------------------------------------------

def _runner(root, **kw):
    from cron import script_runner

    return script_runner.ScriptRunner(**kw)


def test_b_last_ok_at_only_on_admitted_tick(root):
    from cron.lanes import TickOutcome

    r = _runner(root)
    r.on_home_cycle("default", root, True, TickOutcome.ADMITTED)
    first = _hb(root)
    assert first["state"] == "active" and first["last_ok_at"]
    assert first["pid"] == os.getpid() and first["pid_start"] is not None
    assert first["last_outcome"] == "ADMITTED"
    time.sleep(0.01)
    for outcome in (TickOutcome.REFUSED_RETIREMENT, TickOutcome.LOCK_BUSY, TickOutcome.ESTOP,
                    TickOutcome.DISPATCH_PAUSED, TickOutcome.SHUTTING_DOWN,
                    TickOutcome.ADMISSION_HOLD, OSError(errno.EMFILE, "Too many open files"),
                    None):
        r.on_home_cycle("default", root, True, outcome)
        hb = _hb(root)
        assert hb["last_ok_at"] == first["last_ok_at"], outcome
        assert hb["at"] >= first["at"]
    # Gated out: standby, last_ok_at carried.
    r.on_home_cycle("default", root, False, None)
    assert _hb(root)["state"] == "standby" and _hb(root)["last_ok_at"] == first["last_ok_at"]
    r.on_home_cycle("default", root, True, TickOutcome.ADMITTED)
    assert _hb(root)["last_ok_at"] > first["last_ok_at"]


def test_b_provider_hands_over_the_raise_not_the_earlier_report(root, monkeypatch):
    """A tick that reported ``ADMITTED`` and then raised reaches ``on_home_cycle`` as the exception,
    so ``last_ok_at`` does not advance; the runner tick gets ``lane="script"`` and the hooks."""
    import cron.scheduler as sched
    from cron.lanes import TickOutcome
    from cron.scheduler_provider import InProcessCronScheduler

    calls, seen, hooks = [], [], []
    stop = threading.Event()

    def fake_tick(**kwargs):
        calls.append(kwargs)
        kwargs["report"](TickOutcome.ADMITTED)
        if len(calls) == 1:
            raise OSError(errno.EMFILE, "Too many open files")
        stop.set()
        return 0

    monkeypatch.setattr(sched, "tick", fake_tick)
    InProcessCronScheduler().start(
        stop, interval=0.01, profile_homes=lambda: [("default", root)], lane="script",
        heartbeat=False, startup_recovery=False,
        on_home_cycle=lambda name, home, gated_in, outcome: seen.append((name, gated_in, outcome)),
        tick_hooks=(lambda h: hooks.append(("start", h)), lambda h: hooks.append(("end", h))))
    assert [c["lane"] for c in calls] == ["script", "script"]
    assert isinstance(seen[0][2], OSError) and seen[1][2] is TickOutcome.ADMITTED
    assert hooks[:2] == [("start", root), ("end", root)]
    names = {p.name for p in (root / "cron").iterdir()}
    assert not names & {"ticker_heartbeat", "ticker_last_success", "ticker_last_error"}, names


@pytest.mark.slow_test(120, reason="runs the provider loop in-process for a few cycles")
def test_b_deleted_home_gets_no_heartbeat(root, monkeypatch):
    from cron import script_runner
    from cron.scheduler_provider import InProcessCronScheduler

    gone = root / "profiles" / "gone"
    r = script_runner.ScriptRunner(
        profile_homes=lambda: [("default", root), ("gone", gone)], interval=0.2)
    stop = threading.Event()
    t = threading.Thread(target=InProcessCronScheduler().start, args=(stop,),
                         kwargs=r.start_kwargs(stop), daemon=True)
    t.start()
    try:
        _wait_for(lambda: (_hb(root) or {}).get("last_ok_at"), 60, what="root heartbeat")
        time.sleep(0.5)
    finally:
        stop.set()
        t.join(timeout=30)
    assert not gone.exists() and not (root / "profiles").exists()


# --- S2 (h): no ticker markers --------------------------------------------------------------------

@pytest.mark.slow_test(120, reason="runs the provider loop in-process for a few cycles")
def test_h_runner_writes_no_ticker_markers(root):
    from cron import script_runner
    from cron.scheduler_provider import InProcessCronScheduler

    r = script_runner.ScriptRunner(profile_homes=lambda: [("default", root)], interval=0.2)
    stop = threading.Event()
    t = threading.Thread(target=InProcessCronScheduler().start, args=(stop,),
                         kwargs=r.start_kwargs(stop), daemon=True)
    t.start()
    try:
        _wait_for(lambda: (_hb(root) or {}).get("last_ok_at"), 60, what="root heartbeat")
        time.sleep(0.6)
    finally:
        stop.set()
        t.join(timeout=30)
    names = {p.name for p in (root / "cron").iterdir()}
    assert "ticker_heartbeat" not in names and "ticker_last_success" not in names, names
    assert "ticker_last_error" not in names


# --- AC-4c: a profile holder on S1+ code that does not delegate ---------------------------------

@pytest.mark.spawns_gateway_lookalike
def test_ac4c_profile_holder_on_s1_that_does_not_delegate(root, tmp_path, procs, monkeypatch):
    from cron import lanes, script_runner

    monkeypatch.setattr(lanes, "code_contains_s1", lambda sha, **_kw: sha == S1_SHA)
    prof = _make_profile(root, "p1")
    gw, st = _fake_gateway(procs, tmp_path, "p1")
    _gateway_record(prof, pid=gw.pid, start_time=st)  # live holder, S1 sha, field absent
    r = script_runner.ScriptRunner(profile_homes=lambda: [("default", root), ("p1", prof)])
    homes = r.enumerate_homes()
    assert r.host_failing == []
    assert r.profile_gate("default", root) is True
    assert r.profile_gate("p1", prof) is False
    for name, home in homes:
        r.on_home_cycle(name, home, name == "default", None)
    assert _hb(root)["state"] == "active"
    assert _hb(prof)["state"] == "standby"
    assert _hb(prof)["standby_reason"] == "home_gateway_not_delegating"


# --- S2 (f) / AC-4d: host precondition ------------------------------------------------------------

def _scratch_repo(tmp_path) -> tuple:
    repo = tmp_path / "tree"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c",
           "commit.gpgsign=false"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "OLD"], check=True)
    old = subprocess.check_output(git[:3] + ["rev-parse", "HEAD"], text=True).strip()
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "S1"], check=True)
    s1 = subprocess.check_output(git[:3] + ["rev-parse", "HEAD"], text=True).strip()
    return repo, old, s1


@pytest.mark.spawns_gateway_lookalike
@pytest.mark.slow_test(120, reason="three runner cycles + a holder process")
def test_f_ac4d_host_precondition(root, tmp_path, procs, monkeypatch, caplog):
    from cron import lanes, script_runner
    from cron.scheduler_provider import InProcessCronScheduler

    repo, old, s1 = _scratch_repo(tmp_path)
    monkeypatch.setattr(lanes, "S1_MERGE_SHA", s1)
    monkeypatch.setattr(lanes, "_runtime_tree", lambda: repo)
    lanes._ancestry_memo.clear()
    p1 = _make_profile(root, "p1")
    p2 = _make_profile(root, "p2")
    for home in (root, p1, p2):
        _script(home, "ok.sh", "echo ok")
        _job(home, "j", "ok.sh")
    gw, st = _fake_gateway(procs, tmp_path, "p1")
    _gateway_record(p1, pid=gw.pid, start_time=st, code_sha=old)
    homes = [("default", root), ("p1", p1), ("p2", p2)]
    r = script_runner.ScriptRunner(profile_homes=lambda: list(homes), interval=0.3)
    caplog.set_level(logging.WARNING, logger="cron.script_runner")
    cycles = []
    orig = r.on_home_cycle

    def counting(name, home, gated_in, outcome):
        if name == "default":
            cycles.append(gated_in)
        return orig(name, home, gated_in, outcome)

    r.on_home_cycle = counting
    stop = threading.Event()
    t = threading.Thread(target=InProcessCronScheduler().start, args=(stop,),
                         kwargs=r.start_kwargs(stop), daemon=True)
    t.start()
    try:
        _wait_for(lambda: len(cycles) >= 3, 30, what="3 runner cycles")
        for home in (root, p1, p2):
            assert _rows(home) == [], home
            hb = _hb(home)
            assert (hb["state"], hb["standby_reason"]) == ("standby", "host_precondition"), home
        warnings = [rec.getMessage() for rec in caplog.records if "host precondition" in rec.getMessage()]
        assert len(warnings) == 1, warnings
        assert "p1" in warnings[0] and str(gw.pid) in warnings[0] and old[:12] in warnings[0]
        # p1's holder restarts onto S1 code: root and p2 tick; p1 stays standby (field absent).
        _gateway_record(p1, pid=gw.pid, start_time=st, code_sha=s1)
        _wait_for(lambda: _rows(root, "status = 'completed'") and _rows(p2, "status = 'completed'"),
                  30, what="root and p2 runs")
        assert _rows(p1) == []
        assert _hb(p1)["standby_reason"] == "home_gateway_not_delegating"
    finally:
        stop.set()
        t.join(timeout=30)


@pytest.mark.spawns_gateway_lookalike
def test_f_ac4d_precondition_cases(root, tmp_path, procs, monkeypatch):
    import gateway.status as status
    from cron import lanes, script_runner

    repo, old, s1 = _scratch_repo(tmp_path)
    monkeypatch.setattr(lanes, "S1_MERGE_SHA", s1)
    monkeypatch.setattr(lanes, "_runtime_tree", lambda: repo)
    lanes._ancestry_memo.clear()
    p1 = _make_profile(root, "p1")
    r = script_runner.ScriptRunner(profile_homes=lambda: [("default", root), ("p1", p1)])

    def failing():
        r.enumerate_homes()
        return [e.home.name for e in r.host_failing]

    gw, st = _fake_gateway(procs, tmp_path, "p1")
    _gateway_record(p1, pid=gw.pid, start_time=st, code_sha=old)
    assert failing() == ["p1"]
    # Mutation (2): the same code_version on both records does not make OLD pass.
    _gateway_record(root, pid=gw.pid, start_time=st, code_sha=s1)
    assert failing() == ["p1"]
    (root / "gateway_state.json").unlink()
    # Mutation (4): clock step (start time shifted, record fresh) stays live -> standby.
    _gateway_record(p1, pid=gw.pid, start_time=st + 3600, code_sha=old)
    assert failing() == ["p1"]
    # Mutation (3): unreadable start time fails toward live -> standby.
    _gateway_record(p1, pid=gw.pid, start_time=st, code_sha=old, updated_at=_now_iso(-600))
    real = status.get_process_start_time
    monkeypatch.setattr(status, "get_process_start_time",
                        lambda pid: None if pid == gw.pid else real(pid))
    assert failing() == ["p1"]
    monkeypatch.setattr(status, "get_process_start_time", real)
    # Missing or non-40-hex code_sha -> standby.
    for bad in (None, "abc", old[:12]):
        _gateway_record(p1, pid=gw.pid, start_time=st, code_sha=bad)
        assert failing() == ["p1"], bad
    # Killing the OLD holder arms the runner.
    _gateway_record(p1, pid=gw.pid, start_time=st, code_sha=old)
    gw.kill()
    gw.wait(timeout=10)
    assert failing() == []
    assert r.profile_gate("default", root) is True


# --- S2 (e) / AC-15: config refusals ---------------------------------------------------------------

@pytest.mark.slow_test(180, reason="three runner subprocess starts")
@pytest.mark.parametrize("case", ["machine_id_env", "external_worker_env", "machine_id_dotenv"])
def test_e_ac15_config_refusals_exit_78(root, tmp_path, procs, case):
    extra = {}
    if case == "machine_id_env":
        extra["HERMES_MACHINE_ID"] = "x"
    elif case == "external_worker_env":
        extra["_HERMES_CRON_EXTERNAL_WORKER"] = "x"
    else:
        (root / ".env").write_text("HERMES_MACHINE_ID=x\n", encoding="utf-8")
    log = tmp_path / "runner.log"
    p = _spawn_runner(procs, root, log, **extra)
    rc = p.wait(timeout=120)
    assert rc == 78, log.read_text(encoding="utf-8")[-3000:]
    assert _hb(root) is None
    text = log.read_text(encoding="utf-8")
    assert sum("refusing to start" in line for line in text.splitlines()) == 1, text[-3000:]


# --- S2 (i): one strike re-arm ---------------------------------------------------------------------

def test_i_rearm_and_knob_toggle_clear_the_trip(root, capsys):
    from cron import script_runner

    marker = script_runner.trip_marker_path()
    marker.write_text(json.dumps({"home": str(root), "pid": 1}), encoding="utf-8")
    r = script_runner.ScriptRunner(profile_homes=lambda: [("default", root)])
    r.enumerate_homes()
    assert r.profile_gate("default", root) is False
    r.on_home_cycle("default", root, False, None)
    assert _hb(root)["standby_reason"] == "watchdog_tripped"
    # (i) hermes cron runner rearm: deletes the marker and prints its content.
    assert script_runner.rearm() == 0
    assert not marker.exists() and '"pid": 1' in capsys.readouterr().out
    r.enumerate_homes()
    assert r.profile_gate("default", root) is True
    # (ii) knob off -> on: a cycle that sees the knob false deletes the marker.
    marker.write_text("{}", encoding="utf-8")
    _write_config(root, knob=False)
    r.enumerate_homes()
    assert r.profile_gate("default", root) is False and not marker.exists()
    _write_config(root, knob=True)
    r.enumerate_homes()
    assert r.profile_gate("default", root) is True


def test_recovery_runs_once_per_home_only_when_gated_in(root, monkeypatch):
    """Prism df257f6b181d: startup recovery touches only homes the runner owns (gated in), once
    each, never a home that is knob-off, tripped or held by a non-delegating gateway."""
    from cron import script_runner
    from cron.scheduler_provider import InProcessCronScheduler

    prof = _make_profile(root, "p1")
    seen = []
    monkeypatch.setattr(InProcessCronScheduler, "recover_interrupted",
                        lambda self: seen.append(__import__("hermes_constants").get_hermes_home()) or 0)
    gate = {"default": "knob_disabled", "p1": "home_gateway_not_delegating"}
    monkeypatch.setattr(script_runner, "runner_gate_reason", lambda name, home: gate[name])
    r = script_runner.ScriptRunner(profile_homes=lambda: [("default", root), ("p1", prof)])
    for _ in range(2):
        for name, home in r.enumerate_homes():
            r.profile_gate(name, home)
    assert seen == []
    gate["default"] = None
    for _ in range(3):
        for name, home in r.enumerate_homes():
            r.profile_gate(name, home)
    assert [Path(h) for h in seen] == [root]


# --- S2 (d): code skew -----------------------------------------------------------------------------

def test_d_code_skew_drains_then_exits_0(root, monkeypatch):
    from cron import script_runner

    inflight = {"n": 1}
    monkeypatch.setattr(script_runner, "_detect_code_skew", lambda: ("aaaa", "bbbb"))
    monkeypatch.setattr(script_runner, "_inflight_count", lambda: inflight["n"])
    stop = threading.Event()
    r = script_runner.ScriptRunner(profile_homes=lambda: [("default", root)])
    r.attach_stop(stop)
    r.enumerate_homes()
    assert r.can_dispatch() is False
    r.on_home_cycle("default", root, True, None)
    hb = _hb(root)
    assert hb["state"] == "draining" and hb["draining_since"]
    assert not stop.is_set()
    first_since = hb["draining_since"]
    r.enumerate_homes()
    r.on_home_cycle("default", root, True, None)
    assert _hb(root)["draining_since"] == first_since and not stop.is_set()
    inflight["n"] = 0
    r.enumerate_homes()
    assert stop.is_set() and r.exit_code == 0
    assert not script_runner.trip_marker_path().exists()


# --- S2 (g) / AC-4e: watchdog, tree-kill, one strike -------------------------------------------------

def test_g_watchdog_snapshot_survives_a_held_procs_lock(root, monkeypatch, procs, caplog):
    """RC-1: the wedge may be a thread holding ``_script_procs_lock``. The tree-kill snapshots
    without blocking on it and the exit comes before the hard deadline."""
    import cron.scheduler as sched
    from cron import lanes, script_runner

    monkeypatch.setattr(lanes, "RUNNER_WATCHDOG_KILL_GRACE_S", 1)
    child = subprocess.Popen(["sleep", "300"], start_new_session=True)
    procs.append(child)
    pgid = os.getpgid(child.pid)
    monkeypatch.setitem(sched._active_script_procs, id(child), child)
    exits = []
    done = threading.Event()

    def fake_exit(code):
        exits.append((code, time.monotonic()))
        done.set()

    r = script_runner.ScriptRunner(profile_homes=lambda: [("default", root)], exit_fn=fake_exit)
    r.enumerate_homes()
    held = threading.Event()
    release = threading.Event()

    def hold():
        with sched._script_procs_lock:
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    held.wait(5)
    caplog.set_level(logging.WARNING, logger="cron.script_runner")
    started = time.monotonic()
    try:
        r._watchdog_fire(root, _now_iso())
        assert done.wait(10)
    finally:
        release.set()
        r.cancel_deadline()
    assert exits[0][0] == 70
    assert exits[0][1] - started < lanes.RUNNER_WATCHDOG_KILL_GRACE_S + 1.5
    child.wait(timeout=5)
    assert _group_gone(pgid)
    assert any("procs lock busy" in rec.getMessage() for rec in caplog.records)
    marker = json.loads(script_runner.trip_marker_path().read_text(encoding="utf-8"))
    assert marker["home"] == str(root) and marker["pid"] == os.getpid()
    assert (_hb(root)["state"], _hb(root)["standby_reason"]) == ("standby", "watchdog_tripped")


def test_g_watchdog_trip_names_the_blocking_frame(root, caplog):
    """t_f2e71f40: the 2026-10-28 20:22 trip left a marker with no clue where the tick was. The
    marker now carries every thread's stack and the tick thread's innermost cron frame (phase), and
    the CRITICAL log names that frame, so the next trip says which call blocked."""
    from cron import jobs, script_runner

    r = script_runner.ScriptRunner(profile_homes=lambda: [("default", root)],
                                   exit_fn=lambda _c: None)
    r.enumerate_homes()
    tick_ready = threading.Event()

    def tick_thread():
        r.on_tick_start(root)
        tick_ready.set()
        with jobs._jobs_lock():  # blocks: the holder below owns the in-process jobs lock
            pass

    caplog.set_level(logging.CRITICAL, logger="cron.script_runner")
    with jobs._jobs_file_lock:
        t = threading.Thread(target=tick_thread, name="cron-script-runner", daemon=True)
        t.start()
        assert tick_ready.wait(5)
        time.sleep(0.3)  # let the tick thread reach the blocking acquire
        try:
            r._watchdog_fire(root, _now_iso())
        finally:
            r.cancel_deadline()
            r.on_tick_end(root)
    t.join(5)
    marker = json.loads(script_runner.trip_marker_path().read_text(encoding="utf-8"))
    assert marker["home"] == str(root)
    assert marker["phase"].startswith("cron.jobs:_jobs_lock:"), marker["phase"]
    assert marker["tick_thread"] == "cron-script-runner"
    tick_frames = marker["threads"]["cron-script-runner"]
    assert any("_jobs_lock" in f for f in tick_frames), tick_frames
    assert any(name.startswith("cron-runner-watchdog") or name == "MainThread"
               for name in marker["threads"])
    assert any("cron.jobs:_jobs_lock:" in rec.getMessage() for rec in caplog.records)


def test_g_watchdog_kills_a_group_whose_leader_already_exited(root, monkeypatch, tmp_path):
    """Prism d9f3c814a2c6: the script leader exited but a background child keeps the output pipe,
    so ``communicate()`` has not reaped it yet (a zombie). The watchdog still kills the group; a
    ``poll()`` that reaps the leader must not make it skip the survivors."""
    import cron.scheduler as sched
    from cron import lanes, script_runner

    monkeypatch.setattr(lanes, "RUNNER_WATCHDOG_KILL_GRACE_S", 1)
    pidfile = tmp_path / "bg.pid"
    leader = subprocess.Popen(["sh", "-c", f"sleep 300 & echo $! > {pidfile}; exit 0"],
                              start_new_session=True, stdout=subprocess.PIPE)
    try:
        int(_wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(), 10, what="bg"))

        def zombie():
            with contextlib.suppress(OSError):
                return Path(f"/proc/{leader.pid}/stat").read_text().split()[2] == "Z"
            return False

        if sys.platform.startswith("linux"):
            _wait_for(zombie, 10, what="leader exited, unreaped")
        else:
            time.sleep(0.5)
        assert leader.returncode is None and not _group_gone(leader.pid)
        monkeypatch.setitem(sched._active_script_procs, id(leader), leader)
        r = script_runner.ScriptRunner(profile_homes=lambda: [("default", root)],
                                       exit_fn=lambda _c: None)
        r._tree_kill(1.0)
        leader.poll()
        assert _wait_for(lambda: _group_gone(leader.pid), 3, what="background child gone")
    finally:
        with contextlib.suppress(Exception):
            os.killpg(leader.pid, signal.SIGKILL)  # windows-footgun: ok — skipped on win32
        with contextlib.suppress(Exception):
            leader.communicate(timeout=10)


@pytest.mark.slow_test(150, reason="real runner subprocess, a trip and a restart")
def test_g_ac4e_watchdog_tree_kill_one_strike(root, tmp_path, procs):
    from cron import script_runner

    ctl = tmp_path / "ctl"
    ctl.mkdir()
    spid = ctl / "s.pid"
    _script(root, "s.sh", f"echo $$ > {spid}\nsleep 300")
    _job(root, "S", "s.sh")
    log = tmp_path / "runner.log"
    env = dict(RUNNER_HARNESS_CYCLE_S=1, RUNNER_HARNESS_WATCHDOG_S=2, RUNNER_HARNESS_GRACE_S=1,
               RUNNER_HARNESS_BLOCK=ctl / "block")
    runner = _spawn_runner(procs, root, log, **env)

    def tail():
        return log.read_text(encoding="utf-8")[-4000:]

    # Cycle 1, unpatched: S starts. Record its pid / pgid.
    s_pid = int(_wait_for(lambda: spid.exists() and spid.read_text().strip(), 60, what="S pid"))
    s_pgid = os.getpgid(s_pid)
    assert s_pgid != os.getpgid(runner.pid)  # its own session: exactly one job's tree
    # From the next cycle get_due_jobs blocks while holding .tick-script.lock.
    (ctl / "block").touch()
    _wait_for(lambda: (ctl / "blocked").exists(), 20, what="blocked tick")
    blocked_at = time.monotonic()
    lock = root / "cron" / ".tick-script.lock"
    if runner.poll() is None:
        assert _lock_free(lock) is False, "seam precondition: the stub must block inside the lock"
    rc = runner.wait(timeout=15)
    exited_at = time.monotonic()
    assert rc == 70, tail()
    assert exited_at - blocked_at <= 2 + 1 + 1 + 1.0, (exited_at - blocked_at, tail())
    assert _wait_for(lambda: _group_gone(s_pgid), 2, what="S group gone")
    assert _wait_for(lambda: _lock_free(lock), 1, what="lock released")
    assert script_runner.trip_marker_path().exists()
    # The supervisor restarts it: standby/watchdog_tripped, no tick, no exit in 10 s.
    (ctl / "blocked").unlink()
    runner = _spawn_runner(procs, root, log, **env)
    _wait_for(lambda: (_hb(root) or {}).get("pid") == runner.pid, 60, what="restarted heartbeat")
    hb = _hb(root)
    assert (hb["state"], hb["standby_reason"]) == ("standby", "watchdog_tripped")
    time.sleep(10)
    assert runner.poll() is None, tail()
    assert not (ctl / "blocked").exists()
    # A gateway-role "all" tick runs a due script job the runner is not running.
    _script(root, "ok.sh", "echo ok")
    j2 = _job(root, "J2", "ok.sh")
    subprocess.run([sys.executable, "-c", "from cron.scheduler import tick; tick(lane='all')"],
                   cwd=str(REPO_ROOT), env=_child_env(root), check=True, timeout=120,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert _rows(root, f"job_id = '{j2['id']}' AND status = 'completed'")
    # Re-arm (stub disabled): the runner ticks at its next cycle.
    (ctl / "block").unlink()
    assert script_runner.rearm() == 0
    _wait_for(lambda: (_hb(root) or {}).get("state") == "active"
              and (_hb(root) or {}).get("last_ok_at"), 20, what="re-armed tick")
    assert runner.poll() is None


# --- E2E + S2 (c): the runner against a scratch home ------------------------------------------------

def test_rows_treats_a_tableless_executions_db_as_empty(tmp_path):
    """The runner creates executions.db before its table: a poll in that window reads [], not raises."""
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "executions.db").write_bytes(b"")
    assert _rows(tmp_path) == []
    assert _rows(tmp_path, "status = 'completed'") == []


@pytest.mark.slow_test(180, reason="real runner subprocess")
def test_e2e_runner_fires_echo_job(root, tmp_path, procs):
    from cron.runtime_state import merged_jobs

    _script(root, "ok.sh", "echo ok")
    job = _job(root, "echo", "ok.sh")
    log = tmp_path / "runner.log"
    runner = _spawn_runner(procs, root, log, RUNNER_HARNESS_CYCLE_S=1)
    try:
        _wait_for(lambda: _rows(root, "status = 'completed'"), 90, step=0.2, what="completed row")
        rec = {r["id"]: r for r in merged_jobs(root / "cron" / "jobs.json")}[job["id"]]
        _wait_for(lambda: {r["id"]: r for r in merged_jobs(root / "cron" / "jobs.json")}[job["id"]]
                  .get("last_status") == "ok", 10, what="last_status ok")
        assert rec is not None
        assert _rows(root, "status = 'completed'")[0]["pid"] == runner.pid
        names = {p.name for p in (root / "cron").iterdir()}
        assert "ticker_heartbeat" not in names and "ticker_last_success" not in names
        assert _hb(root)["state"] == "active"
    finally:
        runner.send_signal(signal.SIGTERM)
        assert runner.wait(timeout=30) == 0, log.read_text(encoding="utf-8")[-3000:]


@pytest.mark.slow_test(180, reason="real runner subprocess")
def test_c_sigterm_terminates_script_requeues_and_exits_0(root, tmp_path, procs):
    from cron.jobs import RESTART_REQUEUE_KEY
    from cron.runtime_state import merged_jobs

    spid = tmp_path / "s.pid"
    _script(root, "long.sh", f"echo $$ > {spid}\nsleep 300")
    job = _job(root, "long", "long.sh")
    log = tmp_path / "runner.log"
    runner = _spawn_runner(procs, root, log, RUNNER_HARNESS_CYCLE_S=1)
    s_pid = int(_wait_for(lambda: spid.exists() and spid.read_text().strip(), 90, what="script"))
    sent = time.monotonic()
    runner.send_signal(signal.SIGTERM)
    rc = runner.wait(timeout=30)
    assert time.monotonic() - sent <= 25
    assert rc == 0, log.read_text(encoding="utf-8")[-3000:]
    assert _group_gone(s_pid)
    rec = {r["id"]: r for r in merged_jobs(root / "cron" / "jobs.json")}[job["id"]]
    assert rec.get(RESTART_REQUEUE_KEY), rec
