"""t_3c08d829: the runner's bounded code-skew drain holds for a home-reconcile land before it kills.

A kill inside the tick's live-tree write leaves the land-killed latch (2026-11-11 09:05 and 15:08 on
the Studio). Before the bounded kill the runner starts the fleet helper ``land_wait.py default
--owner <runner pid>`` (non-blocking) and proceeds once it has exited. A missing, failing or hung
helper never blocks the drain.

The helper here is a stand-in with the same contract: it records its argv, waits while the
marker file exists and exits 0 (or the exit code / hang a case asks for).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="process groups")

_FAKE_HELPER = """\
import os, sys, time
from pathlib import Path
d = Path(sys.argv[0]).parent
owner = sys.argv[sys.argv.index("--owner") + 1]
record = d / f"restart-pending.{owner}.json"
if "--release" in sys.argv:
    (d / "release-argv").write_text(" ".join(sys.argv[1:]))
    record.unlink(missing_ok=True)
    sys.exit(0)
(d / "argv").write_text(" ".join(sys.argv[1:]))
record.write_text("{}")
mode = os.environ.get("FAKE_LAND_MODE", "wait")
if mode == "hang":
    while True:
        time.sleep(1)
if mode == "fail":
    print("boom")
    sys.exit(3)
marker = d / "land-inflight.1.x"
held = marker.exists()
while marker.exists():
    time.sleep(0.05)
if held:
    print("PHASE=land_wait_done default: land finished; proceeding")
sys.exit(0)
"""


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from cron import lanes, script_runner

    home = tmp_path / "home" / ".hermes"
    (home / "cron").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home.parent))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("SGR_LAND_WAIT_SECS", raising=False)
    monkeypatch.delenv("FAKE_LAND_MODE", raising=False)
    helper = tmp_path / "land_wait.py"
    helper.write_text(_FAKE_HELPER, encoding="utf-8")
    clock = {"t": 1000.0}
    monkeypatch.setattr(script_runner, "time", types.SimpleNamespace(
        monotonic=lambda: clock["t"], time=time.time, sleep=time.sleep))
    monkeypatch.setattr(script_runner, "_detect_code_skew", lambda: ("aaaa", "bbbb"))
    monkeypatch.setattr(script_runner, "_inflight_count", lambda: 1)
    monkeypatch.setattr(script_runner, "_inflight_refire_status", lambda: ([], set()))
    monkeypatch.setattr(script_runner, "_inflight_finish_requested", lambda: [])
    monkeypatch.setattr(script_runner, "_land_wait_helper", lambda: helper)
    monkeypatch.setattr(script_runner.ScriptRunner, "_check_host", lambda self: None)
    stop = threading.Event()
    r = script_runner.ScriptRunner(profile_homes=lambda: [])
    r.attach_stop(stop)
    r.enumerate_homes()
    assert r.draining and not stop.is_set()
    clock["t"] += float(lanes.RUNNER_SKEW_DRAIN_MAX_S)
    ns = types.SimpleNamespace(r=r, stop=stop, clock=clock, helper=helper,
                               marker=tmp_path / "land-inflight.1.x", dir=tmp_path)
    yield ns
    r.end_land_hold()


def _bounded(ns) -> bool:
    return bool(ns.r._skew_bounded and ns.stop.is_set() and ns.r.sigterm)


def _poll_until(ns, cond, timeout=15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ns.r.poll_skew_bound()
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_live_land_marker_holds_the_kill_until_it_goes(rig):
    rig.marker.touch()
    rig.r.poll_skew_bound()  # bound crossed: starts the hold, kills nothing
    assert not _bounded(rig)
    assert _poll_until(rig, lambda: (rig.dir / "argv").exists())
    assert (rig.dir / "argv").read_text() == f"default --owner {os.getpid()}"
    for _ in range(20):  # 1 s with the marker up: the poll keeps returning without a kill
        rig.r.poll_skew_bound()
        time.sleep(0.05)
    assert not _bounded(rig) and not rig.r.sigterm
    rig.marker.unlink()
    assert _poll_until(rig, lambda: _bounded(rig))


def test_ticker_path_also_holds(rig):
    """The ticker's ``enumerate_homes`` -> ``_check_skew`` -> ``_check_skew_bound`` path holds too."""
    rig.marker.touch()
    rig.r.enumerate_homes()
    assert not _bounded(rig) and rig.r._land_hold is not None
    rig.r.enumerate_homes()
    assert not _bounded(rig)
    rig.marker.unlink()
    deadline = time.monotonic() + 15
    while not _bounded(rig) and time.monotonic() < deadline:
        rig.r.enumerate_homes()
        time.sleep(0.05)
    assert _bounded(rig)


def test_no_marker_adds_no_wait(rig):
    started = time.monotonic()
    rig.r.poll_skew_bound()
    assert _poll_until(rig, lambda: _bounded(rig), timeout=10)
    assert time.monotonic() - started < 10


def test_missing_helper_proceeds_as_before(rig, monkeypatch, caplog):
    from cron import script_runner

    monkeypatch.setattr(script_runner, "_land_wait_helper", lambda: rig.dir / "absent.py")
    with caplog.at_level(logging.WARNING, logger="cron.script_runner"):
        rig.r.poll_skew_bound()
    assert _bounded(rig)
    assert "land hold skipped: no helper" in caplog.text


def test_helper_start_failure_proceeds(rig, monkeypatch, caplog):
    from cron import script_runner

    def boom(*_a, **_k):
        raise OSError("spawn refused")

    monkeypatch.setattr(script_runner.subprocess, "Popen", boom)
    with caplog.at_level(logging.WARNING, logger="cron.script_runner"):
        rig.r.poll_skew_bound()
    assert _bounded(rig)
    assert "helper start failed" in caplog.text


