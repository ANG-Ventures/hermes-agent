"""Post-condition: no committed kanban row points at a staged copy that is gone.

A transition that stages artifact copies (``attachments/<task_id>/...``) and then
fails or cleans up must never leave a committed row naming a copy it deleted.
Prism P1 83af2fcef278 on #1859 was that shape: the review transition committed,
a second write_txn raised, and the except path discarded the copies the
committed handoff named (t_8bfdc207 fix, t_f577ddd5 audit). The conftest runs
:func:`dangling_staged_refs` after every ``request_review`` / ``complete_task`` /
``block_task`` call, whether it returned or raised, so any future sibling of
that shape fails the test that exercises it.

Only paths inside the card's own attachments dir are checked: those are copies
the kernel made, so a missing one is a kernel bug, not a test's fake path.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

_ARTIFACT_KEYS = ("artifacts", "routed_artifacts")
_EVENT_KINDS = ("review_requested", "completed")


def _staged(path: Any, task_id: str) -> bool:
    return isinstance(path, str) and f"/attachments/{task_id}/" in path


def _artifact_paths(raw: Any) -> list[str]:
    try:
        data = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for key in _ARTIFACT_KEYS:
        value = data.get(key)
        if isinstance(value, (list, tuple)):
            out.extend(v for v in value if isinstance(v, str))
    return out


def dangling_staged_refs(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Committed refs for *task_id* naming a staged copy that does not exist."""
    found: list[str] = []
    for row_id, path in conn.execute(
        "SELECT id, stored_path FROM task_attachments WHERE task_id = ?", (task_id,),
    ):
        if _staged(path, task_id) and not Path(path).exists():
            found.append(f"task_attachments#{row_id}: {path}")
    for run_id, meta in conn.execute(
        "SELECT id, metadata FROM task_runs WHERE task_id = ?", (task_id,),
    ):
        for path in _artifact_paths(meta):
            if _staged(path, task_id) and not Path(path).exists():
                found.append(f"task_runs#{run_id}.metadata: {path}")
    marks = ",".join("?" * len(_EVENT_KINDS))
    for event_id, kind, payload in conn.execute(
        f"SELECT id, kind, payload FROM task_events WHERE task_id = ? AND kind IN ({marks})",
        (task_id, *_EVENT_KINDS),
    ):
        for path in _artifact_paths(payload):
            if _staged(path, task_id) and not Path(path).exists():
                found.append(f"task_events#{event_id}.{kind}: {path}")
    return found


def assert_no_dangling_staged_refs(conn: sqlite3.Connection, task_id: str, *, after: str) -> None:
    if getattr(conn, "in_transaction", False):
        # A caller's outer txn is still open; its rows are not committed yet.
        return
    try:
        found = dangling_staged_refs(conn, task_id)
    except sqlite3.ProgrammingError:  # connection already closed by the caller
        return
    assert not found, (
        f"{after}({task_id!r}) left committed rows pointing at staged copies "
        f"that do not exist: {found}"
    )
