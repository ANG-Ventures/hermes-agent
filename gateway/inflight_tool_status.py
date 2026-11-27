"""In-flight tool visibility for the gateway heartbeat and progress bubble (t_90e5c823).

The progress bubble paints only on a NEW tool call and the heartbeat showed only "iteration i/N, <tool>",
so one long tool call (a synchronously joined ``delegate_task``, a 10-min test gate) looked like a hang for
its whole duration. These pure renderers read the parent agent's in-memory state:

* ``agent._current_tool`` / ``agent._current_tool_started_at`` (stamped by ``agent.tool_executor``);
* ``agent._active_children`` (live delegate children, under ``_active_children_lock``) and
  ``agent._delegate_children_log`` (``(attached_at, key)`` per child, appended by
  ``tools.delegate_tool_child_run._attach_child``) to count finished children of the current call.

In-memory, not SessionDB: the heartbeat runs in the same process as the synchronous join, the child agents
are live objects there, and a SessionDB read every tick would add load to a contended DB for data that is
already in RAM. A background delegation returns its tool call immediately, so it never has an in-flight call
to render.
"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any, Optional

# Below this, a call is "short": the heartbeat line stays exactly as before.
HEARTBEAT_INFLIGHT_MIN_SECONDS = 60.0
# Default for display.tool_inflight_notice_seconds (progress-bubble "still inside" line; 0 = off).
DEFAULT_INFLIGHT_NOTICE_SECONDS = 300
# Re-edit cadence for the "still inside" line (an edit, never a new message).
INFLIGHT_NOTICE_REPAINT_SECONDS = 60.0
_DELEGATE_LOG_MAX = 256


def format_duration(seconds: float) -> str:
    """``45s`` / ``71m`` (plain minutes, matching the heartbeat's own ``N min``)."""
    s = max(0, int(seconds))
    return f"{s}s" if s < 60 else f"{s // 60}m"


def child_key(child: Any) -> str:
    """Stable identity for a delegate child: its session id, else ``id()``."""
    sid = getattr(child, "session_id", None)
    return str(sid) if sid else f"obj{id(child)}"


def record_delegate_child(parent: Any, child: Any, now: Optional[float] = None) -> None:
    """Note a child's attach time on the parent (bounded; never raises)."""
    with suppress(Exception):
        log = getattr(parent, "_delegate_children_log", None)
        if not isinstance(log, list):
            log = []
            parent._delegate_children_log = log
        log.append((float(now if now is not None else time.time()), child_key(child)))
        if len(log) > _DELEGATE_LOG_MAX:
            del log[: len(log) - _DELEGATE_LOG_MAX]


def inflight_tool(agent: Any, now: Optional[float] = None) -> Optional[tuple[str, float]]:
    """``(tool_name, elapsed_seconds)`` for the agent's in-flight tool call, or None."""
    if agent is None:
        return None
    name = getattr(agent, "_current_tool", None)
    started = getattr(agent, "_current_tool_started_at", None)
    if not name or not isinstance(started, (int, float)):
        return None
    clock = float(now if now is not None else time.time())
    return str(name), max(0.0, clock - float(started))


def _child_detail(child: Any, now: float) -> tuple[int, str]:
    """``(api_calls, "child af3bcd: API call #78, in terminal 40s")`` for one running child."""
    key = child_key(child)
    short = key[-6:] if not key.startswith("obj") else key[3:][-6:]
    calls = 0
    with suppress(Exception):
        calls = int(getattr(child, "_api_call_count", 0) or 0)
    parts = [f"API call #{calls}"]
    cur = inflight_tool(child, now)
    if cur:
        parts.append(f"in {cur[0]} {format_duration(cur[1])}")
    else:
        last_name = getattr(child, "_last_tool_name", None)
        last_at = getattr(child, "_last_tool_ended_at", None)
        if last_name and isinstance(last_at, (int, float)):
            parts.append(f"last tool {last_name} {format_duration(now - float(last_at))} ago")
    return calls, f"child {short}: " + ", ".join(parts)


def delegate_children_summary(agent: Any, started_at: float, now: Optional[float] = None) -> Optional[str]:
    """``3 children: 1 done, 2 running (child af3bcd: API call #78, ...)`` for children attached since
    ``started_at`` (the in-flight call's start), or None when the call has spawned none."""
    clock = float(now if now is not None else time.time())
    log = getattr(agent, "_delegate_children_log", None) or []
    with suppress(Exception):
        keys = {k for ts, k in list(log) if ts >= started_at}
        if not keys:
            return None
        lock = getattr(agent, "_active_children_lock", None)
        active = getattr(agent, "_active_children", None) or []
        if lock is not None:
            with lock:
                active = list(active)
        else:
            active = list(active)
        running = [c for c in active if child_key(c) in keys]
        total = len(keys)
        done = max(0, total - len(running))
        noun = "child" if total == 1 else "children"
        text = f"{total} {noun}: {done} done, {len(running)} running"
        if running:
            busiest = max((_child_detail(c, clock) for c in running), key=lambda d: d[0])
            text += f" ({busiest[1]})"
        return text
    return None


def format_inflight_detail(
    agent: Any, now: Optional[float] = None, min_elapsed: float = HEARTBEAT_INFLIGHT_MIN_SECONDS,
) -> Optional[str]:
    """``delegate_task 71m - 3 children: ...`` / ``terminal 12m``; None when no call has run ``min_elapsed``."""
    clock = float(now if now is not None else time.time())
    cur = inflight_tool(agent, clock)
    if not cur or cur[1] < min_elapsed:
        return None
    name, elapsed = cur
    text = f"{name} {format_duration(elapsed)}"
    children = delegate_children_summary(agent, clock - elapsed, clock)
    if children:
        text += f" - {children}"
    return text


def still_inside_line(agent: Any, threshold: float, now: Optional[float] = None) -> Optional[str]:
    """Progress-bubble line once a single call passes ``threshold`` seconds (<=0 disables)."""
    if not threshold or threshold <= 0:
        return None
    clock = float(now if now is not None else time.time())
    cur = inflight_tool(agent, clock)
    if not cur or cur[1] < threshold:
        return None
    name, elapsed = cur
    line = f"⏳ still inside {name} for {format_duration(elapsed)}"
    children = delegate_children_summary(agent, clock - elapsed, clock)
    return f"{line} - {children}" if children else line


def heartbeat_status_detail(
    activity: dict, agent: Any, want_iteration_detail: bool, now: Optional[float] = None,
) -> str:
    """The ``detail`` suffix of ``gateway.progress.working_heartbeat``.

    Unchanged from the old inline code when no call has been in flight ``HEARTBEAT_INFLIGHT_MIN_SECONDS``:
    `` — iteration i/N, <current_tool or last activity>``. A longer call replaces the action part with the
    call's elapsed time and, for delegate_task, its children."""
    from agent.session_activity import format_iteration_progress

    if not activity:
        return ""
    parts = []
    with suppress(Exception):
        if want_iteration_detail:
            parts.append(format_iteration_progress(activity["api_call_count"], activity["max_iterations"]))
    action = None
    with suppress(Exception):
        action = format_inflight_detail(agent, now)
    action = action or activity.get("current_tool") or activity.get("last_activity_desc")
    if action:
        parts.append(str(action))
    return " — " + ", ".join(parts) if parts else ""
