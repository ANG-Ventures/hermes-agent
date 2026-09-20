"""Durable Git survivors shared by completion and workspace reclamation.

A remote HEAD only protects a CLEAN tree. Otherwise save a binary Git patch
against the dispatch baseline, using a temporary index (never stage user work).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

from hermes_cli import kanban_db as kb

_log = logging.getLogger(__name__)


class SurvivorUnavailable(ValueError):
    """Completion/reclamation must retain the workspace for recovery."""


def _git(repo, *args, env=None, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, timeout=30, env=env,
    )
    if check and result.returncode:
        # Git stderr can contain credential-bearing remote URLs. Do not persist it.
        raise SurvivorUnavailable("survivor_unavailable: git inspection failed")
    return result


def _repos(workspace):
    """Find repos created inside scratch, including linked worktrees; no symlinks."""
    found = []
    def fail(exc):
        raise exc
    for root, dirs, files in os.walk(workspace, followlinks=False, onerror=fail):
        if ".git" in dirs or ".git" in files:
            found.append(Path(root))
        dirs[:] = sorted(d for d in dirs if d != ".git" and not (Path(root) / d).is_symlink())
    if workspace not in found:
        tracked = _git(workspace, "ls-files", "--", ".", check=False)
        if tracked.returncode == 0 and tracked.stdout:
            found.insert(0, workspace)
    return found


def _state(conn, task_id):
    row = conn.execute(
        "SELECT bases, held_reason, survivor FROM task_workspace_survivors WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return (json.loads(row[0]), row[1], json.loads(row[2]) if row[2] else None) if row else ({}, None, None)


def record_baseline(conn, task_id, workspace):
    """Record once, before dispatch; retries must not reset the task's base."""
    if conn.execute("SELECT 1 FROM task_workspace_survivors WHERE task_id = ?", (task_id,)).fetchone():
        return
    bases, held, survivor = _state(conn, task_id)
    workspace = Path(workspace)
    for repo in _repos(workspace):
        key = str(repo.relative_to(workspace))
        if key not in bases:
            head = _git(repo, "rev-parse", "--verify", "HEAD", check=False)
            bases[key] = head.stdout.decode().strip() if head.returncode == 0 else None
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO NOTHING",
            (task_id, json.dumps(bases)),
        )


def _attachment_dir(conn, task_id):
    # The connection, not the process-global current board, owns the survivor.
    db = next(Path(r[2]) for r in conn.execute("PRAGMA database_list") if r[1] == "main" and r[2])
    if db.parent == kb.kanban_home():
        return db.parent / "kanban" / "attachments" / task_id
    return db.parent / "attachments" / task_id


