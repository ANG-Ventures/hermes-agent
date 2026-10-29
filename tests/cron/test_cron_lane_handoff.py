"""S3 of the cron script runner spec: the gateway delegates the script lane.

Spec: Obsidian ``AI/Hermes Agent/Cron Script Runner Out Of Gateway — Spec (2026-10-27).md`` v1.0,
§5.3 (who owns a home's script lane, gateway side), §7 "S3: the gateway delegates". Depends on S1
(hermes-agent#2396) and S2 (hermes-agent#2403).

AC -> test:
  S3 unit: last_ok_at 179 s / 181 s; at fresh + last_ok_at 181 s
                                test_delegate_progress_window
  S3 unit: dead pid, unreadable start time, standby, standby/watchdog_tripped, draining
                                test_delegate_takes_back_when_runner_not_progressing
  S3 unit: knob false / missing / "true"     test_delegate_knob
  S3 unit: published field 1 / 0 / flipped mid-run (mutation: constant 1)
                                test_published_delegation_field_follows_knob,
                                test_housekeeping_heartbeat_publishes_lane_fields
  S3 unit: both ticker loops pick the lane per home
                                test_multiplex_loop_ticks_agent_only_while_delegating,
                                test_single_profile_loop_ticks_agent_only_while_delegating,
                                test_loop_without_delegation_never_passes_lane
  S3 unit: LOCK_BUSY -> LOCK-SKIPPED, never gateway (AC-4e "status ignores LOCK_BUSY")
                                test_lock_skipped_script_lane_is_published_and_shown
  S3 unit: tripped heartbeat -> "watchdog tripped ... rearm"
                                test_status_lines
  knob default false            test_knob_default_is_false
  AC-4a (E2E)                   test_ac4a_takeover_dead_runner
  AC-4b (E2E)                   test_ac4b_takeover_runner_failing_every_tick (EMFILE, retirement)
  restart -> gateway stops ticking the lane; AC-3 query = 0 across each E2E
                                test_ac4a_takeover_dead_runner
  AC-9 unit / I8 negative       test_ac9_knob_false_mid_run_returns_lane_to_gateway
"""

from __future__ import annotations

import contextlib
import errno
import json
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
RUNNER_HARNESS = Path(__file__).resolve().parent / "_runner_harness.py"
GATEWAY_HARNESS = Path(__file__).resolve().parent / "_gateway_lane_harness.py"
UTC = timezone.utc


# --- fixtures -----------------------------------------------------------------------------------

@pytest.fixture
def root(tmp_path, monkeypatch):
    """A scratch host root (``<tmp>/home/.hermes``) with the knob on and no gateway records."""
    for key in list(os.environ):
        if key.endswith("_API_KEY") or key in (
                "INVOCATION_ID", "HERMES_TIMEZONE", "HERMES_MACHINE_ID",
                "_HERMES_CRON_EXTERNAL_WORKER"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home" / ".hermes"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HOME", str(home.parent))
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_config(home, knob=True)
    from cron import lanes

    lanes._knob_cache.clear()
    lanes._gateway_outcomes.clear()
    yield home
    lanes._knob_cache.clear()
    lanes._gateway_outcomes.clear()


def _write_config(home: Path, *, knob):
    text = "cron:\n  oneshot_catchup_s: 0\n"
    if knob is not None:
        text += f"  script_runner:\n    enabled: {knob}\n"
    (home / "config.yaml").write_text(text, encoding="utf-8")
    st = (home / "config.yaml").stat()
    os.utime(home / "config.yaml", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def _make_profile(root_home: Path, name: str) -> Path:
    prof = root_home / "profiles" / name
    (prof / "cron").mkdir(parents=True)
    (prof / "config.yaml").write_text("cron:\n  oneshot_catchup_s: 0\n", encoding="utf-8")
    return prof


T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch):
    """Injected clock for ``cron.lanes``: ``clock["now"]`` is what ``lanes._now()`` returns."""
    from cron import lanes

    state = {"now": T0}
    monkeypatch.setattr(lanes, "_now", lambda: state["now"])
    return state


def _own_start():
    from gateway.status import get_process_start_time

    return get_process_start_time(os.getpid())


