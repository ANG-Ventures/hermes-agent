"""``hermes kanban placement [--fleet | <id>]``: THE placement reader (spec §4.3, Phase 3).

One module owns the join every placement surface prints from:

* ``task_events`` of kind ``placement`` / ``worker_placed`` on each running
  card's CURRENT run (where it runs and the rule that put it there), and the
  latest ``placement_wait`` of each ready card (why it is held, and on which
  host);
* ``load_gate.json``'s ``pool`` block (band, pressure, free per host, from the
  last gateway tick), read from a path argument so a copy can be fed in;
* the pool files (``fleet/kanban-pool.json`` + ``fleet-roles.json``) for the
  ladder order and slots.

Nothing is hand-maintained: no host list, no reason string lives here. Event
kinds and reason codes come from :mod:`kanban_placement_reasons` (I-S5).
Read-only: every board is opened ``mode=ro``; no probe, no write.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from hermes_cli import kanban_placement_reasons as _pr
from hermes_cli import kanban_worker_pool as _kwp

HELD_ROWS_SHOWN = 20
TOP_REASONS_SHOWN = 5


def _map(v) -> Mapping:
    return v if isinstance(v, Mapping) else {}


def _payload(raw) -> dict:
    try:
        d = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def wait_code(payload: Mapping) -> Optional[str]:
    """The hold code of a ``placement_wait`` payload: ``code`` (Phase 2a), else
    the head of the legacy ``reason`` string (``pin_host_hot (cpu ..)`` ->
    ``pin_host_hot``; ``not_portable:linked(blocks->t_x)`` ->
    ``not_portable:linked``). Grouping is by code, never by detail."""
    code = payload.get("code")
    if isinstance(code, str) and code:
        return code
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason:
        return None
    return reason.split(" (", 1)[0].split("(", 1)[0].strip() or None


# -- board reads ------------------------------------------------------------

def _running(conn) -> List[dict]:
    """Every running card with its current run's placement (None = local)."""
    rows = conn.execute(
        "SELECT t.id, t.assignee, t.current_run_id FROM tasks t WHERE t.status = 'running'"
    ).fetchall()
    out = []
    for tid, assignee, run_id in rows:
        placed = rule = None
        if run_id is not None:
            ev = {}
            for kind, raw in conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? AND run_id = ? "
                "AND kind IN (?, ?) ORDER BY id",
                (tid, run_id, _pr.WORKER_PLACED_EVENT, _pr.PLACEMENT_EVENT),
            ).fetchall():
                ev[kind] = _payload(raw)  # latest of each kind wins
            wp = ev.get(_pr.WORKER_PLACED_EVENT)
            pl = ev.get(_pr.PLACEMENT_EVENT) or {}
            if wp is not None:
                placed = wp.get("host")
            rule = pl.get("rule") or (wp or {}).get("reason")
        out.append({"task_id": tid, "assignee": assignee, "run_id": run_id,
                    "host": placed or None, "rule": rule})
    return out


def _held(conn) -> List[dict]:
    """Ready cards whose latest ``placement_wait`` is still the open episode."""
    end = sorted(_pr.PLACEMENT_WAIT_EPISODE_END_KINDS)
    rows = conn.execute(
        "SELECT t.id, t.assignee, e.id, e.payload, e.created_at FROM tasks t "
        "JOIN task_events e ON e.id = (SELECT MAX(id) FROM task_events "
        "  WHERE task_id = t.id AND kind = ?) "
        "WHERE t.status = 'ready' AND NOT EXISTS (SELECT 1 FROM task_events x "
        "  WHERE x.task_id = t.id AND x.id > e.id AND x.kind IN (%s))" % ",".join("?" * len(end)),
        (_pr.PLACEMENT_WAIT_EVENT, *end),
    ).fetchall()
    out = []
    for tid, assignee, _eid, raw, created in rows:
        p = _payload(raw)
        if p.get("cleared"):
            continue
        code = wait_code(p)
        if code is None:
            continue
        out.append({"task_id": tid, "assignee": assignee, "code": code,
                    "detail": p.get("detail"), "route": p.get("route"), "host": p.get("host"),
                    "since": p.get("since") or created, "at": created})
    return out