def test_nonzero_helper_proceeds(rig, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_LAND_MODE", "fail")
    with caplog.at_level(logging.WARNING, logger="cron.script_runner"):
        rig.r.poll_skew_bound()
        assert _poll_until(rig, lambda: _bounded(rig))
    assert "land hold helper exited 3" in caplog.text


def test_hung_helper_is_cut_at_its_bound(rig, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_LAND_MODE", "hang")
    monkeypatch.setenv("SGR_LAND_WAIT_SECS", "60")
    with caplog.at_level(logging.WARNING, logger="cron.script_runner"):
        rig.r.poll_skew_bound()
        hold = rig.r._land_hold
        rig.r.poll_skew_bound()
        assert not _bounded(rig)
        rig.clock["t"] += 60 + 30  # SGR_LAND_WAIT_SECS + slack
        rig.r.poll_skew_bound()
    assert _bounded(rig)
    assert hold.wait(5) is not None  # the hung helper was killed
    assert "still running after 90 s; killed it, proceeding" in caplog.text


def test_exit_kills_a_running_hold(rig):
    """The drain ended without a kill (in-flight went to 0): ``main`` kills the helper on exit."""
    rig.marker.touch()
    rig.r.poll_skew_bound()
    hold = rig.r._land_hold
    assert hold.poll() is None
    rig.r.end_land_hold()
    assert hold.wait(5) is not None


# --- round 2: main() removes the restart-pending record on exit -----------------------------------


def _run_main(rig, monkeypatch, *, ticker) -> tuple:
    """Run ``script_runner.main`` around the rig's runner with a stand-in ticker thread body.
    Returns (exit code, ordered list of exit steps)."""
    import signal as _signal

    from cron import scheduler_provider, script_runner

    steps: list = []
    monkeypatch.setattr(script_runner, "_setup_logging", lambda: None)
    monkeypatch.setattr(script_runner, "_bootstrap_env", lambda: None)
    monkeypatch.setattr(scheduler_provider, "resolve_cron_scheduler",
                        lambda: scheduler_provider.InProcessCronScheduler())
    monkeypatch.setattr(scheduler_provider.InProcessCronScheduler, "start",
                        lambda self, stop, **_k: ticker(stop))
    monkeypatch.setattr(script_runner, "ScriptRunner", lambda: rig.r)
    monkeypatch.setattr(_signal, "signal", lambda *_a: None)
    monkeypatch.setattr(rig.r, "shutdown", lambda: (steps.append("shutdown"), 0)[1])
    real_release = rig.r.release_land_hold

    def release():
        steps.append("release")
        real_release()

    monkeypatch.setattr(rig.r, "release_land_hold", release)
    return script_runner.main([]), steps


def _record(rig):
    return rig.dir / f"restart-pending.{os.getpid()}.json"


def test_held_drain_kill_releases_the_record_after_the_kill(rig, monkeypatch):
    """Held drain -> marker goes -> bounded kill -> shutdown() -> release, in that order."""
    rig.marker.touch()

    def ticker(stop):
        rig.r.poll_skew_bound()  # starts the hold
        deadline = time.monotonic() + 15
        while not _record(rig).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        rig.marker.unlink()
        stop.wait(30)

    rc, steps = _run_main(rig, monkeypatch, ticker=ticker)
    assert rc == 0 and rig.r._skew_bounded
    assert steps == ["shutdown", "release"]
    assert (rig.dir / "release-argv").read_text() == f"default --release --owner {os.getpid()}"
    assert not _record(rig).exists()


def test_nothing_in_flight_exit_with_running_hold_releases(rig, monkeypatch):
    """The drain ends with nothing in flight while the helper still holds: main kills the helper,
    then releases the record it already published."""
    from cron import script_runner

    rig.marker.touch()

    def ticker(stop):
        rig.r.poll_skew_bound()
        deadline = time.monotonic() + 15
        while not _record(rig).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        monkeypatch.setattr(script_runner, "_inflight_count", lambda: 0)
        rig.r.enumerate_homes()  # _check_skew: in flight 0 -> exit 0, no kill
        stop.wait(30)

    hold_box: list = []
    orig_end = rig.r.end_land_hold

    def end():
        hold_box.append(rig.r._land_hold)
        orig_end()

    monkeypatch.setattr(rig.r, "end_land_hold", end)
    rc, steps = _run_main(rig, monkeypatch, ticker=ticker)
    assert rc == 0 and not rig.r._skew_bounded
    assert hold_box and hold_box[0].poll() is not None  # helper killed and reaped
    assert steps == ["release"]
    assert (rig.dir / "release-argv").read_text() == f"default --release --owner {os.getpid()}"
    assert not _record(rig).exists()
    rig.marker.unlink()


def test_no_hold_started_no_release_call(rig, monkeypatch):
    """A runner that never held (no bound crossed) calls no helper on exit."""
    from cron import script_runner

    monkeypatch.setattr(script_runner, "_inflight_count", lambda: 0)

    def ticker(stop):
        rig.r.enumerate_homes()
        stop.wait(30)

    rc, steps = _run_main(rig, monkeypatch, ticker=ticker)
    assert rc == 0 and steps == ["release"]
    assert not (rig.dir / "release-argv").exists()


def test_release_failure_never_fails_the_exit(rig, monkeypatch, caplog):
    from cron import script_runner

    rig.r._land_hold_spawned = True

    def boom(*_a, **_k):
        raise script_runner.subprocess.TimeoutExpired("land_wait.py", 10)

    monkeypatch.setattr(script_runner.subprocess, "run", boom)
    with caplog.at_level(logging.WARNING, logger="cron.script_runner"):
        rig.r.release_land_hold()
    assert "land hold release failed (TimeoutExpired" in caplog.text
