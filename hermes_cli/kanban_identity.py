"""Shared identity helpers for dispatcher-spawned Kanban workers.

Two concerns live here:

* ``resolve_kanban_worker_chat_identity`` — the worker's chat id / display name.
* ``resolve_comment_provenance`` — the *trusted* per-run / per-session
  attribution stamped onto a comment. Two concurrent sessions running the SAME
  profile used to be indistinguishable on the board (both rendered as
  ``apollo``); this resolves the run id and a bounded session fingerprint from
  runtime context so they no longer are.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Optional


def resolve_kanban_worker_chat_identity(
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Return ``(chat_id, chat_name)`` from the worker's pinned environment."""
    source = os.environ if env is None else env
    task_id = (source.get("HERMES_KANBAN_TASK") or "").strip()
    board = (source.get("HERMES_KANBAN_BOARD") or "").strip()
    chat_name = (
        " / ".join(part for part in ("kanban", board, task_id) if part)
        if task_id
        else ""
    )
    return task_id, chat_name


def _resolve_current_session_id() -> Optional[str]:
    """Resolve the active session id, contextvar-first with an env fallback.

    Delegates to ``tools.kanban_tools._current_session_id`` so the gateway
    concurrency rules (per-turn contextvar is authoritative; a cleared "" must
    not fall through to a clobbered global) are enforced in exactly one place.
    Falls back to the raw env var when that import is unavailable (a trimmed
    install without the agent toolset).
    """
    try:
        from tools.kanban_tools import _current_session_id

        return _current_session_id()
    except Exception:
        return os.environ.get("HERMES_SESSION_ID") or None


def resolve_comment_provenance(
    task_id: str,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[Optional[int], Optional[str]]:
    """Return ``(run_id, session_ref)`` for a comment about ``task_id``.

    Both values come from trusted runtime context only — never from tool args
    or comment text — so a model cannot attribute its writes to another run or
    session (see ``kanban_db.add_comment``'s validation for the write-side
    choke point).

    ``run_id`` is only returned when ``HERMES_KANBAN_RUN_ID`` is scoped to
    ``task_id`` (same gate ``_worker_run_id`` applies to complete/block/
    heartbeat). A worker's run attests to its OWN card; stamping it on a
    cross-task comment would claim a write the run never made there.

    ``session_ref`` is the bounded fingerprint of the originating session id and
    is always safe to record: it identifies *which session wrote this*, which is
    the whole point on a cross-task handoff.
    """
    from hermes_cli.kanban_db import derive_session_ref

    source = os.environ if env is None else env
    run_id: Optional[int] = None
    if source.get("HERMES_KANBAN_TASK") == task_id:
        raw = source.get("HERMES_KANBAN_RUN_ID")
        if raw:
            try:
                run_id = int(raw)
            except (TypeError, ValueError):
                run_id = None
            else:
                if run_id <= 0:
                    run_id = None
    if env is None:
        session_id = _resolve_current_session_id()
    else:
        session_id = source.get("HERMES_SESSION_ID") or None
    return run_id, derive_session_ref(session_id)
