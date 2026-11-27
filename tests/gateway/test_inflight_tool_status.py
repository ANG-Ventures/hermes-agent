"""One long tool call must stay visible in chat (t_90e5c823).

Ace, 2026-11-08: a delegate_task joined synchronously for 71 min showed "Working - 71 min" and nothing
else; the children's 78 API calls were invisible. The heartbeat now carries the in-flight call's elapsed
time and per-child activity, and the progress bubble gets one "still inside <tool>" line, edited in place.
"""

import asyncio
import queue
import threading
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest

from agent.i18n import t
from agent.session_activity import format_iteration_progress
from gateway.config import Platform
from gateway.inflight_tool_status import (
    format_duration,
    heartbeat_status_detail,
    record_delegate_child,
    still_inside_line,
)
from gateway.turn_context import TurnContext
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter

NOW = 1_800_000_000.0


def _child(session_id, calls, tool=None, tool_started=None, last_tool=None, last_ended=None):
    return SimpleNamespace(
        session_id=session_id, _api_call_count=calls, _current_tool=tool, _current_tool_started_at=tool_started,
        _last_tool_name=last_tool, _last_tool_ended_at=last_ended,
    )


def _parent(tool, elapsed, active=(), now=NOW):
    return SimpleNamespace(
        _current_tool=tool, _current_tool_started_at=(now - elapsed) if tool else None,
        _active_children=list(active), _active_children_lock=threading.Lock(), _delegate_children_log=[],
    )


def _legacy_detail(activity, want_iteration_detail):
    """The heartbeat detail exactly as gateway/run_turn.py built it before this change (oracle)."""
    detail = ""
    with suppress(Exception):
        if activity:
            parts = []
            if want_iteration_detail:
                parts.append(format_iteration_progress(activity["api_call_count"], activity["max_iterations"]))
            action = activity.get("current_tool") or activity.get("last_activity_desc")
            if action:
                parts.append(str(action))
            if parts:
                detail = " — " + ", ".join(parts)
    return detail


def _delegate_parent_with_three_children():
    running_a = _child("20261108_154800_af3bcd", 78, tool="terminal", tool_started=NOW - 40)
    running_b = _child("20261108_154801_99e0c1", 12, last_tool="read_file", last_ended=NOW - 5)
    finished = _child("20261108_154802_d0d0d0", 30)
    earlier_call = _child("20261108_140000_old000", 3)   # attached before this call started
    review_agent = _child("review_bg", 1)                # in _active_children but never a delegate child
    parent = _parent("delegate_task", 71 * 60, active=[running_a, running_b, review_agent])
    record_delegate_child(parent, earlier_call, now=NOW - 80 * 60)
    for child in (running_a, running_b, finished):
        record_delegate_child(parent, child, now=NOW - 70 * 60)
    return parent


def test_heartbeat_renders_inflight_delegate_with_three_children():
    parent = _delegate_parent_with_three_children()
    activity = {"api_call_count": 79, "max_iterations": 300, "current_tool": "delegate_task",
                "last_activity_desc": "executing tool: delegate_task"}
    detail = heartbeat_status_detail(activity, parent, True, now=NOW)
    assert t("gateway.progress.working_heartbeat", minutes=71, detail=detail) == (
        "⏳ Working — 71 min — iteration 79/300, delegate_task 71m - 3 children: 1 done, 2 running "
        "(child af3bcd: API call #78, in terminal 40s)"
    )


def test_heartbeat_child_between_tools_shows_last_tool():
    child = _child("sess_abc123", 4, last_tool="read_file", last_ended=NOW - 40)
    parent = _parent("delegate_task", 10 * 60, active=[child])
    record_delegate_child(parent, child, now=NOW - 9 * 60)
    detail = heartbeat_status_detail({"api_call_count": 2, "max_iterations": 300}, parent, False, now=NOW)
    assert detail == " — delegate_task 10m - 1 child: 0 done, 1 running (child abc123: API call #4, last tool read_file 40s ago)"


