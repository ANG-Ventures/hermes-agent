#!/usr/bin/env python3
# SCHEDULE: manual (Apollo runs it on the Studio before and after the t_e604c0f9 deploy)
"""Replay a window of kanban ``placement`` events: BEFORE vs AFTER Studio placements by rule (t_e604c0f9).

Read-only. For every placement event in the window that landed on the Studio (route ``local``), re-judge the card
with the Studio-last-resort rules of t_c2fce87e (#138) + t_e604c0f9:

* ``host_studio``: stays only if the card's ``requires:`` names a capability the Studio row of
  fleet/host-capabilities.json serves (or control-plane-read); else -> ``fwa``.
* ``local_files``: continuity. -> ``studio-while-room`` (runs here while the gate admits, carried to the pool when
  it does not) if the scratch dir is carryable NOW, else ``local_files`` (kept).
* ``workspace_kind``: -> ``fwa`` when ``kanban_worktree_port.port_for(..., allow_studio_tree=True)`` ports the card
  with the repo state as it is NOW (an own clean pushed tree, a not-yet-made .worktrees/<id>), else kept.
* every other rule: unchanged.

Usage (runtime python, so the package imports):
    python3 scripts/kanban_studio_pin_replay.py [--db ~/.hermes/kanban.db] [--hours 24] [--json]

The AFTER column is a classification, not a simulation of capacity: ``fwa`` means "no longer pinned; FWA places it
(the Studio only as last resort)". Directory and repo state is read now, not as it was at the event.
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
import time
from pathlib import Path

STUDIO_ROUTES = {"local"}


def _home() -> Path:
    from hermes_cli import kanban_db as kb
    return kb.kanban_home()


def _studio_reasons(fleet: Path):
    from hermes_cli import kanban_host_capabilities as khc
    from hermes_cli import kanban_worker_pool as kwp
    try:
        return kwp.studio_map_reasons(khc.load_map(fleet))
    except Exception:
        return None


def after_rule(rule: str, task, fleet: Path, reasons) -> str:
    from hermes_cli import kanban_worker_hosts as kwh
    from hermes_cli import kanban_worker_pool as kwp
    from hermes_cli import kanban_worktree_port as kwt
    if rule == "host_studio":
        return "host_studio" if kwp.studio_pin_justified(task["body"], reasons) else "fwa"
    if rule == "local_files":
        return "studio-while-room" if kwh.carry_size_ok(task["workspace_path"]) else "local_files"
    if rule == "workspace_kind":
        try:
            port = kwt.port_for(task["id"], task["workspace_path"], kwt.board_default_workdir(None), fleet,
                                branch=task["branch_name"], allow_studio_tree=True)
        except Exception:
            port = None
        return "fwa" if port is not None else "workspace_kind"
    return rule


def replay(db: Path, hours: float, fleet: Path, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT task_id, payload FROM task_events WHERE kind IN ('placement', 'allocation') AND created_at >= ?",
                        (int(now - hours * 3600),)).fetchall()
    reasons = _studio_reasons(fleet)
    before, after = collections.Counter(), collections.Counter()
    moved = collections.defaultdict(list)
    tasks: dict = {}
    total = 0
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
        except ValueError:
            continue
        total += 1
        if p.get("route") not in STUDIO_ROUTES:
            continue
        rule = str(p.get("rule") or "default").split("(")[0].split(":")[0]
        t = tasks.get(r["task_id"])
        if t is None:
            t = conn.execute("SELECT id, body, workspace_path, branch_name FROM tasks WHERE id=?",
                             (r["task_id"],)).fetchone()
            tasks[r["task_id"]] = t
        before[rule] += 1
        new = after_rule(rule, t, fleet, reasons) if t is not None else rule
        after[new] += 1
        if new != rule:
            moved[f"{rule}->{new}"].append(r["task_id"])
    return {"window_h": hours, "placements": total, "studio_before": sum(before.values()),
            "studio_after_pinned": sum(n for k, n in after.items() if k not in ("fwa", "studio-while-room")),
            "before": dict(before), "after": dict(after),
            "moved": {k: sorted(set(v)) for k, v in moved.items()}, "capability_map": reasons is not None}


def render(res: dict) -> str:
    rules = sorted(set(res["before"]) | {k for k in res["after"] if k not in ("fwa", "studio-while-room")})
    out = [f"placements {res['placements']} in {res['window_h']:g} h; Studio {res['studio_before']} BEFORE, "
           f"{res['studio_after_pinned']} still pinned AFTER (capability map read: {res['capability_map']})",
           f"{'rule':<28}{'BEFORE':>8}{'AFTER':>8}"]
    for k in rules:
        out.append(f"{k:<28}{res['before'].get(k, 0):>8}{res['after'].get(k, 0):>8}")
    for k in ("studio-while-room", "fwa"):
        out.append(f"{'-> ' + k:<28}{'':>8}{res['after'].get(k, 0):>8}")
    for k, ids in sorted(res["moved"].items()):
        out.append(f"  {k}: {len(ids)} card(s): {', '.join(ids[:8])}{' ...' if len(ids) > 8 else ''}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    home = _home()
    db = a.db or home / "kanban.db"
    if not db.exists():
        print(f"no board db at {db}", file=sys.stderr)
        return 2
    res = replay(db, a.hours, home / "fleet")
    print(json.dumps(res, indent=2) if a.json else render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
