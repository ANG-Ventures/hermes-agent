"""child_timeout_seconds floor must clear the tool-activity heartbeat.

A leaf inside one silent tool call refreshes ``last_activity_ts`` only on the
tool-activity heartbeat (``agent/tool_executor.py``, 30 s) plus scheduling
overhead. With the old 30 s floor, any configured cap <= 30 s became exactly
the heartbeat interval: at the timeout check the live leaf looked idle for
>= child_timeout and was hard-stopped mid-tool (reproduced 3/3 at 1/10 scale,
t_448304ce). Real imports against a temp fleet home; only the agent is a stub.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def fleet_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("DELEGATION_CHILD_TIMEOUT_SECONDS", raising=False)
    return home


def _heartbeats():
    from agent.tool_executor import _TOOL_ACTIVITY_HEARTBEAT_INTERVAL_S
    from tools.delegate_tool import _HEARTBEAT_INTERVAL

    return float(_TOOL_ACTIVITY_HEARTBEAT_INTERVAL_S), float(_HEARTBEAT_INTERVAL)


@pytest.mark.parametrize("configured", [1, 29.9, 30, 45])
def test_config_floor_clears_every_heartbeat(fleet_home, monkeypatch, configured):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"child_timeout_seconds": configured})
    cap = delegate_tool._get_child_timeout()
    for hb in _heartbeats():
        assert cap >= 2 * hb, (configured, cap, hb)


def test_env_floor_clears_every_heartbeat(fleet_home, monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setenv("DELEGATION_CHILD_TIMEOUT_SECONDS", "5")
    cap = delegate_tool._get_child_timeout()
    for hb in _heartbeats():
        assert cap >= 2 * hb, (cap, hb)


def test_disabled_and_large_caps_unchanged(fleet_home, monkeypatch):
    from tools import delegate_tool

    for val, want in ((0, None), (-1, None), (600, 600.0)):
        monkeypatch.setattr(delegate_tool, "_load_config", lambda v=val: {"child_timeout_seconds": v})
        assert delegate_tool._get_child_timeout() == want, val


class _SilentToolLeaf:
    """A leaf one API call in, now inside one silent long tool call."""

    def __init__(self, last_activity_ts):
        self._subagent_id = "leaf-silent"
        self._parent_subagent_id = None
        self._last_activity_ts = last_activity_ts

    def get_activity_summary(self):
        return {
            "api_call_count": 1,
            "current_tool": "terminal",
            "last_activity_ts": self._last_activity_ts,
        }


def test_live_leaf_in_silent_tool_survives_min_cap_timeout(fleet_home, monkeypatch):
    """Cap at the floor, last tick one full heartbeat + overhead ago: still live."""
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"child_timeout_seconds": 1})
    cap = delegate_tool._get_child_timeout()
    tool_hb, _ = _heartbeats()
    # Worst case just before the next tool-activity tick lands.
    leaf = _SilentToolLeaf(time.time() - (tool_hb + 1.0))
    entry = delegate_tool._timed_out_running_entry(
        task_index=0,
        child=leaf,
        subagent_id=leaf._subagent_id,
        child_timeout=cap,
        duration=cap,
    )
    assert entry is not None, f"live leaf treated as hung at cap={cap}s (tool heartbeat {tool_hb}s)"


def test_leaf_silent_past_cap_is_still_hung(fleet_home, monkeypatch):
    """The floor must not blind the hang check: no tick for a full cap is hung."""
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"child_timeout_seconds": 1})
    cap = delegate_tool._get_child_timeout()
    leaf = _SilentToolLeaf(time.time() - (cap + 1.0))
    assert (
        delegate_tool._timed_out_running_entry(
            task_index=0, child=leaf, subagent_id=leaf._subagent_id, child_timeout=cap, duration=cap
        )
        is None
    )
