"""Turn-end guard for kanban workers.

Kanban workers must close their run with a terminal lifecycle handoff. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

When a kanban worker tries to finish without closing its originating run,
return a bounded synthetic nudge so the conversation loop continues instead
of exiting. A successor's task status must not invalidate an earlier handoff.
"""

from __future__ import annotations

import logging
import os
from contextlib import closing
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete", "kanban_block", "kanban_request_review", "kanban_request_changes",
})

_DEFAULT_MAX_ATTEMPTS = 2
_log = logging.getLogger(__name__)


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
                return True
    return False


def _worker_run_ended(task_id: str, run_id: str) -> bool:
    """Check the pinned run, never the task's mutable current_run_id/status."""
    try:
        from hermes_cli import kanban_db

        with closing(kanban_db.connect_readonly()) as conn:
            row = conn.execute(
                "SELECT outcome, ended_at FROM task_runs WHERE id = ? AND task_id = ?",
                (int(run_id), task_id),
            ).fetchone()
        return bool(row and row[0] and row[1] is not None)
    except Exception:
        # An unreadable/missing run is not proof of a handoff. Keep the bounded
        # reminder rather than letting a failed tool invocation suppress it.
        _log.warning("Could not verify kanban worker run closure", exc_info=True)
        return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a handoff.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    originating run already closed, or nudge budget exhausted). Only legacy
    workers without a run pin fall back to tool-call history.
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    if run_id is not None:
        if _worker_run_ended(tid, run_id):
            return None
    elif session_called_kanban_terminal(messages):
        return None

    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"No terminal handoff was confirmed for your run of task `{tid}`. "
        "Ending without closing your run causes a protocol violation.\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, `kanban_request_review(summary=...)` for a review handoff, "
        "`kanban_request_changes(reason=...)` to return a review for rework, "
        "OR `kanban_block(reason=...)` if you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
