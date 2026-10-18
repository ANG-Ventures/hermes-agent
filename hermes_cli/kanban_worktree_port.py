"""Worktree cards on pool hosts: clone from origin + the host's git mirror.

A ``--workspace worktree:`` card used to be Studio-only (``portable()`` rule
``workspace_kind``): its checkout is a linked worktree of a Studio repo. When
the repo has a mirror on a pool host (``/srv/ci/git-mirrors/*.git`` on
ci-box / ace-ai / ace-media), the card can run there instead (t_6d9e7d90):
the dispatcher clones origin into the card's scratch path on that host with
the mirror as ``--reference`` (objects shared, ~8 s for hermes-home on
ci-box), checks out the card's branch (``wt/<id>`` unless set), and converts
the card to ``scratch`` at that path. From then on it is an ordinary remote
scratch card: continuation keeps reruns on that host, remote GC removes it.

Which repos: ``<fleet>/kanban-worktree-mirrors.json`` (schema 1)::

    {"schema": 1, "repos": {"ANG-Ventures/hermes-home":
        {"mirror": "/srv/ci/git-mirrors/hermes-home-mirror.git",
         "hosts": ["ci-box", "ace-ai", "ace-media"]}}}

Absent / unreadable / malformed file = no repo ports (fail closed; the card
stays Studio-local as before). That file is the kill switch: drop a row.

Only an ANCHOR worktree ports (``workspace_path`` unset, or naming a repo
root): a concrete target path is a place on the Studio someone chose. A card
whose ``.worktrees/<id>`` already exists on the Studio keeps it (that tree
may hold a prior run's work).
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

MIRRORS_FILE = "kanban-worktree-mirrors.json"
PORTED_EVENT = "worktree_ported"
CLONE_TIMEOUT_S = 300
_SLUG_RE = re.compile(r"github\.com[:/]+([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")
_ORIGIN_TTL_S = 300.0
_origin_cache: Dict[str, Tuple[float, Optional[str]]] = {}
_log = logging.getLogger(__name__)


def read_mirrors(fleet_dir: Path) -> Dict[str, Tuple[str, Tuple[str, ...]]]:
    """``{owner/repo (lower): (mirror path, hosts)}``; {} on any doubt."""
    try:
        doc = json.loads((Path(fleet_dir) / MIRRORS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    repos = doc.get("repos") if isinstance(doc, dict) and doc.get("schema") == 1 else None
    out: Dict[str, Tuple[str, Tuple[str, ...]]] = {}
    for slug, row in (repos.items() if isinstance(repos, dict) else ()):
        if not isinstance(slug, str) or "/" not in slug or not isinstance(row, dict):
            continue
        mirror, hosts = row.get("mirror"), row.get("hosts")
        if not (isinstance(mirror, str) and mirror.startswith("/")) or not isinstance(hosts, list):
            continue
        hs = tuple(h.lower() for h in hosts if isinstance(h, str) and h)
        if hs:
            out[slug.lower()] = (mirror, hs)
    return out


def origin_slug(repo: Path, runner: Callable = subprocess.run) -> Optional[str]:
    """``owner/repo`` of ``repo``'s ``origin`` (GitHub URLs only), cached."""
    key = str(repo)
    hit = _origin_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _ORIGIN_TTL_S:
        return hit[1]
    slug = None
    try:
        proc = runner(["git", "-C", key, "remote", "get-url", "origin"], capture_output=True,
                      text=True, timeout=10, stdin=subprocess.DEVNULL)
        if getattr(proc, "returncode", 1) == 0:
            m = _SLUG_RE.search((proc.stdout or "").strip())
            slug = m.group(1) if m else None
    except (OSError, subprocess.SubprocessError):
        slug = None
    _origin_cache[key] = (time.monotonic(), slug)
    return slug


