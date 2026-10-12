"""One #logs line per card that has waited on a DRAINED pin (t_e10add5a).

A card pinned ``host:<id>`` to a pool member that is draining (fleet-roles.json
``state``) or disabled (kanban-pool.json ``enabled: false``) waits until an
operator re-pins it or re-admits the host; nothing else moves it. 2026-10-11:
t_d69f0790 / t_b83edbdc sat 25 min on a draining ci-box reading
``pin_host_full``. After ``DRAINED_PIN_NOTIFY_SECONDS`` of one wait episode
(the ``since`` of its latest ``placement_wait`` event) the dispatcher posts ONE
line, latched per ``(task_id, since)`` in the board state dir, and only once the
send went out, so a failed send retries next tick and a new episode is reported
again.
"""

from __future__ import annotations

import json
import sys
from typing import Mapping, Optional

DRAINED_PIN_NOTIFY_SECONDS = 15 * 60
DRAINED_PIN_REASONS = frozenset({"pin_host_draining", "pin_host_disabled"})
_MARKER = ".drained_pin_waits.json"


def drained_pin_waits(conn, waits: Mapping[str, str], now: int) -> list[dict]:
    """``[{task_id, reason, since, waited}]`` for held cards whose current
    drained-pin wait episode is older than the threshold."""
    hits = []
    for tid, reason in (waits or {}).items():
        if reason not in DRAINED_PIN_REASONS:
            continue
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'placement_wait' "
            "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        try:
            last = json.loads(row[0] or "{}") if row else {}
        except (TypeError, ValueError):
            last = {}
        if last.get("reason") != reason or not isinstance(last.get("since"), int):
            continue
        waited = now - last["since"]
        if waited >= DRAINED_PIN_NOTIFY_SECONDS:
            hits.append({"task_id": tid, "reason": reason, "since": last["since"],
                         "host": last.get("host"), "waited": waited})
    return hits


def _episode_since(conn, task_id: str) -> Optional[int]:
    """``since`` of the card's open drained-pin wait episode, else None."""
    from hermes_cli import kanban_placement_reasons as _pr

    status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if status is None or status[0] != "ready":
        return None
    row = conn.execute(
        "SELECT payload, id FROM task_events WHERE task_id = ? AND kind = 'placement_wait' "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if row is None or conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND id > ? AND kind IN (%s) LIMIT 1"
            % ",".join("?" * len(_pr.PLACEMENT_WAIT_EPISODE_END_KINDS)),
            (task_id, row[1], *sorted(_pr.PLACEMENT_WAIT_EPISODE_END_KINDS))).fetchone():
        return None
    try:
        last = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return None
    return last.get("since") if last.get("reason") in DRAINED_PIN_REASONS else None


def notify_drained_pins(conn, board: Optional[str], waits: Mapping[str, str], now: int) -> list[dict]:
    """Post the #logs line for each new hit; returns the hits posted."""
    from hermes_cli import kanban_budget as _kbudget
    from hermes_cli import kanban_db as _kb

    hits = drained_pin_waits(conn, waits, now)
    marker = _kb.board_state_dir(board) / _MARKER
    try:
        seen = json.loads(marker.read_text(encoding="utf-8-sig"))
    except Exception:
        seen = {}
    if not isinstance(seen, dict):
        seen = {}
    current = {h["task_id"]: h["since"] for h in hits}
    # Prune by board state, never by this tick's view: a held card can be
    # missing from ``waits`` for a tick (the ready scan stopped at the no-slot
    # wall, a gateway restart's first ticks) and must not re-post when it
    # reappears (Prism a54ce82ab27c).
    latched = {k: v for k, v in seen.items()
               if current.get(k, _episode_since(conn, k)) == v}
    changed = latched != seen
    script = _kbudget._notify_script_path()
    posted = []
    for h in hits:
        if latched.get(h["task_id"]) == h["since"]:
            continue
        state = h["reason"][len("pin_host_"):]
        body = (f"📌 Kanban '{board or 'default'}': {h['task_id']} pinned to {h['host'] or '?'} "
                f"({state}) has waited {h['waited'] // 60} min. Re-pin it or re-admit the host.")
        _kb._log.warning("PHASE=kanban_drained_pin_wait %s", body)
        if script is None:
            continue  # a hermetic home never pages; nothing latched, nothing posted
        if _kbudget._run_notify([sys.executable, script, "--channel", "discord",
                                 "--target", _kbudget.RECOVERY_TARGET, "--send", body]) is False:
            continue
        latched[h["task_id"]] = h["since"]
        changed = True
        posted.append(h)
    if changed:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(latched), encoding="utf-8")
    return posted
