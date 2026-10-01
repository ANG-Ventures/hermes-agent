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

from agent.delegation_context import owned_kanban_task


# Every tool that ends this worker's responsibility for the card, not just the two that
# close it out: ``kanban_request_review`` moves it to ``review`` (goals.py's continuation /
# finalize prompts tell builders to call it) and ``kanban_request_changes`` returns it to
# ``ready`` (the sdlc-review skill tells reviewers to). Nudging after either asks a worker
# that did the right thing to ``kanban_complete`` a card it must not close.
_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete",
    "kanban_block",
    "kanban_request_review",
    "kanban_request_changes",
})

_DEFAULT_MAX_ATTEMPTS = 2
_log = logging.getLogger(__name__)


def kanban_stop_nudge_enabled() -> bool:
    """On when ``HERMES_KANBAN_TASK`` is set for the dispatcher-owned worker, unless
    ``HERMES_KANBAN_STOP_NUDGE`` disables it. In-process delegate_task children and cron runs
    inherit the env var but own no board task and carry no kanban toolset."""
    if (os.environ.get("HERMES_KANBAN_STOP_NUDGE") or "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(owned_kanban_task())


def _tool_call_name(tc: Any) -> str:
    """Tool name from a dict or object tool call (``function.name`` first, then ``name``)."""
    if isinstance(tc, dict):
        fn = tc.get("function")
        return str((fn.get("name") if isinstance(fn, dict) else tc.get("name")) or "")
    fn = getattr(tc, "function", None)
    return str((getattr(fn, "name", "") if fn is not None else getattr(tc, "name", "")) or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    for msg in filter(lambda m: isinstance(m, dict), messages or ()):
        role = msg.get("role")
        if role == "assistant" and any(
            _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS for tc in msg.get("tool_calls") or []
        ):
            return True
        if role == "tool" and str(msg.get("name") or "") in _TERMINAL_KANBAN_TOOLS:
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


_KANBAN_TERMINAL_TOOLS = frozenset(
    {"kanban_complete", "kanban_request_review", "kanban_request_changes", "kanban_block"}
)


def _tool_def_name(tool: Any) -> str:
    """Name of an OpenAI-format tool definition (``{"function": {"name": ..}}``)."""
    if isinstance(tool, dict):
        fn = tool.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tool.get("name") or "")
    fn = getattr(tool, "function", None)
    return str(getattr(fn, "name", None) or getattr(tool, "name", None) or "")


def session_has_kanban_terminal_tool(tools: Iterable[Any] | None) -> bool:
    """True when at least one kanban terminal tool is exposed to the model.

    ``None`` means "unknown" (caller didn't pass a tool list) and is treated
    as True so the guard keeps its legacy behaviour. An explicit list without
    any kanban terminal tool means the nudge is unsatisfiable: the model
    literally cannot call what the nudge demands.
    """
    if tools is None:
        return True
    return any(_tool_def_name(t) in _KANBAN_TERMINAL_TOOLS for t in tools)


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
    tools: Iterable[Any] | None = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a handoff.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    originating run already closed, nudge budget exhausted, or the session
    exposes no kanban terminal tool at all). Only legacy workers without a
    run pin fall back to tool-call history.

    The ``tools`` check exists because ``HERMES_KANBAN_TASK`` is inherited by
    every child process a worker spawns — including ``hermes -z`` one-shots
    with a restricted toolset (e.g. ``-t web``). Nudging a model that has no
    kanban tool just replaces its real answer with "I cannot call that"
    (2026-09-17: six research fan-out workers lost their stdout this way).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if not session_has_kanban_terminal_tool(tools):
        _log.info(
            "kanban stop-guard skipped: no kanban terminal tool in this session's "
            "toolset (inherited HERMES_KANBAN_TASK=%s)",
            os.environ.get("HERMES_KANBAN_TASK", ""),
        )
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
        "OR `kanban_block(reason=...)` if you are blocked.\n"
        "3. If you found the work ALREADY DONE on current main — nothing left "
        "to implement and no blocker — that is still a terminal outcome, not a "
        "reason to stop silently: call "
        "`kanban_complete(superseded_by=\"<card|PR|sha>\", summary=\"how you "
        "verified it\")`.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "session_has_kanban_terminal_tool",
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
