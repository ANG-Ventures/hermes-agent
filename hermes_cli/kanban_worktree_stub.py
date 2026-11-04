"""Dead linked-worktree stubs at a kanban worktree target (t_42e59999).

A stub is a non-empty path where ``git worktree add`` must create a worktree,
but which is not a live worktree of the repo. The incident case: a ``.git``
gitfile pointing at a ``.git/worktrees/<name>`` admin dir that a prune removed
(t_5c6c67c5). ``worktree add`` refuses "already exists" on every retry, so the
card used to burn its whole retry budget and end ``gave_up``.

A stub is always HELD, never moved: :class:`WorkspaceUnavailable` makes the
dispatcher's refusal path release the claim without charging a failure (the
card stays ready, paged once per episode, and can never reach ``gave_up``).
The message names the stub, what its ``.git`` points at, whether that admin
dir exists, and any admin dir still registered to the path, so the operator
can recover that one stub. It never suggests a repo-wide ``worktree prune``:
that would also drop registrations of live worktrees on an unmounted volume.
An automatic move is t_224e5062.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable

DEAD_STUB_HELD = "dead_worktree_stub_held"


def _resolve(base: Path, pointed: str) -> str:
    """*pointed* as an absolute real path; a relative entry is relative to *base*."""
    return os.path.realpath(os.path.join(base, pointed))


def _gitfile_target(stub: Path) -> tuple[str, Path | None]:
    """Describe what ``<stub>/.git`` points at, and the admin dir it names."""
    dot_git = stub / ".git"
    if dot_git.is_symlink() or dot_git.is_dir():
        return f"{dot_git} is a directory, not a gitfile", None
    if not dot_git.exists():
        return f"{stub} has no .git", None
    text = dot_git.read_text(errors="replace").strip()
    if not text.startswith("gitdir:"):
        return f"{dot_git} is not a gitfile ({text[:80]!r})", None
    admin = Path(_resolve(stub, text[len("gitdir:"):].strip()))
    state = "exists" if admin.is_dir() else "does not exist"
    return f"its .git points at {admin}, which {state}", admin


def _registered_admin_dirs(repo_root: Path, stub: Path) -> list[Path]:
    """Admin dirs under ``<common>/worktrees`` whose ``gitdir`` names *stub*."""
    out = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--path-format=absolute",
         "--git-common-dir"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise OSError(f"cannot resolve the common git dir of {repo_root}")
    admin_root = Path(out.stdout.strip()) / "worktrees"
    want = os.path.realpath(stub / ".git")
    found = []
    for admin in sorted(admin_root.glob("*")) if admin_root.is_dir() else []:
        try:
            pointed = (admin / "gitdir").read_text().strip()
        except FileNotFoundError:
            continue
        if _resolve(admin, pointed) == want:  # `--relative-paths` entries too
            found.append(admin)
    return found


def _recovery_command(stub: Path) -> str:
    """The paste-safe shell line that moves *stub* aside.

    Every literal path is ``shlex.quote``d, so ``$(...)``, backticks and
    apostrophes in a path stay inert. The one expansion is the stamp, kept
    outside the quoted literal (adjacent words concatenate): the message is the
    page latch's key and must not change between ticks.
    """
    aside = f"{stub.parent}/.aside"
    return (
        f"mkdir -p {shlex.quote(aside)} && mv {shlex.quote(str(stub))} "
        f"{shlex.quote(f'{aside}/{stub.name}-stub-')}$(date -u +%Y%m%dT%H%M%SZ)"
    )


def dead_stub_hold(repo_root: Path, stub: Path, why: str = "") -> WorkspaceUnavailable:
    """The hold for a non-empty *stub* that is not a live worktree of *repo_root*.

    Never raises: anything unreadable is reported in the message instead.
    """
    try:
        pointer, admin = _gitfile_target(stub)
    except OSError as exc:
        pointer, admin = f"its .git is unreadable ({exc})", None
    try:
        registered = _registered_admin_dirs(repo_root, stub)
    except (OSError, subprocess.SubprocessError) as exc:
        registered, pointer = [], f"{pointer}; registrations unreadable ({exc})"
    admins = list(dict.fromkeys([*registered, *([admin] if admin and admin.is_dir() else [])]))
    reg = (", registered to it: " + ", ".join(map(str, registered))) if registered else ""
    step = (
        "; then, if " + " or ".join(shlex.quote(str(a)) for a in admins)
        + " still points at it, check that dir's index, HEAD and logs/ before "
        "removing ONLY that dir"
    ) if admins else ""
    return WorkspaceUnavailable(
        f"{DEAD_STUB_HELD}: {stub} is not a live worktree of {repo_root} "
        f"({why + '; ' if why else ''}{pointer}{reg}). Nothing was moved and no "
        f"retry was charged. Recover this stub only: inspect it; "
        f"{_recovery_command(stub)}{step}. "
        f"No repo-wide prune."
    )


def target_occupied(repo_root: Path, target: Path) -> bool:
    """True when something non-empty sits at *target*; an unreadable target holds."""
    try:
        return target.is_symlink() or (
            target.exists() and (not target.is_dir() or any(target.iterdir()))
        )
    except OSError as exc:
        raise dead_stub_hold(repo_root, target, f"unreadable: {exc}") from exc
