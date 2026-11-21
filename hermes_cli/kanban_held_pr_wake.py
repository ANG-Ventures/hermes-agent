"""Wake the home session once when a ready card waits behind its own HELD PR (t_4cface27).

A ready card under ``respawn_guarded:active_pr`` is never spawned while its PR is open. When that PR carries the
``hold`` label no closer lands it either, so the card waits on the operator, and nothing told the operator:
house-voice t_6721e909 sat ready 11.7 h behind pipecat-house-voice#1457 (2026-11-20/21). Measured over 14 days on
default + house-voice, that was the only active_pr streak of 30 min or more whose PR carried the label (5 streaks
in all), so this rule would have woken once, for it, and for nothing else.

Rule: a card whose newest guard decline this tick is ``active_pr`` with hold
:data:`kanban_db.RESPAWN_GUARD_HOLD_LABELLED`, continuously for :data:`WAKE_AFTER_S` on the same PR, gets ONE
``mechanical_wake`` event (source :data:`WAKE_SOURCE`) per (card, PR). The gateway owner-wake turns that into a
silent turn in the card's home session. Ordinary queueing (a saturated host, a concurrency cap, a pause) never
reaches the respawn guard with this hold, so it never wakes.

Switch: ``kanban.respawn_guard.held_pr_wake`` in the ROOT config.yaml, read every tick, default on.
"""
from __future__ import annotations

import json
import time
from typing import Any, Mapping, Optional

WAKE_SOURCE = "active-pr-held"
WAKE_EVENT = "mechanical_wake"
WAKE_AFTER_S = 30 * 60


def enabled(kcfg: Optional[Mapping]) -> bool:
    sect = (kcfg or {}).get("respawn_guard") if isinstance(kcfg, Mapping) else None
    val = sect.get("held_pr_wake", True) if isinstance(sect, Mapping) else True
    if isinstance(val, str):
        return val.strip().lower() not in ("off", "false", "no", "0")
    return bool(val)


def _already_woke(conn, task_id: str, pr: str) -> bool:
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                               (task_id, WAKE_EVENT)):
        try:
            p = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(p, dict) and p.get("source") == WAKE_SOURCE and p.get("pr") == pr:
            return True
    return False


def _held_since(conn, task_id: str, pr: str) -> Optional[int]:
    """Start of the current continuous active_pr streak on ``pr``, or None.

    The streak restarts at the last event that can change the guard's answer (same reset set as
    ``kanban_db.respawn_guard_stuck_tasks``) or at a guard decline for another reason or another PR."""
    from hermes_cli import kanban_db as _kb
    resets = _kb._RESPAWN_GUARD_STUCK_RESET_KINDS
    first = None
    for kind, created, raw in conn.execute(
            "SELECT kind, created_at, payload FROM task_events WHERE task_id = ? AND kind IN (%s) ORDER BY id DESC"
            % ",".join("?" * (len(resets) + 1)), (task_id, "respawn_guarded", *resets)):
        if kind != "respawn_guarded":
            break
        try:
            p = json.loads(raw or "{}")
        except (TypeError, ValueError):
            p = {}
        if not isinstance(p, dict) or p.get("reason") != "active_pr" or p.get("pr") != pr:
            break
        first = int(created)
    return first


def wake_text(task_id: str, pr: str, minutes: int, board: Optional[str]) -> str:
    from hermes_cli import kanban_db as _kb
    requeue = _kb.render_operator_command(board or _kb.get_current_board(), "requeue", task_id, "<reason>")
    return (f"{task_id} is ready but held {minutes} min by respawn guard active_pr behind its own PR {pr}, and that "
            f"PR carries the `hold` label, so no closer will land it and no worker will start. Decide: land the PR "
            f"(drop the label), or send the card back for more work: `{requeue}`.")


def notify(conn, details: Mapping[str, Mapping[str, Any]], *, kcfg: Optional[Mapping] = None,
           board: Optional[str] = None, now: Optional[int] = None) -> list:
    """Write one wake per (card, PR) held :data:`WAKE_AFTER_S` behind a hold-labelled PR. Returns the ids woken."""
    from hermes_cli import kanban_db as _kb
    if not enabled(kcfg):
        return []
    now = int(time.time()) if now is None else int(now)
    woke = []
    for tid, detail in (details or {}).items():
        if not isinstance(detail, Mapping) or detail.get("hold") != _kb.RESPAWN_GUARD_HOLD_LABELLED:
            continue
        pr = detail.get("pr")
        if not pr:
            continue
        try:
            if _already_woke(conn, tid, pr):
                continue
            since = _held_since(conn, tid, pr)
            if since is None or now - since < WAKE_AFTER_S:
                continue
            with _kb.write_txn(conn):
                _kb._append_event(conn, tid, WAKE_EVENT, {
                    "source": WAKE_SOURCE, "guard": "active_pr", "pr": pr,
                    "text": wake_text(tid, pr, (now - since) // 60, board)})
            woke.append(tid)
        except Exception as exc:  # one bad card never stops the tick
            _kb._log.warning("kanban held-PR wake on %s failed: %s", tid, exc)
    return woke
