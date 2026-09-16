"""``--replace`` must wait the old gateway's full graceful-stop budget before SIGKILL.

A fixed 10s takeover grace force-killed a draining gateway mid-SQLite-write on every busy
restart (``restart_drain_timeout`` can be minutes); ``state.db`` later reported ``database disk
image is malformed``. The wait now derives from the same budget systemd's ``TimeoutStopSec``
uses (``resolve_systemd_timeout_stop_sec``).
"""
from __future__ import annotations

import pytest

from gateway.restart import (
    CRON_DRAIN_CLEANUP_RESERVE_S,
    REPLACE_TAKEOVER_GRACE_FLOOR_S,
    REPLACE_TAKEOVER_GRACE_HEADROOM_S,
    resolve_replace_takeover_grace_s,
    resolve_systemd_timeout_stop_sec,
)


class TestResolveReplaceTakeoverGrace:
    def test_idle_default_exceeds_the_old_ten_second_cap(self):
        got = resolve_replace_takeover_grace_s(0, 0)
        assert got == max(REPLACE_TAKEOVER_GRACE_FLOOR_S, REPLACE_TAKEOVER_GRACE_HEADROOM_S)
        assert got > 10.0

    def test_drain_timeout_extends_the_grace(self):
        assert resolve_replace_takeover_grace_s(180, 0) == 180 + REPLACE_TAKEOVER_GRACE_HEADROOM_S

    def test_cron_budget_wins_when_larger(self):
        got = resolve_replace_takeover_grace_s(30, 600)
        assert got == 600 + CRON_DRAIN_CLEANUP_RESERVE_S + REPLACE_TAKEOVER_GRACE_HEADROOM_S

    def test_agrees_with_systemd_model(self):
        for drain, cron in ((0, 0), (180, 0), (30, 600), (5, 5)):
            assert resolve_replace_takeover_grace_s(
                drain, cron, headroom_s=30.0, floor_s=60.0
            ) == float(resolve_systemd_timeout_stop_sec(drain, cron, headroom_s=30.0, floor_s=60.0))

    def test_garbage_degrades_like_idle(self):
        got = resolve_replace_takeover_grace_s("nope", None)  # type: ignore[arg-type]
        assert got == resolve_replace_takeover_grace_s(0, 0)
        assert got >= REPLACE_TAKEOVER_GRACE_FLOOR_S


@pytest.mark.asyncio
async def test_replace_waits_past_ten_seconds_before_sigkill(monkeypatch, tmp_path):
    """RED on the old code: SIGKILL after 20 x 0.5s while the old gateway is still inside a
    40s drain. GREEN with the fix: the wait honors the drain budget and the old gateway exits
    on its own."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_RESTART_DRAIN_TIMEOUT", "40")
    monkeypatch.setenv("HERMES_CRON_DRAIN_TIMEOUT", "0")

    import gateway.run as run_mod

    events = []
    # Virtual clock: each asyncio.sleep(d) advances d; the old gateway exits 25s after SIGTERM
    # (inside a 40s drain, outside the old 10s cap).
    clock = {"t": 0.0}
    old_alive = {"v": True}

    async def _fake_sleep(d):
        clock["t"] += d
        if clock["t"] >= 25.0:
            old_alive["v"] = False

    monkeypatch.setattr(run_mod.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: old_alive["v"])
    monkeypatch.setattr("gateway.status._get_process_start_time", lambda pid: 0)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 0, raising=False)
    monkeypatch.setattr("gateway.status.write_takeover_marker", lambda pid: None)
    monkeypatch.setattr("gateway.status._snapshot_gateway_children", lambda pid: [])
    monkeypatch.setattr("gateway.status.reap_gateway_children",
                        lambda children, *, parent_pid, timeout=5.0: 0)
    monkeypatch.setattr("gateway.status.remove_pid_file", lambda: None)
    monkeypatch.setattr("gateway.status.release_all_scoped_locks", lambda **kwargs: 0)
    monkeypatch.setattr(
        "gateway.status._read_pid_record",
        lambda path=None: {
            "pid": 42, "kind": "hermes-gateway",
            "argv": ["python", "-m", "hermes_cli.main", "gateway", "run"],
            "start_time": 0, "hermes_home": str(tmp_path),
        },
    )

    def _terminate(pid, force=False, **kwargs):
        events.append(("terminate", pid, force, clock["t"]))
        if force:
            old_alive["v"] = False

    monkeypatch.setattr("gateway.status.terminate_pid", _terminate)
    monkeypatch.setattr("gateway.run.os.getpid", lambda: 100)

    ok = await run_mod._start_gateway_replace_existing_instance(42, replace=True)
    assert ok is True
    forced = [e for e in events if e[2] is True]
    assert not forced, (
        f"SIGKILL fired at t={forced[0][3]}s while old gateway was still inside its 40s drain"
    )
    assert events and events[0][:3] == ("terminate", 42, False)
    assert old_alive["v"] is False
