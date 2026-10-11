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
    out = {"state": "MERGED" if payload.get("merged_at") else str(payload["state"]).upper()}
    if out["state"] == "MERGED" and payload.get("merge_commit_sha"):
        out["merge_commit_sha"] = str(payload["merge_commit_sha"])
    head = payload.get("head")
    if out["state"] == "MERGED" and isinstance(head, dict) and head.get("sha"):
        out["head_sha"] = str(head["sha"])
    return out


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
    """Qualified PR refs (URL or ``owner/repo#N``), first-seen order, deduped.

    A ``survivor_pr`` claim may carry a ``<repository>=`` qualifier; only the value after it names a PR
    (the grammar ``preserve`` verifies with), so ``pull/3421=o/r#3328`` is #3328 alone, never #3421 too.
    """
    from hermes_cli.kanban_survivor import _split_qualifier

    sources = [t for t in texts if isinstance(t, str) and t.strip()]
    if isinstance(metadata, dict):
        for key in ("pr_url", "pr_urls", "pr"):
            sources.extend(_iter_strings(metadata.get(key)))
    sources.extend(_split_qualifier(claim)[1] for claim in _iter_strings(survivor_pr))
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
# t_a3b910ce -> ace-media-homelab#81): the content never reached the default branch. ``done`` (and
# ``archive``) is refused when the card's OWN PR ref (``metadata.pr_url``/``pr``/``pr_urls`` or
# ``--survivor-pr``) is closed-unmerged, unless the handoff carries the canonical close-reason token for
# THAT closed PR (Ace 2026-09-27 10:38; skills-shared/coding/coding-guardrails/references/
# pr-close-reasons.md): ``SUPERSEDED-BY <ref>`` / ``RE-CARRIED-AS <ref>`` (or ``--superseded-by <ref>``)
# where <ref> is a MERGED PR (``owner/repo#N``, a PR URL, or ``#N`` in the closed PR's repo) or a commit
# SHA on the closed PR's repo's default branch. A lookup that fails refuses (fail closed).

_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
ShaCheckFn = Callable[[str, str], Optional[bool]]
SUPERSEDE_TOKENS = ("SUPERSEDED-BY", "RE-CARRIED-AS")
_TARGET = (r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+"
           r"|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+|#\d+|[0-9a-f]{7,40}")
_SUPERSEDE_RE = re.compile(r"\b(?:SUPERSEDED-BY|RE-CARRIED-AS)\s+(" + _TARGET + r")\b")
_TARGET_RE = re.compile(r"(?<![\w/#])(" + _TARGET + r")\b")
_BARE_PR_RE = re.compile(r"^#(\d+)$")
# Archive only (not done): an explicit close decision recorded on the card also explains the closed PR.
_DECISION_RE = re.compile(r"\bCLOSED: (?:REJECTED|ABANDONED|THROWAWAY|STALE|DUPLICATE-OF)\b")


