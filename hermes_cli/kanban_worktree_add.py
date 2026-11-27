"""A timed-out ``git worktree add`` is killed whole: no git child outlives the spawn (t_f609a5a0).

2026-11-11: card t_33172156's spawn ran ``git worktree add --no-track -b wt/<id> <repo>/.worktrees/<id> origin/main``
and the 60 s ``subprocess.run`` timeout killed only git's parent. Its checkout child lived on: in 150 of the 189
``initializing``-locked trees on the Studio it finished and wrote the index, and nothing dropped the lock; in the
rest it died too and left a half-built tree (no index).

:func:`run_git_group` runs the add in its own process group and SIGKILLs the whole group on timeout or any
exceptional exit. It deletes, unlocks and writes nothing (ruling:t_f609a5a0-no-automatic-deletion); what a killed
add leaves is left as git left it.
"""
from __future__ import annotations

import os
import signal
import subprocess
from typing import Sequence

# Studio home tree 2026-11-11 (30,578 files, 1,150 registrations, load1 36): two timed adds took 32.0 s and 106.1 s,
# and 83 of 6,540 spawns in 7 days failed at the old 60 s. 300 s is ~3x the slowest measured add; a run past it is
# killed whole, so the longer wait costs a slower failure, never a surviving git child.
ADD_TIMEOUT_S = 300


class WorktreeAddTimeout(RuntimeError):
    """``git worktree add`` ran past its timeout; its whole process group was killed."""


