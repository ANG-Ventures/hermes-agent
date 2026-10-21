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

import contextlib
import json
import logging
import os
import subprocess
import sys
import tarfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hermes_cli._subprocess_compat import noninteractive_git_env

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
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            env={**noninteractive_git_env(), "GIT_OPTIONAL_LOCKS": "0"},
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
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            env={**noninteractive_git_env(), "GIT_OPTIONAL_LOCKS": "0"},
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
        repo, "--no-optional-locks", "status", "--porcelain", "--untracked-files=all", "--", str(rel)
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
    
    P2 FIX: Fail CLOSED when fcntl unavailable (Windows, some BSD). Shared-tree
    curation requires advisory locking to serialize curator-vs-curator; platforms
    without fcntl simply skip the shared pass.
    """
    root = shared_root or _shared_root()
    lock_path = root / LOCK_NAME
    if fcntl is None:
        logger.warning(
            "fcntl unavailable (platform does not support advisory locking); "
            "shared-tree curation skipped — cannot serialize curator-vs-curator"
        )
        yield False
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
    scope_paths: Optional[Iterable[Path]] = None,
) -> Tuple[bool, str, List[str]]:
    """Clean-tree gate. Returns (ok, reason, dirty_paths).

    When *scope_paths* is given, only dirt **under those paths** blocks the
    pass: a live agent home churns constantly (cron state, memories, sibling
    skills), so requiring the whole skills-shared/ tree to be clean makes the
    gate fail almost always for dirt the curator will never touch. With a
    scope, in-scope dirt still hard-fails (reason "dirty working tree",
    dirty=the in-scope paths), while out-of-scope dirt passes with reason
    "clean (out-of-scope dirt ignored)" and dirty=the ignored paths — callers
    MUST thread that list into ``commit_shared(precheck_dirty=...)`` so the
    pre-commit drift-abort doesn't fire on paths that were already dirty at
    precheck time. If any scope path cannot be resolved inside the repo, the
    gate falls back to the conservative whole-tree behavior.
    """
    root = shared_root or _shared_root()
    if not root.is_dir():
        return False, "no skills-shared/ tree", []
    repo = _git_toplevel(root)
    if repo is None:
        return False, "skills-shared/ is not inside a git repo", []
    dirty = _porcelain_shared(repo, root)
    if dirty is None:
        return False, "git status failed", []
    if dirty and scope_paths is not None:
        repo_resolved = repo.resolve()
        scoped_rel: Optional[List[str]] = []
        for sp in scope_paths:
            try:
                scoped_rel.append(
                    str(Path(sp).resolve(strict=False).relative_to(repo_resolved))
                )
            except ValueError:
                # A scope path outside the repo -> fall back to the
                # conservative whole-tree gate rather than guessing.
                scoped_rel = None
                break
        if scoped_rel:
            def _in_scope(p: str) -> bool:
                return any(p == s or p.startswith(s + "/") for s in scoped_rel)

            in_scope = [p for p in dirty if _in_scope(p)]
            if in_scope:
                return False, "dirty working tree", in_scope
            return True, "clean (out-of-scope dirt ignored)", dirty
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

    P1 FIX: Manifest paths are validated for traversal and cross-checked against
    the baseline commit to prevent arbitrary file deletion via planted manifests.

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
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False, "unreadable snapshot manifest"
    
    # P1 FIX: Validate manifest paths before trusting them for deletion
    intended_raw = manifest.get("intended_writes") or []
    intended = set()
    repo_resolved = repo.resolve()
    root_resolved = root.resolve()
    
    for path_str in intended_raw:
        # Reject absolute paths
        p = Path(path_str)
        if p.is_absolute():
            return False, f"manifest contains absolute path (rejected): {path_str}"
        
        # Require the path to resolve inside the shared tree
        try:
            full_path = (repo / path_str).resolve(strict=False)
            full_path.relative_to(root_resolved)
        except ValueError:
            return False, f"manifest path escapes shared tree (rejected): {path_str}"
        
        intended.add(path_str)
    
    # P1 FIX: Cross-check intended deletions against baseline commit
    # Only delete files that existed in the baseline (curator couldn't have created
    # a file that never existed in git)
    baseline_rev = manifest.get("baseline_rev")
    if baseline_rev:
        for path_str in intended:
            # Check if this path existed in the baseline commit
            code, _, _ = _git(repo, "cat-file", "-e", f"{baseline_rev}:{path_str}")
            if code != 0:
                # Path didn't exist in baseline — curator couldn't have written it
                # during this run, so it's either a new sibling file or a manifest lie
                logger.debug(
                    "recovery: path in manifest but not in baseline %s: %s",
                    baseline_rev, path_str
                )
                # Don't fail recovery for untracked new files (carves), but DO
                # fail if a tracked file in dirty set wasn't in baseline
                if path_str in dirty:
                    code2, _, _ = _git(repo, "ls-files", "--", path_str)
                    if code2 == 0:  # It's tracked now but wasn't in baseline
                        return False, (
                            f"manifest claims path not in baseline (rejected): {path_str}"
                        )
    
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
            if p not in intended:
                continue  # Defense in depth: only delete validated manifest paths
            fp = repo / p
            try:
                if fp.exists() and not fp.is_dir():
                    # Re-validate containment before unlink
                    fp_resolved = fp.resolve()
                    # noqa: root-equality  target is a FILE (guarded by the
                    # `not fp.is_dir()` above) and unlink() cannot remove a
                    # directory, so the path == root case is unreachable.
                    fp_resolved.relative_to(root_resolved)
                    fp.unlink()
                    removed.append(p)
            except (OSError, ValueError):
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
                        # Re-validate containment before unlink
                        fp_resolved = fp.resolve()
                        # noqa: root-equality  target is a FILE (guarded by the
                        # `not fp.is_dir()` above) and unlink() cannot remove a
                        # directory, so the path == root case is unreachable.
                        fp_resolved.relative_to(root_resolved)
                        fp.unlink()
                except (OSError, ValueError):
                    pass
        still = _porcelain_shared(repo, root)
        if still:
            return False, "recovery incomplete: tree still dirty"
    return True, "restored self-inflicted dirt from git/manifest"


# Ace's standing rulings (2026-10-18; 2026-10-19 13:45 PT, msg 1561842711190053015): a shared skill is NEVER
# archived or deleted. One with 0 loads in the idle window may be UNSUBSCRIBED by moving it to
# skills-shared/<group>-aux/ (still on disk, loadable via skill-finder GROUP mode); one used within it stays.
UNSUBSCRIBE_OPERATOR = "Ace ruling 2026-10-19 13:45 PT msg 1561842711190053015 (curator idle unsubscribe)"
SKILL_GROUP_SCRIPT = ("hermes-harness", "skill-finder", "scripts", "skill-group.py")
SKILL_TIER_LIB = ("scripts", "lib", "skill_tier.py")


def _load_skill_tier(fleet_root: Path):
    """The fleet's ONE tier module (``<root>/scripts/lib/skill_tier.py``): idle threshold + last-used reader."""
    import importlib.util

    path = fleet_root.joinpath(*SKILL_TIER_LIB)
    spec = importlib.util.spec_from_file_location("_fleet_skill_tier", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"no skill_tier module at {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso_ts(value) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def shared_skill_idle_days(name: str, fleet_root: Path, record: Optional[Dict[str, Any]] = None,
                           now: Optional[float] = None) -> Tuple[Optional[float], float, str]:
    """(idle days or None when unknown, threshold days, why) for a shared skill.

    Last use = max over the tier module's sources (fleet ``skills/.usage.json`` + ``var/skills-portal/
    stats-model.json``) and this profile's usage *record* (last use/view/patch, else created_at). The
    threshold is ``skill_tier.DEFAULT_IDLE_DAYS`` (one source). No timestamp anywhere AND no fleet
    stats file = unknown (the caller refuses)."""
    import time as _time

    tier = _load_skill_tier(fleet_root)
    threshold = float(tier.DEFAULT_IDLE_DAYS)
    stats = fleet_root / "var" / "skills-portal" / "stats-model.json"
    last, created = tier.load_last_used(fleet_root / "skills" / ".usage.json", stats)
    stamps = [t for t in (last.get(name), created.get(name)) if t]
    rec = record or {}
    for key in ("last_used_at", "last_viewed_at", "last_patched_at", "last_activity_at", "created_at"):
        ts = _iso_ts(rec.get(key))
        if ts:
            stamps.append(ts)
    now = _time.time() if now is None else now
    if stamps:
        return (now - max(stamps)) / 86400.0, threshold, "last activity"
    if stats.is_file():
        return float("inf"), threshold, "no load recorded fleet-wide"
    return None, threshold, "no usage evidence (no record, no fleet stats-model.json)"


