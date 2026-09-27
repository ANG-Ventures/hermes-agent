"""Shared fake for the survivor verifier's GitHub PR lookup.

``kanban_external_survivor.verify_pr`` reads PR state over REST
(``gh api repos/<slug>/pulls/<n>``), not GraphQL ``gh pr view`` (t_1e080b8d).
Fixtures keep describing a PR in the field names the verifier reasons about
(``state``/``headRefOid``/``headRefName``/``mergeCommit``/``title``/``body``)
and answer the subprocess seam with the REST shape GitHub actually returns.
"""
import re

_PULLS = re.compile(r"repos/([^/]+/[^/]+)/pulls/([0-9]+)")


def pr_target(args):
    """``(slug, number)`` for a ``gh api repos/<slug>/pulls/<n>`` call, else None."""
    if len(args) >= 3 and args[0] == "gh" and args[1] == "api":
        match = _PULLS.fullmatch(args[2])
        if match:
            return match[1], match[2]
    return None


def rest_pr(view):
    """The REST payload GitHub returns for the PR ``view`` describes."""
    state = view.get("state")
    merged = state == "MERGED"
    return {
        "state": "open" if state == "OPEN" else "closed",
        "merged": merged,
        "merged_at": "2026-09-25T00:00:00Z" if merged else None,
        "merge_commit_sha": (view.get("mergeCommit") or {}).get("oid"),
        "head": {"sha": view.get("headRefOid"), "ref": view.get("headRefName")},
        "title": view.get("title"),
        "body": view.get("body"),
    }
