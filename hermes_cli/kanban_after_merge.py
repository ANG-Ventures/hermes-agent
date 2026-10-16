"""``after-merge`` link edges: gate a child on the parent's PR merging.

A ``blocks`` edge holds the child until the parent card is DONE. That is the
wrong gate for a child that is itself part of the parent's close condition
("Prism residue after merge", "acceptance drill after the PR lands"): the
parent cannot close until the child runs, and the child cannot start until
the parent closes. t_a0afd4d7 (2026-10-16) deadlocked exactly that way until
an operator unlinked by hand.

An ``after-merge`` edge gates like ``blocks`` (every gating query reads
:data:`kanban_db.GATING_LINK_KINDS`) until :func:`release_merged_edges`
sees every fleet PR the parent's runs recorded read MERGED. It then rewrites
the edge to ``derived-from`` (the lineage stays, the gate goes) and the same
dispatcher tick promotes the child. A parent that reaches ``done`` first
releases the child the ordinary way.

:func:`refuse_close_condition_deadlock` is the link-time lint: a ``blocks``
edge whose child id appears in the parent's body is refused, naming this kind.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from typing import Callable, Optional

QueryFn = Callable[[str, int], Optional[dict]]

# Bounded GitHub I/O per tick; the rest wait for a later tick.
MAX_LOOKUPS_PER_TICK = 10
# Non-MERGED states are re-read after this long; MERGED is cached for good.
CACHE_TTL_SECONDS = 300

_CACHE: dict[tuple[str, int], tuple[str, float]] = {}


def describe_gate(parent_id: str, child_id: str, kind: str) -> str:
    """The gate an edge creates, in the words a card author needs."""
    from hermes_cli import kanban_db as kb

    if kind == kb.LINK_KIND_AFTER_MERGE:
        return (f"{child_id} will not start until {parent_id}'s PR is MERGED "
                f"(or {parent_id} is DONE)")
    if kind == kb.LINK_KIND_DERIVED_FROM:
        return f"{child_id} is not gated on {parent_id} (provenance only)"
    return f"{child_id} will not start until {parent_id} is DONE"


def refuse_close_condition_deadlock(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    """Raise ValueError when a ``blocks`` edge would gate a child the parent's
    body names. Such a child is (or reads as) part of the parent's close
    condition, so the parent waits on the child and the child on the parent.
    A parent already done/archived gates nothing and is let through."""
    row = conn.execute("SELECT status, body FROM tasks WHERE id = ?", (parent_id,)).fetchone()
    if row is None or row["status"] in ("done", "archived"):
        return
    if not re.search(rf"(?<![0-9A-Za-z_]){re.escape(child_id)}(?![0-9A-Za-z_])", row["body"] or ""):
        return
    raise ValueError(
        f"refusing `blocks` {parent_id} -> {child_id}: {parent_id}'s body names {child_id}, "
        f"so {child_id} reads as part of {parent_id}'s close condition, and a `blocks` edge "
        f"holds {child_id} until {parent_id} is DONE (deadlock, t_a0afd4d7). To start it once "
        f"the PR merges: `hermes kanban link {parent_id} {child_id} --kind after-merge`; "
        f"for lineage only: `--kind derived-from`"
    )


def _default_query() -> Optional[QueryFn]:
    # No live GitHub reads inside pytest: tests pass an explicit oracle.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    from hermes_cli import kanban_pr_gate as prg

    return prg.query_pr


def _state(ref, query_fn: QueryFn, now: float, budget: list) -> Optional[str]:
    key = (ref.repo.lower(), ref.number)
    hit = _CACHE.get(key)
    if hit is not None and (hit[0] == "MERGED" or now - hit[1] < CACHE_TTL_SECONDS):
        return hit[0]
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    try:
        payload = query_fn(ref.repo, ref.number)
    except Exception:
        return None
    state = str((payload or {}).get("state") or "").upper() or None
    if state:
        _CACHE[key] = (state, now)
    return state


def release_merged_edges(
    conn: sqlite3.Connection,
    *,
    query_fn: Optional[QueryFn] = None,
    max_lookups: int = MAX_LOOKUPS_PER_TICK,
    now: Optional[float] = None,
) -> list[tuple[str, str]]:
    """Rewrite every ``after-merge`` edge whose parent's recorded fleet PRs all
    read MERGED to ``derived-from``; return the released ``(parent, child)``
    pairs. A parent with no recorded PR, an OPEN/CLOSED PR or an unreadable one
    keeps the gate. Lookups happen before the short write txn."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_open_pr as _open_pr

    edges = conn.execute(
        "SELECT l.parent_id, l.child_id FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id JOIN tasks c ON c.id = l.child_id "
        "WHERE l.kind = ? AND p.status NOT IN ('done', 'archived') "
        "AND c.status NOT IN ('done', 'archived') ORDER BY l.parent_id, l.child_id",
        (kb.LINK_KIND_AFTER_MERGE,),
    ).fetchall()
    if not edges:
        return []
    query_fn = query_fn or _default_query()
    if query_fn is None:
        return []
    from hermes_cli import kanban_pr_gate as prg

    # A stubbed oracle must never release real cards (the 2026-09-21 probe).
    prg.assert_write_allowed(query_fn)
    now = time.time() if now is None else now
    budget = [max(0, int(max_lookups))]
    merged_prs: dict[str, list[str]] = {}
    for parent_id in dict.fromkeys(e["parent_id"] for e in edges):
        refs = _open_pr.split_fleet(_open_pr.extract_pr_refs(
            survivor_pr=kb._card_recorded_pr_refs(conn, parent_id)))[0]
        if refs and all(_state(r, query_fn, now, budget) == "MERGED" for r in refs):
            merged_prs[parent_id] = [str(r) for r in refs]
    released: list[tuple[str, str]] = []
    if not merged_prs:
        return released
    with kb.write_txn(conn):
        for edge in edges:
            parent_id, child_id = edge["parent_id"], edge["child_id"]
            if parent_id not in merged_prs:
                continue
            cur = conn.execute(
                "UPDATE task_links SET kind = ? WHERE parent_id = ? AND child_id = ? AND kind = ?",
                (kb.LINK_KIND_DERIVED_FROM, parent_id, child_id, kb.LINK_KIND_AFTER_MERGE),
            )
            if cur.rowcount == 1:
                kb._append_event(conn, child_id, "after_merge_released",
                                 {"parent": parent_id, "prs": merged_prs[parent_id]})
                released.append((parent_id, child_id))
    return released
