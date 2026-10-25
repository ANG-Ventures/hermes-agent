"""No-op handback gate: route a worker completion to review only when its PR has a commit the reviewer has not seen.

t_f716021d (2026-10-24). A worker run that ended with nothing new on its open PR was still auto-routed to review
(``completion_routed_to_review``), costing a reviewer round and resetting the review deadman:

* t_d72c9cfc / hermes-home#6525: the run posted one comment; the head was still 89acb51039, the head Apollo had
  just sent back.
* t_fafca854 / hermes-home#5877: the only new commit (484605f6d5) was the fold bot merging origin/main.

The reviewer's last-seen head is the ``head_sha`` of the card's newest ``review_coverage`` record (a send-back's
coverage, or the APPROVE record). The invariant is "the PR's own content is unchanged since ``seen``" (Apollo r1,
t_c9887522 fold-2): a merge commit can carry a hand conflict resolution, so commit shape alone proves nothing.

* The head must DESCEND from ``seen`` (``compare/<seen>...<head>`` status ``ahead``/``identical``); a rewound or
  rewritten head is a change the reviewer has not seen.
* The PR's content at ``seen`` (``compare/<base>...<seen>``) and at the head (``compare/<base>...<head>``) are
  compared per path: ``status``, ``previous_filename``, the ordered patch lines with only the line NUMBERS of each
  ``@@`` header dropped (hunk offsets move when main moves; the trailing function context stays), and the PR's
  mode change for the path, merge base -> tip, from the git trees (``patch`` text carries no mode). Equal means a pure fold. The
  equivalence is lossless or it routes (Apollo r2, t_f716021d).

:func:`verdict` answers ``"unchanged"`` (head == seen), ``"merge_only"`` (content unchanged) or None (route as
today). None is also the answer for every case it cannot prove: no seen head, a ``n/a`` head, an unreadable PR, a
file list at the REST cap, a file with no ``patch``, an unreadable mode. Inside pytest there is no default reader; tests pass a stub.
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
# GitHub returns at most 300 files for a compare and for pulls/N/files (3 pages of 100 here).
_FILES_CAP = 300
_DESCENDS = frozenset({"ahead", "identical"})
# Line numbers of a hunk header; the trailing function context after the second ``@@`` is kept.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")

# (repo, number, seen) -> {"head": sha, "status": compare(seen...head).status,
#                          "seen_files": [{"filename", "status", "patch", ...}], "head_files": [...],
#                          "seen_modes": {path: [mode at merge base, mode at tip]}, "head_modes": {...}}
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


def content(files, modes) -> Optional[dict]:
    """The PR's content per path, or None when it cannot be proved.

    Per path: ``status``, ``previous_filename``, the mode change (merge base -> tip) and the ordered hunk lines
    (context, ``+``, ``-``, and each ``@@`` header with its line numbers dropped). Context lines stay in: a
    conflict resolution that keeps main's adjacent insert beside the PR's own lines leaves the ``+``/``-``
    multiset equal and changes only the context (t_c9887522 fold-2, 44e346d675). The header's function context
    stays so an edit moved to another region does not compare equal (Prism 8b66a52674df), and the modes are
    compared because ``patch`` omits them (Prism e8f58301f565).
    """
    if not isinstance(files, list) or len(files) >= _FILES_CAP or not isinstance(modes, dict):
        return None
    out: dict = {}
    for f in files:
        if not isinstance(f, dict) or not f.get("filename") or not isinstance(f.get("patch"), str):
            return None
        path = str(f["filename"])
        mode = modes.get(path)
        if path in out or not isinstance(mode, (list, tuple)) or len(mode) != 2:
            return None
        # The PR's mode CHANGE, not the absolute modes: a chmod on main moves both ends and is not PR content.
        delta = tuple(mode) if mode[0] != mode[1] else ("same",)
        out[path] = (str(f.get("status") or ""), str(f.get("previous_filename") or ""), delta,
                     tuple(_HUNK_RE.sub("@@ @@", ln) for ln in f["patch"].splitlines()))
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
    if view.get("status") not in _DESCENDS:
        return None
    at_seen = content(view.get("seen_files"), view.get("seen_modes"))
    at_head = content(view.get("head_files"), view.get("head_modes"))
    if at_seen is None or at_head is None or at_seen != at_head:
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


def _tree_modes(repo: str, commit: str) -> Optional[dict]:
    """``{path: mode}`` for every blob at ``commit`` (one recursive tree read), or None if unreadable/truncated."""
    tree = _gh_json([f"repos/{repo}/git/trees/{commit}?recursive=1"])
    if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list) or tree.get("truncated"):
        return None
    return {e.get("path"): e.get("mode") for e in tree["tree"] if isinstance(e, dict)}


def _modes(repo: str, files, merge_base: str, tip: str) -> Optional[dict]:
    """``{path: [mode at merge_base, mode at tip]}`` (None = absent there), or None when unreadable."""
    if not isinstance(files, list) or not merge_base or not tip:
        return None
    at_base, at_tip = _tree_modes(repo, merge_base), _tree_modes(repo, tip)
    if at_base is None or at_tip is None:
        return None
    out = {}
    for f in files:
        if not isinstance(f, dict) or not f.get("filename"):
            return None
        path = str(f["filename"])
        out[path] = [at_base.get(str(f.get("previous_filename") or path)), at_tip.get(path)]
    return out


def read_view(repo: str, number: int, seen: str) -> Optional[dict]:
    """REST: PR head, whether it descends from ``seen``, and the PR's files and modes at ``seen`` and at the head."""
    pr = _gh_json([f"repos/{repo}/pulls/{number}"])
    if not isinstance(pr, dict):
        return None
    head = str((pr.get("head") or {}).get("sha") or "")
    base = str((pr.get("base") or {}).get("ref") or "")
    if not head or not base:
        return None
    if head.lower().startswith(seen):
        return {"head": head}
    ahead = _gh_json([f"repos/{repo}/compare/{seen}...{head}"])
    at_seen = _gh_json([f"repos/{repo}/compare/{base}...{seen}"])
    at_head = _gh_json([f"repos/{repo}/compare/{base}...{head}"])
    if not all(isinstance(x, dict) for x in (ahead, at_seen, at_head)):
        return None
    view = {"head": head, "status": ahead.get("status"),
            "seen_files": at_seen.get("files"), "head_files": at_head.get("files")}
    if ahead.get("status") not in _DESCENDS:
        return view
    for side, cmp_, tip in (("seen", at_seen, seen), ("head", at_head, head)):
        view[f"{side}_modes"] = _modes(repo, cmp_.get("files"),
                                       str((cmp_.get("merge_base_commit") or {}).get("sha") or ""), tip)
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


def bounced_note(conn: sqlite3.Connection, task_id: str, expected_run_id: Optional[int]) -> Optional[str]:
    """The NOT-handed-back note when ``expected_run_id`` (or, with none, the newest run) ended in a bounce, else None.

    Read by the supplied run id: a bounce returns the card to ready, so a successor run can be claimed before the
    caller gets here, and the newest run would hide the bounce (Prism bec5d5358114).
    """
    from hermes_cli import kanban_db as kb

    if expected_run_id is not None:
        run = kb.get_run(conn, int(expected_run_id))
        if run is not None and run.task_id != task_id:
            return None
    else:
        run = kb.latest_run(conn, task_id)
    if run is None or run.outcome != OUTCOME:
        return None
    first = (run.summary or "").splitlines()[0] if run.summary else "no new head"
    return f"NOT handed back: {first}. Push the requested changes, then complete again."