def test_heartbeat_long_plain_tool_shows_elapsed_only():
    parent = _parent("terminal", 12 * 60 + 5)
    detail = heartbeat_status_detail({"api_call_count": 5, "max_iterations": 300, "current_tool": "terminal"},
                                     parent, True, now=NOW)
    assert detail == " — iteration 5/300, terminal 12m"


@pytest.mark.parametrize("activity,parent,want_iter", [
    # short call: < 60 s in flight -> today's line, unchanged
    ({"api_call_count": 5, "max_iterations": 300, "current_tool": "terminal", "last_activity_desc": "x"},
     _parent("terminal", 30), True),
    ({"api_call_count": 5, "max_iterations": 300, "current_tool": "terminal"}, _parent("terminal", 59), False),
    # no call in flight
    ({"api_call_count": 9, "max_iterations": 300, "current_tool": None, "last_activity_desc": "tool completed: x"},
     _parent(None, 0), True),
    # agent without the new stamps (older agent / fake)
    ({"api_call_count": 1, "max_iterations": 300, "current_tool": "web_search"}, SimpleNamespace(), True),
    ({}, None, True),
])
def test_heartbeat_short_or_no_call_is_unchanged(activity, parent, want_iter):
    assert heartbeat_status_detail(activity, parent, want_iter, now=NOW) == _legacy_detail(activity, want_iter)


def test_short_call_heartbeat_text_literal():
    detail = heartbeat_status_detail({"api_call_count": 5, "max_iterations": 300, "current_tool": "terminal"},
                                     _parent("terminal", 30), True, now=NOW)
    assert t("gateway.progress.working_heartbeat", minutes=3, detail=detail) == "⏳ Working — 3 min — iteration 5/300, terminal"


def test_still_inside_line_threshold():
    parent = _delegate_parent_with_three_children()
    assert still_inside_line(parent, 300, now=NOW) == (
        "⏳ still inside delegate_task for 71m - 3 children: 1 done, 2 running (child af3bcd: API call #78, in terminal 40s)"
    )
    assert still_inside_line(_parent("terminal", 299), 300, now=NOW) is None
    assert still_inside_line(_parent("terminal", 301), 300, now=NOW) == "⏳ still inside terminal for 5m"
    assert still_inside_line(parent, 0, now=NOW) is None  # knob off


def test_delegate_child_log_is_bounded():
    parent = SimpleNamespace()
    for i in range(300):
        record_delegate_child(parent, SimpleNamespace(session_id=f"s{i}"), now=float(i))
    assert len(parent._delegate_children_log) == 256
    assert parent._delegate_children_log[-1] == (299.0, "s299")


def test_format_duration():
    assert [format_duration(s) for s in (0, 59, 60, 3599, 4260, 7500)] == ["0s", "59s", "1m", "59m", "71m", "125m"]


def test_attach_child_records_on_parent():
    from tools.delegate_tool_child_run import _attach_child

    parent = SimpleNamespace(_active_children=[], _active_children_lock=threading.Lock(), _interrupt_requested=False)
    child = SimpleNamespace(session_id="sess_1")
    _attach_child(parent, child)
    assert parent._active_children == [child]
    assert [k for _, k in parent._delegate_children_log] == ["sess_1"]


# ── progress bubble: one "still inside" line, edited in place ────────────────────────────────

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