def read_boards(boards: Iterable[Tuple[str, Path]]) -> Tuple[List[dict], List[dict], List[str]]:
    """``(running, held, errors)`` over every board, each row tagged ``board``.
    An unreadable board is an error, never an empty board."""
    from hermes_cli import kanban_db as _kb

    running: List[dict] = []
    held: List[dict] = []
    errors: List[str] = []
    for slug, path in boards:
        try:
            conn = _kb.connect_readonly(Path(path))
        except Exception as exc:
            errors.append(f"{slug}: {exc}")
            continue
        try:
            conn.execute("BEGIN")  # one snapshot per board: running and held agree
            running += [dict(r, board=slug) for r in _running(conn)]
            held += [dict(r, board=slug) for r in _held(conn)]
        except Exception as exc:
            errors.append(f"{slug}: {exc}")
        finally:
            conn.close()
    return running, held, errors


def live_boards() -> List[Tuple[str, Path]]:
    """Every non-archived board on disk, as ``(slug, db path)``."""
    from hermes_cli import kanban_db as _kb

    out = []
    for meta in _kb.enumerating_each(_kb.list_boards(include_archived=False)):
        slug = (meta.get("slug") if isinstance(meta, dict) else None) or _kb.DEFAULT_BOARD
        # Each slug's OWN db: a worker's HERMES_KANBAN_DB pin would map every
        # slug to one file and multiply every count by N boards.
        # A registered board whose DB is gone stays in: read_boards reports it
        # as an error, never as a board with no running work.
        out.append((slug, _kb._board_db_path_ignoring_pin(_kb._normalize_board_slug(slug) or slug)))
    return out


# -- the view ---------------------------------------------------------------