def _write_patch(path, data):
    """Atomic, fsynced publication. Keep prior versions rather than overwrite work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == data:
            return path
        digest = hashlib.sha256(data).hexdigest()
        path = path.with_name(f"implementation-{digest}.patch")
        if path.exists() and path.read_bytes() == data:
            return path
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        try:
            os.link(temp, path)
        except FileExistsError:
            # Another completion won publication; never overwrite its survivor.
            return _write_patch(path, data)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        if path.read_bytes() != data:
            raise OSError("patch verification failed")
        return path
    finally:
        temp.unlink(missing_ok=True)


def _remote_survivor(repo, head):
    # Never trust stale refs/remotes: query the actual remote. A local object
    # for its advertised tip permits ancestry proof; otherwise fall back to patch.
    for remote in _git(repo, "remote").stdout.decode().splitlines():
        try:
            advertised = _git(repo, "ls-remote", "--heads", remote, check=False)
        except subprocess.TimeoutExpired:
            continue
        if advertised.returncode:
            continue
        for line in advertised.stdout.decode().splitlines():
            sha, ref = line.split("\t", 1)
            if sha == head or _git(repo, "merge-base", "--is-ancestor", head, sha, check=False).returncode == 0:
                return {"remote": remote, "branch": ref.removeprefix("refs/heads/"), "sha": sha, "head": head}
    return None


def _base(repo, bases, key):
    if key in bases and bases[key]:
        return bases[key]
    # Repos created after dispatch have no recorded baseline. Preserve their
    # unpublished history from a reachable remote ancestor, or their whole tree.
    for remote in _git(repo, "remote").stdout.decode().splitlines():
        try:
            advertised = _git(repo, "ls-remote", "--heads", remote, check=False)
        except subprocess.TimeoutExpired:
            continue
        if advertised.returncode:
            continue
        for line in advertised.stdout.decode().splitlines():
            sha = line.split("\t", 1)[0]
            base = _git(repo, "merge-base", "HEAD", sha, check=False)
            if base.returncode == 0:
                return base.stdout.decode().strip()
    return _git(repo, "hash-object", "-t", "tree", os.devnull).stdout.decode().strip()


def _snapshot(repo, base, prefix):
    with tempfile.TemporaryDirectory(prefix="kanban-index-") as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        index = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.decode().strip())
        if index.exists():
            shutil.copyfile(index, env["GIT_INDEX_FILE"])
        else:
            _git(repo, "read-tree", "--empty", env=env)
        _git(repo, "add", "-A", "--", ".", env=env)
        return _git(
            repo, "diff", "--cached", "--relative", "--binary", "--full-index", "--no-ext-diff",
            "--no-textconv", "--no-renames", f"--src-prefix=a/{prefix}",
            f"--dst-prefix=b/{prefix}", base, "--", ".", env=env,
        ).stdout


def _hold(conn, task_id, reason):
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_workspace_survivors(task_id, held_reason) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET held_reason = excluded.held_reason",
            (task_id, reason),
        )
        kb._append_event(conn, task_id, "workspace_held", {"reason": reason})
    _log.warning("Workspace HELD for task %s: %s", task_id, reason)


def preserve(conn, task_id, metadata=None, *, cleanup=False):
    """Return a verified survivor or None for non-code work; fail closed on doubt."""
    bases, held, previous = _state(conn, task_id)
    if cleanup and held:
        raise SurvivorUnavailable(held)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            return None
        claimed = bool((metadata or {}).get("changed_files"))
        # Review approval often has no new changed_files: inherit the implementer's claim.
        claimed = claimed or any(
            bool(json.loads(r[0]).get("changed_files"))
            for r in conn.execute("SELECT metadata FROM task_runs WHERE task_id = ? AND metadata IS NOT NULL", (task_id,))
        )
        workspace = Path(task.workspace_path) if task.workspace_path else None
        if workspace is None or not workspace.is_dir():
            if bases or claimed:
                raise SurvivorUnavailable("survivor_unavailable: workspace missing")
            return None
        repos = _repos(workspace)
        if any(a != b and a.is_relative_to(b) for a in repos for b in repos):
            # A patch cannot add a gitlink and files below the same path.
            raise SurvivorUnavailable("survivor_unavailable: nested repository requires separate recovery")
        keys = {str(r.relative_to(workspace)) for r in repos}
        if set(bases) - keys:
            raise SurvivorUnavailable("survivor_unavailable: recorded repository missing")
        patches, refs = [], []
        for repo in repos:
            key = str(repo.relative_to(workspace))
            base = _base(repo, bases, key)
            prefix = "" if key == "." else key + "/"
            patch = _snapshot(repo, base, prefix)
            if patch:
                header = f"# kanban repository={json.dumps(key)} base={base}\n".encode()
                patches.append(header + patch)
            head = _git(repo, "rev-parse", "--verify", "HEAD", check=False)
            dirty = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
            ref = None
            if not dirty and head.returncode == 0:
                ref = _remote_survivor(repo, head.stdout.decode().strip())
            if ref:
                refs.append(dict(ref, repository=key))
        if repos and len(refs) == len(repos):
            survivor = {"kind": "ref", "refs": refs}
        elif patches:
            data = b"".join(patches)
            if len(data) > kb.KANBAN_ATTACHMENT_MAX_BYTES:
                raise SurvivorUnavailable("survivor_unavailable: implementation patch exceeds attachment limit")
            path = _write_patch(_attachment_dir(conn, task_id) / "implementation.patch", data)
            if not any(a.stored_path == str(path) for a in kb.list_attachments(conn, task_id)):
                kb.add_attachment(conn, task_id, filename=path.name, stored_path=str(path),
                                  content_type="text/x-patch", size=len(data), uploaded_by="harness")
            survivor = {"kind": "patch", "path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data), "notice": "NOT PUSHED"}
        elif claimed:
            raise SurvivorUnavailable("survivor_unavailable: empty patch despite claimed code changes")
        else:
            survivor = None
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_workspace_survivors(task_id, survivor) VALUES (?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET survivor = excluded.survivor, held_reason = NULL",
                (task_id, json.dumps(survivor)),
            )
            if survivor and survivor != previous:
                kb._append_event(conn, task_id, "workspace_survivor", survivor)
        return survivor
    except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, SurvivorUnavailable) else "survivor_unavailable: capture failed"
        _hold(conn, task_id, reason)
        raise SurvivorUnavailable(reason) from exc


def allow_cleanup(conn, task_id):
    try:
        preserve(conn, task_id, cleanup=True)
        return True
    except SurvivorUnavailable:
        return False