def _bubble(monkeypatch, agent, threshold=300):
    import gateway.run_turn_runner as rtr
    from gateway.run_turn_runner import TurnRunner

    clock = _Clock()
    monkeypatch.setattr(rtr, "time", clock)
    adapter = ProgressCaptureAdapter(Platform.DISCORD)
    ctx = TurnContext(source=SimpleNamespace(chat_id="c", platform=Platform.DISCORD), progress_queue=queue.Queue(),
                      _run_still_current=lambda: True, progress_grouping="accumulate")
    ctx.agent_holder = [agent]
    ctx.user_config = {}
    ctx.resolve_display_setting = lambda cfg, pk, key: threshold if key == "tool_inflight_notice_seconds" else None
    tr = TurnRunner(SimpleNamespace(_delivery_adapter_for=lambda source: adapter), ctx)
    st = tr._progress_edit_state(adapter)
    st.progress_lines = ['🔀 delegate_task: "3 tasks"']
    st.progress_msg_id = "progress-1"
    return tr, st, adapter, clock


@pytest.mark.asyncio
async def test_bubble_still_inside_line_edits_in_place(monkeypatch):
    child = _child("sess_af3bcd", 7, tool="terminal", tool_started=time.time() - 10)
    agent = _parent("delegate_task", 6 * 60 + 5, active=[child], now=time.time())
    record_delegate_child(agent, child)
    tr, st, adapter, clock = _bubble(monkeypatch, agent)

    await tr._paint_inflight_notice(st)
    assert adapter.sent == []
    assert len(adapter.edits) == 1 and adapter.edits[0]["message_id"] == "progress-1"
    first = adapter.edits[0]["content"].split("\n")
    assert first[0] == '🔀 delegate_task: "3 tasks"'
    assert first[1].startswith("⏳ still inside delegate_task for 6m - 1 child: 0 done, 1 running (child af3bcd: API call #7")

    clock.t += 30  # inside the repaint window: no edit
    await tr._paint_inflight_notice(st)
    assert len(adapter.edits) == 1

    clock.t += 31  # next repaint: same message, line replaced (not appended)
    agent._current_tool_started_at -= 60
    child._api_call_count = 9
    await tr._paint_inflight_notice(st)
    assert adapter.sent == [] and len(adapter.edits) == 2
    lines = adapter.edits[1]["content"].split("\n")
    assert len(lines) == 2 and sum("still inside" in ln for ln in lines) == 1
    assert lines[1].startswith("⏳ still inside delegate_task for 7m") and "API call #9" in lines[1]

    agent._current_tool = None  # call finished: the line is removed again
    await tr._paint_inflight_notice(st)
    assert adapter.edits[-1]["content"] == '🔀 delegate_task: "3 tasks"'
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_bubble_real_progress_line_supersedes_still_inside(monkeypatch):
    agent = _parent("terminal", 400, now=time.time())
    tr, st, adapter, _ = _bubble(monkeypatch, agent)
    await tr._paint_inflight_notice(st)
    assert st.progress_lines[-1].startswith("⏳ still inside terminal for 6m")
    tr._progress_absorb(st, "📖 read_file: x.py")
    assert st.progress_lines == ['🔀 delegate_task: "3 tasks"', "📖 read_file: x.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed,threshold", [(120, 300), (900, 0)])
async def test_bubble_short_call_or_knob_off_paints_nothing(monkeypatch, elapsed, threshold):
    agent = _parent("terminal", elapsed, now=time.time())
    tr, st, adapter, _ = _bubble(monkeypatch, agent, threshold=threshold)
    await tr._paint_inflight_notice(st)
    assert adapter.edits == [] and adapter.sent == []
    assert st.progress_lines == ['🔀 delegate_task: "3 tasks"']


@pytest.mark.asyncio
async def test_bubble_holding_only_the_notice_is_not_left_stale(monkeypatch):
    """Prism 9c6bdb4bcd34: the notice painted into an EMPTY bubble; when the call ends, removing it empties
    the buffer and _flush_progress_edit skips empty buffers, so the notice used to stay on screen."""
    agent = _parent("terminal", 400, now=time.time())
    tr, st, adapter, clock = _bubble(monkeypatch, agent)
    st.progress_lines = []
    await tr._paint_inflight_notice(st)
    assert adapter.edits[-1]["content"].startswith("⏳ still inside terminal for 6m")

    clock.t += 5
    agent._current_tool = None
    await tr._paint_inflight_notice(st)
    final = adapter.edits[-1]["content"]
    assert "still inside" not in final, final
    assert final.startswith("✓ terminal done ("), final


