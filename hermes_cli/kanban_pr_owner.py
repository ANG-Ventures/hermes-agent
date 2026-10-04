"""One running card per PR.

Incident 2026-10-03 (t_cb70d390): ``ANG-Ventures/hermes-agent#1624`` had its
review card t_7c46c0d0 plus helpers t_91f21ce8, t_fba53592 and t_34200a86
pushing to the same branch in the same afternoon. Every push restarted CI for
the others, and Apollo had to post STOP on each card by hand. Over the 3 days
before the fix, 52 pairs of worker runs overlapped on one fleet PR; 16 of them
were on #1624.

The rule: a PR has at most ONE running card.

* :func:`card_pr_targets` is the PRs a card works on: the PR in a
  ``rebase:<repo>#<n>@<head>`` idempotency key, fleet-qualified PR refs in the
  TITLE, and ``pr_url`` / ``pr_urls`` / ``pr`` in the card's run metadata. The
  body is NOT read. Bodies cite other PRs as context, and measured over the
  same 3 days they added 43 overlap pairs of mostly unrelated cards.
* The dispatcher refuses to spawn a ready card while another card running on
  one of its PRs is fresh (``pr_owner_busy``, owner named). A STALE owner (its
  run is older than :data:`STALE_OWNER_SECONDS` and the PR head's commit is
  older too) is reclaimed and the PR passes to the waiting card. There is never
  a second worker. An unknown head-commit time counts as fresh: no reclaim and
  no spawn.
* ``create_task`` refuses a ``rebase:``-keyed helper card at birth while a
  fresh owner runs (:class:`PrOwnerBusyError`). This covers both minters
  (kanban-review-merge-pass and fleet-merge-horizon-watch) without changing
  their scripts. A refused create exits non-zero with the owner named.
* :func:`pr_cards` lists every card on a PR for ``kanban show``.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import time
from datetime import datetime
from typing import Callable, Iterable, Optional

from hermes_cli import kanban_pr_gate as _prg
from hermes_cli.kanban_open_pr import FLEET_OWNERS

_log = logging.getLogger(__name__)

# The card's rule: "a stale owner (no push/commit in 2 h) is reassigned".
STALE_OWNER_SECONDS = 2 * 3600
GUARD_REASON = "pr_owner_busy"
REASSIGN_EVENT = "pr_owner_reassigned"

_REBASE_KEY_RE = re.compile(r"^rebase:([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(\d+)@", re.IGNORECASE)
_META_KEYS = ("pr_url", "pr_urls", "pr")
_GH_TIMEOUT_SECONDS = 10
_HEAD_CACHE_TTL = 300
_HEAD_CACHE: dict = {}

HeadCommitFn = Callable[[str, int], Optional[int]]


class PrOwnerBusyError(ValueError):
    """``create_task`` refused a rebase helper: the PR already has a running card."""

    def __init__(self, message: str, owner: dict):
        super().__init__(message)
        self.owner = owner


def _fleet(repo: str) -> bool:
    return repo.split("/", 1)[0].lower() in FLEET_OWNERS


def _refs(text) -> Iterable[tuple]:
    if not isinstance(text, str):
        return ()
    return [(r.repo.lower(), r.number) for r in _prg.parse_pr_refs(text, default_repo=None)
            if _fleet(r.repo)]


def targets_from(title: Optional[str], idempotency_key: Optional[str],
                 metadatas: Iterable) -> set:
    """``{(repo_lower, number)}`` from a card's title, key and run metadata."""
    out = set()
    m = _REBASE_KEY_RE.match(idempotency_key or "")
    if m and _fleet(m.group(1)):
        out.add((m.group(1).lower(), int(m.group(2))))
    out.update(_refs(title))
    for md in metadatas:
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except ValueError:
                continue
        if not isinstance(md, dict):
            continue
        for key in _META_KEYS:
            value = md.get(key)
            for text in ([value] if isinstance(value, str) else
                         value if isinstance(value, (list, tuple)) else ()):
                out.update(_refs(text))
    return out


