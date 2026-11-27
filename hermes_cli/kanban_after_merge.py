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
dispatcher tick promotes the child. The parent's status does not release it:
a parent marked ``done`` while its PR is still open (t_ccdca9ef, completed at
2026-10-18 02:02 with hermes-home#4662 a reviewer-held draft) keeps the gate
until that PR merges (t_ab2499c5). Only a parent that closes with NO recorded
fleet PR releases the edge on close (:func:`release_prless_closed_parents`).

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

# Keyed by (id(oracle), repo, pr): a sandbox/test oracle's synthetic MERGED must never release a live card
# read through the real oracle in the same interpreter (Prism 50dc72190d84). id() is unique only while the
# object lives, so each row RETAINS its oracle and a hit counts only when it ``is`` the caller's: a freed
# sandbox oracle's id reused by a new live one can never inherit its MERGED (Prism 7aaa3acdd804).
_CACHE: dict[tuple[int, str, int], tuple[QueryFn, str, float]] = {}
# parent id -> when its PRs were last looked up. Parents are checked oldest
# first, so a fixed budget cannot starve the parents that sort last.
_LAST_CHECKED: dict[str, float] = {}


def describe_gate(parent_id: str, child_id: str, kind: str) -> str:
    """The gate an edge creates, in the words a card author needs."""
    from hermes_cli import kanban_db as kb

    if kind == kb.LINK_KIND_AFTER_MERGE:
        return (f"{child_id} will not start until {parent_id}'s PR is MERGED "
                f"(or {parent_id} is DONE with no PR recorded)")
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


def _cached(ref, query_fn: QueryFn, now: float) -> Optional[tuple]:
    # Keyed on the repo bytes as recorded (the identifier-key lint): a case variant of one repo is one extra read,
    # never a wrong verdict.
    hit = _CACHE.get((id(query_fn), ref.repo, ref.number))
    if hit is not None and hit[0] is query_fn and (hit[1] == "MERGED" or now - hit[2] < CACHE_TTL_SECONDS):
        return hit
    return None


def _state(ref, query_fn: QueryFn, now: float, budget: list,
           prefetched: Optional[dict] = None) -> Optional[str]:
    key = (id(query_fn), ref.repo, ref.number)
    hit = _cached(ref, query_fn, now)
    if hit is not None:
        return hit[1]
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    pkey = (ref.repo, ref.number)
    try:
        payload = prefetched[pkey] if prefetched is not None and pkey in prefetched else query_fn(ref.repo, ref.number)
    except Exception:
        return None
    state = str((payload or {}).get("state") or "").upper() or None
    if state:
        _CACHE[key] = (query_fn, state, now)
    return state


def _batch_reads(conn: sqlite3.Connection, parents: list, query_fn: QueryFn, now: float,
                 budget: int) -> Optional[dict]:
    """The real oracle only (t_97a78e19): the first ``budget`` uncached refs the loop below will read, fetched
    with one ``gh api graphql`` per repo (:func:`kanban_pr_gate.query_prs`) instead of one ``gh`` per PR. The loop
    still spends one lookup per ref, so the per-tick budget and every verdict are what per-PR reads give. A test
    oracle keeps the per-PR path (None)."""
    from hermes_cli import kanban_pr_gate as prg

    if query_fn is not prg._REAL_QUERY_PR or budget <= 0:
        return None
    plan: dict = {}
    for parent_id in parents:
        for ref in _fleet_refs(conn, parent_id):
            key = (ref.repo, ref.number)
            if len(plan) < budget and key not in plan and _cached(ref, query_fn, now) is None:
                plan[key] = ref
        if len(plan) >= budget:
            break
    by_repo: dict = {}
    for key, ref in plan.items():
        by_repo.setdefault(key[0], (ref.repo, []))[1].append(ref.number)
    out: dict = {}
    for slug, (repo, numbers) in by_repo.items():
        try:
            answers = prg.query_prs(repo, numbers)
        except Exception:
            answers = {}
        for n in numbers:
            out[(slug, n)] = answers.get(n)
    return out


def _fleet_refs(conn: sqlite3.Connection, parent_id: str) -> list:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_open_pr as _open_pr

    return _open_pr.split_fleet(_open_pr.extract_pr_refs(
        survivor_pr=kb._card_recorded_pr_refs(conn, parent_id)))[0]


def release_merged_edges(
    conn: sqlite3.Connection,
    *,
    query_fn: Optional[QueryFn] = None,
    max_lookups: int = MAX_LOOKUPS_PER_TICK,
    now: Optional[float] = None,
) -> list[tuple[str, str]]:
    """Rewrite every ``after-merge`` edge whose parent's recorded fleet PRs all
    read MERGED to ``derived-from``; return the released ``(parent, child)``
    pairs. An OPEN (draft or not) or unreadable PR keeps the gate, open
    parent or done one alike. A parent with no recorded PR is left to
    :func:`release_prless_closed_parents`. Lookups happen before the short
    write txn, which re-reads the parent's PR set and skips any parent whose
    set changed meanwhile."""
    from hermes_cli import kanban_db as kb

    edges = conn.execute(
        "SELECT l.parent_id, l.child_id FROM task_links l "
        "JOIN tasks c ON c.id = l.child_id "
        "WHERE l.kind = ? "
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
    closed_prs: dict[str, tuple[list[str], list[str]]] = {}
    parents = sorted(dict.fromkeys(e["parent_id"] for e in edges),
                     key=lambda pid: (_LAST_CHECKED.get(pid, 0.0), pid))
    prefetched = _batch_reads(conn, parents, query_fn, now, budget[0])
    for parent_id in parents:
        if budget[0] <= 0:
            break
        refs = _fleet_refs(conn, parent_id)
        if not refs:
            continue
        _LAST_CHECKED[parent_id] = now
        # No short-circuit: every ref is read, so the cache fills evenly.
        states = [_state(r, query_fn, now, budget, prefetched) for r in refs]
        closed = [str(r) for r, state in zip(refs, states) if state == "CLOSED"]
        if closed:
            closed_prs[parent_id] = ([str(r) for r in refs], closed)
        if all(st == "MERGED" for st in states):
            merged_prs[parent_id] = [str(r) for r in refs]
    released: list[tuple[str, str]] = []
    if not merged_prs and not closed_prs:
        return released
    with kb.write_txn(conn):
        for edge in edges:
            parent_id, child_id = edge["parent_id"], edge["child_id"]
            if parent_id in closed_prs:
                recorded, closed = closed_prs[parent_id]
                if [str(r) for r in _fleet_refs(conn, parent_id)] != recorded:
                    continue
                reason = (f"prerequisite {', '.join(closed)} closed unmerged: "
                          "name a replacement PR or waive")
                cur = conn.execute(
                    "UPDATE tasks SET status='triage', result=? WHERE id=? "
                    "AND status IN ('todo', 'ready') AND EXISTS ("
                    "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=? AND kind=?)",
                    (reason, child_id, parent_id, child_id, kb.LINK_KIND_AFTER_MERGE),
                )
                if cur.rowcount:
                    kb._append_event(conn, child_id, "after_merge_prerequisite_closed",
                                     {"parent": parent_id, "prs": closed, "reason": reason})
                continue
            if parent_id not in merged_prs:
                continue
            # The lookups ran outside the txn: a parent that recorded a new PR,
            # or closed/reopened since, is re-judged next tick, never released
            # on the stale read.
            if [str(r) for r in _fleet_refs(conn, parent_id)] != merged_prs[parent_id]:
                continue
            cur = conn.execute(
                "UPDATE task_links SET kind = ? WHERE parent_id = ? AND child_id = ? AND kind = ? "
                "AND EXISTS (SELECT 1 FROM tasks c WHERE c.id = ? "
                "            AND c.status NOT IN ('done', 'archived'))",
                (kb.LINK_KIND_DERIVED_FROM, parent_id, child_id, kb.LINK_KIND_AFTER_MERGE, child_id),
            )
            if cur.rowcount == 1:
                kb._append_event(conn, child_id, "after_merge_released",
                                 {"parent": parent_id, "prs": merged_prs[parent_id]})
                released.append((parent_id, child_id))
    return released


def release_prless_closed_parents(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Rewrite to ``derived-from`` every ``after-merge`` edge whose parent is
    ``done``/``archived`` and recorded NO fleet PR: there is nothing to merge,
    so the close is the release (the pre-t_ab2499c5 behaviour for that case).
    A closed parent WITH a recorded PR is left to :func:`release_merged_edges`.
    No GitHub I/O; opens its own write txn only when something is released."""
    from hermes_cli import kanban_db as kb

    edges = conn.execute(
        "SELECT l.parent_id, l.child_id FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id JOIN tasks c ON c.id = l.child_id "
        "WHERE l.kind = ? AND p.status IN ('done', 'archived') "
        "AND c.status NOT IN ('done', 'archived') ORDER BY l.parent_id, l.child_id",
        (kb.LINK_KIND_AFTER_MERGE,),
    ).fetchall()
    prless = {pid for pid in dict.fromkeys(e["parent_id"] for e in edges) if not _fleet_refs(conn, pid)}
    released: list[tuple[str, str]] = []
    if not prless:
        return released
    with kb.write_txn(conn):
        for edge in edges:
            parent_id, child_id = edge["parent_id"], edge["child_id"]
            if parent_id not in prless:
                continue
            # The PR-less read ran outside the txn: a parent that recorded a PR
            # meanwhile waits for that PR to merge, never released on the stale read.
            if _fleet_refs(conn, parent_id):
                continue
            cur = conn.execute(
                "UPDATE task_links SET kind = ? WHERE parent_id = ? AND child_id = ? AND kind = ? "
                "AND EXISTS (SELECT 1 FROM tasks p WHERE p.id = ? "
                "            AND p.status IN ('done', 'archived'))",
                (kb.LINK_KIND_DERIVED_FROM, parent_id, child_id, kb.LINK_KIND_AFTER_MERGE, parent_id),
            )
            if cur.rowcount == 1:
                kb._append_event(conn, child_id, "after_merge_released",
                                 {"parent": parent_id, "prs": [], "reason": "parent_closed_without_pr"})
                released.append((parent_id, child_id))
    return released