def _hb(home: Path, *, state="active", last_ok_s=10.0, at_s=1.0, pid=None, pid_start=None,
        reason=None, tick_started=None):
    from cron.lanes import write_runner_heartbeat

    payload = {
        "at": (T0 - timedelta(seconds=at_s)).isoformat(),
        "pid": os.getpid() if pid is None else pid,
        "pid_start": _own_start() if pid_start is None else pid_start,
        "state": state, "standby_reason": reason, "draining_since": None,
        "tick_started_at": tick_started,
        "last_ok_at": None if last_ok_s is None else (T0 - timedelta(seconds=last_ok_s)).isoformat(),
        "last_outcome": None, "code_sha": None,
    }
    assert write_runner_heartbeat(home, payload)
    return payload


# --- delegate(home) ------------------------------------------------------------------------------

def test_delegate_progress_window(root, clock):
    from cron import lanes

    _hb(root, last_ok_s=179)
    assert lanes.delegate(root) is True
    _hb(root, last_ok_s=181)
    assert lanes.delegate(root) is False
    # Liveness (`at`) is fresh but progress is not: take back (AC-4b mutation: read hb.at).
    _hb(root, at_s=0.5, last_ok_s=181)
    assert lanes.delegate(root) is False
    # Never progressed at all.
    _hb(root, last_ok_s=None)
    assert lanes.delegate(root) is False


def test_delegate_takes_back_when_runner_not_progressing(root, clock, monkeypatch):
    import gateway.status as status
    from cron import lanes

    # No heartbeat file.
    assert lanes.delegate(root) is False
    # Dead pid (a reaped child).
    p = subprocess.Popen(["true"])
    p.wait()
    _hb(root, pid=p.pid)
    assert lanes.delegate(root) is False
    # pid_start does not match (pid reused).
    _hb(root, pid_start=12345)
    assert lanes.delegate(root) is False
    # Start time unreadable: fails toward dead.
    _hb(root)
    assert lanes.delegate(root) is True
    with monkeypatch.context() as m:
        m.setattr(status, "get_process_start_time", lambda _pid: None)
        assert lanes.delegate(root) is False
    assert lanes.delegate(root) is True
    for state, reason in (("standby", "host_precondition"), ("standby", "watchdog_tripped"),
                          ("draining", None)):
        _hb(root, state=state, reason=reason)
        assert lanes.delegate(root) is False, (state, reason)
    # Unparseable heartbeat.
    (root / "cron" / lanes.HEARTBEAT_NAME).write_text("{not json", encoding="utf-8")
    assert lanes.delegate(root) is False


@pytest.mark.parametrize("knob, expected", [
    (True, True), (False, False), (None, False), ("'true'", False), ("1", False)])
def test_delegate_knob(root, clock, knob, expected):
    from cron import lanes

    _write_config(root, knob=knob)
    _hb(root)
    assert lanes.delegate(root) is expected


def test_knob_default_is_false():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["cron"]["script_runner"] == {"enabled": False}


# --- the published field ---------------------------------------------------------------------------

def test_published_delegation_field_follows_knob(root):
    """knob true -> 1; false -> 0; flipped false mid-run -> 0 at the next housekeeping cycle.
    Mutation: publish a constant 1 -> the knob-false asserts go RED."""
    import gateway.status as status
    from cron import lanes

    status.write_runtime_status(reload_existing=True, **lanes.gateway_lane_status_fields())
    assert status.read_runtime_status()["cron_script_lane_delegation"] == 1
    _write_config(root, knob=False)
    status.write_runtime_status(**lanes.gateway_lane_status_fields())
    assert status.read_runtime_status()["cron_script_lane_delegation"] == 0
    _write_config(root, knob=True)
    status.write_runtime_status(**lanes.gateway_lane_status_fields())
    assert status.read_runtime_status()["cron_script_lane_delegation"] == 1
    _write_config(root, knob=None)
    status.write_runtime_status(**lanes.gateway_lane_status_fields())
    assert status.read_runtime_status()["cron_script_lane_delegation"] == 0


def test_housekeeping_heartbeat_publishes_lane_fields(root, monkeypatch):
    """The housekeeping "Runtime heartbeat" chore carries both fields every cycle."""
    import gateway.run as gw_run
    import gateway.status as status
    from cron import lanes

    published = []
    monkeypatch.setattr(status, "publish_runtime_status", lambda **kw: published.append(kw) or 0)
    lanes.record_gateway_script_outcome(root, "ADMITTED")
    gw_run._write_runtime_heartbeat()
    _write_config(root, knob=False)
    gw_run._write_runtime_heartbeat()
    assert [p["cron_script_lane_delegation"] for p in published] == [1, 0]
    assert published[0]["cron_script_lane_last_outcome"]["default"]["outcome"] == "ADMITTED"


