"""PLACEMENT blocks: a worker that ran on the wrong host blocks, the dispatcher re-routes it (t_a32116ec).

A ``kind=capability`` block on a run the pool placed on a remote host is a placement fault, not a
human question: the worker found that the host lacks what the card needs (a Studio shell, the vault,
fleet ssh, the lander). On 2026-10-13, 30 such cards sat ``blocked`` for 1-176 h (t_9a038e36 26 h
while the live tree drifted to ahead=429), each saying "re-run on the Studio" to nobody.

Each tick, for every ``blocked``/``capability`` card whose latest block ended a run that carried a
``worker_placed`` event (or whose reason names a host pin), the dispatcher:

* re-routes it ONCE: prepends a ``host:`` pin line to the body (the same pin an operator writes, so
  :func:`kanban_worker_pool.resolve_pin` honours it and the body shows why) and unblocks it with a
  comment. The target is the host a ``host: <x>`` line in the reason names, else the Studio (the
  dispatcher's own machine, which holds every Studio-only resource);
* records ``placement_unroutable`` ONCE when it cannot: the named host is not an enabled pool host,
  is the host that already failed, the card carries an operator pin to another host, or a re-routed
  run blocked again. kanban-placement-watch (hermes-home) pages #alerts through episode_pager once
  per card on that event, and flags any placement block older than 60 min with neither event.

:func:`classify` is THE classifier; the watch imports it, never re-implements it.
"""
from __future__ import annotations

import json
import re
import time
from typing import List, Optional, Tuple

from hermes_cli import kanban_placement_reasons as _pr

REROUTED_EVENT = "placement_rerouted"
UNROUTABLE_EVENT = "placement_unroutable"
# A block older than this is not re-routed (the work may be stale): it is recorded unroutable, so the
# watch pages it once for a human to re-pin or close.
REROUTE_MAX_AGE_SECONDS = 24 * 3600
AUTHOR = "kanban-dispatcher"
# A reason that names a host pin or a Studio placement, for blocks on runs with no worker_placed
# event (pre-event runs, or a worker that names the host itself).
PLACEMENT_REASON_RE = re.compile(
    r"(?im)^[ \t]*host:[ \t]*[A-Za-z0-9]|\bhost:[ \t]*(?:studio|mac-studio)\b|studio[- ]placed|"
    r"(?:re-?run|re-?dispatch|re-?place)\w*\s+(?:it\s+|this\s+(?:card\s+)?)?on\s+(?:the\s+)?"
    r"(?:mac[- ]?)?studio|spilled to|placed on (?:ci-box|ace-ai|ace-media)|landed on (?:ci-box|ace-ai|ace-media)")
# Sentence punctuation is not part of a host id ("host: studio." / "host: ace-ai,").
_REASON_PIN_RE = re.compile(r"\bhost:[ \t]*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)", re.IGNORECASE)


def _reason_pin(reason: str, pool) -> Optional[str]:
    """The canonical host the first resolvable ``host: <x>`` mention in the reason names (studio / a
    pool host); else the first raw word (prose like "qBt host: re-dispatch" is skipped when a real pin
    follows); None when the reason names no pin."""
    from hermes_cli import kanban_worker_pool as _kwp
    first = None
    for m in _REASON_PIN_RE.finditer(reason or ""):
        pin, ignored = _kwp.resolve_pin(f"host: {m.group(1)}", getattr(pool, "hosts", ()) or (),
                                        getattr(pool, "pin_aliases", None))
        if pin:
            return pin
        first = first or ignored
    return first


