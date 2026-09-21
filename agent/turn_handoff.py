"""Durable handoff for a turn cut by an unrecoverable provider failure.

When the fallback chain exhausts mid-turn, the turn dies and everything in
flight at the cut is lost: the tool calls already issued, their results, the
half-written assistant text, the open todo items. The next session rebuilds
that from chat scrollback, at 15-25 minutes per turnover.

This module persists that state as machine-readable JSON under
``$HERMES_HOME/state/turn-handoff/<session_key>.json`` and hands it to the
next turn. Design rules, all load-bearing:

* **Never raise into a dying turn.** Every public entry point swallows its own
  failures. A failed handoff costs context; a raised handoff costs the user's
  error message too.
* **Bounded.** Tool results are previewed to
  :data:`TOOL_RESULT_PREVIEW_CHARS`, so a 4 MB ``read_file`` result cannot
  turn the handoff into a second context problem.
* **Consume-once, expire at 24 h.** A resumed turn deletes the file; a stale
  one is dropped rather than injected into an unrelated conversation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

HANDOFF_TTL_SECONDS = 24 * 60 * 60
TOOL_RESULT_PREVIEW_CHARS = 500

# Enough to re-establish intent without re-importing the whole dead turn.
_USER_MESSAGE_PREVIEW_CHARS = 2000
_ASSISTANT_PROGRESS_CHARS = 4000
_ARGUMENTS_PREVIEW_CHARS = 500

_OPEN_TODO_STATUSES = ("pending", "in_progress")
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")

_HANDOFF_RELPATH = Path("state") / "turn-handoff"


def _default_root() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()) / _HANDOFF_RELPATH
    except Exception:  # pragma: no cover - defensive
        return Path.home() / ".hermes" / _HANDOFF_RELPATH


def handoff_path_for(session_key: str, *, root: Optional[Path] = None) -> Path:
    """Return the handoff file path for ``session_key``.

    The key is sanitized and disambiguated with a digest, so a platform key
    containing separators (``discord:123/456``) or traversal (``../..``) can
    never resolve outside the handoff root.
    """
    base = Path(root) if root is not None else _default_root()
    raw = str(session_key or "unknown")
    safe = _SAFE_KEY_RE.sub("_", raw)[:80] or "unknown"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return base / f"{safe}.{digest}.json"


def _truncate(text: Any, limit: int) -> str:
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [truncated, {len(text)} chars total]"


def _message_text(content: Any) -> str:
    """Flatten a message body (str or content-parts list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in {"text", "input_text"}:
                parts.append(str(part.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return ""


def _open_todos(agent) -> List[Dict[str, str]]:
    try:
        store = getattr(agent, "_todo_store", None)
        items = store.read() if store is not None else []
    except Exception:
        logger.debug("todo snapshot for handoff failed", exc_info=True)
        return []
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").strip() in _OPEN_TODO_STATUSES:
            out.append({
                "id": str(item.get("id") or ""),
                "content": str(item.get("content") or ""),
                "status": str(item.get("status") or ""),
            })
    return out


def build_turn_handoff(
    agent,
    messages: Optional[List[Dict[str, Any]]],
    *,
    turn_start_idx: int = 0,
    reason: str = "",
) -> Optional[Dict[str, Any]]:
    """Snapshot the in-flight state of the turn being cut.

    ``turn_start_idx`` is the index of THIS turn's user message in ``messages``
    — everything from there on is the work in flight. Returns ``None`` only
    when there is no user request, visible assistant progress, tool call, or
    open todo worth resuming.
    """
    try:
        return _build_turn_handoff(agent, messages, turn_start_idx, reason)
    except Exception:
        logger.debug("turn handoff build failed", exc_info=True)
        return None


def _merge_progress(materialized: str, streamed: str) -> str:
    """Append streamed progress without repeating its materialized prefix."""
    if not materialized:
        return streamed
    if not streamed or streamed in materialized:
        return materialized
    if materialized in streamed:
        return streamed

    # A completed interim assistant row can also be the prefix of the current
    # live stream. Preserve earlier rows while removing the longest exact
    # overlap at that boundary.
    max_overlap = min(len(materialized), len(streamed))
    for size in range(max_overlap, 7, -1):
        if materialized.endswith(streamed[:size]):
            return materialized + streamed[size:]
    return f"{materialized}\n\n{streamed}"


def _build_turn_handoff(agent, messages, turn_start_idx, reason):
    rows = list(messages or [])
    if not rows:
        return None
    idx = max(0, int(turn_start_idx or 0))
    if idx >= len(rows):
        return None
    window = rows[idx:]

    last_user: Optional[Dict[str, Any]] = None
    for row in window:
        if isinstance(row, dict) and row.get("role") == "user":
            last_user = {
                "row_id": row.get("row_id"),
                "content": _truncate(_message_text(row.get("content")),
                                     _USER_MESSAGE_PREVIEW_CHARS),
            }
            break

    # Tool results are keyed by call id, so an in-flight call (issued, never
    # answered) is exactly the one with no matching tool row — the most
    # valuable single fact in the handoff.
    results: Dict[str, str] = {}
    for row in window:
        if isinstance(row, dict) and row.get("role") == "tool":
            call_id = str(row.get("tool_call_id") or "")
            if call_id:
                results[call_id] = _message_text(row.get("content"))

    progress: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for row in window:
        if not isinstance(row, dict) or row.get("role") != "assistant":
            continue
        text = _message_text(row.get("content")).strip()
        if text:
            progress.append(text)
        for call in row.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            call_id = str(call.get("id") or "")
            completed = call_id in results
            tool_calls.append({
                "id": call_id,
                "name": str(fn.get("name") or ""),
                "arguments": _truncate(fn.get("arguments"),
                                       _ARGUMENTS_PREVIEW_CHARS),
                "completed": completed,
                "result_preview": (
                    _truncate(results[call_id], TOOL_RESULT_PREVIEW_CHARS)
                    if completed else None
                ),
            })

    materialized_progress = "\n\n".join(progress)
    streamed_progress = getattr(agent, "_current_streamed_assistant_text", "") or ""
    if not isinstance(streamed_progress, str):
        streamed_progress = ""
    if streamed_progress:
        strip_think_blocks = getattr(agent, "_strip_think_blocks", None)
        if callable(strip_think_blocks):
            streamed_progress = strip_think_blocks(streamed_progress)
        if not isinstance(streamed_progress, str):
            streamed_progress = ""
        streamed_progress = streamed_progress.strip()

    assistant_progress = _truncate(
        _merge_progress(materialized_progress, streamed_progress),
        _ASSISTANT_PROGRESS_CHARS,
    )
    open_todos = _open_todos(agent)
    if not last_user and not assistant_progress and not tool_calls and not open_todos:
        return None

    return {
        "version": 1,
        "created_at": time.time(),
        "reason": str(reason or ""),
        "session_id": str(getattr(agent, "session_id", "") or ""),
        "provider": str(getattr(agent, "provider", "") or ""),
        "model": str(getattr(agent, "model", "") or ""),
        "last_user_message": last_user,
        "assistant_progress": assistant_progress,
        "tool_calls": tool_calls,
        "open_todos": open_todos,
    }


def write_turn_handoff(
    session_key: str,
    handoff: Optional[Dict[str, Any]],
    *,
    root: Optional[Path] = None,
) -> bool:
    """Persist ``handoff`` atomically. Returns False on any problem."""
    if not handoff:
        return False
    path = handoff_path_for(session_key, root=root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(handoff, fh)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:
        logger.debug("turn handoff write failed for %s", session_key, exc_info=True)
        return False


def _load_turn_handoff(
    session_key: str,
    *,
    root: Optional[Path] = None,
    consume: bool,
) -> Optional[Dict[str, Any]]:
    """Read a valid handoff, optionally deleting it after a successful read."""
    path = handoff_path_for(session_key, root=root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception:
        logger.debug("turn handoff read failed for %s", session_key, exc_info=True)
        return None

    def _drop():
        try:
            path.unlink()
        except OSError:
            pass

    try:
        payload = json.loads(raw)
    except Exception:
        _drop()
        return None
    if not isinstance(payload, dict):
        _drop()
        return None
    created_at = payload.get("created_at")
    if not isinstance(created_at, (int, float)):
        _drop()
        return None
    if (time.time() - float(created_at)) > HANDOFF_TTL_SECONDS:
        _drop()
        return None
    if consume:
        _drop()
    return payload


def peek_turn_handoff(
    session_key: str,
    *,
    root: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Read a valid handoff without consuming it.

    Corrupt and expired files are still removed so they cannot be retried
    forever. Used by ``/resume-handoff`` to preview what the next model turn
    will receive.
    """
    return _load_turn_handoff(session_key, root=root, consume=False)


def consume_turn_handoff(
    session_key: str,
    *,
    root: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Read and DELETE the handoff for ``session_key``.

    Returns ``None`` when absent, corrupt, or older than
    :data:`HANDOFF_TTL_SECONDS`. Corrupt and expired files are removed so a
    bad payload cannot be retried on every subsequent turn.
    """
    return _load_turn_handoff(session_key, root=root, consume=True)


def prune_expired_handoffs(*, root: Optional[Path] = None) -> int:
    """Delete handoffs past their TTL. Returns the number removed."""
    base = Path(root) if root is not None else _default_root()
    removed = 0
    try:
        entries = list(base.glob("*.json"))
    except Exception:
        return 0
    now = time.time()
    for path in entries:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            created_at = float(payload.get("created_at") or 0)
        except Exception:
            created_at = 0.0
        if (now - created_at) > HANDOFF_TTL_SECONDS:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def capture_turn_handoff(
    agent,
    messages: Optional[List[Dict[str, Any]]],
    *,
    turn_start_idx: int = 0,
    reason: str = "",
    root: Optional[Path] = None,
) -> str:
    """Persist the in-flight turn state and return the 3-line chat notice.

    Called from the conversation loop's TERMINAL provider-failure return —
    the point where the chain is exhausted and the turn is about to die.
    Returns ``""`` (and writes nothing) when there is no session key or
    nothing worth resuming. Never raises.
    """
    try:
        session_key = str(getattr(agent, "_gateway_session_key", "") or "")
        if not session_key:
            return ""
        handoff = build_turn_handoff(
            agent, messages, turn_start_idx=turn_start_idx, reason=reason
        )
        if not handoff:
            return ""
        if not write_turn_handoff(session_key, handoff, root=root):
            return ""
        return format_handoff_notice(handoff)
    except Exception:
        logger.debug("turn handoff capture failed", exc_info=True)
        return ""


def preview_handoff_context(agent, *, root: Optional[Path] = None) -> str:
    """Return saved handoff context without consuming the next-turn injection."""
    try:
        session_key = str(getattr(agent, "_gateway_session_key", "") or "")
        if not session_key:
            return ""
        handoff = peek_turn_handoff(session_key, root=root)
        return render_handoff_context(handoff)
    except Exception:
        logger.debug("turn handoff preview failed", exc_info=True)
        return ""


def consume_handoff_context(agent, *, root: Optional[Path] = None) -> str:
    """Return injected context from a prior cut turn, consuming the handoff.

    Called from the turn prologue. Returns ``""`` when there is no session
    key, no handoff, or the handoff has expired. Never raises.
    """
    try:
        session_key = str(getattr(agent, "_gateway_session_key", "") or "")
        if not session_key:
            return ""
        handoff = consume_turn_handoff(session_key, root=root)
        if not handoff:
            return ""
        logger.info(
            "Injecting turn handoff for %s (cut: %s)",
            session_key, handoff.get("reason") or "unknown",
        )
        return render_handoff_context(handoff)
    except Exception:
        logger.debug("turn handoff consume failed", exc_info=True)
        return ""


def format_handoff_notice(handoff: Optional[Dict[str, Any]]) -> str:
    """Render the 3-line chat notice posted at the cut."""
    if not handoff:
        return ""
    calls = len(handoff.get("tool_calls") or [])
    todos = len(handoff.get("open_todos") or [])
    reason = str(handoff.get("reason") or "an unrecoverable provider failure")
    return (
        f"⛔ Turn cut — {reason}.\n"
        f"💾 Handoff saved: {calls} tool call{'' if calls == 1 else 's'}, "
        f"{todos} open todo{'' if todos == 1 else 's'}, "
        f"in-progress text preserved.\n"
        f"↩️ Send `/resume-handoff` (or just keep talking) to continue from there."
    )


def render_handoff_context(handoff: Optional[Dict[str, Any]]) -> str:
    """Render the handoff as context text injected into the next turn."""
    if not handoff or not isinstance(handoff, dict):
        return ""
    lines = [
        "[RESUMED TURN — the previous turn was cut by "
        f"{handoff.get('reason') or 'a provider failure'}. "
        "This is what was in flight at the cut.]",
    ]
    user = handoff.get("last_user_message") or {}
    if user.get("content"):
        lines.append(f"\nOriginal request: {user['content']}")
    progress = handoff.get("assistant_progress")
    if progress:
        lines.append(f"\nWork already reported:\n{progress}")
    calls = handoff.get("tool_calls") or []
    if calls:
        lines.append("\nTool calls issued this turn:")
        for call in calls:
            status = "completed" if call.get("completed") else "NEVER COMPLETED"
            lines.append(f"- {call.get('name')}({call.get('arguments')}) [{status}]")
            if call.get("result_preview"):
                lines.append(f"  result: {call['result_preview']}")
    todos = handoff.get("open_todos") or []
    if todos:
        lines.append("\nStill open:")
        for todo in todos:
            lines.append(f"- [{todo.get('status')}] {todo.get('content')}")
    lines.append(
        "\nContinue from here. Do not redo completed tool calls; re-issue the "
        "one that never completed."
    )
    return "\n".join(lines)