# --- both ticker loops ----------------------------------------------------------------------------

def _run_loop(monkeypatch, *, cycles, delegated, profile_homes=None, delegate_script_lane=True,
              outcomes=None):
    """Run the provider loop for ``cycles`` cycles with ``cron.scheduler.tick`` faked. Returns a
    list of (home, lane-or-None, cycle)."""
    import cron.scheduler as sched
    from cron import lanes
    from cron.scheduler_provider import InProcessCronScheduler
    from hermes_constants import get_hermes_home

    calls = []
    stop = threading.Event()
    cycle = {"n": 0}

    def fake_tick(**kwargs):
        home = Path(get_hermes_home())
        calls.append((home, kwargs.get("lane"), cycle["n"]))
        if outcomes and home in outcomes:
            if kwargs.get("scan_report"):
                kwargs["scan_report"](outcomes[home][1])
            if kwargs.get("report"):
                kwargs["report"](outcomes[home][0])
        return 0

    def fake_delegate(home):
        return Path(home) in delegated

    real_wait = stop.wait

    def counting_wait(timeout=None):
        cycle["n"] += 1
        if cycle["n"] >= cycles:
            stop.set()
        return real_wait(0)

    monkeypatch.setattr(sched, "tick", fake_tick)
    monkeypatch.setattr(lanes, "delegate", fake_delegate)
    monkeypatch.setattr(stop, "wait", counting_wait)
    kwargs = {"interval": 0, "startup_recovery": True}
    if profile_homes is not None:
        kwargs["profile_homes"] = profile_homes
    if delegate_script_lane:
        kwargs["delegate_script_lane"] = True
    InProcessCronScheduler().start(stop, **kwargs)
    return calls


def test_multiplex_loop_ticks_agent_only_while_delegating(root, monkeypatch):
    prof = _make_profile(root, "p1")
    delegated = {root}
    calls = _run_loop(monkeypatch, cycles=1, delegated=delegated,
                      profile_homes=lambda: [("default", root), ("p1", prof)])
    assert (root, "agent", 0) in calls and (prof, None, 0) in calls
    delegated.clear()
    delegated.add(prof)
    calls = _run_loop(monkeypatch, cycles=1, delegated=delegated,
                      profile_homes=lambda: [("default", root), ("p1", prof)])
    assert (root, None, 0) in calls and (prof, "agent", 0) in calls


def test_single_profile_loop_ticks_agent_only_while_delegating(root, monkeypatch):
    calls = _run_loop(monkeypatch, cycles=1, delegated={root})
    assert [(h, lane) for h, lane, _c in calls] == [(root, "agent")]
    calls = _run_loop(monkeypatch, cycles=1, delegated=set())
    assert [(h, lane) for h, lane, _c in calls] == [(root, None)]


def test_loop_without_delegation_never_passes_lane(root, monkeypatch):
    """Callers that do not opt in (desktop ticker, deprecated shim) keep ``"all"``: the tick call
    is byte-identical (no ``lane`` keyword) even when a runner would be delegated to."""
    calls = _run_loop(monkeypatch, cycles=1, delegated={root}, delegate_script_lane=False)
    assert [(h, lane) for h, lane, _c in calls] == [(root, None)]
    calls = _run_loop(monkeypatch, cycles=1, delegated={root}, delegate_script_lane=False,
                      profile_homes=lambda: [("default", root)])
    assert [(h, lane) for h, lane, _c in calls] == [(root, None)]


def test_loops_record_the_script_lane_outcome(root, monkeypatch):
    from cron import lanes
    from cron.lanes import TickOutcome

    prof = _make_profile(root, "p1")
    homes = lambda: [("default", root), ("p1", prof)]  # noqa: E731
    _run_loop(monkeypatch, cycles=1, delegated={prof}, profile_homes=homes,
              outcomes={root: (TickOutcome.ADMITTED, "agent")})
    out = lanes.gateway_lane_status_fields()["cron_script_lane_last_outcome"]
    assert out["default"]["outcome"] == "LOCK_BUSY"  # narrowed to agent: script lane skipped
    assert out["p1"]["outcome"] == "DELEGATED"
    _run_loop(monkeypatch, cycles=1, delegated=set(), profile_homes=homes,
              outcomes={root: (TickOutcome.ADMITTED, "all"),
                        prof: (TickOutcome.LOCK_BUSY, None)})
    out = lanes.gateway_lane_status_fields()["cron_script_lane_last_outcome"]
    assert out["default"]["outcome"] == "ADMITTED"
    assert out["p1"]["outcome"] == "LOCK_BUSY"