def classify(*, kind: Optional[str], reason: str, placed_host: Optional[str], body: Optional[str],
             pool, rerouted_before: bool, assignee: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """``("reroute", host)``, ``("unroutable", why)`` or None (not a placement block).

    ``placed_host`` is the pool host of the blocked run's ``worker_placed`` event (None = local run).
    ``host`` is ``studio`` or an enabled pool host id."""
    from hermes_cli import kanban_worker_pool as _kwp
    if kind != "capability":
        return None
    if rerouted_before:
        # Any capability block after a re-route escalates, whatever its wording: the re-routed run is local.
        return "unroutable", f"re-routed once already and blocked again: {(reason or '')[:160]}"
    if placed_host is None and not PLACEMENT_REASON_RE.search(reason or ""):
        return None
    card_pin, _ign = _kwp.resolve_pin(body, getattr(pool, "hosts", ()) or (),
                                      getattr(pool, "pin_aliases", None))
    if card_pin not in (None, _kwp.PIN_ANY, _kwp.PIN_STUDIO):
        return "unroutable", f"operator pin host:{card_pin} lacks the capability; the pin is not overridden"
    want = _reason_pin(reason, pool)
    if want in (None, _kwp.PIN_STUDIO, _kwp.PIN_ANY):
        if placed_host is None and card_pin == _kwp.PIN_STUDIO:
            return None                      # already ran on the Studio: a real capability block
        return "reroute", _kwp.PIN_STUDIO
    if card_pin == _kwp.PIN_STUDIO:
        return "unroutable", f"operator pin host:studio conflicts with the reason's host:{want}; not overridden"
    enabled = {h.name.lower() for h in (getattr(pool, "pool_hosts", ()) or ())
               if getattr(h, "enabled", False) and getattr(h, "state", "active") == "active"}
    if want == (placed_host or "").lower():
        return "unroutable", f"the reason names host:{want}, the host that already failed"
    if want not in enabled:
        return "unroutable", f"the reason names host:{want}, which is not an enabled pool host"
    serves = {h.name.lower(): tuple(getattr(h, "profiles", ()) or ()) for h in pool.pool_hosts}
    if assignee and assignee not in serves.get(want, ()):
        # PoolPlan.take() would refuse the pin with pin_profile on every tick: stranded in ready.
        return "unroutable", f"host:{want} does not serve profile {assignee}"
    return "reroute", want


def _latest_blocks(conn) -> list:
    return conn.execute(
        "SELECT t.id, t.body, t.status, t.assignee, b.payload AS blk, b.run_id, b.created_at AS blocked_at, "
        "(SELECT p.payload FROM task_events p WHERE p.run_id = b.run_id AND p.kind = ? "
        " ORDER BY p.id DESC LIMIT 1) AS placed, "
        "EXISTS(SELECT 1 FROM task_events r WHERE r.task_id = t.id AND r.kind = ?) AS rerouted, "
        "EXISTS(SELECT 1 FROM task_events u WHERE u.task_id = t.id AND u.kind = ? AND u.id > b.id) AS noted "
        "FROM tasks t JOIN task_events b ON b.id = (SELECT MAX(e.id) FROM task_events e "
        " WHERE e.task_id = t.id AND e.kind IN ('blocked', 'block_loop_detected')) "
        "WHERE t.status IN ('blocked', 'triage') AND t.block_kind = 'capability'",
        (_pr.WORKER_PLACED_EVENT, REROUTED_EVENT, UNROUTABLE_EVENT),
    ).fetchall()


def _json(raw) -> dict:
    try:
        v = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def reroute_placement_blocks(conn, pool, *, now: Optional[int] = None) -> Tuple[List[Tuple[str, str]], List[str]]:
    """One pass over the board: ``([(task_id, host)] re-routed, [task_id] newly unroutable)``."""
    from hermes_cli import kanban_db as _kb
    now = int(time.time()) if now is None else int(now)
    rerouted: List[Tuple[str, str]] = []
    unroutable: List[str] = []
    for row in _latest_blocks(conn):
        blk = _json(row["blk"])
        placed_host = _json(row["placed"]).get("host") if row["placed"] else None
        verdict = classify(kind=blk.get("kind"), reason=str(blk.get("reason") or ""), placed_host=placed_host,
                           body=row["body"], pool=pool, rerouted_before=bool(row["rerouted"]),
                           assignee=row["assignee"])
        if verdict is None:
            continue
        action, arg = verdict
        tid = row["id"]
        if action == "reroute" and row["status"] != "blocked":
            # The unblock-loop breaker already sent it to triage (a 2nd same-kind block): a human's card.
            action, arg = "unroutable", "in triage after a repeated capability block"
        elif action == "reroute" and now - int(row["blocked_at"] or now) > REROUTE_MAX_AGE_SECONDS:
            action, arg = "unroutable", f"blocked > {REROUTE_MAX_AGE_SECONDS // 3600} h ago; re-pin or close it by hand"
        if action == "unroutable":
            if row["noted"]:
                continue
            with _kb.write_txn(conn):
                _kb._append_event(conn, tid, UNROUTABLE_EVENT,
                                  {"placed_host": placed_host, "why": arg, "run_id": row["run_id"]})
            unroutable.append(tid)
            continue
        pin_line = f"host: {arg}"
        new_body = f"{pin_line}\n{row['body'] or ''}"
        note = (f"placement re-route: run {row['run_id']} ran on {placed_host or 'a host'} and blocked "
                f"kind=capability, so this card now carries `{pin_line}` (line 1) and is back in the queue. "
                f"Once only: a second capability block pages #alerts instead. Reason was: "
                f"{str(blk.get('reason') or '')[:300]}")
        # Order is the atomicity (unblock_task owns its own write txn and cannot nest): the pin goes in with a
        # compare-and-swap on the body read above (a concurrent edit is never overwritten), then the unblock,
        # and the placement_rerouted marker ONLY after the unblock landed. A failed unblock reverts the pin
        # under the same CAS, so the card stays blocked, unmarked, and is retried next tick, never escalated.
        with _kb.write_txn(conn):
            cur = conn.execute("UPDATE tasks SET body = ? WHERE id = ? AND status = 'blocked' AND body IS ?",
                               (new_body, tid, row["body"]))
            if cur.rowcount != 1:
                continue
        if not _kb.unblock_task(conn, tid, comment=(AUTHOR, note, None, None)):
            with _kb.write_txn(conn):
                conn.execute("UPDATE tasks SET body = ? WHERE id = ? AND status = 'blocked' AND body IS ?",
                             (row["body"], tid, new_body))
            continue
        with _kb.write_txn(conn):
            _kb._append_event(conn, tid, REROUTED_EVENT,
                              {"from_host": placed_host, "to_host": arg, "run_id": row["run_id"]})
        rerouted.append((tid, arg))
    return rerouted, unroutable