class _AsyncioCancelOnSleep:
    """asyncio stand-in whose sleep() raises CancelledError: the turn ending during the 0.3 s idle wait."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def sleep(self, *_a, **_k):
        raise self._real.CancelledError()


@pytest.mark.asyncio
async def test_cancel_during_idle_sleep_still_retires_the_notice(monkeypatch):
    """Prism 3e31f1099cb9: a cancel raised inside the `except queue.Empty` block skipped the sibling
    CancelledError clause, so the drain never ran and the notice outlived the turn."""
    import asyncio as real_asyncio

    import gateway.run_turn_runner as rtr

    agent = _parent("terminal", 400, now=time.time())
    tr, st, adapter, clock = _bubble(monkeypatch, agent)
    monkeypatch.setattr(rtr, "asyncio", _AsyncioCancelOnSleep(real_asyncio))
    seeded = tr._progress_edit_state
    monkeypatch.setattr(tr, "_progress_edit_state", lambda _a: st)   # the loop uses the seeded bubble
    assert seeded is not None

    with pytest.raises(real_asyncio.CancelledError):   # the cancel still propagates to the awaiting caller
        await tr.send_progress_messages()

    final = adapter.edits[-1]["content"]
    assert "still inside" not in final, final


# ── the real heartbeat loop (gateway/run_turn.py::_run_agent_notify_long_running) ──────────────

async def _one_heartbeat(monkeypatch, agent, activity):
    from unittest.mock import AsyncMock, MagicMock

    from tests.gateway.test_wake_progress_header import _bare_runner

    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "0.01")
    runner = _bare_runner()
    sends = []

    async def _send(chat_id, content, metadata=None):
        sends.append(content)
        runner._restart_requested = True  # one heartbeat, then stop
        return SimpleNamespace(success=True, message_id="hb-1")

    adapter = MagicMock()
    adapter.send = _send
    adapter.edit_message = AsyncMock()
    runner._delivery_adapter_for = lambda source: adapter
    runner._agent_activity_summary = staticmethod(lambda a: activity)
    runner._running_agents["sess"] = agent
    disp = MagicMock()
    disp._display_surface_mode.return_value = "on"
    disp.resolve_display_setting.return_value = True
    ctx = TurnContext(source=SimpleNamespace(chat_id="c", platform="discord"), session_key="sess")
    ctx.agent_holder[0] = agent
    await asyncio.wait_for(runner._run_agent_notify_long_running(disp, ctx, [None]), 5)
    assert len(sends) == 1
    return sends[0]


@pytest.mark.asyncio
async def test_heartbeat_loop_shows_inflight_delegate_children(monkeypatch):
    now = time.time()
    child = _child("20261108_154800_af3bcd", 78, tool="terminal", tool_started=now - 40)
    agent = _parent("delegate_task", 71 * 60, active=[child], now=now)
    record_delegate_child(agent, child, now=now - 70 * 60)
    text = await _one_heartbeat(monkeypatch, agent, {"api_call_count": 79, "max_iterations": 300,
                                                     "current_tool": "delegate_task"})
    assert text.startswith("⏳ Working — 0 min — iteration 79/300, delegate_task 71m - 1 child: 0 done, 1 running ")
    assert "(child af3bcd: API call #78, in terminal 4" in text


@pytest.mark.asyncio
async def test_heartbeat_loop_short_call_unchanged(monkeypatch):
    agent = _parent("terminal", 20, now=time.time())
    text = await _one_heartbeat(monkeypatch, agent, {"api_call_count": 5, "max_iterations": 300,
                                                     "current_tool": "terminal"})
    assert text == "⏳ Working — 0 min — iteration 5/300, terminal"
