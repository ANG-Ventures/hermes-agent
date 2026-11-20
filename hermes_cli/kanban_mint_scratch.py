"""A worktree card anchored on a checkout whose origin is an upstream we do not own mints as scratch (t_efd1497a).

Measured (t_b720fdea, Studio board, 30 days to 2026-11-19): 4 of the 5 ``workspace_kind`` Studio overrides were
send-back reruns of cards minted ``--workspace worktree:/Users/alexgierczyk/.hermes/hermes-agent``. That
checkout fetches from NousResearch/hermes-agent and pushes to ANG-Ventures/hermes-agent. Once the first run
leaves ``.worktrees/<id>`` on the Studio, every rerun needs its branch pushed to origin first, origin is not ours,
and the porter keeps the card on the Studio (``not_portable:workspace_kind:origin_not_ours``) until an operator
overrides it. The house rule is a scratch workspace plus a ``Repo:`` line; this applies it at the one create
choke point: such a card is minted ``scratch`` with ``Repo: <owner/repo>`` (the checkout's own push repo) and the
branch carried by name in a ``Branch:`` line, never by a Studio path.

Only that case: a worktree whose origin IS ours ports today and is left alone, and so is a path that is not a
git checkout or whose push URL is not a fleet-owned GitHub repo (nothing to name). Also left alone (Prism round 1):
a body whose ``Branch:`` line names another branch than the one asked for (the target is ambiguous), and a branch
that exists in the checkout with commits the push repo does not hold (a clone of the fork could not check them
out; only the tracking refs of a remote fetching that repo count). Never raises.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Callable, Optional, Tuple

_REPO_LINE_RE = re.compile(r"^[ \t]*repo:[ \t]*\S", re.IGNORECASE | re.MULTILINE)
_BRANCH_LINE_RE = re.compile(r"^[ \t]*branch:[ \t]*`?([^\s`]+)", re.IGNORECASE | re.MULTILINE)
NOTICE_PREFIX = "kanban: minted scratch, not worktree: "


def _git(path: str, *args: str, runner: Callable) -> str:
    try:
        proc = runner(["git", "-C", path, *args], capture_output=True, text=True, encoding="utf-8",
                      timeout=10, stdin=subprocess.DEVNULL,
                      env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip() if getattr(proc, "returncode", 1) == 0 else ""


def foreign_origin_slug(workspace_path: Optional[str], runner: Callable = subprocess.run) -> Optional[str]:
    """``owner/repo`` to name in the ``Repo:`` line when ``workspace_path`` is a checkout whose origin FETCHES
    from a repo the fleet does not own and PUSHES to one it does; else None."""
    from hermes_cli import kanban_worktree_port as kwp
    path = str(workspace_path or "")
    if not path or not os.path.isabs(path) or not os.path.isdir(path):
        return None
    fetch = _git(path, "remote", "get-url", "origin", runner=runner)
    if not fetch or kwp._own_destination(fetch):
        return None
    push = _git(path, "remote", "get-url", "--push", "origin", runner=runner)
    m = kwp._SLUG_RE.match(push)
    if not m or not kwp._own_destination(push):
        return None
    return m.group(1)


def _push_repo_remotes(workspace_path: str, slug: str, runner: Callable) -> list:
    """Names of the checkout's remotes whose FETCH url is the push repo ``slug`` (the repo a scratch clone uses).
    origin here fetches the upstream, so its tracking refs never count; neither does another fork's."""
    from hermes_cli import kanban_worktree_port as kwp
    names = []
    for name in _git(workspace_path, "remote", runner=runner).split():
        m = kwp._SLUG_RE.match(_git(workspace_path, "remote", "get-url", name, runner=runner))
        if m and m.group(1).lower() == slug.lower():
            names.append(name)
    return names


def has_local_only_commits(workspace_path: str, branch_name: str, runner: Callable = subprocess.run,
                           slug: Optional[str] = None) -> bool:
    """True when ``branch_name`` is a local branch in the checkout whose tip the push repo ``slug`` does not hold:
    a worker cloning that repo could not check the state out. Only the tracking refs of a remote that fetches
    ``slug`` count (Prism 06ea8a9299c1: the upstream or a contributor fork holding the tip is not enough); with no
    such remote, or stale refs, the worktree is kept. A failed probe of an existing branch counts as True."""
    tip = _git(workspace_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}^{{commit}}",
               runner=runner)
    if not tip:
        return False  # no such local branch: the worker creates it from the fork's default branch
    slug = slug or foreign_origin_slug(workspace_path, runner) or ""
    if not slug:
        return True
    for name in _push_repo_remotes(workspace_path, slug, runner):
        if _git(workspace_path, "for-each-ref", "--count=1", "--contains", tip, "--format=%(refname)",
                f"refs/remotes/{name}/", runner=runner):
            return False
    return True


def rewrite(workspace_kind: Optional[str], workspace_path: Optional[str], branch_name: Optional[str],
            body: Optional[str], runner: Callable = subprocess.run) -> Optional[Tuple[str, str]]:
    """``(new body, notice)`` when the card must mint scratch instead, else None (mint as asked)."""
    if workspace_kind != "worktree":
        return None
    try:
        slug = foreign_origin_slug(workspace_path, runner)
    except Exception:  # noqa: BLE001 - a probe fault mints the card as asked, never a refused create
        return None
    if not slug:
        return None
    text = body or ""
    stated = _BRANCH_LINE_RE.search(text)
    if branch_name and stated and stated.group(1) != branch_name:
        return None  # the body names another branch: converting would drop the requested one
    try:
        if branch_name and has_local_only_commits(str(workspace_path), branch_name, runner, slug):
            return None  # the branch holds commits only this checkout has: keep the worktree
    except Exception:  # noqa: BLE001 - a probe fault mints the card as asked
        return None
    add = []
    if not _REPO_LINE_RE.search(text):
        add.append(f"Repo: {slug}")
    if branch_name and not stated:
        add.append(f"Branch: {branch_name}")
    if add:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + "\n".join(add) + "\n"
    notice = (NOTICE_PREFIX + f"worktree:{workspace_path} fetches from an upstream the fleet does not own, so a "
              "rerun could never leave the Studio (origin_not_ours). The card is scratch with "
              f"`Repo: {slug}`" + (f" and `Branch: {branch_name}`" if branch_name else "")
              + "; the worker clones it (hermes kanban clone) and checks the branch out by name.")
    return text, notice