class ClosedUnmergedPrError(ValueError):
    """A completion's own PR ref is closed without merge (or unreadable) and no superseder is named."""

    def __init__(self, task_id: str, prs: list, unverified: Optional[list] = None, verb: str = "done",
                 untokened: Optional[list] = None, survivors: Optional[list] = None):
        self.task_id = task_id
        self.closed = list(prs)
        self.unverified = list(unverified or [])
        self.untokened = list(untokened or [])
        self.prs = self.closed + self.unverified
        parts = [f"{verb} refused:"]
        if self.closed:
            parts.append(
                f"{', '.join(self.closed)} {'is' if len(self.closed) == 1 else 'are'} CLOSED WITHOUT MERGE, "
                f"so the work is not on the default branch (a stacked PR auto-closed when its base branch "
                f"was deleted looks exactly like this). Reopen/retarget and land it, or name what superseded "
                f"it with the close-reason token: 'SUPERSEDED-BY owner/repo#N' / 'RE-CARRIED-AS #N' (a MERGED "
                f"PR) or 'SUPERSEDED-BY <sha>' (a commit on the default branch), in --result/--summary or "
                f"--superseded-by <ref>."
            )
            if len(self.closed) > 1:
                parts.append("Each closed PR needs its own token: name it before the token, e.g. "
                             "'owner/repo#5 SUPERSEDED-BY #9; owner/repo#6 RE-CARRIED-AS #10'.")
        if self.untokened:
            parts.append(
                f"The merged survivor {', '.join(survivors or [])} does not cover "
                f"{', '.join(self.untokened)}: {'it carries' if len(self.untokened) == 1 else 'they carry'} "
                f"no 'CLOSED: <TOKEN> -- <reason>' close-reason comment on GitHub (or its comments could "
                f"not be read). Close-record the PR with the contract line, then retry."
            )
        if self.unverified:
            parts.append(
                f"GitHub could not be read for {', '.join(self.unverified)}, so it is unknown whether the "
                f"work landed; retry when `gh api repos/<owner>/<repo>/pulls/<n>` works."
            )
        parts.append(f"{task_id} is still in-flight (no state change)." if verb == "done"
                     else f"{task_id} is not archived (no state change).")
        super().__init__(" ".join(parts))


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


def closed_unmerged_refs(refs, *, query_fn: Optional[QueryFn] = None, unverified: Optional[list] = None) -> list:
    """The subset of ``refs`` GitHub positively reports CLOSED (not merged). A lookup that raises or
    returns no state is appended to ``unverified`` (the caller fails closed on it). No oracle at all
    (pytest) checks nothing."""
    if query_fn is None:
        query_fn = _default_query()
        if query_fn is None:
            return []
    out = []
    # EVERY primary ref is looked up (FleetReview #1339): a cap here let the 11th own PR close unmerged
    # and still pass. Primary refs are the card's own PRs, not prose, so they are bounded by the card.
    for ref in refs:
        try:
            payload = query_fn(ref.repo, ref.number)
        except Exception as exc:
            _log.warning("kanban closed-pr check: lookup %s failed: %s", ref, exc)
            payload = None
        state = str(payload.get("state") or "").upper() if isinstance(payload, dict) else ""
        if not state:
            if unverified is not None:
                unverified.append(f"{ref.repo}#{ref.number}")
            continue
        if state == "CLOSED":
            out.append(ref)
    return out


def _is_merged(query_fn, repo: str, number: int) -> bool:
    try:
        payload = query_fn(repo, number)
    except Exception as exc:
        _log.warning("kanban closed-pr check: superseder %s#%s lookup failed: %s", repo, number, exc)
        return False
    return isinstance(payload, dict) and str(payload.get("state") or "").upper() == "MERGED"


def supersedes(closed_ref, target: str, *, query_fn: Optional[QueryFn],
               sha_check: Optional[ShaCheckFn]) -> bool:
    """Does ``target`` (one token target) name landed work for ``closed_ref``? Unreadable -> False."""
    m = _BARE_PR_RE.match(target)
    if m:
        repo, number = closed_ref.repo, int(m.group(1))
    elif "#" in target or "/pull/" in target:
        refs = extract_pr_refs(target)
        if not refs:
            return False
        repo, number = refs[0].repo, refs[0].number
    else:  # a SHA: must be on the CLOSED PR's repo's default branch
        if not re.search(r"[a-f]", target) or sha_check is None:
            return False
        return bool(sha_check(closed_ref.repo, target))
    if (repo.lower(), number) == (closed_ref.repo.lower(), closed_ref.number) or query_fn is None:
        return False  # a closed PR cannot supersede itself
    return _is_merged(query_fn, repo, number)


def names_superseder(closed, *texts: Optional[str], superseded_by: Optional[str] = None,
                     query_fn: Optional[QueryFn] = None, sha_check: Optional[ShaCheckFn] = None) -> bool:
    """Is EVERY closed PR covered by a SUPERSEDED-BY/RE-CARRIED-AS token naming merged work?"""
    return not _unsuperseded(closed, *texts, superseded_by=superseded_by, query_fn=query_fn, sha_check=sha_check)


