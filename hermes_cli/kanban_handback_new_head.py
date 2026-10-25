"""No-op handback gate: route a worker completion to review only when its PR has a commit the reviewer has not seen.

t_f716021d (2026-10-24). A worker run that ended with nothing new on its open PR was still auto-routed to review
(``completion_routed_to_review``), costing a reviewer round and resetting the review deadman:

* t_d72c9cfc / hermes-home#6525: the run posted one comment; the head was still 89acb51039, the head Apollo had
  just sent back.
* t_fafca854 / hermes-home#5877: the only new commit (484605f6d5) was the fold bot merging origin/main.

The reviewer's last-seen head is the ``head_sha`` of the card's newest ``review_coverage`` record (a send-back's
coverage, or the APPROVE record). A commit is NEW when it is one of the PR's own commits (``pulls/N/commits``, so
main's commits brought in by a merge are excluded), is not an ancestor of the seen head (``compare/<seen>...<head>``),
and is not a merge commit. Author is not used: implementer and fold bot push under the same GitHub App identity.

:func:`verdict` answers ``"unchanged"`` (head == seen), ``"merge_only"`` (only merge commits since seen) or None
(route as today). None is also the answer for every case it cannot prove: no seen head, a ``n/a`` head, an
unreadable PR, a compare that GitHub truncates. Inside pytest there is no default reader; tests pass a stub.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from typing import Callable, Iterable, Optional

EVENT = "completion_no_new_head"
OUTCOME = "no_new_head"
# Bounces per send-back before the completion routes to review anyway (a worker that cannot move the PR).
MAX_BOUNCES = 2
_TIMEOUT_S = 10
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# GitHub caps both lists at 250 commits; a full page means the answer may be incomplete.
_LIST_CAP = 250

# (repo, number, seen) -> {"head": sha, "pr_commits": [{"sha", "parents"}], "since_seen": [sha], "complete": bool}
Reader = Callable[[str, int, str], Optional[dict]]


def comment(seen: str) -> str:
    """The ONE comment a bounced handback leaves on the card."""
    return f"no new head since {seen[:10]}; the send-back stands"


def seen_head(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """``head_sha`` of the card's newest parseable ``review_coverage`` record, lowercased, or None."""
    from hermes_cli import kanban_db as kb

    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND body LIKE '%review_coverage:%' "
        "ORDER BY id DESC LIMIT 20", (task_id,),
    ).fetchall()
    record, _err = kb._latest_review_coverage([{"body": r[0] or ""} for r in rows])  # noqa: SLF001 -- the one parser
    if not isinstance(record, dict):
        return None
    head = str(record.get("head_sha") or "").strip().lower()
    return head if _SHA_RE.match(head) else None


def new_commits(view: dict, seen: str) -> Optional[list]:
    """Non-merge PR commits not reachable from ``seen``; None when the view cannot prove the answer."""
    if not isinstance(view, dict) or not view.get("complete"):
        return None
    since = {str(s).lower() for s in view.get("since_seen") or ()}
    out = []
    for c in view.get("pr_commits") or ():
        sha = str(c.get("sha") or "").lower()
        if sha in since and int(c.get("parents") or 0) < 2:
            out.append(sha)
    return out


def classify(view: Optional[dict], seen: str) -> Optional[str]:
    """``"unchanged"`` / ``"merge_only"`` / None (route) for one PR."""
    if not isinstance(view, dict):
        return None
    head = str(view.get("head") or "").lower()
    if not head:
        return None
    if head.startswith(seen) or seen.startswith(head):
        return "unchanged"
    fresh = new_commits(view, seen)
    if fresh is None or fresh:
        return None
    return "merge_only"


