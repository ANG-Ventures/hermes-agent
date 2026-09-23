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


def _owns_dispatcher_task() -> bool:
    """Whether THIS process is the dispatcher's granted worker.

    Read through ``agent.delegation_context`` so this guard uses the SAME
    ownership predicate as the tool gate in ``tools/kanban_tools.py``. Fails
    open on import/None error, matching the read-side accessors there.
    """
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        return is_dispatcher_owned_worker_context()
    except Exception:
        return True


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set AND this process actually owns that
    grant, unless ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.

    The ownership half is load-bearing, and it is the SECOND of two gates.
    ``session_has_kanban_terminal_tool`` (added 2026-09-17) asks "can the model
    satisfy this nudge?"; this asks "is this process even the addressee?".
    They are complementary, and neither subsumes the other:

    * A dispatcher-spawned worker with a restricted toolset (``-t web``) owns
      the grant but has no terminal tool — caught by the toolset gate.
    * A CHILD of a worker that DOES see kanban tools — the codex
      ``hermes_tools_mcp_server`` callback hardcodes ``kanban_complete`` into
      its tool list, and orchestrator profiles enable the kanban toolset
      outright — passes the toolset gate while not owning the card. Nudging it
      pressures a non-owner toward a terminal call on its PARENT's card. That
      is the 2026-08-12 ``t_09b90233`` hole (a nested ``hermes chat`` completed
      its parent's card with an unrelated summary) arriving through the
      stop-guard instead of the tool gate.

    ``HERMES_KANBAN_*`` is ordinary process environment, so *every* child a
    worker spawns inherits it — a nested ``hermes -p <profile> -z`` one-shot, a
    research fan-out worker, a script that shells out. Reading the bare task id
    treats that inheritance as proof of ownership, which is exactly the mistake
    ``tools/kanban_tools.py::_owned_worker_task_id`` exists to prevent. Routing
    through the shared predicate is what keeps this gate from drifting away
    from the tool gate again.

    Field evidence for the pair: 2026-09-17, 6/6 deep-research fan-out workers
    launched from a kanban worker lost their entire report — the nudge demanded
    a tool they did not have, they spent the budget refusing, and the refusal
    became the last assistant message, so ``<out>/<name>.md`` landed 0 bytes
    (reports recovered from ``lcm.db``). Card ``t_8acd8da3``.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task:
        return False
    return _owns_dispatcher_task()


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