# A token/decision is bound to the PR(s) named in its own clause (FleetReview #1352): clauses split on
# ``;`` and newlines. A lone token owns its clause (the PR before it, else the PR after it); several
# tokens in one clause each own only the text since the previous token's target, and if any of them
# names no PR there the clause is ambiguous and binds nothing but a sole PR (#1363 review).
# ``PR #5 CLOSED: REJECTED; PR #6 still needs work`` covers #5 only; ``r#5 SUPERSEDED-BY #9`` covers #5 only.
_CLAUSE_SPLIT_RE = re.compile(r"[;\n]")
_BARE_REF_RE = re.compile(r"(?<![\w/])#(\d+)\b")
_NO_PR = ("", -1)  # subject key that matches no PR: an ambiguous clause that names some other PR


def _subject_keys(text: str) -> set:
    """PR keys a subject region names: ``(repo_lower, n)`` for qualified refs, ``(None, n)`` for bare ``#N``."""
    keys = {(r.repo.lower(), r.number) for r in extract_pr_refs(text)}
    qualified_numbers = {n for _, n in keys}
    keys |= {(None, int(n)) for n in _BARE_REF_RE.findall(text) if int(n) not in qualified_numbers}
    return keys


def _bound_matches(text: Optional[str], pattern) -> list:
    """``(match, subject_keys)`` for every ``pattern`` match in ``text``, bound to its own clause."""
    if not isinstance(text, str):
        return []
    out = []
    for clause in _CLAUSE_SPLIT_RE.split(text):
        matches = list(pattern.finditer(clause))
        if len(matches) == 1:  # one token/decision: its subject precedes it, else follows it
            m = matches[0]
            # The target sits inside the match, outside both regions. A ref naming the target elsewhere
            # in the clause is still a named subject, never erased into "unattributed" (#1363 review).
            subject = _subject_keys(clause[:m.start()]) or _subject_keys(clause[m.end():])
            out.append((m, subject))
            continue
        # Several in one clause: each owns ONLY the text between the previous match's end (past its
        # target) and its own start. If any of them names no PR there, the text between two matches
        # could belong to either, so the whole clause is unattributed (covers a sole PR only).
        regions = [clause[(matches[i - 1].end() if i else 0):m.start()] for i, m in enumerate(matches)]
        subjects = [_subject_keys(r) for r in regions]
        if not all(subjects):
            # Ambiguous. If the clause names any PR besides the token targets, it is about THAT PR and
            # binds nothing (never falls back to a sole PR); otherwise it is unattributed.
            named = _subject_keys(" ".join(regions + [clause[matches[-1].end():]]))
            subjects = [{_NO_PR} if named else set() for _ in matches]
        out.extend(zip(matches, subjects))
    return out


def _binds(closed_ref, subject: set, *, sole: bool) -> bool:
    """Is a token/decision whose clause names ``subject`` about ``closed_ref``? An unattributed one
    (its clause names no PR) is only about the card's sole candidate."""
    if not subject:
        return sole
    return (closed_ref.repo.lower(), closed_ref.number) in subject or (None, closed_ref.number) in subject