def anchor_repo(task_id: str, workspace_path: Optional[str],
                board_default: Optional[str]) -> Optional[Path]:
    """The repo root a worktree card would anchor on, or None (not portable)."""
    from hermes_cli.kanban_db_workspace import _git_toplevel
    raw = (workspace_path or "").strip() or (board_default or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute() or not p.is_dir():
        return None
    top = _git_toplevel(p)
    if top is None or top != p.resolve():
        return None  # a concrete target path, not an anchor: keep it local
    if (top / ".worktrees" / task_id).exists():
        return None  # a prior run's local worktree: its work stays where it is
    return top


def port_for(task_id: str, workspace_path: Optional[str], board_default: Optional[str],
             fleet_dir: Path, runner: Callable = subprocess.run
             ) -> Optional[Tuple[str, str, Tuple[str, ...]]]:
    """``(owner/repo, mirror, hosts)`` when this worktree card may run on a
    pool host, else None."""
    mirrors = read_mirrors(fleet_dir)
    if not mirrors:
        return None
    repo = anchor_repo(task_id, workspace_path, board_default)
    slug = origin_slug(repo, runner) if repo is not None else None
    row = mirrors.get(slug.lower()) if slug else None
    return (slug, row[0], row[1]) if row else None


def clone_script(workspace: str, slug: str, mirror: str, branch: str, *,
                 url: Optional[str] = None) -> str:
    """Remote sh: clone origin into the (empty) workspace with the mirror as
    reference, then check out ``branch`` (tracking origin's if it exists,
    else a new branch off origin's default, --no-track). Idempotent: a
    workspace that is already a checkout is left as is. rc 3 = the workspace
    holds something else (never overwritten)."""
    w, m, b = shlex.quote(workspace), shlex.quote(mirror), shlex.quote(branch)
    url = shlex.quote(url or f"https://github.com/{slug}.git")
    return (
        f"set -e; w={w}; mkdir -p -- \"$w\"; [ -d \"$w/.git\" ] && exit 0; "
        "[ -z \"$(ls -A -- \"$w\")\" ] || exit 3; "
        # the mirror is owned by the runner user: trust that one path only
        f"export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0={m}; "
        f"git clone -q --reference-if-able {m} {url} \"$w\"; cd \"$w\"; "
        f"if git ls-remote --exit-code --heads origin {b} >/dev/null 2>&1; then "
        f"git checkout -q -B {b} --track origin/{b}; "
        f"else git checkout -q --no-track -b {b} origin/HEAD; fi"
    )


def clone_on_host(host, workspace: str, slug: str, mirror: str, branch: str,
                  runner: Callable = subprocess.run) -> None:
    """Run :func:`clone_script` on ``host`` (a ``PoolHost``); raise on failure."""
    from hermes_cli.kanban_worker_pool import _ssh_argv
    proc = runner(_ssh_argv(host, clone_script(workspace, slug, mirror, branch)),
                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=CLONE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    rc = getattr(proc, "returncode", 1)
    if rc != 0:
        raise RuntimeError(f"worktree clone of {slug} on {host.name} failed rc={rc}: "
                           f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")


def convert(conn, task, workspace: str, branch: str, *, host: str, slug: str,
            mirror: str) -> None:
    """Persist the port: the card is now a scratch card at ``workspace``."""
    from hermes_cli import kanban_db as _kb
    with _kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_kind='scratch', workspace_path=?, branch_name=? "
                     "WHERE id=?", (workspace, branch, task.id))
        _kb._append_event(conn, task.id, PORTED_EVENT,
                          {"host": host, "repo": slug, "mirror": mirror, "branch": branch,
                           "workspace": workspace}, run_id=task.current_run_id)
    task.workspace_kind, task.workspace_path, task.branch_name = "scratch", workspace, branch


def board_default_workdir(board: Optional[str]) -> Optional[str]:
    from hermes_cli import kanban_db as _kb
    try:
        slug = board if board else _kb.get_current_board()
        return (_kb.read_board_metadata(slug).get("default_workdir") or "").strip() or None
    except Exception:
        return None