def unsubscribe_shared_skill(
    skill_dir: Path,
    *,
    shared_root: Optional[Path] = None,
    record: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Tuple[bool, str, List[Path]]:
    """The curator's ONLY idle action on a shared skill: move ``<group>/<name>`` to ``<group>-aux/<name>``.

    Never ``.archive`` (t_776ae7bc). Refused when the skill was used within ``skill_tier.DEFAULT_IDLE_DAYS``,
    when its idle time is unknown, or when it already sits in an ``-aux`` group. The move itself is
    ``skill-group.py move --in-place`` (the fleet's one mover), run under the full safety contract
    (lock, scoped precheck, snapshot, explicit-pathspec ``curator:`` commit). Returns (ok, message, touched).
    """
    root = shared_root or _shared_root()
    skill_dir = Path(skill_dir)
    try:
        rel = skill_dir.resolve().relative_to(root.resolve())
    except ValueError:
        return False, f"{skill_dir} is not under the shared tree", []
    if len(rel.parts) != 2:
        return False, f"{skill_dir} is not a <group>/<skill> dir", []
    group, dname = rel.parts
    if group.endswith("-aux"):
        return False, f"{dname} is already unsubscribed ({group}); shared skills are never archived", []
    fleet_root = root.resolve().parent
    mover = root.joinpath(*SKILL_GROUP_SCRIPT)
    try:
        idle, threshold, why = shared_skill_idle_days(dname, fleet_root, record=record, now=now)
    except Exception as e:  # no tier module = no threshold: refuse, never fall back to a second number
        return False, f"refused: cannot read the idle threshold ({e}); shared skills are never archived", []
    if idle is None or idle < threshold:
        shown = "unknown" if idle is None else f"{idle:.0f}d"
        return False, (f"refused: {dname} idle {shown} ({why}) < {threshold:g}d: stays subscribed in {group}; "
                       "shared skills are never archived"), []
    if not mover.is_file():
        return False, f"refused: mover {mover} missing; shared skills are never archived", []
    dest_dir = root / f"{group}-aux"
    dest = dest_dir / dname
    readme = dest_dir / "README.md"
    new_group = not dest_dir.exists()

    with shared_pass_lock(root) as acquired:
        if not acquired:
            return False, "lock contention (another curator holds shared lock)", []
        ok, reason, dirty = git_precheck_shared(root, scope_paths=[skill_dir, dest_dir])
        if not ok and reason == "dirty working tree":
            recovered, _why = attempt_crash_recovery(root)
            if recovered:
                ok, reason, dirty = git_precheck_shared(root, scope_paths=[skill_dir, dest_dir])
        if not ok:
            return False, f"precheck failed ({reason})", []
        repo = _git_toplevel(root)
        if repo is None:
            return False, "no git repo", []
        repo_resolved = repo.resolve()
        intended_pre: List[str] = []
        for item in skill_dir.rglob("*"):
            if item.is_file():
                sub = item.relative_to(skill_dir)
                for base in (skill_dir, dest):
                    with contextlib.suppress(ValueError):
                        intended_pre.append(str((base / sub).resolve(strict=False).relative_to(repo_resolved)))
        if new_group:
            intended_pre.append(str(readme.resolve(strict=False).relative_to(repo_resolved)))
        snap = snapshot_shared([skill_dir.parent], intended_pre, shared_root=root)
        if snap is None:
            return False, "shared snapshot failed (hard gate)", []
        p = subprocess.run(
            [sys.executable, str(mover), "--home", str(fleet_root), "move", dname, dest_dir.name,
             "--in-place", "--create", "--json", "--operator", UNSUBSCRIBE_OPERATOR],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            env={**os.environ, "HERMES_FLEET_HOME": str(fleet_root)},
        )
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
        except ValueError:
            out = {}
        if p.returncode != 0 or not out.get("ok") or not dest.is_dir():
            return False, f"skill-group move rc={p.returncode}: {(out.get('error') or p.stderr).strip()[:300]}", []
        touched = [skill_dir, dest] + ([readme] if out.get("created_group") else [])
        ok_commit, msg_commit = commit_shared(
            f"unsubscribe idle shared skill {dname} {group} -> {dest_dir.name}",
            touched, precheck_dirty=(dirty or []) + intended_pre, shared_root=root,
        )
        if not ok_commit:
            with contextlib.suppress(OSError):
                dest.rename(skill_dir)
                if out.get("created_group"):
                    readme.unlink()
                    dest_dir.rmdir()
            return False, f"commit failed: {msg_commit}", []
        return True, f"unsubscribed to {dest} (idle {idle:.0f}d >= {threshold:g}d)", touched