def compute(root: Path, *, boards: Iterable[Tuple[str, Path]], gate_state: Optional[Mapping],
            kanban_cfg: Optional[Mapping] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """The fleet view as a dict; the text is printed from exactly this dict."""
    now = time.time() if now is None else float(now)
    gate = _map(gate_state)
    block = _map(gate.get("pool"))
    tick = _map(block.get("hosts"))
    pool = _kwp.read_pool(Path(root) / "fleet", kanban_cfg=_map(kanban_cfg))
    running, held, errors = read_boards(boards)

    # Ladder order: the pool's priority, then hosts the pool files disable,
    # then any host the tick or a live run names that the files no longer do.
    ladder = list(dict.fromkeys([
        *(h.name for h in sorted(pool.pool_hosts, key=lambda h: h.priority)),
        *pool.disabled, *tick, *(r["host"] for r in running if r["host"]),
    ]))
    slots = {h.name: h.slots for h in pool.pool_hosts}
    hosts: Dict[str, dict] = {}
    for name in ladder:
        row = _map(tick.get(name))
        cards = [{"task_id": r["task_id"], "board": r["board"], "assignee": r["assignee"],
                  "rule": r["rule"]} for r in running if r["host"] == name]
        hosts[name] = {
            "running": len(cards),
            "slots": slots.get(name, row.get("slots")),
            "free": row.get("free"),
            "band": row.get("band") or _kwp.BAND_UNKNOWN,
            "hot": bool(row.get("hot")),
            "pressure": row.get("pressure"),
            "in_pool": name in slots,
            "cards": cards,
        }
    local = [{"task_id": r["task_id"], "board": r["board"], "assignee": r["assignee"],
              "rule": r["rule"]} for r in running if not r["host"]]
    by_code: Dict[str, int] = {}
    for h in held:
        by_code[h["code"]] = by_code.get(h["code"], 0) + 1
    top = sorted(by_code.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "now": now,
        "gate": {"band": gate.get("band"), "updated_at": gate.get("updated_at"),
                 "pool_planned": bool(block.get("planned")), "pool_reason": block.get("reason")},
        "pool_refused": pool.refused,
        "local": {"host": _kwp.STUDIO_HOST, "running": len(local), "band": gate.get("band"),
                  "cards": local},
        "hosts": hosts,
        "held": {"count": len(held), "by_code": dict(top),
                 "rows": sorted(held, key=lambda h: (h["code"], h["since"] or 0, h["task_id"]))},
        "errors": errors,
    }


def _age(now: float, ts) -> str:
    try:
        s = max(0, int(now - float(ts)))
    except (TypeError, ValueError):
        return "?"
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def format_fleet(rep: Mapping) -> List[str]:
    now = float(rep.get("now") or time.time())
    g = _map(rep.get("gate"))
    out = [f"Placement (fleet): gate band={g.get('band') or '?'} "
           f"(updated {_age(now, g.get('updated_at')) if g.get('updated_at') else '?'} ago), "
           f"pool {'planned' if g.get('pool_planned') else 'not planned (' + str(g.get('pool_reason') or '-') + ')'}"]
    if rep.get("pool_refused"):
        out.append(f"  pool refused: {rep['pool_refused']}")
    for e in rep.get("errors") or ():
        out.append(f"  board unreadable: {e}")
    loc = _map(rep.get("local"))
    out.append(f"  {loc.get('host')} (local): running={loc.get('running')} band={loc.get('band') or '?'}")
    for c in loc.get("cards") or ():
        out.append(f"    {c['task_id']} [{c['board']}] @{c['assignee']} rule={c.get('rule') or '-'}")
    for name, h in _map(rep.get("hosts")).items():
        flags = (" HOT" if h.get("hot") else "") + ("" if h.get("in_pool") else " (not in pool)")
        # A pool host with no count (t_c834e0d8) reads ``uncounted``; '?' = not in the pool files.
        slots_txt = (h.get("slots") if h.get("slots") is not None
                     else "uncounted" if h.get("in_pool") else "?")
        out.append(f"  {name}: running={h['running']} slots={slots_txt} "
                   f"free={h.get('free') if h.get('free') is not None else '?'} band={h['band']}{flags}")
        if h.get("pressure"):
            out.append(f"    pressure: {h['pressure']}")
        for c in h.get("cards") or ():
            out.append(f"    {c['task_id']} [{c['board']}] @{c['assignee']} rule={c.get('rule') or '-'}")
    held = _map(rep.get("held"))
    top = list(_map(held.get("by_code")).items())[:TOP_REASONS_SHOWN]
    out.append(f"  held: {held.get('count', 0)}"
               + (" (" + ", ".join(f"{k} {v}" for k, v in top) + ")" if top else ""))
    rows = held.get("rows") or []
    for r in rows[:HELD_ROWS_SHOWN]:
        out.append(f"    {r['task_id']} [{r['board']}] {r['code']} host={r.get('host') or '-'} "
                   f"route={r.get('route') or '-'} since {_age(now, r.get('since'))} ago")
    if len(rows) > HELD_ROWS_SHOWN:
        out.append(f"    ... {len(rows) - HELD_ROWS_SHOWN} more (--json for all)")
    return out


# -- one card ---------------------------------------------------------------

def task_block(conn, task_id: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The ``Placement:`` block for one card (None = no such card)."""
    now = time.time() if now is None else float(now)
    own = not conn.in_transaction  # one snapshot; a caller's transaction is left as found
    if own:
        conn.execute("BEGIN")
    try:
        return _task_block(conn, task_id, now)
    finally:
        if own and conn.in_transaction:
            conn.execute("ROLLBACK")  # read-only: nothing to keep


def _task_block(conn, task_id: str, now: float) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    status, run_id = row
    placed = None
    for r in _running(conn):
        if r["task_id"] == task_id:
            placed = {"host": r["host"], "rule": r["rule"], "run_id": r["run_id"]}
    last = conn.execute(
        "SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1", (task_id, _pr.PLACEMENT_EVENT)).fetchone()
    held = next((h for h in _held(conn) if h["task_id"] == task_id), None)
    return {"task_id": task_id, "status": status, "run_id": run_id, "now": now,
            "running_on": placed,
            "last_placement": None if last is None else dict(_payload(last[0]), at=last[1]),
            "held": held}


def format_task(blk: Mapping) -> List[str]:
    now = float(blk.get("now") or time.time())
    out = [f"Placement: {blk['task_id']} ({blk.get('status')})"]
    run = blk.get("running_on")
    if run:
        out.append(f"  running on {run.get('host') or _kwp.STUDIO_HOST + ' (local)'} "
                   f"run={run.get('run_id')} rule={run.get('rule') or '-'}")
    last = blk.get("last_placement")
    if last:
        out.append(f"  last placement: route={last.get('route') or '-'} rule={last.get('rule') or '-'} "
                   f"host={last.get('host') or '-'} {_age(now, last.get('at'))} ago")
    h = blk.get("held")
    if h:
        out.append(f"  held: {h['code']} host={h.get('host') or '-'} since {_age(now, h.get('since'))} ago"
                   + (f" ({h['detail']})" if h.get("detail") else ""))
    if not (run or last or h):
        out.append("  no placement recorded")
    return out
