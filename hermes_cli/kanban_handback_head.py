"""Handback-head drift gate: a handoff must cite its PR's CURRENT head (t_e52337cb).

A handoff whose summary names a commit of the card's open PR branch that is NOT
the PR head, and never names the head itself, is describing an older state of
the PR: "CI green on 3609522f" while the head is eb5936b2. A reviewer reads
those claims against the wrong head. Replay of 14 days of fleet handoffs
(2026-09-26..10-10, head read by the freshness gate at handoff time): 21 of
3,279 handoffs that named a sha did this.

Rules (:func:`decide`):

* Only shas of 7-40 lowercase hex holding a letter AND a digit count (a run id
  like ``38062422030`` is not a sha).
* A cited sha that is not a commit of the PR branch is ignored (base commits,
  main shas, force-pushed-away commits: no way to call them stale).
* The handoff passes when it names the current head anywhere (summary, result,
  or a metadata string). Naming it is how a worker "states why" an older
  commit is cited.

GitHub reads go through ``gh api`` (the process's own gh lane, the same read
the freshness gate makes), cached 60 s per PR. Any read failure is fail-OPEN:
the caller logs ``head_check_unavailable`` and the handoff proceeds. Inside
pytest there is no default reader; tests pass a stub.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Callable, Iterable, Optional

CACHE_TTL_S = 60
MAX_PRS = 5
_TIMEOUT_S = 10
_SHA_RE = re.compile(r"(?<![0-9A-Za-z/])[0-9a-f]{7,40}(?![0-9A-Za-z])")
_HAS_ALPHA = re.compile(r"[a-f]")
_HAS_DIGIT = re.compile(r"[0-9]")

# (repo, number) -> {"state", "head", "commits"} or None (unreadable).
PrReader = Callable[[str, int], Optional[dict]]
_CACHE: dict = {}


class StaleHandbackHeadError(ValueError):
    """The handoff cites an older commit of an open PR and never names its head."""

    def __init__(self, task_id: str, stale: list):
        self.task_id = task_id
        self.stale = list(stale)
        parts = [f"handback cites {s['cited']}, PR head is {s['head'][:12]} "
                 f"({s['pr']}): re-verify at head or state why" for s in self.stale]
        first = self.stale[0]
        repo, number = first["pr"].split("#", 1)
        super().__init__(
            "; ".join(parts)
            + f". Read the head with `gh pr view {number} -R {repo} --json headRefOid` "
            f"(REST: `gh api repos/{repo}/pulls/{number} --jq .head.sha`), re-check your "
            f"claims at it, and name it in the summary. {task_id} is still in-flight "
            f"(no state change)."
        )


def cited_shas(*texts: Optional[str]) -> list:
    out = []
    for text in texts:
        for m in _SHA_RE.findall(text or ""):
            if _HAS_ALPHA.search(m) and _HAS_DIGIT.search(m) and m not in out:
                out.append(m)
    return out


def _strings(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def decide(texts: list, metadata, head: str, commits: list) -> Optional[str]:
    """The stale cited sha (first one), or None when the handoff passes."""
    head = (head or "").lower()
    if not head:
        return None
    every = list(texts) + list(_strings(metadata))
    named = cited_shas(*every)
    if any(head.startswith(s) for s in named):
        return None
    for sha in cited_shas(*texts):
        if any(c.lower().startswith(sha) for c in commits) and not head.startswith(sha):
            return sha
    return None


def _gh(args: list) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["gh", "api", *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def read_pr(repo: str, number: int) -> Optional[dict]:
    """REST: PR state + head sha + branch commit shas. None when unreadable."""
    raw = _gh([f"repos/{repo}/pulls/{number}"])
    try:
        pr = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(pr, dict) or not (pr.get("head") or {}).get("sha"):
        return None
    out = {"state": str(pr.get("state") or "").lower(), "head": str(pr["head"]["sha"])}
    if out["state"] != "open":
        out["commits"] = []
        return out
    shas = _gh(["--paginate", f"repos/{repo}/pulls/{number}/commits?per_page=100", "--jq", ".[].sha"])
    if shas is None:
        return None
    out["commits"] = shas.split()
    return out


def _default_reader() -> Optional[PrReader]:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return read_pr


def _cached(reader: PrReader, repo: str, number: int, now: float) -> Optional[dict]:
    key = (repo.lower(), number)
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    try:
        value = reader(repo, number)
    except Exception:  # fail-open on any reader fault
        value = None
    if value is not None:
        _CACHE[key] = (now, value)
    return value


def check(*, task_id: str, summary: Optional[str], result: Optional[str] = None,
          metadata=None, survivor_pr=None, reader: Optional[PrReader] = None) -> dict:
    """Raise :class:`StaleHandbackHeadError`, or return ``{"unavailable": [...]}``.

    PRs checked: the card's own fleet PRs (``metadata.pr_url|pr|pr_urls``,
    ``survivor_pr``), else the fleet PRs the prose names; at most :data:`MAX_PRS`.
    """
    report: dict = {"unavailable": []}
    texts = [t for t in (summary, result) if isinstance(t, str)]
    if not cited_shas(*texts):
        return report
    if reader is None:
        reader = _default_reader()
        if reader is None:
            return report
    from hermes_cli.kanban_open_pr import extract_pr_refs, split_fleet

    md = metadata if isinstance(metadata, dict) else None
    refs = split_fleet(extract_pr_refs(metadata=md, survivor_pr=survivor_pr))[0]
    if not refs:
        refs = split_fleet(extract_pr_refs(*texts))[0]
    stale = []
    now = time.time()
    for ref in refs[:MAX_PRS]:
        key = f"{ref.repo}#{ref.number}"
        pr = _cached(reader, ref.repo, ref.number, now)
        if not isinstance(pr, dict):
            report["unavailable"].append(key)
            continue
        if pr.get("state") != "open":
            continue
        cited = decide(texts, metadata, pr.get("head") or "", pr.get("commits") or [])
        if cited:
            stale.append({"pr": key, "cited": cited, "head": pr["head"]})
    if stale:
        raise StaleHandbackHeadError(task_id, stale)
    return report