_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the process group ``proc`` leads, then reap ``proc``. POSIX; elsewhere ``proc.kill()``.

    SIGKILL, never SIGTERM first (Prism #265 P1 c-f6873eb7b7d1e8fb): on SIGTERM ``git worktree add`` runs its own
    cleanup and deletes the incomplete tree, files someone wrote into it included. A killed add's tree is left as
    git left it (ruling:t_f609a5a0-no-automatic-deletion)."""
    if not hasattr(os, "killpg"):
        proc.kill()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        os.killpg(proc.pid, _SIGKILL)  # windows-footgun: ok — guarded by hasattr(os, "killpg") above
    except OSError:
        pass  # ESRCH: the group is already gone
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def run_git_group(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    """``argv`` in its own session; on timeout every process of that group is killed, then
    :class:`WorktreeAddTimeout` is raised. Never raises on a non-zero exit."""
    proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=1.0)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        raise WorktreeAddTimeout(f"`{' '.join(argv[3:6])} ...` timed out after {timeout:g} seconds "
                                 f"(process group {proc.pid} killed)") from None
    except BaseException:
        # KeyboardInterrupt / SystemExit / anything else mid-wait (Prism #265 P2 c-038d5f46d9069eb3): no git child
        # outlives the caller either.
        _kill_group(proc)
        raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


# --- the hand-over gate (ruling:t_f609a5a0-cut-plus-proven-usable-gate) -----------------------------------------
#
# With the group kill a timed-out add always leaves a HALF-BUILT tree (files, a gitfile, a registration, the branch,
# a lock `initializing`, no index), and main's two reuse returns hand that tree to the next worker. The default is
# inverted: a linked tree is handed over only when it is positively proven finished, from the tree itself, with no
# git process and no registry scan. Anything not proven holds (WorkspaceUnavailable: uncharged, card stays ready,
# paged once per episode by the dispatcher's refusal path). Nothing here deletes, unlocks or writes.
#
# Proof (all four, in order):
#   1. <tree>/.git is a REGULAR file by lstat (a symlink is not proven) and a readable gitfile naming an admin dir;
#   2. os.path.samefile(<admin>/gitdir's path, <tree>/.git) AND os.path.samefile(dirname(<admin>/gitdir's path),
#      <tree>): identity of the gitfile and of the directory, by device and inode, never a string compare (the r8
#      bypass was a path spelled differently from git's registration; a hard- or symlinked gitfile in another
#      directory passes the first compare and fails the second);
#   3. <admin>/index exists and <admin>/index.lock does not;
#   4. <admin>/locked is absent, OR its reason is not `initializing`, OR its mtime is older than 2 x ADD_TIMEOUT_S.
# Rule 3 is the proof that the checkout finished. `git worktree add` writes the admin dir's index through
# index.lock: for the whole checkout the admin dir has index.lock and no index, and the index appears (by rename)
# only once every file is written (measured: Studio git 2.53.0, a smudge filter sleeping per file, the admin dir
# sampled every 0.5 s; test_a_running_add_is_held_on_rule_3_at_every_sample repeats it in CI on Linux, and the
# pool hosts ace-ai, ace-media and ci-box run git 2.53.0). What can remain after rule 3 holds is the unlock of
# `initializing`; rule 4's age is margin for that instant between the index rename and the unlock, not the proof.
# A tree with an index, no index.lock and an old `initializing` lock is one whose add was killed in that instant
# (the 150 finished-but-locked legacy trees on the Studio). Limit: the gate reasons about adds this dispatcher runs;
# an add started by another tool with a post-checkout hook that runs longer than 2 x ADD_TIMEOUT_S is not covered.
# Limit: git runs post-checkout after it writes the index and drops the lock, so a repo with a slow post-checkout
# hook can have that hook cut off by the timeout kill, and the gate cannot see it (nothing in the tree records a
# running hook; Studio 2026-11-12: 0 of 111 repositories have their own executable post-checkout hook).
# A leftover index.lock holds its tree with no age-out (git itself refuses to work there): that hold needs an
# operator.
# Any read error, including "does not exist" on a file a rule needs, is not proof. A main checkout (.git is a
# directory by lstat) is not a linked tree: main's behaviour is kept.

UNPROVEN_HELD = "unproven_worktree_held"
INIT_LOCK = b"initializing"


def _err(exc: OSError) -> str:
    """A stable errno name for the hold text (the page latch keys on the message)."""
    import errno as _errno
    return _errno.errorcode.get(exc.errno or 0, type(exc).__name__)


def _pointed(base: str, raw: bytes) -> str:
    """The path in a gitfile / gitdir entry; a relative entry is relative to ``base`` (``--relative-paths``)."""
    p = os.fsdecode(raw.rstrip(b"\r\n"))       # line terminators only: a path may end in a space
    return p if os.path.isabs(p) else os.path.join(base, p)


def unproven(tree: "os.PathLike[str] | str") -> "str | None":
    """``None`` when ``tree`` may be handed over (proven finished, or a main checkout); else the failed rule."""
    import stat as _stat
    import time as _time

    tree = os.fspath(tree)
    dot = os.path.join(tree, ".git")
    try:
        mode = os.lstat(dot).st_mode
        if _stat.S_ISDIR(mode):
            return None                                             # a main checkout: main's guards apply
        if not _stat.S_ISREG(mode):
            return f"rule 1: {dot} is not a regular file"
        with open(dot, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return f"rule 1: {dot} unreadable ({_err(exc)})"
    raw = raw.lstrip(b"\xef\xbb\xbf").rstrip(b"\r\n")
    if not raw.startswith(b"gitdir: ") or not raw[len(b"gitdir: "):]:      # git's own read_gitfile format
        return f"rule 1: {dot} is not a gitfile"
    admin = _pointed(tree, raw[len(b"gitdir: "):])

    backlink = os.path.join(admin, "gitdir")
    try:
        with open(backlink, "rb") as fh:
            back = _pointed(admin, fh.read())
        same = os.path.samefile(back, dot) and os.path.samefile(os.path.dirname(back), tree)
    except OSError as exc:
        return f"rule 2: backlink {backlink} unresolvable ({_err(exc)})"
    if not same:
        return f"rule 2: backlink {backlink} names another tree"

    try:
        os.stat(os.path.join(admin, "index"))
    except OSError as exc:
        return f"rule 3: {admin}/index missing or unreadable ({_err(exc)})"
    try:
        os.lstat(os.path.join(admin, "index.lock"))
        return f"rule 3: {admin}/index.lock exists"
    except FileNotFoundError:
        pass
    except OSError as exc:
        return f"rule 3: {admin}/index.lock unreadable ({_err(exc)})"

    locked = os.path.join(admin, "locked")
    try:
        with open(locked, "rb") as fh:
            reason = fh.read().strip()
            mtime = os.fstat(fh.fileno()).st_mtime
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"rule 4: {locked} unreadable ({_err(exc)})"
    if reason != INIT_LOCK or _time.time() - mtime > 2 * ADD_TIMEOUT_S:
        return None
    return f"rule 4: locked initializing less than {2 * ADD_TIMEOUT_S:g} s ago, an add may still be writing"


def hold_unless_proven(tree: "os.PathLike[str] | str") -> None:
    """Raise :class:`WorkspaceUnavailable` unless :func:`unproven` proves ``tree`` may be handed over."""
    why = unproven(tree)
    if why is None:
        return
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable

    raise WorkspaceUnavailable(
        f"{UNPROVEN_HELD}: {os.fspath(tree)} is not proven a finished worktree ({why}). Nothing was moved, unlocked "
        f"or deleted and no retry was charged; the operator inspects this tree."
    )
