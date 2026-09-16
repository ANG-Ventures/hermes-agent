"""``--replace`` must wait the full graceful-stop budget before SIGKILL.

Incident 2026-09-16 (fork): a fixed 10s takeover grace SIGKILLed a draining
gateway mid-SQLite-write on every busy restart; ``state.db`` went malformed
days later. The wait now derives from the same budget systemd's
``TimeoutStopSec`` uses (#94759).
"""
from __future__ import annotations

import asyncio

import pytest

from gateway.restart import (
    CRON_DRAIN_CLEANUP_RESERVE_S,
    REPLACE_TAKEOVER_GRACE_FLOOR_S,
    REPLACE_TAKEOVER_GRACE_HEADROOM_S,
    resolve_replace_takeover_grace_s,
    resolve_systemd_timeout_stop_sec,
)


class TestResolveReplaceTakeoverGrace:
    def test_idle_default_is_headroom_only_and_above_the_old_ten_seconds(self):
        # drain=0, cron=0 → just the headroom for SQLite checkpoint+close.
        # Must still exceed the historical 10s cap that caused the incident.
        got = resolve_replace_takeover_grace_s(0, 0)
        assert got == max(REPLACE_TAKEOVER_GRACE_FLOOR_S, REPLACE_TAKEOVER_GRACE_HEADROOM_S)
        assert got > 10.0

    def test_drain_timeout_extends_the_grace(self):
        # The fleet value: 180s chat drain must not be cut at 10s.
        got = resolve_replace_takeover_grace_s(180, 0)
        assert got == 180 + REPLACE_TAKEOVER_GRACE_HEADROOM_S

    def test_cron_budget_wins_when_larger(self):
        got = resolve_replace_takeover_grace_s(30, 600)
        assert got == 600 + CRON_DRAIN_CLEANUP_RESERVE_S + REPLACE_TAKEOVER_GRACE_HEADROOM_S

    def test_agrees_with_systemd_model(self):
        # Same budget shape as TimeoutStopSec so both supervisors judge
        # "stuck" identically (only floor/headroom differ).
        for drain, cron in ((0, 0), (180, 0), (30, 600), (5, 5)):
            assert resolve_replace_takeover_grace_s(
                drain, cron, headroom_s=30.0, floor_s=60.0
            ) == float(resolve_systemd_timeout_stop_sec(drain, cron, headroom_s=30.0, floor_s=60.0))

    def test_garbage_degrades_like_idle_never_below_floor(self):
        got = resolve_replace_takeover_grace_s("nope", None)  # type: ignore[arg-type]
        assert got == resolve_replace_takeover_grace_s(0, 0)
        assert got >= REPLACE_TAKEOVER_GRACE_FLOOR_S


@pytest.mark.asyncio
async def test_replace_waits_past_ten_seconds_before_sigkill(monkeypatch, tmp_path):
    """RED without the fix: the old loop gave up at 20×0.5s and SIGKILLed a
    still-draining gateway. With the fix the wait honors the drain budget."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Configure a 40s drain: the grace must be ≥ 40s, i.e. > the old 10s cap.
    monkeypatch.setenv("HERMES_RESTART_DRAIN_TIMEOUT", "40")
    monkeypatch.setenv("HERMES_CRON_DRAIN_TIMEOUT", "0")

    from gateway.config import GatewayConfig
    import gateway.run as run_mod

    events = []
    # Virtual clock: each asyncio.sleep(0.5) advances 0.5s; the old gateway
    # "exits" 25s after SIGTERM (well inside a 40s drain, outside 10s).
    clock = {"t": 0.0}
    old_alive = {"v": True}
    EXIT_AT = 25.0

    async def _fake_sleep(s):
        clock["t"] += s
        if clock["t"] >= EXIT_AT:
            old_alive["v"] = False

    monkeypatch.setattr(run_mod.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(run_mod.time, "monotonic", lambda: clock["t"])

    class _Runner:
        def __init__(self, config):
            self.config = config
            self.should_exit_cleanly = True
            self.exit_reason = None
            self.exit_code = None
            self.adapters = {}

        async def start(self):
            return True

        async def stop(self):
            return None

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 42 if old_alive["v"] else None)
    monkeypatch.setattr("gateway.status.remove_pid_file", lambda: None)
    monkeypatch.setattr(
        "gateway.status._read_pid_record",
        lambda path=None: {
            "pid": 42, "kind": "hermes-gateway",
            "argv": ["python", "-m", "hermes_cli.main", "gateway", "run"],
            "start_time": 0, "hermes_home": str(tmp_path),
        },
    )
    monkeypatch.setattr("gateway.status._get_process_start_time", lambda pid: 0 if pid == 42 else None)
    monkeypatch.setattr("gateway.status.release_all_scoped_locks", lambda **kwargs: 0)
    monkeypatch.setattr("gateway.status._snapshot_gateway_children", lambda pid: [])
    monkeypatch.setattr("gateway.status.reap_gateway_children", lambda children, *, parent_pid, timeout=5.0: 0)

    def _terminate(pid, force=False):
        events.append(("terminate", pid, force, clock["t"]))
        if force:
            old_alive["v"] = False

    monkeypatch.setattr("gateway.status.terminate_pid", _terminate)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: old_alive["v"])
    monkeypatch.setattr("gateway.run.os.getpid", lambda: 100)
    monkeypatch.setattr("time.sleep", lambda _: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *a, **k: None)
    monkeypatch.setattr("gateway.run.GatewayRunner.__new__", lambda cls, *a, **k: _Runner(a[0] if a else None))

    ok = await run_mod.start_gateway(config=GatewayConfig(), replace=True, verbosity=None)
    assert ok is True
    forced = [e for e in events if e[2] is True]
    assert not forced, f"SIGKILL fired at t={forced[0][3]}s while old gateway was still inside its 40s drain"
    assert events and events[0][:3] == ("terminate", 42, False)
    assert old_alive["v"] is False  # it exited on its own at 25s
