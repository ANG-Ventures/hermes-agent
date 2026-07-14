"""Shared-tree (skills-shared/) curator safety contract.

The git-backed shared skills tree is mutated by the curator ONLY under the
hard gate implemented here (see the curator-scope-shared-skills spec,
Invariant 3):

- an fcntl ``.curator.lock`` held for the whole shared pass. The lock is
  ADVISORY and serializes CURATOR-vs-CURATOR only — an arbitrary sibling
  writer (another agent's ``skill_manage``, a human, the sync habit) never
  acquires it. The anti-clobber primitive against a non-cooperating
  concurrent writer is the EXPLICIT PATHSPEC commit + pre-commit porcelain
  drift-abort in :func:`commit_shared`.
- a clean ``git status --porcelain -- skills-shared/`` precheck; a dirty
  tree skips the shared pass (with crash recovery for self-inflicted dirt).
- a pre-mutation snapshot: baseline git rev + a separate
  ``shared-<ts>.tar.gz`` of the in-scope dirs + a manifest recording the
  INTENDED-WRITE file set (written BEFORE mutation — crash recovery keys
  on it).
- every mutation lands as one ``curator:`` commit staged from an explicit
  pathspec — NEVER ``git add skills-shared/`` (a wildcard stage would
  absorb a concurrent sibling file into the commit and entangle its
  revert).

fcntl semantics honesty: the kernel auto-releases an fcntl lock when its
holder dies, so a dead owner never leaves a HELD lock. "Stale" therefore
means: the lock ACQUIRES cleanly yet the shared tree is dirty. The PID
recorded in the lockfile is diagnostic only.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tarfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None

logger = logging.getLogger(__name__)

LOCK_NAME = ".curator.lock"
SHARED_MANIFEST_NAME = "shared-manifest.json"


def _shared_root() -> Path:
    from agent.skill_utils import get_shared_skills_root

    return get_shared_skills_root()


def _git_toplevel(shared_root: Path) -> Optional[Path]:
    """The git working tree that tracks skills-shared/, or None."""
    try:
        out = subprocess.run(
            ["git", "-C", str(shared_root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    top = out.stdout.strip()
    return Path(top) if top else None


def _git(repo: Path, *args: str, timeout: int = 30) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def _porcelain_shared(repo: Path, shared_root: Path) -> Optional[List[str]]:
    """Dirty paths under skills-shared/ (repo-relative), or None on error."""
    try:
        rel = shared_root.resolve().relative_to(repo.resolve())
    except ValueError:
        return None
    code, out, _err = _git(
        repo, "status", "--porcelain", "--untracked-files=all", "--", str(rel)
    )
    if code != 0:
        return None
    lines = [ln for ln in out.splitlines() if ln.strip()]
    paths = []
    for ln in lines:
        # porcelain v1: XY <path> (or XY <old> -> <new> for renames)
        payload = ln[3:]
        if " -> " in payload:
            payload = payload.split(" -> ", 1)[1]
        payload = payload.strip().strip('"')
        # The curator's own lockfile lives inside the shared tree; it is
        # infrastructure, not content — never treated as dirt (and never
        # committed: commit_shared only stages explicit content paths).
        if payload.rsplit("/", 1)[-1] == LOCK_NAME:
            continue
        paths.append(payload)
    return paths


@contextmanager
def shared_pass_lock(shared_root: Optional[Path] = None):
    """Non-blocking fcntl lock over the shared tree for the whole pass.

    Yields True when acquired, False on contention (another curator holds
    it — the shared pass must be skipped, never blocked behind it). Records
    owner PID + start time in the lockfile (diagnostic only).
    """
    root = shared_root or _shared_root()
    lock_path = root / LOCK_NAME
    if fcntl is None:
        yield True
        return
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(lock_path, "a+", encoding="utf-8")
    except OSError as e:
        logger.debug("shared lockfile open failed: %s", e)
        yield False
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            fd.seek(0)
            fd.truncate()
            fd.write(json.dumps({
                "pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }))
            fd.flush()
        except OSError:
            pass
        yield True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()


def git_precheck_shared(
    shared_root: Optional[Path] = None,
) -> Tuple[bool, str, List[str]]:
    """Clean-tree gate. Returns (ok, reason, dirty_paths)."""
    root = shared_root or _shared_root()
    if not root.is_dir():
        return False, "no skills-shared/ tree", []
    repo = _git_toplevel(root)
    if repo is None:
        return False, "skills-shared/ is not inside a git repo", []
    dirty = _porcelain_shared(repo, root)
    if dirty is None:
        return False, "git status failed", []
    if dirty:
        return False, "dirty working tree", dirty
    return True, "clean", []


def snapshot_shared(
    dirs: Iterable[Path],
    intended_writes: Iterable[str],
    *,
    shared_root: Optional[Path] = None,
    dest_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Pre-mutation shared snapshot. Returns the snapshot dir or None.

    Writes ``shared-<ts>.tar.gz`` of the given in-scope dirs (a SEPARATE
    tarball — never appended into the agent snapshot's gzip stream) plus a
    ``shared-manifest.json`` recording the baseline git rev, the tar path,
    and the INTENDED-WRITE file set (repo-relative). The manifest is written
    BEFORE any mutation; crash recovery keys its exact-set match on it.

    Failure returns None — the caller MUST hard-gate: no shared mutation
    without a successful snapshot (unlike the agent tree's log-and-continue).
    """
    root = shared_root or _shared_root()
    repo = _git_toplevel(root)
    if repo is None:
        return None
    code, head, _ = _git(repo, "rev-parse", "HEAD")
    if code != 0:
        return None
    if dest_dir is None:
        from agent.curator_backup import _backups_dir, _utc_id

        dest_dir = _backups_dir() / f"shared-{_utc_id()}"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        tar_path = dest_dir / f"shared-{ts}.tar.gz"
        with tarfile.open(tar_path, "w:gz", compresslevel=6) as tf:
            for d in dirs:
                d = Path(d)
                if d.exists():
                    tf.add(str(d), arcname=d.name, recursive=True)
        manifest = {
            "baseline_rev": head.strip(),
            "tar": tar_path.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "intended_writes": sorted(set(str(p) for p in intended_writes)),
            "pid": os.getpid(),
        }
        (dest_dir / SHARED_MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return dest_dir
    except (OSError, tarfile.TarError) as e:
        logger.debug("shared snapshot failed: %s", e, exc_info=True)
        return None


def latest_shared_snapshot() -> Optional[Path]:
    """Most recent shared snapshot dir, or None."""
    from agent.curator_backup import _backups_dir

    base = _backups_dir()
    if not base.is_dir():
        return None
    candidates = sorted(
        (p for p in base.iterdir()
         if p.is_dir() and p.name.startswith("shared-")),
        key=lambda p: p.name,
    )
    return candidates[-1] if candidates else None


def commit_shared(
    summary: str,
    written_files: List[Path],
    precheck_dirty: Optional[List[str]] = None,
    *,
    shared_root: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Commit exactly *written_files* with curator provenance.

    Safety order:
      1. re-run the porcelain check; if any dirty path is NOT one of the
         files this run wrote (tracked set drifted since the precheck —
         e.g. a non-cooperating sibling wrote during the pass), ABORT with
         a drift report and stage nothing.
      2. ``git add -- <exact file list>`` (explicit pathspec; NEVER a
         directory wildcard).
      3. ``git commit -m "curator: <summary>"``.

    Returns (ok, message). ``message`` carries the commit sha or the reason.
    """
    root = shared_root or _shared_root()
    repo = _git_toplevel(root)
    if repo is None:
        return False, "skills-shared/ is not inside a git repo"
    if not written_files:
        return True, "nothing to commit"

    repo_resolved = repo.resolve()
    rel_files: List[str] = []
    for f in written_files:
        try:
            rel_files.append(str(Path(f).resolve().relative_to(repo_resolved)))
        except ValueError:
            return False, f"refusing to commit a path outside the repo: {f}"

    dirty = _porcelain_shared(repo, root)
    if dirty is None:
        return False, "pre-commit git status failed"
    expected = set(rel_files) | set(precheck_dirty or [])
    drifted = [p for p in dirty if p not in expected]
    if drifted:
        return False, (
            "aborted: tracked set drifted since precheck "
            f"(unexpected paths: {', '.join(sorted(drifted)[:10])})"
        )

    code, _, err = _git(repo, "add", "--", *rel_files)
    if code != 0:
        return False, f"git add failed: {err.strip()}"
    code, out, err = _git(
        repo, "commit", "-m", f"curator: {summary}", "--", *rel_files,
    )
    if code != 0:
        return False, f"git commit failed: {err.strip() or out.strip()}"
    code, sha, _ = _git(repo, "rev-parse", "--short", "HEAD")
    return True, sha.strip() if code == 0 else "committed"


def attempt_crash_recovery(
    shared_root: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Recover a dirty shared tree left by a KILLED prior curator run.

    Fires ONLY under the exact-set rule (spec 5.3.3a): every dirty path must
    be in the last shared snapshot's manifested intended-write set and
    byte-restorable from that snapshot's tar, and NO un-manifested path may
    be dirty. Any superset / unknown path / missing snapshot →
    skip-and-report (NEVER auto-clobber a sibling edit).

    The caller must hold the shared pass lock (a cleanly-acquired lock over
    a dirty tree IS the staleness signal — fcntl auto-releases on holder
    death, so a live holder would have made acquisition fail).

    Returns (recovered, reason).
    """
    root = shared_root or _shared_root()
    repo = _git_toplevel(root)
    if repo is None:
        return False, "no git repo"
    dirty = _porcelain_shared(repo, root)
    if not dirty:
        return False, "tree is clean"

    snap = latest_shared_snapshot()
    if snap is None:
        return False, "no shared snapshot to recover from"
    manifest_path = snap / SHARED_MANIFEST_NAME
    if not manifest_path.exists():
        return False, "snapshot has no manifest"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "unreadable snapshot manifest"
    intended = set(manifest.get("intended_writes") or [])
    unmanifested = [p for p in dirty if p not in intended]
    if unmanifested:
        return False, (
            "sibling edit present — skip "
            f"(un-manifested dirty paths: {', '.join(sorted(unmanifested)[:10])})"
        )

    # Every dirty path is a manifested in-flight file → self-inflicted dirt.
    # Restore via git (the files were tracked and clean at the precheck).
    code, _, err = _git(repo, "checkout", "--", *sorted(dirty))
    if code != 0:
        # untracked leftovers (new carve files) — remove them, they were
        # curator-written per the manifest.
        removed = []
        for p in dirty:
            fp = repo / p
            try:
                if fp.exists() and not fp.is_dir():
                    fp.unlink()
                    removed.append(p)
            except OSError:
                pass
        still = _porcelain_shared(repo, root)
        if still:
            return False, f"recovery incomplete: {err.strip()}"
    else:
        # git checkout restores modified tracked files but leaves untracked
        # curator-written leftovers; clear those too (manifested only).
        leftover = _porcelain_shared(repo, root) or []
        for p in leftover:
            if p in intended:
                fp = repo / p
                try:
                    if fp.exists() and not fp.is_dir():
                        fp.unlink()
                except OSError:
                    pass
        still = _porcelain_shared(repo, root)
        if still:
            return False, "recovery incomplete: tree still dirty"
    return True, "restored self-inflicted dirt from git/manifest"


def archive_shared_skill(
    skill_dir: Path,
    *,
    shared_root: Optional[Path] = None,
) -> Tuple[bool, str, List[Path]]:
    """Archive a shared skill in-tree: ``skills-shared/<group>/.archive/<name>/``.

    A plain rename INSIDE the shared tree so the run-commit's explicit
    pathspec captures both the deletion and the .archive addition — a
    ``git revert`` of the run-commit restores the skill for the whole fleet.
    Never moves the skill to the local ``skills/.archive/`` (other agents
    would lose it). Returns (ok, message, touched_paths).
    """
    root = shared_root or _shared_root()
    skill_dir = Path(skill_dir)
    try:
        rel = skill_dir.resolve().relative_to(root.resolve())
    except ValueError:
        return False, f"{skill_dir} is not under the shared tree", []
    if not rel.parts or len(rel.parts) < 2:
        return False, f"{skill_dir} is not a <group>/<skill> dir", []
    group = rel.parts[0]
    archive_root = root / group / ".archive"
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"failed to create shared archive dir: {e}", []
    dest = archive_root / skill_dir.name
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        dest = archive_root / f"{skill_dir.name}-{stamp}"
    try:
        skill_dir.rename(dest)
    except OSError:
        import shutil

        try:
            shutil.move(str(skill_dir), str(dest))
        except Exception as e:
            return False, f"failed to archive shared skill: {e}", []
    return True, f"archived to {dest}", [skill_dir, dest]