def _gh_json(args: list):
    try:
        proc = subprocess.run(
            ["gh", "api", *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "null")
    except ValueError:
        return None


def read_view(repo: str, number: int, seen: str) -> Optional[dict]:
    """REST: PR head, the PR's own commits, and the commits reachable from head but not from ``seen``."""
    pr = _gh_json([f"repos/{repo}/pulls/{number}"])
    head = str(((pr or {}).get("head") or {}).get("sha") or "") if isinstance(pr, dict) else ""
    if not head:
        return None
    view = {"head": head, "pr_commits": [], "since_seen": [], "complete": False}
    if head.lower().startswith(seen):
        view["complete"] = True
        return view
    commits = _gh_json([f"repos/{repo}/pulls/{number}/commits?per_page=100&page=1"])
    pages = [commits] if isinstance(commits, list) else None
    page = 2
    while pages is not None and len(pages[-1]) == 100 and page <= 3:
        nxt = _gh_json([f"repos/{repo}/pulls/{number}/commits?per_page=100&page={page}"])
        if not isinstance(nxt, list):
            pages = None
            break
        pages.append(nxt)
        page += 1
    cmp = _gh_json([f"repos/{repo}/compare/{seen}...{head}"])
    if pages is None or not isinstance(cmp, dict) or not isinstance(cmp.get("commits"), list):
        return None
    flat = [c for p in pages for c in p if isinstance(c, dict)]
    view["pr_commits"] = [{"sha": c.get("sha"), "parents": len(c.get("parents") or ())} for c in flat]
    view["since_seen"] = [c.get("sha") for c in cmp["commits"] if isinstance(c, dict)]
    total = cmp.get("total_commits")
    view["complete"] = (len(flat) < _LIST_CAP and isinstance(total, int)
                        and total == len(view["since_seen"]))
    return view


def _default_reader() -> Optional[Reader]:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return read_view


def _pending_send_back(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    """Event id of a ``changes_requested`` not yet answered by a ``review_requested``, or None."""
    row = conn.execute(
        "SELECT id, kind FROM task_events WHERE task_id = ? AND kind IN "
        "('changes_requested', 'review_requested') ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return int(row[0]) if row is not None and row[1] == "changes_requested" else None


def bounces_since(conn: sqlite3.Connection, task_id: str, event_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ? AND id > ?",
        (task_id, EVENT, event_id),
    ).fetchone()[0])


def verdict(conn: sqlite3.Connection, task_id: str, refs: Iterable, *,
            reader: Optional[Reader] = None) -> Optional[dict]:
    """``{"seen", "reason", "prs"}`` when NO ref has a commit the reviewer has not seen, else None (route).

    Applies only to a card under a pending send-back (its newest review transition is
    ``changes_requested``), and at most :data:`MAX_BOUNCES` times per send-back: past that the
    completion routes as today so a worker that cannot move the PR still reaches a human.
    ``refs`` are the PRs the completion would route on. Every one must be read and classified
    no-op; any unreadable or changed PR keeps today's route.
    """
    refs = list(refs)
    if not refs:
        return None
    send_back = _pending_send_back(conn, task_id)
    if send_back is None or bounces_since(conn, task_id, send_back) >= MAX_BOUNCES:
        return None
    seen = seen_head(conn, task_id)
    if seen is None:
        return None
    if reader is None:
        reader = _default_reader()
        if reader is None:
            return None
    reasons = {}
    for ref in refs:
        try:
            view = reader(ref.repo, ref.number, seen)
        except Exception:  # fail-open to today's route on any reader fault
            return None
        reason = classify(view, seen)
        if reason is None:
            return None
        reasons[f"{ref.repo}#{ref.number}"] = reason
    overall = "unchanged" if all(r == "unchanged" for r in reasons.values()) else "merge_only"
    return {"seen": seen, "reason": overall, "prs": reasons}


def bounce(conn: sqlite3.Connection, task_id: str, no_new: dict, *,
           expected_run_id: Optional[int], summary: Optional[str] = None,
           metadata: Optional[dict] = None) -> bool:
    """Close the worker run and return the card to its queue with the send-back standing. No review_requested.

    The run ends ``no_new_head`` carrying the worker's ``summary``/``metadata`` (the caller prefixes the
    bounce line and stages artifacts); :data:`EVENT` (a respawn-guard continuation kind) lets the next
    spawn through ``active_pr``. False when the run no longer owns the card.
    """
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None or row["status"] != "running" or row["current_run_id"] is None:
            return False
        run_id = int(row["current_run_id"])
        if expected_run_id is not None and run_id != int(expected_run_id):
            return False
        new_status = kb._landing_status_after_parents(conn, task_id)  # noqa: SLF001
        conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, worker_started_at = NULL WHERE id = ? AND current_run_id = ?",
            (new_status, task_id, run_id),
        )
        text = comment(no_new["seen"])
        kb._end_run(conn, task_id, outcome=OUTCOME, status=OUTCOME, summary=summary or text,  # noqa: SLF001
                    metadata=metadata or {"no_new_head": no_new})
        kb.add_comment(conn, task_id, "kanban", text)
        kb._append_event(conn, task_id, EVENT, dict(no_new, status=new_status),  # noqa: SLF001
                         run_id=run_id)
    return True
