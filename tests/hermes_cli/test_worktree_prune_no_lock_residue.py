"""The startup/cron worktree pruner must not leave git lock residue (t_fbeea95b).

Measured on the Studio 2026-11-11: every pruner pass left ~50 ``.git/worktrees/<name>/index.lock``
files (246 of 261 archived that day) plus the live repo's own ``.git/index.lock``. Cause:
``_worktree_is_dirty`` ran a plain ``git status`` with a 5 s timeout on big aged trees; ``status``
takes ``index.lock`` to write back its refreshed index, the timeout SIGKILLs it mid-refresh, and
the lock stays. A plain directory under ``.worktrees/`` (no ``.git``) made that same ``status`` run
against the ENCLOSING repo, so the kill left the live tree's index.lock.

The git shim below stands in for a slow index refresh: a ``status`` that is allowed to take
optional locks creates ``index.lock`` and hangs; one run with ``--no-optional-locks`` does not.
"""

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import worktree_ops

REAL_GIT = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()

SHIM = """#!/bin/sh
# slow-refresh stand-in: an optional-lock status takes index.lock and hangs (killed by the caller's timeout)
case " $* " in
  *" status "*)
    case " $* " in *" --no-optional-locks "*) ;; *)
      if [ "${GIT_OPTIONAL_LOCKS:-1}" != 0 ]; then
        d=$("%(git)s" rev-parse --absolute-git-dir) && : > "$d/index.lock" && exec sleep 30
      fi ;;
    esac ;;
esac
exec "%(git)s" "$@"
"""


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _run(["git", "init", "-q", "--initial-branch=main"], r)
    _run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"], r)
    return r


@pytest.fixture
def slow_git(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(SHIM % {"git": REAL_GIT})
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("GIT_OPTIONAL_LOCKS", raising=False)
    return shim


def test_dirty_probe_timeout_leaves_no_worktree_index_lock(repo, slow_git):
    wt = repo / ".worktrees" / "aged"
    _run([REAL_GIT, "worktree", "add", "-q", "--detach", str(wt)], repo)
    admin = Path(_run([REAL_GIT, "rev-parse", "--absolute-git-dir"], wt).stdout.strip())

    worktree_ops._worktree_is_dirty(str(wt), timeout=1)
    assert not (admin / "index.lock").exists(), "killed status left the worktree's index.lock"


def test_plain_dir_under_worktrees_is_not_a_prune_candidate(repo):
    wts = repo / ".worktrees"
    plain = wts / ".aside"
    plain.mkdir(parents=True)
    old = time.time() - 30 * 86400
    os.utime(plain, (old, old))

    names = [e.name for e, _m, _f in worktree_ops._prune_candidates(wts, 24, time.time())]
    assert ".aside" not in names


def test_plain_dir_probe_leaves_no_live_index_lock(repo, slow_git):
    plain = repo / ".worktrees" / ".aside"
    plain.mkdir(parents=True)
    worktree_ops._worktree_is_dirty(str(plain), timeout=1)
    assert not (repo / ".git" / "index.lock").exists(), "status of a plain dir locked the enclosing repo"
