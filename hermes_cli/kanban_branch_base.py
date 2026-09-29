"""Branch-base guard: a worker branch must sit on the CURRENT remote trunk.

Card t_18e781d0 (2026-09-27). In one night five hermes-home PRs were re-cut
or re-ported because their branches were cut from a stale base: #1004 was cut
233 commits behind main (re-cut #1033), a survivor patch touched 138 files for
the same reason (re-cut #1035), #985 was 70 behind and DIRTY in the merge
queue (re-cut #1038), #841/#788 were 307/317 behind with conflicts. The root
was mechanical: the dispatcher created ``worktree`` workspaces with
``git worktree add -b <branch> <path> HEAD`` -- the anchor checkout's local
HEAD, which is whatever that long-lived tree last pulled (plus any local
commits it carries). Nothing re-checked the base before the handoff, so the
failure surfaced at Apollo's merge pass instead of in the worker's run.

Two halves:

* :func:`fresh_trunk_ref` fetches the checkout's remote trunk; the dispatcher
  branches new worktrees from it instead of HEAD.
* :func:`check_checkout` measures a branch against the freshly fetched trunk.
  It FAILS on the stale-base signatures, never on a raw file count:

  - ``conflicts``: ``git merge-tree`` says the branch does not merge cleanly
    into the current trunk (the DIRTY merge-queue case);
  - ``foreign_commits``: commits on the branch the card cannot have authored
    -- patch-equivalent to a commit already on trunk (``git cherry`` ``-``),
    or committed before the card existed (inherited from a stale local HEAD);
  - ``out_of_scope``: files the branch changes that fall outside the card's
    declared scope (only when the card declares one). Scope is matched at
    directory granularity, so an intentionally broad refactor inside the
    declared area passes regardless of how many files it touches;
  - ``behind`` > ``max_behind`` (CLI / pre-push only; at handoff the
    freshness gate already updates a merely-behind PR on GitHub).

``tree_delta_files`` (``git diff <trunk> HEAD``, what a snapshot/patch of the
branch would apply over current trunk) is reported next to ``own_files``
(merge-base..HEAD) so the explosion is visible in numbers.

Handoff enforcement lives in :func:`enforce_handoff`, called by
``kanban_db.complete_task`` / ``request_review`` for worker runs. Fail-open on
anything it cannot measure (no trunk, fetch/merge-tree failure). Kill switch:
``KANBAN_HANDOFF_BASE_GUARD=0``; per-handoff override: metadata
``base_guard_override="<reason>"`` (recorded on the card).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Sequence

_log = logging.getLogger(__name__)

MAX_BEHIND = 20  # same number as fleet-merge.sh FLEET_MERGE_STALE_BASE_MAX
_FETCH_TIMEOUT_S = 30
_GIT_TIMEOUT_S = 30
_SCRUB_ENV = (
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
)
_SCOPE_IGNORED_PARTS = {"", ".", "..", "~", "tests", "test"}


class StaleBaseError(ValueError):
    """A worker handoff whose branch sits on a stale or foreign base."""

    def __init__(self, task_id: str, reports: Sequence["BaseReport"]):
        self.task_id = task_id
        self.reports = list(reports)
        body = "\n".join(r.render() for r in self.reports)
        super().__init__(
            f"handoff refused by the branch-base guard:\n{body}\n"
            f"{task_id} is still in-flight (no state change). Fix the branch and retry, "
            "or pass metadata base_guard_override=\"<reason>\" if the flagged content is intended."
        )


def _git(repo: Path, *args: str, timeout: float = _GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False, env=env,
    )


def _out(repo: Path, *args: str) -> Optional[str]:
    try:
        proc = _git(repo, *args)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _lines(repo: Path, *args: str) -> list[str]:
    return [ln for ln in (_out(repo, *args) or "").splitlines() if ln.strip()]


def resolve_trunk(repo: Path) -> Optional[tuple[str, str]]:
    """Return ``(remote, branch)`` for the checkout's integration trunk.

    Remote order: the remote ``main``/``master`` track, then ``origin``, then
    every other remote. Branch: ``<remote>/HEAD``'s target, else main/master.
    """
    remotes = _lines(repo, "remote")
    if not remotes:
        return None
    order: list[str] = []
    for local in ("main", "master"):
        r = _out(repo, "config", f"branch.{local}.remote")
        if r and r in remotes and r not in order:
            order.append(r)
    for r in ["origin", *remotes]:
        if r in remotes and r not in order:
            order.append(r)
    for remote in order:
        head = _out(repo, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
        candidates = [head.split("/", 1)[1]] if head and "/" in head else []
        candidates += ["main", "master"]
        for branch in candidates:
            if _out(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"):
                return remote, branch
    return None


def fresh_trunk_ref(repo: Path, *, fetch: bool = True) -> Optional[str]:
    """Fetch the trunk and return ``<remote>/<branch>``; None when unresolvable.

    A failed fetch still returns the existing remote-tracking ref (it is at
    worst as fresh as the last fetch, and never carries local-only commits).
    """
    trunk = resolve_trunk(repo)
    if trunk is None:
        return None
    remote, branch = trunk
    if fetch:
        try:
            proc = _git(
                repo, "fetch", "--quiet", "--no-tags", remote,
                f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
                timeout=_FETCH_TIMEOUT_S,
            )
            if proc.returncode != 0:
                _log.warning("branch-base: fetch %s/%s failed in %s: %s", remote, branch,
                             repo, (proc.stderr or "").strip()[:200])
        except (OSError, subprocess.SubprocessError) as exc:
            _log.warning("branch-base: fetch %s/%s failed in %s: %s", remote, branch, repo, exc)
    return f"{remote}/{branch}"


def scope_match(path: str, scope: Iterable[str]) -> bool:
    """True when ``path`` falls inside the declared scope.

    A declared entry containing a glob character is matched with fnmatch.
    Otherwise it scopes its DIRECTORIES: ``path`` is in scope when it has the
    same basename as a declared file, or its first real directory (``tests/``
    prefixes stripped) appears among the declared entry's directory parts.
    ``hermes_cli/x.py`` therefore covers ``hermes_cli/y.py`` and
    ``tests/hermes_cli/test_y.py``, and ``~/.hermes/scripts/a.py`` covers
    ``scripts/b.sh`` -- a card's body names the area, not every test file.
    """
    p = PurePosixPath(path)
    parts = [x for x in p.parts if x not in _SCOPE_IGNORED_PARTS]
    top = parts[0] if len(parts) > 1 else ""
    for entry in scope:
        entry = (entry or "").strip()
        if not entry:
            continue
        if any(ch in entry for ch in "*?["):
            if fnmatch.fnmatch(path, entry):
                return True
            continue
        e = PurePosixPath(entry)
        if e.name == p.name:
            return True
        dirs = {x for x in e.parts[:-1] if x not in _SCOPE_IGNORED_PARTS}
        if entry.endswith("/"):
            dirs.add(e.name)
        if top and top in dirs:
            return True
        if not top and "." in dirs:  # repo-root files scoped by a root-level entry
            return True
    return False


@dataclass
class BaseReport:
    repo: str
    branch: Optional[str]
    trunk: Optional[str]
    head: Optional[str] = None
    merge_base: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    own_files: list[str] = field(default_factory=list)
    tree_delta_files: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    foreign_commits: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    skipped: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.failures

    def render(self) -> str:
        if self.skipped:
            return f"[skip] {self.repo}: {self.skipped}"
        head = (
            f"[{'ok' if self.ok else 'FAIL'}] {self.repo} {self.branch or '(detached)'} "
            f"vs {self.trunk}: ahead {self.ahead}, behind {self.behind}, "
            f"own files {len(self.own_files)}, tree delta vs trunk {len(self.tree_delta_files)} files"
        )
        lines = [head]
        for f in self.failures:
            lines.append(f"  - {f}")
        if not self.ok:
            lines.append(f"  remediation: {self.remediation()}")
        return "\n".join(lines)

    def remediation(self) -> str:
        trunk = self.trunk or "<remote>/main"
        remote = trunk.split("/", 1)[0]
        return (
            f"git fetch {remote} && re-port ONLY this card's commits onto a fresh branch: "
            f"`git switch -c <new-branch> {trunk} && git cherry-pick <your-shas>` "
            f"(or `git rebase {trunk}` when there are no foreign commits), re-run the "
            "narrow tests, push, and hand back the new branch/PR."
        )


def _foreign_commits(repo: Path, trunk: str, since: Optional[float]) -> list[str]:
    out: list[str] = []
    for line in _lines(repo, "cherry", trunk, "HEAD"):
        mark, _, sha = line.partition(" ")
        if mark == "-":
            out.append(f"{sha[:10]} already on {trunk} (patch-equivalent)")
    if since is not None:
        for line in _lines(repo, "log", "--format=%H %ct %s", f"{trunk}..HEAD"):
            sha, ct, subject = (line.split(" ", 2) + ["", ""])[:3]
            try:
                if int(ct) < int(since):
                    out.append(f"{sha[:10]} committed before the card existed: {subject[:70]}")
            except ValueError:
                continue
    return out


def _conflicts(repo: Path, trunk: str) -> Optional[list[str]]:
    """Conflicted paths merging HEAD into trunk; None when merge-tree can't say."""
    try:
        proc = _git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                    trunk, "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return []
    if proc.returncode == 1:
        return [ln for ln in proc.stdout.splitlines()[1:] if ln.strip()]
    return None


