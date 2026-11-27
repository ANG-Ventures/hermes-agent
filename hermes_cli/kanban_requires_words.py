"""A ``requires:`` line may only name capabilities the map defines (t_c754f664).

On 2026-11-21 t_00dabd92 was created with a capability line written as prose (``host=studio reason:
Bambu Studio slicing and ...``). ``kanban create`` took it, the dispatcher read every word as a
capability (``and``, ``bambu``, ``on``, ...), found none in ``fleet/host-capabilities.json``, held the
card on the Mac Studio and it sat ``ready`` 4 h 5 min with no wake.

1. :func:`refusal` -- ``kanban create`` (CLI and tool) and ``kanban edit --body`` refuse a body whose
   line-anchored capability line (:data:`kanban_host_capabilities.REQUIRES_RE`, read by
   :func:`kanban_host_capabilities.declared_words`) holds a word the map does not define. An absent or
   invalid map refuses nothing (F11). The CLI override ``--requires-unknown-ok <reason>`` lets it
   through and records :data:`OVERRIDE_EVENT` on the card.
2. :func:`wake_stranded` -- the dispatcher backstop for a card that still gets there (a worker edited
   it, the map changed under it): a ``ready`` card whose capability line holds an unknown word, past
   :data:`STRANDED_AFTER_S`, writes ONE ``mechanical_wake`` per ready episode, the event the gateway
   owner-wake turns into a turn in the card's home session (``kanban_owner_wake.SCAN_KINDS``). A card
   that is merely unclaimed is NOT woken: ordinary queueing (a saturated host, a cap, a pause) looks
   the same and clears itself.

Switch: ``kanban.requires_unknown_guard`` in config.yaml (default True; non-bool -> default, the
``kanban.pool_box_health`` convention). False turns off both the refusal and the wake.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional

OVERRIDE_EVENT = "requires_unknown_override"
WAKE_EVENT = "mechanical_wake"
WAKE_SOURCE = "kanban-ready-stranded"
STRANDED_AFTER_S = 30 * 60
UNKNOWN_CAUSE = "unknown_capability"
SWITCH = "requires_unknown_guard"


def enabled() -> bool:
    """``kanban.requires_unknown_guard`` (default True): the refusal and the wake. Non-bool -> default."""
    try:
        from hermes_cli.config import load_config_readonly
        value = (load_config_readonly().get("kanban") or {}).get(SWITCH, True)
    except Exception:
        return True
    return value if isinstance(value, bool) else True


def _fleet_dir() -> Path:
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / "fleet"


def _load(fleet_dir: Optional[Path]):
    from hermes_cli import kanban_host_capabilities as khc
    try:
        cmap = khc.load_map(fleet_dir if fleet_dir is not None else _fleet_dir())
    except Exception:
        return None
    return cmap if cmap is not None and cmap.valid else None


def unknown_words(body: Optional[str], cmap) -> List[str]:
    """Declared words the map does not define, in line order. ``[]`` when ``cmap`` is None/invalid."""
    if cmap is None or not cmap.valid:
        return []
    from hermes_cli import kanban_host_capabilities as khc
    from hermes_cli.kanban_placement_reasons import ACCESS_CONTROL_PLANE_READ
    known = set(cmap.capabilities) | {khc.NONE_WORD, ACCESS_CONTROL_PLANE_READ}
    return [w for w in khc.declared_words(body) if w not in known]


def refusal(body: Optional[str], *, fleet_dir: Optional[Path] = None) -> Optional[str]:
    """The refusal text for ``body``, or None (no unknown word, or no valid map)."""
    if not body or not enabled():
        return None
    cmap = _load(fleet_dir)
    bad = unknown_words(body, cmap)
    if not bad:
        return None
    hosts = ""
    try:
        from hermes_cli import kanban_worker_pool as kwp
        ids = kwp.known_host_ids(fleet_dir if fleet_dir is not None else _fleet_dir())
        hosts = f" (pool hosts: {', '.join(ids)})" if ids else ""
    except Exception:
        pass
    return (f"the requires: line names words the capability map does not define: {', '.join(bad)}. "
            f"Known capabilities: {', '.join(sorted(cmap.capabilities))}. "
            "Legal shapes: `requires: <capability>[, <capability>] (optional note in parentheses)`, "
            f"or a separate line `host: <pool host>` to pin the card{hosts}. "
            "Put a reason in the parentheses, not on the line itself. "
            "Operator override (CLI only): `hermes kanban create|edit ... --requires-unknown-ok "
            "\"<reason>\"` (recorded on the card).")


def record_override(conn, task_id: str, body: Optional[str], reason: str, *, verb: str,
                    actor: Optional[str] = None) -> Optional[dict]:
    """One :data:`OVERRIDE_EVENT` when ``body`` really holds unknown words; else nothing."""
    bad = unknown_words(body, _load(None))
    if not bad:
        return None
    from hermes_cli import kanban_db as kb
    payload = {"words": bad, "reason": reason.strip(), "verb": verb, "by": actor or "operator"}
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, OVERRIDE_EVENT, payload)
    return payload


# -- the backstop ---------------------------------------------------------------------------------

def _ready_since(conn, row) -> int:
    entered = conn.execute(
        "SELECT MAX(changed_at) FROM task_status_audit WHERE task_id = ? "
        "AND new_status = 'ready' AND old_status IS NOT 'ready'", (row["id"],)).fetchone()[0]
    return int(entered if entered is not None else (row["created_at"] or 0))


def _woken(conn, task_id: str, cause: str, since: int) -> bool:
    for (raw,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? AND created_at >= ?",
            (task_id, WAKE_EVENT, since)):
        try:
            p = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(p, dict) and p.get("source") == WAKE_SOURCE and p.get("cause") == cause:
            return True
    return False


def wake_text(task_id: str, age_s: int, words: List[str]) -> str:
    return (f"{task_id} has been ready {age_s // 60} min and no host can take it: its requires: line names "
            f"words the capability map does not define ({', '.join(words)}). Rewrite the line as "
            "`requires: <capability> (note)` or a separate `host: <pool host>` line "
            f"(`hermes kanban edit {task_id} --body ...`).")


def wake_stranded(conn, *, now: Optional[int] = None, fleet_dir: Optional[Path] = None,
                  after_s: int = STRANDED_AFTER_S) -> list:
    """ONE ``mechanical_wake`` per (card, ready episode) for a ready card older than ``after_s`` whose
    capability line holds an unknown word. Operator, human-lane and ``no_worker`` cards are skipped.
    Returns ``[(task_id, cause)]``; ``[]`` with the switch off."""
    if not enabled():
        return []
    from hermes_cli import kanban_db as kb
    now = int(time.time()) if now is None else int(now)
    cmap = _load(fleet_dir)
    if cmap is None:
        return []
    woke = []
    for row in conn.execute(
            "SELECT id, assignee, body, created_at FROM tasks WHERE status = 'ready' "
            "AND claim_lock IS NULL AND COALESCE(no_worker, 0) = 0").fetchall():
        try:
            assignee = (row["assignee"] or "").strip()
            if not assignee or assignee in kb.OPERATOR_PROFILES or kb.is_human_reviewer(assignee):
                continue
            since = _ready_since(conn, row)
            if not since or now - since < after_s:
                continue
            words = unknown_words(row["body"], cmap)
            cause = UNKNOWN_CAUSE
            if not words or _woken(conn, row["id"], cause, since):
                continue
            with kb.write_txn(conn):
                kb._append_event(conn, row["id"], WAKE_EVENT, {
                    "source": WAKE_SOURCE, "cause": cause, "ready_since": since,
                    "text": wake_text(row["id"], now - since, words)})
            woke.append((row["id"], cause))
        except Exception as exc:  # one bad card never stops the tick
            kb._log.warning("kanban ready-stranded wake on %s failed: %s", row["id"], exc)
    return woke