def test_delegation_keeps_the_on_home_cycle_report(root, monkeypatch):
    """Prism 619b39593013: with both ``delegate_script_lane`` and ``on_home_cycle`` the lane
    ``report`` must not replace the one that feeds ``on_home_cycle``."""
    import cron.scheduler as sched
    from cron import lanes
    from cron.lanes import TickOutcome
    from cron.scheduler_provider import InProcessCronScheduler

    seen = []
    stop = threading.Event()

    def fake_tick(**kwargs):
        kwargs["report"](TickOutcome.ADMITTED)
        stop.set()
        return 0

    monkeypatch.setattr(sched, "tick", fake_tick)
    monkeypatch.setattr(lanes, "delegate", lambda _h: False)
    InProcessCronScheduler().start(
        stop, interval=0, startup_recovery=True, profile_homes=lambda: [("default", root)],
        delegate_script_lane=True,
        on_home_cycle=lambda name, home, gated_in, outcome: seen.append(outcome))
    assert seen == [TickOutcome.ADMITTED]
    out = lanes.gateway_lane_status_fields()["cron_script_lane_last_outcome"]
    assert out["default"]["outcome"] == "ADMITTED"


# --- hermes cron status: the script-lane owner line -------------------------------------------

def _lock_hold(path: Path):
    import fcntl

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_lock_skipped_script_lane_is_published_and_shown(root, clock, monkeypatch):
    """A real gateway-role tick while ``.tick-script.lock`` is held: the published outcome is
    LOCK_BUSY and the status line reads LOCK-SKIPPED, never the gateway, even though the runner's
    heartbeat is otherwise not progressing. Mutation: status ignores LOCK_BUSY -> "gateway" -> RED."""
    import gateway.status as status
    from cron import lanes
    from cron.scheduler import tick
    from hermes_cli.cron import script_lane_owner_line

    _hb(root, last_ok_s=400, tick_started=(T0 - timedelta(seconds=30)).isoformat(), pid=os.getpid())
    fd = _lock_hold(root / "cron" / ".tick-script.lock")
    try:
        scanned, outcomes = [], []
        tick(verbose=False, sync=True, report=outcomes.append, scan_report=scanned.append)
    finally:
        os.close(fd)
    assert scanned == ["agent"] and outcomes == [lanes.TickOutcome.ADMITTED]
    lanes.record_gateway_script_outcome(root, lanes.gateway_script_outcome(outcomes[-1], scanned[-1]))
    status.write_runtime_status(reload_existing=True, **lanes.gateway_lane_status_fields())
    clock["now"] = T0 + timedelta(seconds=7)
    line = script_lane_owner_line(root, now=T0 + timedelta(seconds=7))
    assert line.startswith(f"script lane: LOCK-SKIPPED by runner pid {os.getpid()}"), line
    assert "gateway did not tick it" in line
    # Holder unknown when the runner is not mid-tick.
    _hb(root, last_ok_s=400)
    assert "LOCK-SKIPPED by unknown" in script_lane_owner_line(root)