def check_checkout(
    repo: Path,
    *,
    trunk: Optional[str] = None,
    scope: Sequence[str] = (),
    since: Optional[float] = None,
    max_behind: Optional[int] = MAX_BEHIND,
    fetch: bool = True,
) -> BaseReport:
    """Measure ``repo``'s HEAD against the (freshly fetched) trunk."""
    repo = Path(repo)
    branch = _out(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    trunk = trunk or fresh_trunk_ref(repo, fetch=fetch)
    rep = BaseReport(repo=str(repo), branch=branch, trunk=trunk, scope=list(scope))
    if trunk is None:
        rep.skipped = "no remote trunk to measure against"
        return rep
    rep.head = _out(repo, "rev-parse", "HEAD")
    rep.merge_base = _out(repo, "merge-base", trunk, "HEAD")
    if not rep.head or not rep.merge_base:
        rep.skipped = f"no merge-base between HEAD and {trunk}"
        return rep
    counts = (_out(repo, "rev-list", "--left-right", "--count", f"{trunk}...HEAD") or "0 0").split()
    rep.behind, rep.ahead = int(counts[0]), int(counts[1])
    if rep.ahead == 0:
        rep.skipped = f"no commits ahead of {trunk}"
        return rep
    # Already landed (t_22c91696): every commit ahead is patch-equivalent to
    # one on trunk -- the squash/queue merge of this branch's own PR. The
    # branch adds nothing, so "foreign commit" and "does not merge cleanly"
    # (trunk moved on over the same lines) are measurements of the merge itself.
    # ``git cherry`` omits merge commits while ``ahead`` counts them
    # (FleetReview #1394): only skip when cherry accounts for EVERY ahead
    # commit, so a merge commit carrying extra changes is still measured.
    cherry = _lines(repo, "cherry", trunk, "HEAD")
    if cherry and len(cherry) == rep.ahead and all(ln.startswith("- ") for ln in cherry):
        rep.skipped = f"already landed on {trunk} ({len(cherry)} commit(s) patch-equivalent)"
        return rep
    rep.own_files = _lines(repo, "diff", "--name-only", rep.merge_base, "HEAD")
    rep.tree_delta_files = _lines(repo, "diff", "--name-only", trunk, "HEAD")

    conflicts = _conflicts(repo, trunk)
    if conflicts:
        rep.conflicts = conflicts
        rep.failures.append(
            f"does not merge cleanly into {trunk} ({len(conflicts)} conflicted: "
            f"{', '.join(conflicts[:8])}{' ...' if len(conflicts) > 8 else ''})"
        )
    rep.foreign_commits = _foreign_commits(repo, trunk, since)
    if rep.foreign_commits:
        rep.failures.append(
            f"{len(rep.foreign_commits)} commit(s) this card did not author: "
            + "; ".join(rep.foreign_commits[:5])
            + (" ..." if len(rep.foreign_commits) > 5 else "")
        )
    if scope:
        rep.out_of_scope = [f for f in rep.own_files if not scope_match(f, scope)]
        if rep.out_of_scope:
            rep.failures.append(
                f"{len(rep.out_of_scope)} changed file(s) outside the card's declared scope: "
                + ", ".join(rep.out_of_scope[:8])
                + (" ..." if len(rep.out_of_scope) > 8 else "")
            )
    if max_behind is not None and rep.behind > max_behind:
        rep.failures.append(
            f"base is {rep.behind} commits behind {trunk} (limit {max_behind}); "
            f"{len(rep.tree_delta_files)} files differ from trunk vs {len(rep.own_files)} this branch changed"
        )
    return rep


def _rc(repo: Path, *args: str) -> Optional[int]:
    try:
        return _git(repo, *args).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def pr_landed_checkout(rep: BaseReport, prs: Sequence[tuple[str, str]]) -> bool:
    """True when one merged PR ``(head_sha, merge_commit_sha)`` provably landed
    this checkout's work on the checkout's OWN trunk. All three must hold:

    * the checkout HEAD is an ancestor of the PR head (the PR carried it);
    * the merge commit is an ancestor of ``rep.trunk`` -- the PR merged into
      this checkout's repository and trunk, not a fork or side branch
      (FleetReview #1434, key 9212a9f6ac95);
    * content: re-applying the checkout's change (merge-base..HEAD) onto the
      merge commit is a clean no-op, so a later revert in the PR or a merge
      that discarded the change does not count (key 8db3d1e0e4ce).

    False whenever it cannot be shown -- the caller keeps the guard.
    """
    repo = Path(rep.repo)
    if not (rep.head and rep.merge_base and rep.trunk):
        return False
    for head_sha, merge_sha in prs:
        if not head_sha or not merge_sha:
            continue
        if _rc(repo, "merge-base", "--is-ancestor", rep.head, head_sha) != 0:
            continue
        if _rc(repo, "merge-base", "--is-ancestor", merge_sha, rep.trunk) != 0:
            continue
        merged_tree = _out(repo, "rev-parse", "--verify", "--quiet", f"{merge_sha}^{{tree}}")
        replay = _out(repo, "merge-tree", "--write-tree", "--no-messages",
                      f"--merge-base={rep.merge_base}", merge_sha, rep.head)
        if merged_tree and replay and replay.splitlines()[0].strip() == merged_tree:
            return True
    return False


def workspace_checkouts(root: Path, depth: int = 2) -> list[Path]:
    """Git checkouts at ``root`` and up to ``depth`` directory levels below it."""
    found: list[Path] = []

    def walk(d: Path, level: int) -> None:
        if (d / ".git").exists():
            found.append(d)
            return
        if level >= depth:
            return
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") \
                    and e.name not in ("node_modules", "__pycache__", "venv"):
                walk(Path(e.path), level + 1)

    if root.is_dir():
        walk(root, 0)
    return found


def enabled() -> bool:
    return os.environ.get("KANBAN_HANDOFF_BASE_GUARD", "1").strip() != "0"


def enforce_handoff(
    task_id: str,
    *,
    workspace_path: Optional[str],
    workspace_kind: Optional[str],
    scope: Sequence[str],
    created_at: Optional[float],
    metadata: Optional[dict] = None,
) -> Optional[dict]:
    """Check every checkout in a worker's workspace; raise on a stale base.

    Returns a JSON-able summary (None when nothing was checked). Only
    ``scratch`` and ``worktree`` workspaces are inspected: a ``dir:``
    workspace is a shared long-lived tree whose commits are not the card's.
    ``behind`` alone never refuses here -- the handoff freshness gate
    updates a merely-behind PR on GitHub.
    """
    if not enabled() or not workspace_path or workspace_kind not in ("scratch", "worktree"):
        return None
    override = (metadata or {}).get("base_guard_override") if isinstance(metadata, dict) else None
    reports = [
        check_checkout(repo, scope=scope, since=created_at, max_behind=None)
        for repo in workspace_checkouts(Path(workspace_path))
    ]
    checked = [r for r in reports if not r.skipped]
    if not checked:
        return None
    failed = [r for r in checked if not r.ok]
    summary = {
        "checked": [
            {"repo": r.repo, "branch": r.branch, "trunk": r.trunk, "ahead": r.ahead,
             "behind": r.behind, "own_files": len(r.own_files),
             "tree_delta_files": len(r.tree_delta_files), "failures": r.failures}
            for r in checked
        ],
    }
    if failed and not (isinstance(override, str) and override.strip()):
        raise StaleBaseError(task_id, failed)
    if failed:
        summary["override"] = str(override).strip()
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="hermes kanban base-check",
        description="Fail if a branch sits on a stale/foreign base before you push or hand back.",
    )
    add_arguments(ap)
    return run(ap.parse_args(argv))


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("path", nargs="?", default=".", help="checkout or workspace dir (default: .)")
    ap.add_argument("--base", default=None, help="trunk ref to compare against (default: fetched remote trunk)")
    ap.add_argument("--scope", action="append", default=[],
                    help="declared scope path/glob (repeatable); default: paths named in --task's body")
    ap.add_argument("--task", default=os.environ.get("HERMES_KANBAN_TASK"),
                    help="card id: scope from its body, foreign-commit cutoff from its creation time")
    ap.add_argument("--max-behind", type=int, default=MAX_BEHIND)
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--json", action="store_true")


def _task_scope(task_id: str) -> tuple[list[str], Optional[float]]:
    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
        try:
            task = kb.get_task(conn, task_id)
        finally:
            conn.close()
    except Exception as exc:  # board unreachable: measure without card context
        _log.warning("branch-base: could not read card %s: %s", task_id, exc)
        return [], None
    if task is None:
        return [], None
    return sorted(kb._extract_explicit_dispatch_file_paths(task.body)), task.created_at


def run(args: argparse.Namespace) -> int:
    scope, since = list(args.scope), None
    if args.task:
        task_scope, since = _task_scope(args.task)
        scope = scope or task_scope
    root = Path(args.path).resolve()
    repos = [root] if (root / ".git").exists() else workspace_checkouts(root)
    reports = [
        check_checkout(r, trunk=args.base, scope=scope, since=since,
                       max_behind=args.max_behind, fetch=not args.no_fetch)
        for r in repos
    ]
    if args.json:
        print(json.dumps([dict(asdict(r), ok=r.ok) for r in reports], indent=2))
    else:
        if not reports:
            print(f"no git checkout under {root}")
        for r in reports:
            print(r.render())
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