def card_pr_targets(conn: sqlite3.Connection, task_id: str) -> set:
    row = conn.execute("SELECT title, idempotency_key FROM tasks WHERE id = ?",
                       (task_id,)).fetchone()
    if row is None:
        return set()
    metas = [r[0] for r in conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? AND metadata IS NOT NULL", (task_id,))]
    return targets_from(row[0], row[1], metas)


def fmt_pr(target: tuple) -> str:
    return f"{target[0]}#{target[1]}"


def running_owners(conn: sqlite3.Connection, targets: set, *, exclude: str = "") -> list:
    """Running cards (worker runs, not operator claims) on any of ``targets``, oldest first."""
    if not targets:
        return []
    out = []
    for row in conn.execute(
        "SELECT t.id, t.assignee, t.started_at, r.started_at, r.profile "
        "FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.id != ?", (exclude or "",),
    ).fetchall():
        if str(row[4] or "").startswith("human"):
            continue  # an operator `claim --review` never pushes
        for target in sorted(targets & card_pr_targets(conn, row[0])):
            out.append({"task_id": row[0], "assignee": row[1], "since": row[3] or row[2],
                        "pr": fmt_pr(target), "repo": target[0], "number": target[1]})
    out.sort(key=lambda o: (o["since"] or 0, o["task_id"]))
    for o in out:
        o["pr_owner"] = pr_owner_card(conn, (o["repo"], o["number"])) or o["task_id"]
    return out


def pr_owner_card(conn: sqlite3.Connection, target: tuple) -> Optional[str]:
    """The card that OWNS ``target``: the oldest card whose own handoff names the PR.

    Rebase helpers (``rebase:`` key) never own a PR; they work on the owner's.
    Falls back to the oldest non-helper card naming it, then the oldest card.
    """
    cards = pr_cards(conn, target)
    if not cards:
        return None
    keys = {r[0]: r[1] or "" for r in conn.execute(
        "SELECT id, idempotency_key FROM tasks WHERE id IN (%s)" % ",".join("?" * len(cards)),
        [c[0] for c in cards])}
    principal = [c[0] for c in cards if not keys.get(c[0], "").lower().startswith("rebase:")]
    for tid in principal:
        metas = [r[0] for r in conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? AND metadata IS NOT NULL", (tid,))]
        if target in targets_from(None, None, metas):
            return tid
    return (principal or [cards[0][0]])[0]


def _gh_json(path: str) -> Optional[dict]:
    try:
        proc = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=_GH_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def pr_head_committed_at(repo: str, number: int) -> Optional[int]:
    """Epoch of the PR head commit's committer date (REST), memoized 5 min. None = unknown."""
    key = (repo.lower(), int(number))
    hit = _HEAD_CACHE.get(key)
    if hit and time.time() - hit[0] < _HEAD_CACHE_TTL:
        return hit[1]
    pr = _gh_json(f"repos/{repo}/pulls/{number}") or {}
    sha = (pr.get("head") or {}).get("sha")
    when = None
    if sha:
        commit = _gh_json(f"repos/{repo}/commits/{sha}") or {}
        date = ((commit.get("commit") or {}).get("committer") or {}).get("date")
        try:
            when = int(datetime.fromisoformat(str(date).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            when = None
    _HEAD_CACHE[key] = (time.time(), when)
    return when


def owner_is_stale(owner: dict, *, now: Optional[int] = None,
                   head_commit_fn: Optional[HeadCommitFn] = None) -> bool:
    """True only when the owner's run AND the PR head commit are both older than 2 h."""
    now = int(time.time()) if now is None else int(now)
    if owner.get("since") and now - int(owner["since"]) < STALE_OWNER_SECONDS:
        return False
    at = (head_commit_fn or pr_head_committed_at)(owner["repo"], owner["number"])
    owner["head_committed_at"] = at
    if at is None:
        return False  # cannot tell: no reclaim, no duplicate
    return now - int(at) >= STALE_OWNER_SECONDS


def _hhmm(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(int(ts))) if ts else "?"


def describe(owner: dict) -> str:
    """``<pr> (owner t_x) already has a running card t_y (<assignee>, since ...)``."""
    pr_owner = owner.get("pr_owner") or owner["task_id"]
    holder = ("the owner itself" if pr_owner == owner["task_id"] else owner["task_id"])
    return (f"{owner['pr']} (owner {pr_owner}) already has a running card: {holder} "
            f"({owner.get('assignee') or '?'}, running since {_hhmm(owner.get('since'))})")


def refuse_rebase_card_at_birth(conn: sqlite3.Connection, idempotency_key: Optional[str], *,
                                now: Optional[int] = None,
                                head_commit_fn: Optional[HeadCommitFn] = None) -> None:
    """Raise :class:`PrOwnerBusyError` for a ``rebase:`` helper whose PR has a fresh owner."""
    m = _REBASE_KEY_RE.match(idempotency_key or "")
    if not m or not _fleet(m.group(1)):
        return
    target = (m.group(1).lower(), int(m.group(2)))
    for owner in running_owners(conn, {target}):
        if owner_is_stale(owner, now=now, head_commit_fn=head_commit_fn):
            continue
        raise PrOwnerBusyError(
            f"refused: {describe(owner)}. One running card per PR (t_cb70d390): the owner folds "
            f"the base branch itself; a stale owner (no PR push in 2 h) is reassigned by the "
            f"dispatcher.", owner)


def spawn_owners(conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None,
                 head_commit_fn: Optional[HeadCommitFn] = None) -> list:
    """Owners that stand between ``task_id`` and a spawn, each with ``stale`` set."""
    owners = running_owners(conn, card_pr_targets(conn, task_id), exclude=task_id)
    for owner in owners:
        owner["stale"] = owner_is_stale(owner, now=now, head_commit_fn=head_commit_fn)
    return owners


def pr_cards(conn: sqlite3.Connection, target: tuple) -> list:
    """Every non-archived card working on ``target``, oldest first: ``[(id, status, assignee)]``."""
    repo, n = target
    rows = conn.execute(
        "SELECT id, status, assignee FROM tasks WHERE status != 'archived' AND ("
        "title LIKE ? OR idempotency_key LIKE ? OR id IN (SELECT task_id FROM task_runs "
        "WHERE metadata LIKE ? OR metadata LIKE ?)) ORDER BY created_at, id",
        (f"%{repo}#{n}%", f"rebase:{repo}#{n}@%", f"%{repo}#{n}%", f"%{repo}/pull/{n}%"),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows if target in card_pr_targets(conn, r[0])]


def pr_card_map(conn: sqlite3.Connection, task_id: str) -> dict:
    """``{"repo#n": [{"id", "status", "assignee"}, ...]}`` for the card's PRs, the card excluded."""
    out = {}
    for target in sorted(card_pr_targets(conn, task_id)):
        out[fmt_pr(target)] = [{"id": i, "status": s, "assignee": a}
                               for i, s, a in pr_cards(conn, target) if i != task_id]
    return out


def concurrent_pr_runs(conn: sqlite3.Connection, since: int) -> list:
    """Pairs of worker runs started at/after ``since`` that overlapped in time on one PR.

    The close-out query for t_cb70d390: after the guard is live this is empty.
    """
    runs = conn.execute(
        "SELECT id, task_id, started_at, COALESCE(ended_at, CAST(strftime('%s','now') AS INTEGER)) "
        "FROM task_runs WHERE started_at >= ? AND profile NOT LIKE 'human%' ORDER BY started_at",
        (int(since),),
    ).fetchall()
    memo: dict = {}

    def targets(tid):
        if tid not in memo:
            memo[tid] = card_pr_targets(conn, tid)
        return memo[tid]

    out = []
    for i, a in enumerate(runs):
        for b in runs[i + 1:]:
            if b[2] >= a[3]:
                continue
            if a[1] == b[1]:
                continue
            for target in sorted(targets(a[1]) & targets(b[1])):
                out.append({"pr": fmt_pr(target), "first": a[1], "first_run": a[0],
                            "second": b[1], "second_run": b[0], "second_started": b[2]})
    return out
