"""Re-evaluate kanban blocks whose premise is an external GitHub PR.

A worker that finishes its code and needs a sibling PR merged before it can
continue blocks with a reason like ``"merge PR #787 then unblock me"``. Nothing
in the dispatcher ever re-read that reason, so the card sat ``blocked`` until a
human board sweep noticed — measured 2026-09-21: six ``needs_input`` cards held
up to **12 hours** on gates whose referenced PRs had already merged.

This module makes that mechanical. Each dispatcher tick:

1. parse the LAST ``blocked``-family event's reason for PR references,
2. resolve each reference's state through ``gh`` (bounded + cached),
3. drop HISTORY refs — a PR that had already merged when the card blocked (or
   that this gate already resolved for this card) cannot be what the card is
   waiting on; it is context the worker cited, not the gate,
4. when EVERY remaining referenced PR is MERGED, unblock the card, post the
   evidence as a comment, and write one ``gate_auto_resolved`` event so a
   sweep can count it. A reason whose refs are ALL history is left alone.

Step 3 exists because of t_20b94ef5 (2026-09-24): blocked twice with
``kind=dependency`` on an unmerged sibling CARD, both reasons mentioning the
long-merged PR #953 as background. Both times the gate read #953 as the gate
condition, unblocked the card within minutes, and respawned a worker for
nothing (runs 8911, 8934).

Deliberate non-actions, each one a fail-safe:

* only ``needs_input`` / ``capability`` / ``dependency`` block kinds — a
  ``transient`` or un-typed block is not a "waiting on an external object" claim;
* a reason naming no PR is never touched, and burns no lookup budget;
* a bare ``#N`` with no unambiguous repo context is dropped rather than guessed;
* CLOSED-unmerged posts ONE advisory comment and never unblocks;
* any lookup failure is a no-op plus one WARN — never a page, never an unblock;
* MERGED is not DEPLOYED: when the card's reason or body names a deploy tree
  (``~/.hermes`` or ``~/.hermes/runtime/hermes-agent``) and the PR's repo
  maps to one (:data:`DEPLOY_TREES`), the merge commit must also be an
  ancestor of that tree's HEAD. Until it is, the card stays blocked and gets
  ONE ``MERGED, awaiting deploy`` comment (t_289e8020: t_2789cbae was
  re-unblocked twice into the same wall while ``~/.hermes`` sat 195 commits
  behind origin/main).
* a block reason that SAYS the gate is a deploy (``merged but NOT deployed``,
  ``gate = #401 DEPLOYED``) is a deploy premise even without naming a tree. A
  mapped repo gets the ancestry check above; an unmapped repo has no provable
  live SHA, so the card is held with ONE ``awaiting deploy (unverifiable)``
  comment and a human unblocks it (t_a7287385: t_213f5d63 was re-unblocked
  three times on the merge of Kyzcreig/pipecat-house-voice#401, which is
  deployed by Apollo, not into a local tree);
* a reason that reserves the unblock for an owner (``Apollo owns the
  unblock``, ``only Apollo should unblock``, ``do not auto-unblock``) is never
  a gate candidate at all.

Harness safety
--------------
``gate_auto_resolved`` is a real state transition justified by real PR
evidence. A test or probe that stubs :func:`query_pr` holds a FABRICATED
oracle, and must therefore never be able to write that transition to a live
board. ``HERMES_HOME`` alone does not sandbox kanban — ``HERMES_KANBAN_DB``
outranks it (see :func:`hermes_cli.kanban_db.kanban_db_path`) — so a probe that
redirects only ``HERMES_HOME`` still resolves to production. On 2026-09-21 a
lock-timing probe did exactly that and wrote 20 ``gate_auto_resolved`` events
to the live board, falsely unblocking seven real cards.
:func:`assert_write_allowed` closes that: a non-default ``query_fn`` raises
:class:`SandboxEscape` instead of landing, unless isolation has been declared
POSITIVELY via ``HERMES_KANBAN_SANDBOX=1``. Containment is deliberately not the
test — the fleet exports ``HERMES_HOME=~/.hermes`` and the live board sits
inside it, so "the DB is under the declared home" is a relation production
already satisfies. It gates on ORACLE IDENTITY alone, never on a test-context
marker: the incident probe was a bare script that set no marker, so a
marker-gated guard would return before ever examining the oracle.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import subprocess
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

_log = logging.getLogger(__name__)

# Block kinds whose reason can legitimately name an external gate. ``transient``
# is excluded on purpose: it means "this may clear on its own", not "this waits
# on a specific object", and auto-resolving it would race the worker's own retry.
GATE_BLOCK_KINDS: frozenset[str] = frozenset(
    {"needs_input", "capability", "dependency"}
)

# Per-tick hard cap on GitHub lookups. Cards past the budget are deferred to a
# later tick, not failed: the board keeps making progress without ever turning a
# 60-second tick into a rate-limit incident.
MAX_LOOKUPS_PER_TICK = 30

# How long a reversible (OPEN/CLOSED) state is reused before re-querying.
# MERGED is irreversible and is cached for the process lifetime instead.
CACHE_TTL_SECONDS = 300

_QUERY_TIMEOUT_SECONDS = 5
_CACHE_LIMIT = 2048

# Event kinds that carry a block reason. ``dependency_wait`` is the
# ``kind="dependency"`` landing event; ``block_loop_detected`` is the
# triage escalation. All three can name a PR.
_BLOCK_EVENT_KINDS: tuple[str, ...] = (
    "blocked",
    "dependency_wait",
    "block_loop_detected",
)

_OWNER_REPO = r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+"
# Full canonical PR URL. Matched first so its digits are never re-read as a
# bare ``#N``/``pull/N`` by the looser patterns below.
_URL_RE = re.compile(
    rf"https?://github\.com/({_OWNER_REPO})/pull/(\d+)",
    re.IGNORECASE,
)
# ``owner/repo#123`` — carries its own context.
_QUALIFIED_RE = re.compile(rf"\b({_OWNER_REPO})#(\d+)\b")
# Bare ``#123`` / ``pull/123`` — needs a repo context to be resolvable.
_BARE_RE = re.compile(r"(?:(?<![\w/#])#|\bpull/)(\d+)\b")

# A naked body mention must be a complete two-segment token. The slash guards
# reject prefixes of deeper paths (``tests/hermes_cli/test_x.py``); the owner
# rule and suffix filter below reject two-segment source paths.
_REPO_MENTION_RE = re.compile(
    rf"(?<![A-Za-z0-9._/-])({_OWNER_REPO})(?![A-Za-z0-9._/-])"
)
# A GitHub *owner* (user or org) is alphanumerics and single hyphens only —
# never ``_`` and never ``.``. This is a POSITIVE property of a real slug, so
# it closes the class that a file-extension denylist cannot: ``hermes_cli/
# kanban*.py`` truncates to ``hermes_cli/kanban`` (no suffix left to deny) and
# ``hermes_cli/kanban_db`` never had one. Both are rejected on the owner.
_OWNER_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\Z")
# An all-digit segment. ``10/10``, ``5/5`` and ``30/31`` are prose, and the
# first of those is ALSO a real GitHub repository — so existence alone cannot
# reject it. Measured over every card body on all 76 boards: 522 bodies reach
# the ranking branch below, and this filter is what separates the one prose
# token that resolves on GitHub from the 18 genuine repo mentions.
_NUMERIC_SEGMENT_RE = re.compile(r"\A\d+\Z")
_SOURCE_PATH_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".py",
        ".rb",
        ".rs",
        ".scss",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)
_GIT_REMOTE_RE = re.compile(
    rf"(?:git@github\.com:|https?://(?:[^@/\s]+@)?github\.com/)({_OWNER_REPO}?)"
    r"(?:\.git)?/?\s*$",
    re.IGNORECASE,
)

# Repo -> the LIVE tree its merges must reach before a deploy-premised card may
# resume. Keys are lowercased ``owner/repo``; values are ``~``-relative and are
# expanded at check time. A repo absent from this map is merge-only (the
# pre-t_289e8020 behaviour): there is no local tree whose HEAD means "live".
DEPLOY_TREES: dict[str, str] = {
    "ang-ventures/hermes-home": "~/.hermes",
    "ang-ventures/hermes-agent": "~/.hermes/runtime/hermes-agent",
}

# A card that names a deploy tree is claiming "the change must be LIVE", not
# merely merged. ``~/.hermes``, ``$HOME/.hermes`` and an absolute home path all
# count; the trailing guard rejects look-alikes such as ``~/.hermes-home``.
# Over-matching (a body naming ``~/.hermes/scripts/x.py``) only ever makes the
# gate STRICTER — it waits for the deploy — never looser.
_DEPLOY_TREE_MENTION_RE = re.compile(
    r"(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s]+|/home/[^/\s]+)/\.hermes(?![\w-])"
)

# A block REASON that states the gate is a deploy. Reason only, never the body:
# a body saying "deploy the fix" is task prose, and treating it as a premise
# would pin every merge-gated card on an unmapped repo to a human forever.
_DEPLOY_WORD_RE = re.compile(r"\b(?:re)?deploy(?:ed|s|ing|ment)?\b", re.IGNORECASE)
# Explicit "no deploy needed" phrasing is the opposite claim; strip it first.
_NO_DEPLOY_RE = re.compile(
    r"\b(?:no|without(?:\s+a)?)\s+(?:re)?deploy(?:ment)?\b"
    r"|\b(?:re)?deploy(?:ment)?\s+(?:is\s+)?not\s+(?:needed|required)\b",
    re.IGNORECASE,
)

# A reason that reserves the unblock for a named owner. The gate must never
# override that claim, whatever the PR state (t_213f5d63, 04:54 PT 09-25:
# Apollo's own block "Apollo owns the unblock" was auto-unblocked 63 s later).
_OWNER_HELD_RE = re.compile(
    r"\b(?:apollo|ace|a\s+human|human|the\s+operator|operator|the\s+orchestrator)"
    r"\s+owns\s+the\s+unblock\b"
    r"|\bdo\s+not\s+auto[- ]?unblock\b"
    r"|\bonly\s+(?:apollo|ace|a\s+human|the\s+operator|the\s+orchestrator)"
    r"\s+(?:should|may|can|will)\s+unblock\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PrRef:
    """One resolved PR reference: an explicit repo plus a number."""

    repo: str
    number: int

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.repo}#{self.number}"


@dataclass(frozen=True)
class GateOutcome:
    """What the re-evaluator did (or deliberately did not do) for one card."""

    task_id: str
    action: str
    prs: tuple[str, ...] = ()
    detail: str = ""


@dataclass
class _CacheEntry:
    state: str
    sha: Optional[str]
    merged_at: Optional[str]
    fetched_at: float
    terminal: bool


@dataclass(frozen=True)
class _PrefetchResult:
    """Network results captured before the dispatcher takes its writer lock."""

    payloads: dict[tuple[str, int], Optional[dict]]
    capped: frozenset[tuple[str, int]]
    now: float
    # Per-key cause text for lookups that raised. Carried rather than logged so
    # the locked pass owns the ONE warning a failed lookup is allowed to emit.
    errors: dict[tuple[str, int], str] = field(default_factory=dict)
    # ``task_id -> (fingerprint, repo)`` repo-context snapshot. Resolving a
    # bare ``#N`` shells out to ``git remote -v``; carrying the answer keeps
    # that subprocess — not just the ``gh`` one — out of the writer lock. The
    # fingerprint is the card state the answer was derived from, so a card
    # re-pointed in between is detected and skipped rather than resolved
    # against a stale repository.
    contexts: dict[str, tuple[tuple, Optional[str]]] = field(default_factory=dict)
    # ``(tree, merge_sha) -> is-ancestor-of-HEAD`` for every merged ref on a
    # deploy-premised card. ``git merge-base`` is a subprocess, so it is
    # answered HERE, outside the writer lock, like every other shell-out. A
    # key absent under the lock is "not verified this tick" and holds silently.
    deployed: dict[tuple[str, str], bool] = field(default_factory=dict)


# Process-lifetime cache keyed by ``(repo.lower(), number)``. MERGED is the
# only irreversible state and therefore the only process-lifetime entry;
# OPEN/CLOSED are reused for CACHE_TTL_SECONDS because GitHub permits reopen.
_CACHE: dict[tuple[str, int], _CacheEntry] = {}

# Process-lifetime cache for :func:`repo_exists`, keyed by lowercased slug.
# Both outcomes are cached: a body's prose tokens are stable per card, so the
# negative answers are what keep this to roughly one lookup per distinct token
# for the life of the dispatcher.
_REPO_EXISTS_CACHE: dict[str, bool] = {}


def clear_cache() -> None:
    """Drop all cached PR states (tests; operator repair)."""
    _CACHE.clear()
    _REPO_EXISTS_CACHE.clear()


# ---------------------------------------------------------------------------
# Harness safety: a stubbed oracle may never write to a live board
# ---------------------------------------------------------------------------


class SandboxEscape(RuntimeError):
    """A fabricated-oracle write was aimed at a board outside the sandbox."""


def _in_test_context() -> bool:
    """True when this process is a pytest run or an explicitly-marked harness.

    ``PYTEST_CURRENT_TEST`` answers for the in-test phase; ``HERMES_IN_PYTEST``
    is the opt-in a bare probe script sets for itself. Either is sufficient —
    the guard is deliberately cheap to trip and cheap to satisfy.
    """
    return bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("HERMES_IN_PYTEST")
    )


def _db_is_sandboxed() -> bool:
    """True only when isolation is POSITIVELY declared, never merely observed.

    Isolation has to be proven by a property a production process cannot
    satisfy. Two containment-based anchors were tried and both were satisfiable
    by the live board:

    * :func:`hermes_cli.kanban_db.kanban_home` — the SHARED kanban root, so the
      production DB sits under it by construction ("is this board internally
      consistent with its own root?" is always yes).
    * the DECLARED ``HERMES_HOME`` — the fleet exports
      ``HERMES_HOME=~/.hermes`` from ~20 installed launchd jobs and shell
      helpers, and the live ``kanban.db`` sits directly inside it. Measured
      2026-09-21 with no pins and no pytest marker: ``sandboxed=True``, a
      fabricated oracle ALLOWED on the real board.

    So containment is not evidence — any relation the live layout already
    satisfies can be reached by inheriting the ordinary fleet env. The predicate
    is instead the explicit opt-in the refusal message already prescribes,
    ``HERMES_KANBAN_SANDBOX=1`` (:func:`kanban_db.kanban_sandbox_enabled`),
    which no fleet component sets and which additionally makes every kanban path
    resolve from ``HERMES_HOME`` and ignore the ``HERMES_KANBAN_*`` pins.

    Containment under the declared ``HERMES_HOME`` is retained as a SECOND
    condition, not a substitute: the flag says "I intend to be isolated", the
    containment check confirms the resolution actually landed there.

    Fail CLOSED: a missing flag, an unset ``HERMES_HOME``, or any resolution
    error counts as not-sandboxed. A guard that cannot prove isolation must not
    grant it.
    """
    try:
        from hermes_cli import kanban_db as kb

        if not kb.kanban_sandbox_enabled():
            return False
        declared = os.environ.get("HERMES_HOME", "").strip()
        if not declared:
            return False
        target = Path(kb.kanban_db_path()).resolve(strict=False)
        root = Path(declared).expanduser().resolve(strict=False)
    except Exception:
        return False
    return target.is_relative_to(root)


def assert_write_allowed(
    query_fn: Optional[Callable] = None,
    *,
    deploy_fn: Optional[Callable] = None,
) -> None:
    """Refuse a gate mutation driven by a fabricated oracle on a live board.

    The one fact that matters is ORACLE IDENTITY: if ``query_fn`` is not the
    real :func:`query_pr`, whatever "MERGED" it reports is invented, and writing
    a ``gate_auto_resolved`` from it unblocks real cards on evidence that does
    not exist. That is provable without any cooperation from the harness.

    It is deliberately NOT preconditioned on a test marker. The 2026-09-21
    incident probe was a bare ``python probe.py`` that set neither
    ``PYTEST_CURRENT_TEST`` nor ``HERMES_IN_PYTEST``, so a guard gated behind
    :func:`_in_test_context` returns before it ever examines the oracle — i.e.
    it cannot stop the one shape it was written for. The marker survives only to
    enrich the refusal message.

    Production is untouched: the dispatcher passes ``None`` or the real
    ``gh``-backed oracle, both of which return immediately.
    """
    # A stubbed DEPLOY oracle fabricates the other half of the unblock
    # evidence ("merged AND live"), so it is held to the same identity test.
    deploy_real = deploy_fn is None or deploy_fn is _REAL_IS_DEPLOYED
    if (query_fn is None or query_fn is _REAL_QUERY_PR) and deploy_real:
        return  # real oracles: the verdict is evidence, not fabrication.
    if _db_is_sandboxed():
        return
    try:
        from hermes_cli import kanban_db as kb

        resolved = str(kb.kanban_db_path())
    except Exception:  # pragma: no cover - diagnostic only
        resolved = "<unresolvable>"
    marker = (
        "this process IS marked as a test context"
        if _in_test_context()
        else "this process carries NO test marker (a bare probe script)"
    )
    try:
        from hermes_cli import kanban_db as kb

        opted_in = kb.kanban_sandbox_enabled()
    except Exception:  # pragma: no cover - diagnostic only
        opted_in = False
    why = (
        f"HERMES_KANBAN_SANDBOX is not set, so isolation was never declared "
        f"(resolved DB {resolved})"
        if not opted_in
        else f"resolved DB {resolved} is not inside the declared HERMES_HOME "
        f"root ({os.environ.get('HERMES_HOME') or '<unset>'})"
    )
    raise SandboxEscape(
        "kanban PR-gate: refusing to mutate a board with a STUBBED PR oracle. "
        f"{why}; {marker}. Containment alone is NOT proof of isolation — the "
        "fleet exports HERMES_HOME=~/.hermes and the live board sits inside "
        "it, so isolation must be declared positively. Set "
        "HERMES_KANBAN_SANDBOX=1 with HERMES_HOME pointed at a throwaway root "
        "(the flag also neutralises the HERMES_KANBAN_* path pins) before "
        "running this harness."
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_pr_refs(
    text: Optional[str], *, default_repo: Optional[str] = None
) -> list[PrRef]:
    """Extract PR references from a block reason, in first-seen order.

    ``default_repo`` resolves bare ``#N`` / ``pull/N`` forms. When it is None
    those forms are DROPPED rather than guessed — an unblock is a real state
    transition and must never rest on an inferred repository.

    ``text``'s OWN qualified refs outrank ``default_repo``. The block reason is
    closer evidence than the card body it was derived from: live card
    ``t_9a74e029`` names ``ANG-Ventures/hermes-home#225`` in plain text, and
    its sibling bare ``#225``/``#228`` were still resolved against
    ``ANG-Ventures/hermes-agent`` — two real PRs in the wrong repository —
    because the BODY carried a qualified hermes-agent ref. A reason that
    contradicts its own sibling numbers is not a repo the gate may act on.

    When the reason names MORE than one distinct repo the bare numbers are
    genuinely ambiguous, so they are dropped entirely rather than attached to
    either — the same fail-safe as disagreeing remotes. Measured over every
    blocked card on all 76 boards: 19 reasons carry bare numbers, 1 also
    carries a qualified ref (``t_9a74e029``, the defect), and 0 name two.

    This is the parse CHOKE POINT — both the locked and unlocked gate paths
    reach bare-``#N`` resolution through here — so the rule cannot be bypassed
    by a caller that assembles its own ``default_repo``.
    """
    if not isinstance(text, str) or not text.strip():
        return []

    refs: list[PrRef] = []
    seen: set[tuple[str, int]] = set()
    found: list[tuple[int, PrRef]] = []

    def add(position: int, repo: Optional[str], raw_number: str) -> None:
        if not repo:
            return
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            return
        if number <= 0:
            return
        key = (repo.lower(), number)
        if key in seen:
            return
        seen.add(key)
        found.append((position, PrRef(repo=repo, number=number)))

    # The reason's own qualified refs override the caller's context for the
    # bare forms below. One distinct repo is an answer; two is ambiguity and
    # resolves to None, which drops the bare numbers.
    own = _corroborated_repos(text)
    if own:
        distinct = {slug.lower(): slug for slug in own}
        default_repo = next(iter(distinct.values())) if len(distinct) == 1 else None

    # Consume the specific forms first, blanking each match so a later, looser
    # pattern cannot re-read the same digits as a different reference. Order is
    # restored by source position afterwards, so the returned list reads in the
    # order a human sees the references in the reason text.
    remaining = text
    for pattern in (_URL_RE, _QUALIFIED_RE):
        out: list[str] = []
        last = 0
        for match in pattern.finditer(remaining):
            add(match.start(), match.group(1), match.group(2))
            out.append(remaining[last:match.start()])
            out.append(" " * (match.end() - match.start()))
            last = match.end()
        out.append(remaining[last:])
        remaining = "".join(out)

    if default_repo:
        for match in _BARE_RE.finditer(remaining):
            add(match.start(), default_repo, match.group(1))

    refs = [ref for _, ref in sorted(found, key=lambda pair: pair[0])]
    return refs


def _git(workspace_path: str, *args: str) -> Optional[str]:
    """Run a read-only git command in ``workspace_path``; None on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", workspace_path, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            timeout=_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


def _is_repo_toplevel(workspace_path: str) -> bool:
    """True only when ``workspace_path`` IS a checkout root, not inside one.

    ``git -C <dir>`` WALKS UP to the first enclosing repository, so a directory
    with no repo of its own silently answers for whatever contains it. Every
    ``scratch``-kind kanban workspace lives under ``~/.hermes``, which is itself
    a checkout — and its remotes all agree, so the walk-up produced a single
    UNANIMOUS (and wrong) slug that the disagreeing-remotes fail-safe in
    :func:`repo_context` cannot see. That is how card ``t_cb701eee`` was
    unblocked two seconds after a verifier blocked it, on merge evidence from
    ``ANG-Ventures/hermes-home`` (2026-09-21).

    Toplevel identity, not ``.git`` presence, is the discriminator: a git
    worktree's ``.git`` is a file, and its toplevel is itself, so a worktree
    workspace still resolves normally.
    """
    out = _git(workspace_path, "rev-parse", "--show-toplevel")
    if not out:
        return False
    toplevel = out.strip()
    if not toplevel:
        return False
    try:
        return Path(toplevel).resolve() == Path(workspace_path).resolve()
    except OSError:
        return False


def _remotes_for(workspace_path: str) -> set[str]:
    """Return the distinct ``owner/repo`` slugs a checkout's remotes point at.

    Empty when ``workspace_path`` is not the checkout TOPLEVEL — see
    :func:`_is_repo_toplevel`. ``repo_context`` then falls through to
    body-based resolution, which yields the card's own repo or None.
    """
    if not _is_repo_toplevel(workspace_path):
        return set()
    stdout = _git(workspace_path, "remote", "-v")
    if stdout is None:
        return set()
    slugs: set[str] = set()
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        match = _GIT_REMOTE_RE.search(parts[1])
        if match:
            slugs.add(match.group(1).removesuffix(".git"))
    return slugs


def _body_repo_mentions(body: str) -> Iterable[str]:
    """Yield plausible naked ``owner/repo`` mentions, excluding source paths.

    Two filters, in increasing strength:

    * the OWNER must look like a GitHub account (``_OWNER_RE``) — this is a
      positive property, and is what rejects ``hermes_cli/kanban*.py`` and
      ``hermes_cli/kanban_db``, neither of which a suffix denylist can catch;
    * the repo segment must not carry a source-file extension.
    """
    seen: set[str] = set()
    for match in _REPO_MENTION_RE.finditer(body):
        slug = match.group(1).rstrip(".")
        owner, _, repo_name = slug.partition("/")
        if not _OWNER_RE.match(owner):
            continue
        if Path(repo_name).suffix.lower() in _SOURCE_PATH_SUFFIXES:
            continue
        if slug in seen:
            continue
        seen.add(slug)
        yield slug


def _corroborated_repos(body: str) -> list[str]:
    """Repos named by a PR URL or a qualified ``owner/repo#N`` in the body.

    A slug that is attached to an actual PR reference is evidence, not a
    guess, so it outranks any number of bare mentions.
    """
    out: list[str] = []
    for pattern in (_URL_RE, _QUALIFIED_RE):
        for match in pattern.finditer(body):
            slug = match.group(1)
            if slug not in out:
                out.append(slug)
    return out


def _body_repo_choice(
    body: str, *, exists_fn: Optional[Callable[[str], bool]] = None
) -> Optional[str]:
    """The single repo a bare ``#N`` in ``body`` may be resolved against.

    ``src/utils`` is a *legal* repo slug, so a body naming both it and a real
    repo has two candidates and no way to rank them. Rather than take the
    first (a coin flip that queries the wrong repo), return None and let the
    re-evaluator take no action — the same fail-safe as disagreeing remotes.

    The remaining hole this closes is the SINGLE-candidate case. A lone
    slash-bearing prose token (``before/after.`` on live card ``t_edb301c0``)
    is the only candidate in its body, so it was RANKED as the default repo —
    the same confidently-wrong shape :func:`_is_repo_toplevel` closed on the
    workspace leg. ``_body_repo_mentions`` cannot tell prose from a slug
    lexically, because real repo names and English word pairs have the same
    shape; measured over all 76 boards, 522 bodies reach this branch and the
    best lexical cue kept only 10 of 22 real repos while still admitting 14
    prose tokens (``P0/P1``, ``title/body``, ``origin/main``).

    So the discriminator is EXISTENCE, not vocabulary: a token may be ranked
    only when GitHub actually has a repository by that name. Measured on the
    same population that is 18 of 18 genuine repo mentions kept and 412 of 412
    prose tokens rejected. ``10/10`` is the one prose token that IS a real
    repository, which is why the numeric-segment filter runs first and why
    existence alone is not sufficient.

    This only ever RANKS — :func:`_body_corroborates` still only MATCHES, so
    the asymmetry #870 relies on is preserved: noise can decline a resolution,
    never redirect one.
    """
    corroborated = _corroborated_repos(body)
    if len(corroborated) == 1:
        return corroborated[0]
    if corroborated:
        return None
    candidates = list(_body_repo_mentions(body))
    if len(candidates) != 1:
        return None
    slug = candidates[0]
    owner, _, name = slug.partition("/")
    if _NUMERIC_SEGMENT_RE.match(owner) or _NUMERIC_SEGMENT_RE.match(name):
        return None
    exists_fn = exists_fn or repo_exists
    return slug if exists_fn(slug) else None


def _body_corroborates(slug: str, body: Optional[str]) -> bool:
    """True when the CARD's own text supports resolving bare ``#N`` in ``slug``.

    A workspace's remotes describe a *directory*, not a card. Two live shapes
    make that directory answer for a repository the card has nothing to do
    with, and only one of them involves a walk-up:

    * ``scratch``/nested — ``git -C`` inherits an ENCLOSING repo's remotes
      (closed by :func:`_is_repo_toplevel`);
    * ``dir``-at-toplevel — the workspace IS a checkout root, just not of the
      card's repo. Four live cards point at ``~/.hermes`` itself, whose remotes
      both read ``ANG-Ventures/hermes-home``. Geometry is perfect; the answer is
      still wrong.

    So the trust test is card IDENTITY, not geometry: the body must either name
    the workspace's repo, or name no repo at all. A body that names repositories
    and omits this one is evidence AGAINST it — which is exactly the 2026-09-21
    ``t_cb701eee`` body, naming ``Kyzcreig/ace-media-homelab`` in prose with its
    PR as a bare ``#160``.

    Silence is allowed on purpose: a worktree card whose body names no repo
    (``t_8e1f2cf3``) is the legitimate case bare-``#N`` resolution exists for.
    The rule MATCHES mentions, it never RANKS them, so slash-bearing prose that
    ``_body_repo_mentions`` cannot tell from a slug (``try/except``, ``30/31``)
    can only DECLINE a resolution, never redirect one at a wrong repo. Measured
    over all 48 gate-candidate blocked cards on all 65 board DBs, that cost is
    zero: the rule dropped exactly the 4 ``dir``-at-``~/.hermes`` cards and all
    11 genuine resolutions survived.
    """
    if not isinstance(body, str):
        return True
    corroborated = _corroborated_repos(body)
    if corroborated:
        # PR-attached refs are evidence, not a guess: they outrank bare mentions.
        return any(named.lower() == slug.lower() for named in corroborated)
    mentions = list(_body_repo_mentions(body))
    if not mentions:
        return True
    return any(named.lower() == slug.lower() for named in mentions)


def repo_context(
    *, workspace_path: Optional[str], body: Optional[str]
) -> Optional[str]:
    """Best-effort repository for resolving a bare ``#N``.

    Order: the card's workspace remote (only when the workspace IS the
    checkout toplevel AND every remote agrees — the hermes-agent checkout has
    ``origin`` = upstream and ``fork`` = ours, so a bare ``#787`` there is
    genuinely ambiguous), then the first ``owner/repo`` mentioned in the card
    body. None means "do not resolve bare numbers".

    Two fail-safes, both of which have to hold before a workspace answer is
    trusted:

    * **no walk-up** — :func:`_is_repo_toplevel` refuses an answer inherited
      from an ENCLOSING repository (every ``scratch`` workspace sits under
      ``~/.hermes``, itself a checkout);
    * **body corroboration** — :func:`_body_corroborates` requires the card's
      own text to name the workspace's repo, or to name none at all. Geometry
      alone is not enough: a ``dir``-kind workspace pointing AT ``~/.hermes``
      is a perfectly valid toplevel and still answers for the wrong repo.
      Ambiguity detection cannot catch a confidently-wrong answer, and an
      unblock reverts a human/verifier decision.
    """
    if workspace_path:
        try:
            exists = Path(workspace_path).is_dir()
        except OSError:
            exists = False
        if exists:
            slugs = _remotes_for(workspace_path)
            if len(slugs) == 1:
                chosen = next(iter(slugs))
                if not _body_corroborates(chosen, body):
                    # The body names repositories and the workspace's is not
                    # among them. Two incompatible answers, no way to rank
                    # them: take no action.
                    return None
                return chosen
            if len(slugs) > 1 and isinstance(body, str):
                # Ambiguous remotes: let an explicit body mention pick one.
                lowered = {s.lower(): s for s in slugs}
                for mention in _body_repo_mentions(body):
                    hit = lowered.get(mention.lower())
                    if hit:
                        return hit
                return None
            if len(slugs) > 1:
                return None
    if isinstance(body, str):
        return _body_repo_choice(body)
    return None


# ---------------------------------------------------------------------------
# GitHub lookup
# ---------------------------------------------------------------------------


def query_pr(repo: str, number: int) -> Optional[dict]:
    """Return normalized PR JSON from ``gh api``, or None on ANY failure.

    The normalized shape matches the in-process test seam:
    ``state`` (OPEN/CLOSED/MERGED), ``mergedAt``, ``mergeCommitSha``.
    None is deliberately indistinguishable from "cannot tell" so every caller
    degrades to no-action rather than to a wrong action.
    """
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{number}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            timeout=_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    merged_at = payload.get("merged_at")
    state = str(payload.get("state") or "").upper()
    return {
        "state": "MERGED" if merged_at else state,
        "mergedAt": merged_at,
        "mergeCommitSha": payload.get("merge_commit_sha"),
    }


def repo_exists(slug: str) -> bool:
    """True only when ``gh api repos/<slug>`` names EXACTLY ``slug``.

    Fail-SAFE in the restrictive direction: any failure (no ``gh``, no network,
    timeout, 404, bad JSON) returns False, which declines a bare-``#N``
    resolution rather than guessing one. That is the same trade
    :func:`query_pr` makes in the other direction — None means "cannot tell"
    and the caller takes no action.

    The full-name comparison is load-bearing, not belt-and-braces. GitHub
    REDIRECTS renamed and numeric paths: ``repos/14/14`` answers 200 with
    ``paglia201/paglia201``. Accepting a bare 200 would have re-admitted the
    prose token ``14/14`` as a repository.

    Results are cached for the process because this runs once per gate
    candidate per tick and a repo's existence is not tick-volatile.
    """
    if not isinstance(slug, str) or "/" not in slug:
        return False
    key = slug.lower()
    cached = _REPO_EXISTS_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{slug}", "--jq", ".full_name"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            timeout=_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return False
    exists = proc.returncode == 0 and (proc.stdout or "").strip().lower() == key
    while len(_REPO_EXISTS_CACHE) >= _CACHE_LIMIT:
        _REPO_EXISTS_CACHE.pop(next(iter(_REPO_EXISTS_CACHE)))
    _REPO_EXISTS_CACHE[key] = exists
    return exists


def _merge_sha(payload: dict) -> Optional[str]:
    commit = payload.get("mergeCommit")
    if isinstance(commit, dict):
        oid = commit.get("oid") or commit.get("sha")
        if oid:
            return str(oid)
    for key in ("mergeCommitSha", "mergeCommitOid"):
        if payload.get(key):
            return str(payload[key])
    return None


def _prune_cache() -> None:
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.pop(next(iter(_CACHE)))


# The genuine ``gh``-backed oracle, captured at import. ``assert_write_allowed``
# compares against THIS, never against the module attribute: a harness that
# monkeypatches ``kanban_pr_gate.query_pr`` would otherwise rebind the very name
# the guard checks and vouch for its own stub. That is exactly how the
# 2026-09-21 probe fabricated 20 gate_auto_resolved events.
_REAL_QUERY_PR = query_pr


# ---------------------------------------------------------------------------
# Deploy premise: MERGED is not LIVE
# ---------------------------------------------------------------------------


def names_deploy_tree(*texts: Optional[str]) -> bool:
    """True when any of ``texts`` names a deploy tree (``~/.hermes`` family)."""
    return any(
        isinstance(text, str) and _DEPLOY_TREE_MENTION_RE.search(text)
        for text in texts
    )


def names_deploy_gate(reason: Optional[str]) -> bool:
    """True when a block reason states its gate is a DEPLOY, not a merge."""
    if not isinstance(reason, str):
        return False
    return bool(_DEPLOY_WORD_RE.search(_NO_DEPLOY_RE.sub(" ", reason)))


def owner_holds_unblock(reason: Optional[str]) -> bool:
    """True when a block reason reserves the unblock for a named owner."""
    return isinstance(reason, str) and bool(_OWNER_HELD_RE.search(reason))


def deploy_tree_for(repo: str) -> Optional[str]:
    """The ``~``-relative deploy tree for ``repo``, or None (merge-only repo)."""
    return DEPLOY_TREES.get(repo.lower()) if isinstance(repo, str) else None


def is_deployed(tree: str, sha: str) -> bool:
    """True only when ``sha`` is an ancestor of ``tree``'s HEAD.

    ``git -C <tree> merge-base --is-ancestor <sha> HEAD``: rc 0 is the only
    success. rc 1 (not an ancestor), rc 128 (object unknown — the tree has not
    even fetched it), a missing tree, or a timeout all answer False: a card is
    never resumed on a deploy that cannot be proven.
    """
    if not tree or not sha:
        return False
    path = os.path.expanduser(tree)
    try:
        proc = subprocess.run(
            ["git", "-C", path, "merge-base", "--is-ancestor", sha, "HEAD"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return False
    return proc.returncode == 0


_REAL_IS_DEPLOYED = is_deployed


def _deploy_checks(
    refs: Iterable[PrRef], *, reason: Optional[str], body: Optional[str],
) -> list[tuple[PrRef, Optional[str]]]:
    """``(ref, tree)`` pairs whose merge must ALSO be live before unblocking.

    Empty unless the card's reason or body names a deploy tree, or the reason
    states a deploy gate. Then one pair per ref whose repo maps to a tree.
    When the REASON states a deploy gate, an unmapped ref is included with
    ``tree=None``: its deploy cannot be proven, so it holds. A tree mention
    alone keeps unmapped repos merge-only (the pre-t_a7287385 behaviour).
    """
    stated = names_deploy_gate(reason)
    if not stated and not names_deploy_tree(reason, body):
        return []
    out: list[tuple[PrRef, Optional[str]]] = []
    for ref in refs:
        tree = deploy_tree_for(ref.repo)
        if tree or stated:
            out.append((ref, tree))
    return out


def _deploy_marker(pending: Iterable[tuple[str, Optional[str]]]) -> str:
    names = sorted({f"{tree}@{sha or 'unknown'}" for tree, sha in pending})
    return "<!-- gate-deploy:" + "|".join(names) + " -->"


_UNVERIFIABLE_TREE = "<no deploy tree>"


def _awaiting_deploy_sentence(
    pending: list[tuple[PrRef, "_CacheEntry", Optional[str]]],
) -> str:
    parts = [
        f"{ref} MERGED, awaiting deploy of {(entry.sha or 'unknown')[:8]} "
        + (f"into {tree}" if tree else "(unverifiable: no deploy tree for this repo)")
        for ref, entry, tree in pending
    ]
    marker = _deploy_marker(
        (tree or _UNVERIFIABLE_TREE, entry.sha) for _, entry, tree in pending
    )
    tail = (
        " A merge in a repo with no mapped deploy tree cannot be proven live, "
        "so this gate will NOT auto-resolve: unblock it by hand once deployed."
        if any(tree is None for _, _, tree in pending) else ""
    )
    return (
        "gate held: " + "; ".join(parts) + ". The card's premise is the "
        "change being LIVE, so it stays blocked until the merge commit is an "
        f"ancestor of the tree's HEAD.{tail}\n{marker}"
    )


class _Resolver:
    """Bounded, cached PR-state resolution for one tick."""

    def __init__(
        self,
        *,
        query_fn: Callable[[str, int], Optional[dict]],
        max_lookups: int,
        now: float,
        prefetched: Optional[_PrefetchResult] = None,
    ) -> None:
        self._query_fn = query_fn
        self._remaining = max_lookups
        self._now = now
        self._prefetched = prefetched
        # Includes failures. A failed unique PR lookup is attempted once in this
        # tick, but remains retryable on the next tick because it never enters
        # the process-lifetime cache.
        self._attempted: dict[tuple[str, int], Optional[_CacheEntry]] = {}
        # Cause text for the keys that failed this tick, surfaced in the single
        # warning the caller emits per failed unique PR.
        self.failure_causes: dict[tuple[str, int], str] = {}
        self.budget_exhausted = False

    def resolve(self, ref: PrRef) -> Optional[_CacheEntry]:
        """Return the cached/fresh state, or None when it cannot be determined."""
        key = (ref.repo.lower(), ref.number)
        if key in self._attempted:
            return self._attempted[key]
        entry = _CACHE.get(key)
        if entry is not None and (
            entry.terminal or self._now - entry.fetched_at < CACHE_TTL_SECONDS
        ):
            self._attempted[key] = entry
            return entry

        if self._prefetched is not None:
            if key in self._prefetched.capped:
                self.budget_exhausted = True
                self._attempted[key] = None
                return None
            # A missing key means the card's block changed after the unlocked
            # snapshot. Never perform replacement network I/O under the lock.
            payload = self._prefetched.payloads.get(key)
            cause = self._prefetched.errors.get(key)
            if cause:
                self.failure_causes[key] = cause
        else:
            if self._remaining <= 0:
                self.budget_exhausted = True
                self._attempted[key] = None
                return None
            self._remaining -= 1
            try:
                payload = self._query_fn(ref.repo, ref.number)
            except Exception as exc:  # defensive provider seam
                # Same failure class as a None return: no action, and the
                # caller emits the one permitted warning.
                self.failure_causes[key] = f"{type(exc).__name__}: {exc}"
                payload = None

        if payload is None:
            self._attempted[key] = None
            return None
        merged_at = payload.get("mergedAt")
        state = "MERGED" if merged_at else str(payload.get("state") or "").upper()
        if state not in {"OPEN", "CLOSED", "MERGED"}:
            self._attempted[key] = None
            return None
        entry = _CacheEntry(
            state=state,
            sha=_merge_sha(payload),
            merged_at=str(merged_at) if merged_at else None,
            fetched_at=self._now,
            terminal=state == "MERGED",
        )
        _CACHE[key] = entry
        _prune_cache()
        self._attempted[key] = entry
        return entry


# ---------------------------------------------------------------------------
# Board reads
# ---------------------------------------------------------------------------


def _latest_block_event(
    conn: sqlite3.Connection, task_id: str
) -> tuple[Optional[str], Optional[float]]:
    """``(reason, created_at)`` of the most recent block-family event.

    ``created_at`` is what separates a PR the card is WAITING on from a PR its
    reason merely cites as history: one already merged at block time cannot be
    the thing the card is blocked on.
    """
    placeholders = ",".join("?" * len(_BLOCK_EVENT_KINDS))
    row = conn.execute(
        f"SELECT payload, created_at FROM task_events WHERE task_id = ? "
        f"AND kind IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (task_id, *_BLOCK_EVENT_KINDS),
    ).fetchone()
    if row is None or not row["payload"]:
        return None, None
    try:
        blocked_at = float(row["created_at"]) if row["created_at"] is not None else None
    except (TypeError, ValueError):
        blocked_at = None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None, blocked_at
    if not isinstance(payload, dict):
        return None, blocked_at
    reason = payload.get("reason")
    return (reason if isinstance(reason, str) else None), blocked_at


def _latest_block_reason(
    conn: sqlite3.Connection, task_id: str
) -> Optional[str]:
    """Reason text of the most recent block-family event for a task."""
    return _latest_block_event(conn, task_id)[0]


def _parse_merged_at(value: Optional[str]) -> Optional[float]:
    """GitHub ``mergedAt`` (ISO-8601, ``Z`` suffix) as epoch seconds, or None."""
    if not value or not isinstance(value, str):
        return None
    from datetime import datetime, timezone

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _previously_resolved_refs(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """Lower-cased PR names a prior ``gate_auto_resolved`` on this card cited.

    A PR this gate already resolved for the card is spent: re-reading it from a
    later block reason is the t_20b94ef5 loop. This is the fallback for a PR
    whose ``mergedAt`` is missing or unparseable, where the time test cannot
    decide.
    """
    spent: set[str] = set()
    for row in conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'gate_auto_resolved'",
        (task_id,),
    ):
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        prs = payload.get("prs") if isinstance(payload, dict) else None
        if isinstance(prs, list):
            spent.update(str(p).lower() for p in prs if isinstance(p, str))
    return spent


def _is_history(
    ref: PrRef,
    entry: _CacheEntry,
    *,
    blocked_at: Optional[float],
    spent: set[str],
) -> bool:
    """True when ``ref`` cannot be what the card is blocked on.

    Only a MERGED PR can be history. It is history when it merged at or before
    the block event, or when this gate already resolved it for this card.
    """
    if entry.state != "MERGED":
        return False
    if str(ref).lower() in spent:
        return True
    merged_ts = _parse_merged_at(entry.merged_at)
    return (
        merged_ts is not None
        and blocked_at is not None
        and merged_ts <= blocked_at
    )


def _closed_ref_marker(refs: Iterable[PrRef]) -> str:
    names = sorted({str(ref).lower() for ref in refs})
    return "<!-- gate-pr-set:" + "|".join(names) + " -->"


def _has_comment_marker(
    conn: sqlite3.Connection, task_id: str, marker: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM task_comments "
        "WHERE task_id = ? AND instr(body, ?) > 0 LIMIT 1",
        (task_id, marker),
    ).fetchone()
    return row is not None


def _already_flagged_closed(
    conn: sqlite3.Connection, task_id: str, refs: Iterable[PrRef]
) -> bool:
    """True when the closed-unmerged advisory was already posted for these PRs.

    Without this the advisory would be re-posted on every 60-second tick, which
    is the exact "automation spams the card" failure mode the board already has
    enough of. The marker includes the PR set so a later re-block on a different
    closed PR still gets the required human advisory.
    """
    marker = _closed_ref_marker(refs)
    row = conn.execute(
        "SELECT 1 FROM task_comments "
        "WHERE task_id = ? AND instr(body, ?) > 0 LIMIT 1",
        (task_id, marker),
    ).fetchone()
    return row is not None


_CLOSED_MARKER = "gate object closed without merge"
_SATISFIED_PREFIX = "gate satisfied"
_AUTHOR = "kanban-pr-gate"


def _satisfied_sentence(entries: list[tuple[PrRef, _CacheEntry]]) -> str:
    parts = []
    for ref, entry in entries:
        sha8 = (entry.sha or "")[:8] or "unknown"
        when = entry.merged_at or "unknown"
        parts.append(f"{ref} merged {sha8} at {when}")
    return f"{_SATISFIED_PREFIX}: " + "; ".join(parts)


def _closed_sentence(entries: list[tuple[PrRef, _CacheEntry]]) -> str:
    refs = [ref for ref, _ in entries]
    names = ", ".join(str(ref) for ref in refs)
    return (
        f"{_CLOSED_MARKER}: {names} — needs a human. "
        "The card stays blocked; re-point it at the live PR or unblock it "
        f"explicitly once the work has landed some other way.\n{_closed_ref_marker(refs)}"
    )


_PREFETCH_WORKERS = 6


def _gate_candidates(
    conn: sqlite3.Connection,
) -> list[tuple[str, tuple, Optional[str], Optional[str], str, Optional[float]]]:
    """In-scope blocked cards whose reason could name a PR. Pure DB reads.

    Returns ``(task_id, fingerprint, workspace_path, body, reason,
    blocked_at)``. The
    fingerprint is every input ``repo_context`` depends on, so a cached
    context can be proven still applicable without re-deriving it.
    """
    placeholders = ",".join("?" * len(GATE_BLOCK_KINDS))
    rows = conn.execute(
        f"SELECT id, body, workspace_path FROM tasks "
        f"WHERE status = 'blocked' AND block_kind IN ({placeholders}) "
        f"ORDER BY id",
        tuple(sorted(GATE_BLOCK_KINDS)),
    ).fetchall()
    out: list[tuple[str, tuple, Optional[str], Optional[str], str, Optional[float]]] = []
    for row in rows:
        reason, blocked_at = _latest_block_event(conn, row["id"])
        if not reason or ("#" not in reason and "pull/" not in reason):
            continue
        if owner_holds_unblock(reason):
            # The blocker reserved the unblock for a named owner. No PR state
            # can override that, so the card is not a candidate (and burns
            # no lookup budget).
            continue
        fingerprint = (row["workspace_path"], row["body"], reason)
        out.append(
            (row["id"], fingerprint, row["workspace_path"], row["body"], reason,
             blocked_at)
        )
    return out


def _blocked_gate_refs(
    conn: sqlite3.Connection,
    *,
    contexts: Optional[dict[str, tuple[tuple, Optional[str]]]] = None,
) -> list[tuple[str, list[PrRef], list[tuple[PrRef, Optional[str]]], Optional[float]]]:
    """Snapshot in-scope blocked cards, their resolvable PR refs, deploy checks, and block time.

    ``contexts`` is a repo-context snapshot taken by the unlocked prefetch.
    When supplied this function performs NO subprocess I/O: a card absent from
    the snapshot, or whose fingerprint moved since it was taken, is dropped so
    the locked pass takes no action on it (the next tick re-derives it). When
    it is None the caller is the direct, unlocked path and contexts are
    resolved inline.
    """
    candidates: list[tuple[str, list[PrRef], list[tuple[PrRef, Optional[str]]], Optional[float]]] = []
    for (
        task_id, fingerprint, workspace_path, body, reason, blocked_at,
    ) in _gate_candidates(conn):
        if contexts is None:
            default_repo = repo_context(workspace_path=workspace_path, body=body)
        else:
            cached = contexts.get(task_id)
            if cached is None or cached[0] != fingerprint:
                # Card is new or changed since the unlocked snapshot. Resolving
                # it here would mean shelling out under the caller's lock, so
                # fail safe instead — a deferred gate costs one tick.
                continue
            default_repo = cached[1]
        refs = parse_pr_refs(reason, default_repo=default_repo)
        if refs:
            candidates.append(
                (task_id, refs, _deploy_checks(refs, reason=reason, body=body), blocked_at)
            )
    return candidates


def prefetch_pr_gate_states(
    conn: sqlite3.Connection,
    *,
    query_fn: Optional[Callable[[str, int], Optional[dict]]] = None,
    max_lookups: int = MAX_LOOKUPS_PER_TICK,
    now: Optional[float] = None,
    deploy_fn: Optional[Callable[[str, str], bool]] = None,
) -> _PrefetchResult:
    """Fetch stale PR states concurrently before the dispatch writer lock.

    The returned snapshot is reapplied only after the card's live blocked state
    and reason are parsed again under the lock. A changed/new reference is absent
    from the snapshot and therefore causes a fail-safe no-op, never lock-held I/O.
    Six workers bound 30 five-second lookups to roughly 25 seconds worst case.
    """
    query_fn = query_fn or query_pr
    deploy_fn = deploy_fn or is_deployed
    now = time.time() if now is None else now
    # Same harness-safety gate as the locked pass: refuse a fabricated oracle
    # aimed at a live board at the FIRST entry point of the tick.
    assert_write_allowed(query_fn, deploy_fn=deploy_fn)
    unique: dict[tuple[str, int], PrRef] = {}
    contexts: dict[str, tuple[tuple, Optional[str]]] = {}
    deploy_checks: list[tuple[PrRef, Optional[str]]] = []
    # Resolve repo context HERE, outside the writer lock: this is the seam that
    # shells out to ``git remote -v`` (same 5 s timeout as ``gh``), and a
    # degraded workspace would otherwise hold the board's single-writer lock
    # for seconds per card.
    for (
        task_id, fingerprint, workspace_path, body, reason, _blocked_at,
    ) in _gate_candidates(conn):
        default_repo = repo_context(workspace_path=workspace_path, body=body)
        contexts[task_id] = (fingerprint, default_repo)
        card_refs = parse_pr_refs(reason, default_repo=default_repo)
        deploy_checks.extend(_deploy_checks(card_refs, reason=reason, body=body))
        for ref in card_refs:
            key = (ref.repo.lower(), ref.number)
            entry = _CACHE.get(key)
            if entry is not None and (
                entry.terminal or now - entry.fetched_at < CACHE_TTL_SECONDS
            ):
                continue
            unique.setdefault(key, ref)

    keys = list(unique)
    selected = keys[:max(0, max_lookups)]
    capped = frozenset(keys[len(selected):])
    payloads: dict[tuple[str, int], Optional[dict]] = {}
    errors: dict[tuple[str, int], str] = {}
    if not selected:
        return _PrefetchResult(
            payloads=payloads, capped=capped, now=now, errors=errors,
            contexts=contexts,
            deployed=_prefetch_deploys(deploy_checks, payloads, deploy_fn),
        )

    workers = min(_PREFETCH_WORKERS, len(selected))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(query_fn, unique[key].repo, unique[key].number): key
            for key in selected
        }
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                payloads[key] = future.result()
            except Exception as exc:  # defensive provider seam
                # Deliberately NOT logged here: a raised lookup and a None
                # lookup are the same failure, and the contract allows ONE
                # warning per failed unique PR per tick. The cause travels to
                # reevaluate_pr_gates(), which owns that single warning.
                payloads[key] = None
                errors[key] = f"{type(exc).__name__}: {exc}"
    return _PrefetchResult(
        payloads=payloads, capped=capped, now=now, errors=errors,
        contexts=contexts,
        deployed=_prefetch_deploys(deploy_checks, payloads, deploy_fn),
    )


def _prefetch_deploys(
    checks: list[tuple[PrRef, Optional[str]]],
    payloads: dict[tuple[str, int], Optional[dict]],
    deploy_fn: Callable[[str, str], bool],
) -> dict[tuple[str, str], bool]:
    """Answer every deploy-ancestry question the locked pass can ask.

    The merge SHA comes from this tick's payload or the permanent MERGED cache
    — the same two sources :class:`_Resolver` will read under the lock — so
    every key the locked pass looks up is present here. Unmerged or
    SHA-less refs are skipped: they cannot be deployed yet.
    """
    out: dict[tuple[str, str], bool] = {}
    for ref, tree in checks:
        if tree is None:
            continue  # unmapped repo: no tree to probe; the locked pass holds it
        key = (ref.repo.lower(), ref.number)
        sha: Optional[str] = None
        payload = payloads.get(key)
        if isinstance(payload, dict):
            if payload.get("mergedAt") or str(payload.get("state") or "").upper() == "MERGED":
                sha = _merge_sha(payload)
        else:
            entry = _CACHE.get(key)
            if entry is not None and entry.state == "MERGED":
                sha = entry.sha
        if not sha or (tree, sha) in out:
            continue
        try:
            out[(tree, sha)] = bool(deploy_fn(tree, sha))
        except Exception:  # defensive seam: unprovable deploy = not deployed
            out[(tree, sha)] = False
    return out


# ---------------------------------------------------------------------------
# The tick pass
# ---------------------------------------------------------------------------


def reevaluate_pr_gates(
    conn: sqlite3.Connection,
    *,
    query_fn: Optional[Callable[[str, int], Optional[dict]]] = None,
    max_lookups: int = MAX_LOOKUPS_PER_TICK,
    now: Optional[float] = None,
    prefetched: Optional[_PrefetchResult] = None,
    deploy_fn: Optional[Callable[[str, str], bool]] = None,
) -> list[GateOutcome]:
    """Re-evaluate every in-scope blocked card against its referenced PRs.

    Returns one :class:`GateOutcome` per card that actually named a PR — cards
    whose block reason mentions none are skipped entirely and never appear in
    the result (nor consume any lookup budget).

    Safe to call on every dispatcher tick: bounded lookups, cached results, and
    every uncertain path degrades to no action.
    """
    query_fn = query_fn or query_pr
    deploy_fn = deploy_fn or is_deployed
    if prefetched is not None:
        now = prefetched.now
    now = time.time() if now is None else now
    # Harness safety gate, BEFORE any card is read or mutated: a stubbed oracle
    # aimed at a live board is refused outright rather than allowed to write a
    # fabricated gate_auto_resolved. See the module docstring.
    assert_write_allowed(query_fn, deploy_fn=deploy_fn)
    resolver = _Resolver(
        query_fn=query_fn,
        max_lookups=max_lookups,
        now=now,
        prefetched=prefetched,
    )

    outcomes: list[GateOutcome] = []
    warned_failures: set[tuple[str, int]] = set()
    # Re-read and re-parse under the caller's dispatch lock. This is the state
    # revalidation seam for an unlocked prefetch: if the card changed in the
    # interim, its fingerprint no longer matches the snapshot and it is skipped
    # (fail-safe), so this pass performs NO subprocess I/O of any kind.
    for task_id, refs, deploy_checks, blocked_at in _blocked_gate_refs(
        conn, contexts=None if prefetched is None else prefetched.contexts,
    ):

        resolved: list[tuple[PrRef, _CacheEntry]] = []
        unresolved = False
        unresolved_ref: Optional[PrRef] = None
        for ref in refs:
            entry = resolver.resolve(ref)
            if entry is None:
                unresolved = True
                unresolved_ref = ref
                break
            resolved.append((ref, entry))

        names = tuple(str(r) for r in refs)
        if unresolved:
            if resolver.budget_exhausted:
                outcomes.append(GateOutcome(
                    task_id=task_id, action="budget_exhausted", prs=names,
                    detail="lookup budget spent this tick; deferred",
                ))
            else:
                failure_key = (
                    (unresolved_ref.repo.lower(), unresolved_ref.number)
                    if unresolved_ref is not None else None
                )
                if failure_key not in warned_failures:
                    cause = resolver.failure_causes.get(failure_key or ("", 0))
                    _log.warning(
                        "kanban PR-gate: could not resolve PR state for %s (%s)%s; "
                        "taking no action",
                        task_id, ", ".join(names),
                        f" ({cause})" if cause else "",
                    )
                    if failure_key is not None:
                        warned_failures.add(failure_key)
                outcomes.append(GateOutcome(
                    task_id=task_id, action="lookup_failed", prs=names,
                    detail="PR state lookup failed",
                ))
            continue

        # A ref already merged when the card blocked (or already resolved by
        # this gate for this card) is context, not the gate. See t_20b94ef5.
        # A deploy-premised ref is never history: a card that blocked on
        # "#N live in <tree>" after #N merged is waiting on the DEPLOY, which
        # the merge time says nothing about (t_289e8020).
        spent = _previously_resolved_refs(conn, task_id)
        # Only a MAPPED deploy check exempts a ref from history: an unmapped
        # stated-deploy ref (tree=None, t_a7287385) can never be proven live, so a
        # merge that predates the block is still just context (t_20b94ef5).
        deploy_keys = {(r.repo.lower(), r.number) for r, t in deploy_checks if t}
        gating = [
            (r, e) for r, e in resolved
            if (r.repo.lower(), r.number) in deploy_keys
            or not _is_history(r, e, blocked_at=blocked_at, spent=spent)
        ]
        if not gating:
            outcomes.append(GateOutcome(
                task_id=task_id, action="history_only", prs=names,
                detail="every referenced PR had already merged before the "
                       "block (or was already resolved for this card); "
                       "not a PR gate",
            ))
            continue
        resolved = gating
        names = tuple(str(r) for r, _ in gating)

        closed = [(r, e) for r, e in resolved if e.state == "CLOSED"]
        if closed:
            detail = _closed_sentence(closed)
            if not _already_flagged_closed(conn, task_id, (r for r, _ in closed)):
                _safe_comment(conn, task_id, detail)
            outcomes.append(GateOutcome(
                task_id=task_id, action="closed_unmerged", prs=names,
                detail=detail,
            ))
            continue

        if any(e.state != "MERGED" for _, e in resolved):
            outcomes.append(GateOutcome(
                task_id=task_id, action="held", prs=names,
                detail="at least one referenced PR is still open",
            ))
            continue

        # MERGED is not DEPLOYED. A card whose premise names a deploy tree may
        # resume only once every mapped merge is an ancestor of that tree's
        # HEAD. Under a prefetch this is a dict read (no subprocess under the
        # writer lock); a key missing from the snapshot holds silently.
        by_ref = {(r.repo.lower(), r.number): e for r, e in resolved}
        pending: list[tuple[PrRef, _CacheEntry, Optional[str]]] = []
        unverified = False
        for ref, tree in deploy_checks:
            entry = by_ref.get((ref.repo.lower(), ref.number))
            if entry is None:
                # Filtered out above as history: it is not the gate, so its
                # deploy state cannot hold the card either.
                continue
            if tree is None or not entry.sha:
                pending.append((ref, entry, tree))
                continue
            if prefetched is not None:
                live = prefetched.deployed.get((tree, entry.sha))
            else:
                try:
                    live = bool(deploy_fn(tree, entry.sha))
                except Exception:
                    live = False
            if live is None:
                unverified = True
            elif not live:
                pending.append((ref, entry, tree))
        if pending:
            detail = _awaiting_deploy_sentence(pending)
            marker = _deploy_marker(
                (t or _UNVERIFIABLE_TREE, e.sha) for _, e, t in pending
            )
            if not _has_comment_marker(conn, task_id, marker):
                _safe_comment(conn, task_id, detail)
            outcomes.append(GateOutcome(
                task_id=task_id, action="awaiting_deploy", prs=names,
                detail=detail,
            ))
            continue
        if unverified:
            outcomes.append(GateOutcome(
                task_id=task_id, action="held", prs=names,
                detail="deploy state not verified this tick; deferred",
            ))
            continue

        detail = _satisfied_sentence(resolved)
        outcomes.append(_apply_satisfied(conn, task_id, names, detail))

    return outcomes


def _safe_comment(conn: sqlite3.Connection, task_id: str, body: str) -> None:
    """Post a board comment; a comment failure must never abort the tick."""
    try:
        from hermes_cli import kanban_db as kb

        kb.add_comment(conn, task_id, _AUTHOR, body)
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning(
            "kanban PR-gate: could not comment on %s: %s", task_id, exc,
        )


def _apply_satisfied(
    conn: sqlite3.Connection,
    task_id: str,
    names: tuple[str, ...],
    detail: str,
) -> GateOutcome:
    """Unblock a card whose every referenced PR has merged."""
    from hermes_cli import kanban_db as kb

    try:
        unblocked = kb.unblock_task(conn, task_id)
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning(
            "kanban PR-gate: unblock of %s failed: %s", task_id, exc,
        )
        return GateOutcome(
            task_id=task_id, action="lookup_failed", prs=names,
            detail=f"unblock failed: {exc}",
        )
    if not unblocked:
        # Raced with a concurrent writer; the next tick re-evaluates.
        return GateOutcome(
            task_id=task_id, action="held", prs=names,
            detail="card left the blocked state before the unblock landed",
        )
    _safe_comment(conn, task_id, detail)
    with kb.write_txn(conn, allow_nested=True):
        kb._append_event(
            conn, task_id, "gate_auto_resolved",
            {"prs": list(names), "detail": detail},
        )
    _log.info("kanban PR-gate: %s auto-resolved — %s", task_id, detail)
    return GateOutcome(
        task_id=task_id, action="unblocked", prs=names, detail=detail,
    )