def _unsuperseded(closed, *texts: Optional[str], superseded_by: Optional[str] = None,
                  query_fn: Optional[QueryFn] = None, sha_check: Optional[ShaCheckFn] = None) -> list:
    """The closed refs NOT covered by a SUPERSEDED-BY/RE-CARRIED-AS token naming merged work FOR THEM.

    A token covers the closed PR named in its clause; an unattributed token (or ``--superseded-by``
    without a token word) covers only a sole closed PR. One token never covers several closed PRs
    unless its clause names each of them."""
    if query_fn is None:
        query_fn = _default_query()
    if sha_check is None and not os.environ.get("PYTEST_CURRENT_TEST"):
        sha_check = sha_on_default
    sole = len(closed) == 1
    bound: list = []  # (target, subject)
    for text in texts:
        bound += [(m.group(1), subj) for m, subj in _bound_matches(text, _SUPERSEDE_RE)]
    if isinstance(superseded_by, str):
        tokens = _bound_matches(superseded_by, _SUPERSEDE_RE)
        if tokens:
            bound += [(m.group(1), subj) for m, subj in tokens]
        else:  # --superseded-by IS the token: every ref/sha in it is an unattributed target
            bound += [(t, set()) for t in _TARGET_RE.findall(superseded_by)]
    bound = list(dict.fromkeys((t, frozenset(s)) for t, s in bound))[:MAX_LOOKUPS_PER_COMPLETION]
    return [c for c in closed
            if not any(_binds(c, subj, sole=sole)
                       and supersedes(c, t, query_fn=query_fn, sha_check=sha_check) for t, subj in bound)]


def _decision_covers(closed_ref, text: str, *, sole: bool) -> bool:
    """Does one ``CLOSED: <reason>`` decision text explain THIS closed PR (FleetReview #1339/#1352)?

    The decision's own clause must name the PR (``owner/repo#N``, its URL, or ``#N``), or the card must
    own exactly one PR and that clause names none. A decision naming PR A never covers PR B, even when
    B is mentioned elsewhere in the same comment."""
    return any(_binds(closed_ref, subj, sole=sole) for _m, subj in _bound_matches(text, _DECISION_RE))


def recorded_pr_refs(metadata) -> list:
    """Every PR string a run's metadata persisted for the card: ``pr_url``/``pr_urls``/``pr``, the card's
    own PRs the open-PR route recorded (``own_prs``) and survivor PR evidence (``survivor.refs[].pr`` /
    ``survivor.claims[].pr``). The card's own PR evidence, independent of which key carried it.

    ``auto_routed_open_prs`` also lists PRs the handoff prose merely mentions, so a mention must never
    gate this card (FleetReview #1352). It is read only for a LEGACY routed run (no ``own_prs`` key:
    routed before the route wrote it), where it is the only persisted copy of a ``--survivor-pr``."""
    if not isinstance(metadata, dict):
        return []
    out: list = []
    legacy = "own_prs" not in metadata and "auto_routed_open_prs" in metadata
    for key in ("pr_url", "pr_urls", "pr", "auto_routed_open_prs" if legacy else "own_prs"):
        out.extend(_iter_strings(metadata.get(key)))
    survivor = metadata.get("survivor")
    if isinstance(survivor, dict):
        for group in ("refs", "claims"):
            for item in survivor.get(group) or ():
                if isinstance(item, dict):
                    out.extend(_iter_strings(item.get("pr")))
    return out


# --------------------------------------------------------------------------- close-record + merged survivor
# t_9aff2951 (2026-10-01): a closed-unmerged own PR whose OWN GitHub thread carries the sanctioned close record
# (``CLOSED: <TOKEN> -- ...``, coding-guardrails/references/pr-close-reasons.md) is not lost work when the
# card also names a MERGED survivor PR whose merge commit is on its repo's default branch. Without that record
# the gate keeps refusing. Cases: t_3ec660ca (hermes-home#1957 RE-CARRIED-AS #2259) and t_60269760
# (hermes-home#2350 SUPERSEDED-BY #2347) were unclosable by any verb.
CLOSE_TOKENS = ("SUPERSEDED-BY", "DUPLICATE-OF", "REJECTED", "ABANDONED", "RE-CARRIED-AS", "THROWAWAY", "STALE")
_CLOSE_RECORD_RE = re.compile(r"^\s*CLOSED: (?:" + "|".join(CLOSE_TOKENS) + r")\b", re.MULTILINE)
CloseRecordFn = Callable[[str, int], Optional[bool]]


