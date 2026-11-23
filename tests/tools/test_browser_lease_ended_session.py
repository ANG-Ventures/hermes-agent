"""t_f68d74f9: after a lease ends, no agent-browser may start against that lease's CDP URL.

nas-fleet 2026-11-23 22:46:24Z: 44 s after lease 14856 was released, the run's end-of-task ``cleanup_browser`` sent
``close`` with the session's stale ``--cdp http://127.0.0.1:35263``. The URL was no longer a live lease, so it took the
non-lease path: the SHARED socket dir, session "default", a screen DISPLAY. A new lease on that ephemeral port would
then be driven by that stale daemon.
"""
from __future__ import annotations

import pytest

from tools import browser_lease
from tools import browser_tool as bt
from tools import browser_tool_cdp as cdp
from tools import browser_tool_lifecycle as lifecycle
from tools import browser_tool_session as bts

LEASE = "http://127.0.0.1:35263"


@pytest.fixture
def lease_session(monkeypatch):
    live = {"url": LEASE}
    monkeypatch.setattr(browser_lease, "active_url", lambda: live["url"])
    spawned: list = []
    monkeypatch.setattr(bts, "_spawn_and_collect", lambda *a, **k: spawned.append(a) or {"success": True, "data": {}})
    monkeypatch.setattr(bts, "_browser_command_preflight", lambda: {"browser_cmd": "agent-browser"})
    monkeypatch.setattr(cdp, "_ensure_cdp_supervisor", lambda task_id: None)
    monkeypatch.setattr(cdp, "_stop_cdp_supervisor", lambda task_id: None)
    monkeypatch.setattr(lifecycle, "_release_session_resources", lambda task_id, info: bt._active_sessions.pop(task_id, None))
    info = bts._create_cdp_session("t_x", "ws://127.0.0.1:35263/devtools/browser/abc")
    monkeypatch.setitem(bt._active_sessions, "t_x", info)
    return live, spawned


def test_cleanup_after_lease_end_starts_no_agent_browser(lease_session):
    live, spawned = lease_session
    live["url"] = ""                       # release_lease / close_lease: the lease is gone
    lifecycle.cleanup_browser("t_x")       # the run's end-of-task close (browser_tool_lifecycle.cleanup_browser)
    assert spawned == []
    assert "t_x" not in bt._active_sessions


def test_command_on_an_ended_lease_session_is_refused(lease_session):
    live, spawned = lease_session
    live["url"] = ""
    out = bts._run_browser_command("t_x", "snapshot", [])
    assert out["success"] is False and "BROWSER_LEASE_ENDED" in out["error"]
    assert spawned == []


def test_live_lease_session_still_runs(lease_session):
    _live, spawned = lease_session
    assert bts._run_browser_command("t_x", "snapshot", [])["success"] is True
    assert len(spawned) == 1
