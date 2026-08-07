"""Shared chat identity for dispatcher-spawned Kanban workers."""

from __future__ import annotations

import os
from collections.abc import Mapping


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