def has_close_record(body) -> bool:
    """Does one PR comment body carry the contract close-reason line?"""
    return isinstance(body, str) and bool(_CLOSE_RECORD_RE.search(body))


def pr_close_record(repo: str, number: int) -> Optional[bool]:
    """True iff a comment on ``repo#number`` carries ``CLOSED: <TOKEN>``; None when GitHub cannot be read."""
    try:
        proc = subprocess.run(
            ["gh", "api", "--paginate", f"repos/{repo}/issues/{number}/comments?per_page=100",
             "--jq", ".[].body | @json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=QUERY_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        try:
            if has_close_record(json.loads(line)):
                return True
        except ValueError:
            continue
    return False


def _default_close_record() -> Optional[CloseRecordFn]:
    """The real ``gh``-backed close-record reader, or None inside pytest (no live GitHub)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return pr_close_record


def _default_sha_check() -> Optional[ShaCheckFn]:
    """The real default-branch ancestry check, or None inside pytest (no live GitHub)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return sha_on_default


def merged_survivors(refs, *, query_fn: Optional[QueryFn], sha_check: Optional[ShaCheckFn]) -> list:
    """The subset of ``refs`` GitHub reports MERGED with a merge commit on the repo's default branch."""
    if query_fn is None or sha_check is None:
        return []
    out = []
    for ref in refs:
        try:
            payload = query_fn(ref.repo, ref.number)
        except Exception as exc:
            _log.warning("kanban closed-pr check: survivor %s lookup failed: %s", ref, exc)
            continue
        if not isinstance(payload, dict) or str(payload.get("state") or "").upper() != "MERGED":
            continue
        sha = str(payload.get("merge_commit_sha") or "")
        if sha and sha_check(ref.repo, sha):
            out.append(ref)
    return out


def enforce_not_closed_unmerged(task_id: str, *texts: Optional[str], metadata: Optional[dict] = None,
                                survivor_pr=None, superseded_by: Optional[str] = None,
                                query_fn: Optional[QueryFn] = None,
                                sha_check: Optional[ShaCheckFn] = None,
                                verb: str = "done", decision_texts=(), recorded=(),
                                close_record_fn: Optional[CloseRecordFn] = None) -> list:
    """Raise :class:`ClosedUnmergedPrError` when the card's own PR is closed-unmerged (or unreadable) and
    the handoff (``texts`` + ``superseded_by``) carries no SUPERSEDED-BY/RE-CARRIED-AS token naming merged
    work for it. ``decision_texts`` (archive only: card comments) may instead record an explicit
    ``CLOSED: REJECTED|ABANDONED|THROWAWAY|STALE|DUPLICATE-OF`` decision. Returns the closed refs accepted."""
    # Fleet-owned refs only: a foreign (upstream / third-party) PR is a mention the fleet cannot land.
    # ``recorded``: PR strings earlier runs persisted for this card (see :func:`recorded_pr_refs`), so a
    # reviewer approving without repeating the PR is still gated on it (FleetReview #1339).
    primary = split_fleet(extract_pr_refs(*recorded, metadata=metadata, survivor_pr=survivor_pr))[0]
    if not primary:
        return []
    unverified: list = []
    closed = closed_unmerged_refs(primary, query_fn=query_fn, unverified=unverified)
    if unverified:
        raise ClosedUnmergedPrError(task_id, [f"{r.repo}#{r.number}" for r in closed], unverified, verb=verb)
    if not closed:
        return []
    # Each closed PR needs its OWN cover: a merged-work token, or (archive) a decision tied to that PR.
    uncovered = _unsuperseded(closed, *texts, *decision_texts, superseded_by=superseded_by,
                              query_fn=query_fn, sha_check=sha_check)
    sole = len(primary) == 1
    uncovered = [c for c in uncovered if not any(_decision_covers(c, t, sole=sole) for t in decision_texts)]
    if not uncovered:
        return closed
    # A merged survivor + the PR's own close record (t_9aff2951). ``done`` takes the survivor from
    # ``--survivor-pr``; ``archive`` has no flag, so any of the card's own PRs that merged qualifies.
    candidates = primary if verb == "archive" else split_fleet(extract_pr_refs(survivor_pr=survivor_pr))[0]
    if sha_check is None:
        sha_check = _default_sha_check()
    survivors = merged_survivors(candidates, query_fn=query_fn or _default_query(), sha_check=sha_check)
    untokened: list = []
    if survivors:
        if close_record_fn is None:
            close_record_fn = _default_close_record()
        for c in uncovered:
            try:
                ok = close_record_fn(c.repo, c.number) if close_record_fn is not None else None
            except Exception as exc:
                _log.warning("kanban closed-pr check: close record %s lookup failed: %s", c, exc)
                ok = None
            if not ok:
                untokened.append(f"{c.repo}#{c.number}")
        if not untokened:
            return closed
    raise ClosedUnmergedPrError(task_id, [f"{r.repo}#{r.number}" for r in closed], verb=verb,
                                untokened=untokened, survivors=[f"{r.repo}#{r.number}" for r in survivors])


# --------------------------------------------------------------------------- routed-PR survivor gate
# t_829fce95 (2026-10-10): t_cee802d5 was routed to review on its own OPEN PR hermes-home#3421, then an
# operator closed it ``done`` with ``--survivor-pr hermes-home#3328 --survivor-unbound`` (an unrelated,
# merged PR). The review-status card skips the open-PR route and the closed-unmerged gate only looks for
# CLOSED, so done went through with #3421 open. A survivor claim that does not name the routed PR while
# that PR is still OPEN (or unreadable) is now refused; ``--abandon-routed-pr <reason>`` is the override.


class RoutedPrOpenError(ValueError):
    """The card's routed own PR is still OPEN/unreadable and the survivor claim names a different one."""

    def __init__(self, task_id: str, prs: list, unverified: Optional[list] = None, blank_reason: bool = False):
        self.task_id = task_id
        self.open = list(prs)
        self.unverified = list(unverified or [])
        self.prs = self.open + self.unverified
        if blank_reason:
            msg = (f"done refused: --abandon-routed-pr needs a non-empty reason (it is the audit record). "
                   f"{task_id} is still in-flight (no state change).")
        else:
            parts = ["done refused:"]
            if self.open:
                parts.append(
                    f"the card was routed to review on its own PR {', '.join(self.open)}, which is still OPEN, "
                    f"and the survivor claim names a different PR/ref. Land {', '.join(self.open)} (the card "
                    f"closes on merged=true), name it with --survivor-pr, or pass --abandon-routed-pr "
                    f"'<reason>' if that PR is really abandoned.")
            if self.unverified:
                parts.append(f"GitHub could not be read for routed PR {', '.join(self.unverified)}; retry when "
                             f"`gh api repos/<owner>/<repo>/pulls/<n>` works.")
            parts.append(f"{task_id} is still in-flight (no state change).")
            msg = " ".join(parts)
        super().__init__(msg)


def routed_prs_still_open(routed, *, survivor_pr=None, query_fn: Optional[QueryFn] = None) -> tuple:
    """``(open, unverified)`` among the fleet PR strings ``routed`` that ``survivor_pr`` does not name.

    Every routed ref is looked up (they are the card's own PRs, bounded by the card). No oracle (pytest
    without a stub) checks nothing."""
    named = {(r.repo.lower(), r.number) for r in extract_pr_refs(survivor_pr=survivor_pr)}
    refs = [r for r in split_fleet(extract_pr_refs(*routed))[0] if (r.repo.lower(), r.number) not in named]
    if not refs:
        return [], []
    opened, unverified = unmerged_refs(refs, query_fn=query_fn, primary=refs)
    return [f"{r.repo}#{r.number}" for r in opened], [f"{r.repo}#{r.number}" for r in unverified]


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
