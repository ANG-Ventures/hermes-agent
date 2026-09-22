"""Append-only mutation journal for kanban boards.

Provenance (card t_357330bf, 2026-09-21): ``~/.hermes/kanban`` was rmtree'd
wholesale twice in 19 hours. 64 boards / 2,525 tasks were restored from an
11-hour-old snapshot, but everything written after that snapshot was gone --
and on the subs-ace board the **comment threads were the only unrecoverable
part**: cards could be reconstructed from other sources, the discussion could
not.

That is what this module exists for. A periodic snapshot bounds loss to the
snapshot interval; an append-only journal bounds it to a single fsync. The two
are complements, not alternatives:

    snapshot (hourly, scripts/kanban-boards-backup.sh)  +  THIS (per mutation)
    = restore the snapshot, replay the journal tail, lose nothing.

Design constraints, each learned from the incident:

* **Outside the kanban home.** The journal lives under
  ``<root>/state/kanban-journal/``, NOT under ``<root>/kanban/``. The deleter
  took the whole kanban directory; a journal inside it would have died with the
  data it was meant to protect.
* **Append-only, one JSON line per mutation, fsync'd.** No rewrite path, no
  truncation, no compaction in the write path. A partially-written trailing
  line is expected after a crash and is skipped by the reader rather than
  treated as corruption.
* **The journal carries the PAYLOAD, not just the event kind.** ``task_events``
  already records that a comment happened (author + length); it does not record
  the body. Journaling events alone would have preserved the metadata of the
  lost subs-ace threads and none of their content.
* **Never breaks a board write.** Every failure path is swallowed and counted.
  A full disk, a read-only mount or a permissions error must degrade the
  journal, never refuse the mutation the user asked for. Use
  :func:`journal_health` to surface degradation.

Format: one JSON object per line, newline-terminated, UTF-8::

    {"ts": 1790042161.123, "board": "subs-ace", "actor": "apollo",
     "task_id": "t_8388f4f6", "kind": "commented",
     "payload": {"author": "apollo", "body": "...", "session_ref": "a14..."}}

``ts`` is a float epoch (ordering within a second matters for replay), ``kind``
mirrors the ``task_events`` vocabulary plus the content-bearing kinds this
module adds (``comment_body``, ``task_body``, ``result``).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

__all__ = [
    "journal_dir",
    "journal_path",
    "append",
    "read_entries",
    "journal_health",
    "JOURNAL_DISABLED_ENV",
]

JOURNAL_DISABLED_ENV = "HERMES_KANBAN_JOURNAL"
"""Set to ``off`` to disable journaling (operator escape hatch)."""

# Counters for :func:`journal_health`. Degradation must be VISIBLE: a journal
# that silently stopped writing is the same class of failure as the backup tier
# that silently stopped on 2026-08-09 and was not noticed for six weeks.
_stats_lock = threading.Lock()
_stats: Dict[str, Any] = {"written": 0, "failed": 0, "last_error": None, "last_write": None}

_write_lock = threading.Lock()


def _root() -> Path:
    """The hermes root, resolved exactly as the rest of kanban_db resolves it.

    ``kanban_home()`` is the root DIRECTORY (``<root>``), not ``<root>/kanban``
    -- ``boards_root()`` builds ``kanban_home() / "kanban" / "boards"`` on top
    of it. Reusing it keeps the journal on the same root as the data it
    protects, including under ``HERMES_KANBAN_SANDBOX``.
    """
    from hermes_cli import kanban_db

    return kanban_db.kanban_home()


def journal_dir() -> Path:
    """``<root>/state/kanban-journal`` -- deliberately OUTSIDE ``<root>/kanban``."""
    override = (os.environ.get("HERMES_KANBAN_JOURNAL_DIR") or "").strip()
    if override:
        return Path(override)
    return _root() / "state" / "kanban-journal"


def journal_path(board: Optional[str]) -> Path:
    """The journal file for ``board``."""
    slug = (board or "default").strip() or "default"
    slug = slug.replace("/", "_").replace("..", "_")
    return journal_dir() / (slug + ".jsonl")


def disabled() -> bool:
    return (os.environ.get(JOURNAL_DISABLED_ENV) or "").strip().lower() == "off"


def append(
    board: Optional[str],
    task_id: Optional[str],
    kind: str,
    payload: Optional[dict] = None,
    *,
    actor: Optional[str] = None,
    run_id: Optional[int] = None,
) -> bool:
    """Append one mutation record. Returns True when it reached the disk.

    NEVER raises. A journal failure must not fail the board write that
    triggered it -- the journal is a safety net, and a safety net that can
    break the thing it protects is a liability. Failures are counted and
    surfaced through :func:`journal_health`.
    """
    if disabled():
        return False
    record = {
        "ts": time.time(),
        "board": (board or "default"),
        "kind": kind,
        "task_id": task_id,
        "actor": actor,
        "run_id": run_id,
        "payload": payload if payload is not None else {},
    }
    try:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        _note_failure(exc)
        return False

    path = journal_path(board)
    data = (line + "\n").encode("utf-8")
    try:
        with _write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            # O_APPEND: concurrent writers from separate processes interleave
            # whole lines rather than overwriting each other. The kanban CLI,
            # the gateway and the dispatcher are all separate processes.
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, data)
                # fsync, not just flush: the incident's failure mode was an
                # abrupt loss of the whole directory tree, and a write sitting
                # in the page cache is a write that did not happen.
                os.fsync(fd)
            finally:
                os.close(fd)
    except (OSError, ValueError) as exc:
        _note_failure(exc)
        return False

    with _stats_lock:
        _stats["written"] += 1
        _stats["last_write"] = record["ts"]
    return True


def _note_failure(exc: BaseException) -> None:
    with _stats_lock:
        _stats["failed"] += 1
        _stats["last_error"] = exc.__class__.__name__ + ": " + str(exc)


def read_entries(board: Optional[str], *, since: float = 0.0) -> Iterator[dict]:
    """Yield journal records for ``board``, oldest first.

    A truncated trailing line -- the expected shape after a crash mid-write --
    is skipped, not raised on. Anything else malformed is skipped too: a
    replay tool must be able to recover everything that IS readable rather
    than refusing the whole file over one bad byte.
    """
    path = journal_path(board)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            try:
                if float(record.get("ts") or 0.0) < since:
                    continue
            except (TypeError, ValueError):
                pass
            yield record


def journal_health() -> dict:
    """Counters for a watchdog: writes, failures, last error, last write."""
    with _stats_lock:
        out = dict(_stats)
    out["enabled"] = not disabled()
    try:
        out["dir"] = str(journal_dir())
    except Exception:  # pragma: no cover - defensive
        out["dir"] = None
    return out
