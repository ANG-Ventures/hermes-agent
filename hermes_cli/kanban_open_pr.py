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

Only FLEET-owned PRs gate (t_06dccfe3, decided on t_f38605be): a PR on a repo
whose owner is not in :data:`FLEET_OWNERS` (e.g. an upstream
``stephenschoettler/hermes-lcm#638`` or ``NousResearch/*``) is a MENTION.
The fleet cannot merge it, so routing on it would park the card in review
waiting on a ``merged=true`` nobody here controls. Foreign refs are never looked
up, never routed, never handed to the freshness gate (no update-branch / arm on a
third-party PR); ``complete_task`` records them as ``mentioned_foreign_prs``.
"""

from __future__ import annotations

import json
import logging
import os
import re
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

# Repo owners whose PRs the fleet merges. Lower-case; matched case-insensitively.
FLEET_OWNERS = frozenset({"ang-ventures", "kyzcreig"})


def is_fleet_ref(ref) -> bool:
    """True when ``ref.repo`` is owned by one of :data:`FLEET_OWNERS`."""
    owner = str(getattr(ref, "repo", "") or "").split("/", 1)[0].strip().lower()
    return owner in FLEET_OWNERS


def split_fleet(refs) -> tuple:
    """``(fleet, foreign)`` partition of ``refs``, order preserved."""
    fleet, foreign = [], []
    for ref in refs:
        (fleet if is_fleet_ref(ref) else foreign).append(ref)
    return fleet, foreign


def foreign_pr_refs(*texts: Optional[str], metadata: Optional[dict] = None,
                    survivor_pr=None) -> list:
    """The non-fleet PR refs a completion names: mentions, never a gate."""
    refs = extract_pr_refs(*texts, metadata=metadata, survivor_pr=survivor_pr)
    return split_fleet(refs)[1]


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
    """Extract + resolve: the FLEET PRs that block ``done`` -- OPEN or unreadable.

    Foreign-owner refs are dropped before any lookup (see :data:`FLEET_OWNERS`).
    """
    refs = split_fleet(extract_pr_refs(*texts, metadata=metadata, survivor_pr=survivor_pr))[0]
    if not refs:
        return []
    primary = split_fleet(extract_pr_refs(metadata=metadata, survivor_pr=survivor_pr))[0]
    opened, unverified = unmerged_refs(refs, query_fn=query_fn, primary=primary)
    return opened + unverified


# --------------------------------------------------------------------------- closed-unmerged done gate
# Card t_a1550189 (2026-09-27): a PR stacked on a branch that was deleted after a squash is auto-CLOSED
# by GitHub, and the card that named it went ``done`` anyway (t_ddb3938c -> hermes-home#341,
# t_a3b910ce -> ace-media-homelab#81): the content never reached the default branch. ``done`` is
# refused when the card's OWN PR ref (``metadata.pr_url``/``pr``/``pr_urls`` or ``--survivor-pr``) is
# closed-unmerged, unless the handoff names the superseding work: a PR that is MERGED, or a commit
# SHA that is on the repo's default branch.

_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
ShaCheckFn = Callable[[str, str], Optional[bool]]


class ClosedUnmergedPrError(ValueError):
    """A completion's own PR ref is closed without merge and no superseder is named."""

    def __init__(self, task_id: str, prs: list):
        self.task_id = task_id
        self.prs = list(prs)
        super().__init__(
            f"done refused: {', '.join(self.prs)} "
            f"{'is' if len(self.prs) == 1 else 'are'} CLOSED WITHOUT MERGE, so the work is not on "
            f"the default branch (a stacked PR auto-closed when its base branch was deleted looks "
            f"exactly like this). Reopen/retarget and land it, or name what superseded it in the "
            f"result: a MERGED PR (owner/repo#N) or a commit SHA on the default branch "
            f"(--result 'superseded by owner/repo#N' / --superseded-by <sha>). "
            f"{task_id} is still in-flight (no state change)."
        )


def sha_on_default(repo: str, sha: str) -> Optional[bool]:
    """True iff ``sha`` is reachable from ``repo``'s default branch; None when GitHub cannot tell."""
    def _get(path):
        try:
            proc = subprocess.run(["gh", "api", path], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=QUERY_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.SubprocessError, TimeoutError):
            return None, False
        if proc.returncode != 0:
            return None, "HTTP 404" in (proc.stderr or "") or "HTTP 422" in (proc.stderr or "")
        try:
            return json.loads(proc.stdout or "{}"), False
        except ValueError:
            return None, False
    meta, _ = _get(f"repos/{repo}")
    default = (meta or {}).get("default_branch")
    if not default:
        return None
    cmp_, missing = _get(f"repos/{repo}/compare/{default}...{sha}")
    if cmp_ is None:
        return False if missing else None
    return str(cmp_.get("status") or "") in ("identical", "behind")


def memo_query(query_fn: Optional[QueryFn] = None) -> Optional[QueryFn]:
    """One lookup per PR per completion: the open-PR route and the closed-unmerged gate share it."""
    base = query_fn or _default_query()
    if base is None:
        return None
    cache: dict = {}

    def _q(repo: str, number: int):
        key = (repo.lower(), number)
        if key not in cache:
            cache[key] = base(repo, number)
        return cache[key]
    return _q


def closed_unmerged_refs(refs, *, query_fn: Optional[QueryFn] = None) -> list:
    """The subset of ``refs`` GitHub positively reports CLOSED (not merged). Unreadable -> not listed
    (the open-PR gate already routes an unreadable primary ref to review)."""
    if query_fn is None:
        query_fn = _default_query()
        if query_fn is None:
            return []
    out = []
    for ref in list(refs)[:MAX_LOOKUPS_PER_COMPLETION]:
        try:
            payload = query_fn(ref.repo, ref.number)
        except Exception as exc:
            _log.warning("kanban closed-pr check: lookup %s failed: %s", ref, exc)
            continue
        if isinstance(payload, dict) and str(payload.get("state") or "").upper() == "CLOSED":
            out.append(ref)
    return out


def names_superseder(closed, *texts: Optional[str], query_fn: Optional[QueryFn] = None,
                     sha_check: Optional[ShaCheckFn] = None) -> bool:
    """Does the handoff text name a MERGED PR, or a SHA on the default branch of a closed PR's repo?"""
    if query_fn is None:
        query_fn = _default_query()
    if sha_check is None and not os.environ.get("PYTEST_CURRENT_TEST"):
        sha_check = sha_on_default
    closed_keys = {(r.repo.lower(), r.number) for r in closed}
    blob = "\n".join(t for t in texts if isinstance(t, str))
    for ref in extract_pr_refs(blob)[:MAX_LOOKUPS_PER_COMPLETION]:
        if (ref.repo.lower(), ref.number) in closed_keys or query_fn is None:
            continue
        try:
            payload = query_fn(ref.repo, ref.number)
        except Exception:
            continue
        if isinstance(payload, dict) and str(payload.get("state") or "").upper() == "MERGED":
            return True
    if sha_check is None:
        return False
    repos = sorted({r.repo for r in closed})
    for sha in list(dict.fromkeys(_SHA_RE.findall(blob)))[:6]:
        if not re.search(r"[a-f]", sha):  # all-digit tokens (PR numbers, timestamps) are not SHAs
            continue
        for repo in repos:
            if sha_check(repo, sha):
                return True
    return False


def enforce_not_closed_unmerged(task_id: str, *texts: Optional[str], metadata: Optional[dict] = None,
                                survivor_pr=None, superseded_by: Optional[str] = None,
                                query_fn: Optional[QueryFn] = None,
                                sha_check: Optional[ShaCheckFn] = None) -> list:
    """Raise :class:`ClosedUnmergedPrError` when the card's own PR is closed-unmerged and the handoff
    (``texts`` + ``superseded_by``) names no merged superseder. Returns the closed refs it accepted."""
    # Fleet-owned refs only: a foreign (upstream / third-party) PR is a mention the fleet cannot land.
    primary = split_fleet(extract_pr_refs(metadata=metadata, survivor_pr=survivor_pr))[0]
    if not primary:
        return []
    closed = closed_unmerged_refs(primary, query_fn=query_fn)
    if not closed:
        return []
    if names_superseder(closed, *texts, superseded_by, query_fn=query_fn, sha_check=sha_check):
        return closed
    raise ClosedUnmergedPrError(task_id, [f"{r.repo}#{r.number}" for r in closed])


ROUTE_COMMENT = "survivor PR open; card closes on merged=true"


def route_note(refs) -> str:
    return "auto-routed: " + ", ".join(f"PR {r.repo}#{r.number}" for r in refs) + " still open"


def route_comment(refs) -> str:
    """The ONE comment a routed completion leaves (the closer reads PR refs from it)."""
    return f"{ROUTE_COMMENT} ({', '.join(f'{r.repo}#{r.number}' for r in refs)})"


def find_done_with_open_pr(conn: sqlite3.Connection, *, days: int = LINT_WINDOW_DAYS,
                           query_fn: Optional[QueryFn] = None,
                           now: Optional[float] = None) -> list:
    """``done`` cards completed in the last ``days`` whose evidence names an OPEN
    FLEET PR. Foreign-owner refs are mentions here too, so the lint agrees with
    the completion gate."""
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
        refs = split_fleet(extract_pr_refs(row["result"], summary,
                                           metadata=meta if isinstance(meta, dict) else None))[0]
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
