"""A completion that names an OPEN PR is a review handoff, not ``done``.

Incident 2026-09-25 (t_1bd02e0b): a worker completed its card while its PR
(``ANG-Ventures/hermes-home#670``) was still OPEN with checks pending. The card went
``done``, its children unblocked, and nobody owned the merge until an operator
noticed the notification said "Task completed" instead of "handed off for
review".

:func:`open_pr_refs` extracts every explicitly-qualified PR reference from a
completion's evidence (result, summary, ``metadata.pr_url``, ``survivor_pr``
claims) and returns the ones GitHub reports OPEN. ``complete_task`` routes such
a completion to ``review`` instead of ``done`` so dependants stay gated.

:func:`find_done_with_open_pr` is the board lint for cards that already slipped
through (``kanban home-lint --open-prs``).

Lookup failures on the completion path fail CLOSED (t_36d0114e, 2026-09-27):
14 of 80 fresh PRs sat "green CI, card done, PR open" because the worker lane's
``gh`` read cap made every lookup time out, the old fail-open read that as
"not open", and ``complete`` wrote ``done``. A PR whose state cannot be read is
now treated like an open one -- the card goes to ``review``, which is not a
wedge: the review-card closer completes it on REST ``merged=true``. A definite
404 (the ref names no PR) is still "not open". The board lint
(:func:`find_done_with_open_pr`) reports only PRs positively read as OPEN.
Bare ``#N`` refs are ignored (no repo context is guessed) -- the same rule as
:mod:`hermes_cli.kanban_pr_gate`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import time
from typing import Callable, Iterable, Optional

from hermes_cli import kanban_pr_gate as _prg

_log = logging.getLogger(__name__)

# Per-completion cap: a result listing dozens of PRs must not turn one
# completion into a GitHub burst.
MAX_LOOKUPS_PER_COMPLETION = 10
LINT_WINDOW_DAYS = 7
MAX_LINT_LOOKUPS = 60

QueryFn = Callable[[str, int], Optional[dict]]


QUERY_TIMEOUT_SECONDS = 10
NOT_FOUND = "NOT_FOUND"


def query_pr_state(repo: str, number: int) -> Optional[dict]:
    """REST PR state: ``{"state": OPEN|CLOSED|MERGED|NOT_FOUND}`` or None.

    Unlike :func:`kanban_pr_gate.query_pr` this separates a definite 404
    (``NOT_FOUND``: the ref names no PR, e.g. an upstream number qualified with
    the fork's slug) from "cannot tell" (None: timeout, rate/READ cap, auth).
    Completion routing fails closed on None and must not on a 404.
    """
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{number}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=QUERY_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None
    if proc.returncode != 0:
        if "HTTP 404" in (proc.stderr or "") or '"status":"404"' in (proc.stdout or "").replace(" ", ""):
            return {"state": NOT_FOUND}
        return None
    try:
        payload = json.loads(proc.stdout or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("state"):
        return None
    return {"state": "MERGED" if payload.get("merged_at") else str(payload["state"]).upper()}


def _default_query() -> Optional[QueryFn]:
    """The real ``gh``-backed oracle, or None inside pytest.

    The unit suite completes many cards whose fixture text names real PRs; a
    real lookup there would make the suite network-dependent and its outcome
    depend on live GitHub state. Tests drive this path with an explicit stub.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return query_pr_state


def _iter_strings(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                yield item


def extract_pr_refs(
    *texts: Optional[str],
    metadata: Optional[dict] = None,
    survivor_pr=None,
) -> list:
    """Qualified PR refs (URL or ``owner/repo#N``), first-seen order, deduped."""
    sources = [t for t in texts if isinstance(t, str) and t.strip()]
    if isinstance(metadata, dict):
        for key in ("pr_url", "pr_urls", "pr"):
            sources.extend(_iter_strings(metadata.get(key)))
    sources.extend(_iter_strings(survivor_pr))
    out = []
    seen = set()
    for text in sources:
        for ref in _prg.parse_pr_refs(text, default_repo=None):
            key = (ref.repo.lower(), ref.number)
            if key not in seen:
                seen.add(key)
                out.append(ref)
    return out


def open_refs(refs, *, query_fn: Optional[QueryFn] = None,
              limit: int = MAX_LOOKUPS_PER_COMPLETION) -> list:
    """The subset of ``refs`` whose state is OPEN. Lookup errors fail open."""
    if query_fn is None:
        query_fn = _default_query()
        if query_fn is None:
            return []
    found = []
    for index, ref in enumerate(refs):
        if index >= limit:
            _log.warning("kanban open-pr check: lookup cap %d reached; rest unchecked", limit)
            break
        try:
            payload = query_fn(ref.repo, ref.number)
        except Exception as exc:  # fail-open, but say so
            _log.warning("kanban open-pr check: lookup %s failed (fail-open): %s", ref, exc)
            continue
        if not isinstance(payload, dict):
            _log.warning("kanban open-pr check: lookup %s returned no state (fail-open)", ref)
            continue
        if str(payload.get("state") or "").upper() == "OPEN":
            found.append(ref)
    return found


def unmerged_refs(refs, *, query_fn: Optional[QueryFn] = None,
                  limit: int = MAX_LOOKUPS_PER_COMPLETION, primary=()) -> tuple:
    """``(open, unverified)`` among ``refs``: the completion-path verdict.

    ``open`` = read OPEN (a merge-queued PR is OPEN over REST). ``unverified``
    = the lookup raised or returned no state -- fail CLOSED, the caller treats
    it like open. A definite NOT_FOUND / CLOSED / MERGED is neither.
    ``primary`` refs (the card's own ``--survivor-pr`` / ``metadata.pr_url``)
    are always looked up; the per-completion ``limit`` only caps prose refs,
    whose overflow stays unchecked as before.
    """
    if query_fn is None:
        query_fn = _default_query()
        if query_fn is None:
            return [], []
    primary_keys = {(r.repo.lower(), r.number) for r in primary}
    opened, unverified = [], []
    prose_seen = 0
    for ref in refs:
        if (ref.repo.lower(), ref.number) not in primary_keys:
            if prose_seen >= limit:
                _log.warning("kanban open-pr check: lookup cap %d reached; %s unchecked", limit, ref)
                continue
            prose_seen += 1
        try:
            payload = query_fn(ref.repo, ref.number)
        except Exception as exc:
            _log.warning("kanban open-pr check: lookup %s failed (fail-closed -> review): %s", ref, exc)
            unverified.append(ref)
            continue
        state = str(payload.get("state") or "").upper() if isinstance(payload, dict) else ""
        if not state:
            _log.warning("kanban open-pr check: lookup %s returned no state (fail-closed -> review)", ref)
            unverified.append(ref)
        elif state == "OPEN":
            opened.append(ref)
    return opened, unverified


def open_pr_refs(*texts: Optional[str], metadata: Optional[dict] = None,
                 survivor_pr=None, query_fn: Optional[QueryFn] = None) -> list:
    """Extract + resolve: the PRs that block ``done`` -- OPEN or unreadable."""
    refs = extract_pr_refs(*texts, metadata=metadata, survivor_pr=survivor_pr)
    if not refs:
        return []
    primary = extract_pr_refs(metadata=metadata, survivor_pr=survivor_pr)
    opened, unverified = unmerged_refs(refs, query_fn=query_fn, primary=primary)
    return opened + unverified


ROUTE_COMMENT = "survivor PR open; card closes on merged=true"


def route_note(refs) -> str:
    return "auto-routed: " + ", ".join(f"PR {r.repo}#{r.number}" for r in refs) + " still open"


def route_comment(refs) -> str:
    """The ONE comment a routed completion leaves (the closer reads PR refs from it)."""
    return f"{ROUTE_COMMENT} ({', '.join(f'{r.repo}#{r.number}' for r in refs)})"


def find_done_with_open_pr(conn: sqlite3.Connection, *, days: int = LINT_WINDOW_DAYS,
                           query_fn: Optional[QueryFn] = None,
                           now: Optional[float] = None) -> list:
    """``done`` cards completed in the last ``days`` whose evidence names an OPEN PR."""
    if query_fn is None:
        query_fn = _prg.query_pr
    cutoff = int((now if now is not None else time.time()) - days * 86400)
    rows = conn.execute(
        "SELECT id, title, result, completed_at FROM tasks "
        "WHERE status = 'done' AND completed_at >= ? ORDER BY completed_at DESC",
        (cutoff,),
    ).fetchall()
    cache = {}
    budget = MAX_LINT_LOOKUPS
    hits = []
    for row in rows:
        run = conn.execute(
            "SELECT summary, metadata FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        summary = run["summary"] if run else None
        try:
            meta = json.loads(run["metadata"]) if run and run["metadata"] else None
        except (TypeError, ValueError):
            meta = None
        refs = extract_pr_refs(row["result"], summary,
                               metadata=meta if isinstance(meta, dict) else None)
        opened = []
        for ref in refs:
            key = (ref.repo.lower(), ref.number)
            if key not in cache:
                if budget <= 0:
                    continue
                budget -= 1
                cache[key] = bool(open_refs([ref], query_fn=query_fn, limit=1))
            if cache[key]:
                opened.append(f"{ref.repo}#{ref.number}")
        if opened:
            hits.append({"id": row["id"], "title": row["title"],
                         "completed_at": row["completed_at"], "open_prs": opened})
    return hits