def test_status_lines(root, clock, monkeypatch):
    from cron import lanes, script_runner
    from hermes_cli.cron import script_lane_owner_line

    # Active and progressing.
    _hb(root, last_ok_s=12)
    assert script_lane_owner_line(root) == f"script lane: runner pid {os.getpid()} (active, ok 12 s ago)"
    # Not progressing: the gateway has it.
    _hb(root, last_ok_s=412)
    assert script_lane_owner_line(root) == "script lane: gateway (runner last ok 412 s ago)"
    # Knob off.
    _write_config(root, knob=False)
    assert script_lane_owner_line(root) == "script lane: gateway (cron.script_runner.enabled=false)"
    _write_config(root, knob=True)
    # Tripped.
    script_runner.trip_marker_path().write_text(json.dumps(
        {"at": "2026-10-28T11:00:00+00:00", "home": str(root)}), encoding="utf-8")
    _hb(root, state="standby", reason="watchdog_tripped", last_ok_s=600)
    line = script_lane_owner_line(root)
    assert line == ("script lane: gateway (runner standby: watchdog tripped "
                    f"2026-10-28T11:00:00+00:00 on {root}; run 'hermes cron runner rearm')"), line
    script_runner.trip_marker_path().unlink()
    # Profile gateway that does not delegate.
    (root / "gateway_state.json").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    _hb(root, state="standby", reason="home_gateway_not_delegating")
    assert script_lane_owner_line(root) == (
        "script lane: profile gateway pid 4242 (runner standby: home gateway does not delegate)")
    # Host precondition.
    monkeypatch.setattr(lanes, "host_gateways", lambda *_a, **_k: [
        lanes.GatewayEntry(root / "profiles" / "aegis", 77, None, "ab" * 20, False)])
    monkeypatch.setattr(lanes, "code_contains_s1", lambda *_a, **_k: False)
    _hb(root, state="standby", reason="host_precondition")
    assert script_lane_owner_line(root) == (
        "script lane: runner standby (host precondition: aegis pid 77 on " + "ab" * 6 + ")")
    # No runner at all.
    (root / "cron" / lanes.HEARTBEAT_NAME).unlink()
    assert script_lane_owner_line(root) == "script lane: gateway (no runner heartbeat)"


def test_status_reads_the_serving_gateways_knob(root, clock, monkeypatch):
    """Prism c506f9815d57: a profile multiplexed by the root gateway is delegated on the ROOT
    gateway's knob, so ``hermes cron status`` run with the profile's home reads the serving
    gateway's published ``cron_script_lane_delegation``, not the CLI process home's knob."""
    from cron import lanes
    from hermes_cli.cron import script_lane_owner_line

    prof = _make_profile(root, "p1")  # its config.yaml has no script_runner key
    (root / "gateway_state.json").write_text(json.dumps(
        {"pid": os.getpid(), "cron_script_lane_delegation": 1}), encoding="utf-8")
    _hb(prof, last_ok_s=12)
    monkeypatch.setenv("HERMES_HOME", str(prof))
    lanes._knob_cache.clear()
    assert lanes.knob_enabled() is False  # the CLI process home's own knob
    assert script_lane_owner_line(prof) == (
        f"script lane: runner pid {os.getpid()} (active, ok 12 s ago)")
    # The serving gateway publishes 0: the profile's script lane is the gateway's.
    (root / "gateway_state.json").write_text(json.dumps(
        {"pid": os.getpid(), "cron_script_lane_delegation": 0}), encoding="utf-8")
    assert script_lane_owner_line(prof) == "script lane: gateway (cron.script_runner.enabled=false)"
    # The profile's own live gateway outranks the root record.
    (prof / "gateway_state.json").write_text(json.dumps(
        {"pid": os.getpid(), "cron_script_lane_delegation": 1}), encoding="utf-8")
    assert script_lane_owner_line(prof).startswith("script lane: runner pid")


def test_cron_status_prints_the_owner_line(root, clock, monkeypatch, capsys):
    import gateway.host_topology as topo
    import hermes_cli.cron as cli_cron
    import hermes_cli.gateway as cli_gw

    _hb(root, last_ok_s=12)
    monkeypatch.setattr(cli_cron, "_active_cron_provider_name", lambda: "builtin")
    monkeypatch.setattr(topo, "host_gateway_serving", lambda *_a, **_k: None)
    monkeypatch.setattr(cli_gw, "find_gateway_pids", lambda *_a, **_k: [])
    monkeypatch.setattr(cli_gw, "named_profile_served_by_running_multiplexer", lambda *_a, **_k: False)
    cli_cron.cron_status()
    assert f"script lane: runner pid {os.getpid()} (active, ok 12 s ago)" in capsys.readouterr().out


# --- E2E: two real processes against a scratch home ---------------------------------------------

def _job(home: Path, name: str, script: str, *, due_now=True):
    from cron.jobs import create_job, update_job
    from cron.scheduler_provider import _profile_cron_scope

    with _profile_cron_scope(home):
        job = create_job(prompt=None, schedule="every 1m", name=name, script=script,
                         no_agent=True, deliver="local")
        if due_now:
            update_job(job["id"], {"next_run_at": (datetime.now(UTC) - timedelta(seconds=2)).isoformat()})
    return job


def _script(home: Path, name: str, body: str) -> str:
    path = home / "scripts" / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return name


def _rows(home: Path, where="") -> list:
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


def _ac3_duplicates(home: Path) -> list:
    path = Path(home) / "cron" / "executions.db"
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        return list(conn.execute(
            "SELECT job_id, scheduled_instant, COUNT(*) c FROM executions WHERE "
            "scheduled_instant IS NOT NULL AND started_at IS NOT NULL GROUP BY 1,2 HAVING c > 1"))
    finally:
        conn.close()


def _wait_for(pred, timeout, step=0.1, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture
def procs():
    started: list = []
    yield started
    for p in started:
        if p.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)  # windows-footgun: ok — skipped on win32
            with contextlib.suppress(Exception):
                p.kill()
        with contextlib.suppress(Exception):
            p.wait(timeout=10)


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


# Scaled clock for the two-process runs: one cycle = 1 s stands for RUNNER_CYCLE_S = 60 s, and the
# progress bound scales with it (180 s -> 3 cycles). Bounds below are asserted in cycles.
CYCLE_S = 1.0
STALE_CYCLES = 6
_SCALE = dict(RUNNER_HARNESS_CYCLE_S=CYCLE_S, RUNNER_HARNESS_STALE_S=STALE_CYCLES * CYCLE_S)


def _spawn(procs, harness: Path, home: Path, log: Path, **extra):
    fh = open(log, "a", encoding="utf-8")  # noqa: SIM115
    p = subprocess.Popen([sys.executable, str(harness)], cwd=str(REPO_ROOT),
                         env=_child_env(home, **_SCALE, **extra), stdin=subprocess.DEVNULL,
                         stdout=fh, stderr=fh, start_new_session=True)
    fh.close()
    procs.append(p)
    return p


def _lanes_log(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out


def _last_lane(path: Path):
    rows = _lanes_log(path)
    return rows[-1]["lane"] if rows else None


def _ran_by(home: Path, job_id: str):
    rows = _rows(home, f"job_id = '{job_id}' AND started_at IS NOT NULL")
    return rows[0]["pid"] if rows else None


def _start_pair(procs, root, tmp_path, *, runner_extra=None):
    """Runner, then a gateway-role process; returns once the gateway delegates (lane "agent") and
    the runner has run a due job."""
    from cron.lanes import read_runner_heartbeat

    _script(root, "ok.sh", "echo ok")
    lanes_log = tmp_path / "gw-lanes.jsonl"
    runner = _spawn(procs, RUNNER_HARNESS, root, tmp_path / "runner.log", **(runner_extra or {}))
    _wait_for(lambda: (read_runner_heartbeat(root) or {}).get("pid") == runner.pid
              and (read_runner_heartbeat(root) or {}).get("last_ok_at"), 60, what="runner progressing")
    gw = _spawn(procs, GATEWAY_HARNESS, root, tmp_path / "gw.log", GATEWAY_HARNESS_LANES=lanes_log)
    _wait_for(lambda: _last_lane(lanes_log) == "agent", 60, what="gateway delegating")
    first = _job(root, "first", "ok.sh")
    _wait_for(lambda: _ran_by(root, first["id"]), 30, what="first job runs")
    assert _ran_by(root, first["id"]) == runner.pid, _tail(tmp_path)
    return gw, runner, lanes_log


def _tail(tmp_path):
    out = []
    for name in ("gw.log", "runner.log"):
        with contextlib.suppress(OSError):
            out.append(f"--- {name}\n" + (tmp_path / name).read_text(encoding="utf-8")[-2500:])
    return "\n".join(out)


@pytest.mark.slow_test(180, reason="two real processes, a kill and a restart")
def test_ac4a_takeover_dead_runner(root, tmp_path, procs):
    """AC-4a: runner killed -> the gateway ticks the script lane within one cycle (60 virtual s).
    Mutation: drop ``runner_pid_alive`` -> takeover waits for the progress bound -> RED. Then a
    restarted runner takes the lane back and the gateway stops ticking it within one cycle."""
    gw, runner, lanes_log = _start_pair(procs, root, tmp_path)
    runner.send_signal(signal.SIGKILL)
    runner.wait(timeout=10)
    killed = time.monotonic()
    _wait_for(lambda: _last_lane(lanes_log) is None, 30, what="gateway takes back")
    took = time.monotonic() - killed
    assert took <= 2.5 * CYCLE_S, (took, _tail(tmp_path))
    after = _job(root, "after-kill", "ok.sh")
    assert _wait_for(lambda: _ran_by(root, after["id"]), 30, what="gateway runs it") == gw.pid
    # Restart: the gateway stops ticking the lane within one runner cycle of its first ADMITTED.
    runner = _spawn(procs, RUNNER_HARNESS, root, tmp_path / "runner.log")
    from cron.lanes import read_runner_heartbeat

    _wait_for(lambda: (read_runner_heartbeat(root) or {}).get("pid") == runner.pid
              and (read_runner_heartbeat(root) or {}).get("last_ok_at"), 60, what="runner back")
    back = time.monotonic()
    _wait_for(lambda: _last_lane(lanes_log) == "agent", 30, what="gateway delegates again")
    assert time.monotonic() - back <= 2.5 * CYCLE_S, _tail(tmp_path)
    again = _job(root, "after-restart", "ok.sh")
    assert _wait_for(lambda: _ran_by(root, again["id"]), 30, what="runner runs it") == runner.pid
    assert gw.poll() is None, _tail(tmp_path)
    assert _ac3_duplicates(root) == []


@pytest.mark.slow_test(180, reason="two real processes per variant")
@pytest.mark.parametrize("fault", ["emfile", "retirement"])
def test_ac4b_takeover_runner_failing_every_tick(root, tmp_path, procs, fault):
    """AC-4b: the runner keeps heartbeating ``active`` but every tick raises EMFILE (or retirement
    refuses every tick) -> the gateway runs the first due script within the progress bound + one
    cycle (240 virtual s), and not before the bound. Mutations: read ``hb.at`` -> never -> RED;
    ``last_ok_at`` on any non-raising return -> the retirement variant never takes back -> RED."""
    flag = tmp_path / "fault"
    gw, runner, lanes_log = _start_pair(procs, root, tmp_path,
                                        runner_extra={"RUNNER_HARNESS_FAULT": f"{fault}:{flag}"})
    flag.touch()
    faulted = time.monotonic()
    # Create the job only once a tick has hit the fault, so no in-flight healthy tick can take it.
    _wait_for(lambda: flag.with_name(flag.name + ".hit").exists(), 30, what="faulted tick")
    due = _job(root, "due", "ok.sh")
    pid = _wait_for(lambda: _ran_by(root, due["id"]), 60, what="first due script runs")
    took = time.monotonic() - faulted
    from cron.lanes import read_runner_heartbeat

    hb = read_runner_heartbeat(root)
    assert pid == gw.pid, (pid, hb, _tail(tmp_path))
    assert hb["state"] == "active" and hb["pid"] == runner.pid, hb  # alive, heartbeating
    assert (STALE_CYCLES - 2) * CYCLE_S <= took <= (STALE_CYCLES + 2.5) * CYCLE_S, (took, _tail(tmp_path))
    assert runner.poll() is None
    assert _ac3_duplicates(root) == []


@pytest.mark.slow_test(180, reason="two real processes")
def test_ac9_knob_false_mid_run_returns_lane_to_gateway(root, tmp_path, procs):
    """AC-9 unit / I8: knob false mid-run -> runner ``standby``, the gateway ticks ``"all"`` within
    one cycle, the next script row has the gateway's pid, no restart, no duplicate."""
    from cron.lanes import read_runner_heartbeat

    gw, runner, lanes_log = _start_pair(procs, root, tmp_path)
    _write_config(root, knob=False)
    flipped = time.monotonic()
    _wait_for(lambda: _last_lane(lanes_log) is None, 30, what="gateway ticks all")
    assert time.monotonic() - flipped <= 2.5 * CYCLE_S, _tail(tmp_path)
    _wait_for(lambda: (read_runner_heartbeat(root) or {}).get("state") == "standby", 10,
              what="runner standby")
    assert read_runner_heartbeat(root)["standby_reason"] == "knob_disabled"
    nxt = _job(root, "next", "ok.sh")
    assert _wait_for(lambda: _ran_by(root, nxt["id"]), 30, what="next row") == gw.pid
    assert runner.poll() is None and gw.poll() is None
    assert _ac3_duplicates(root) == []
