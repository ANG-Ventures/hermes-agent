"""Durable Git survivors shared by completion and workspace reclamation.

A remote HEAD only protects a CLEAN tree. Otherwise save a binary Git patch
against a published ancestor, or a self-contained bundle when none survives.
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
from urllib.parse import unquote, urlsplit

from hermes_cli import kanban_db as kb

_log = logging.getLogger(__name__)


class SurvivorUnavailable(ValueError):
    """Completion/reclamation must retain the workspace for recovery."""


log = logging.getLogger(__name__)

def _git(repo, *args, env=None, check=True, timeout=30):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, timeout=timeout, env=env,
    )
    if check and result.returncode:
        # Git stderr can contain credential-bearing remote URLs. Do not persist it.
        raise SurvivorUnavailable("survivor_unavailable: git inspection failed")
    return result


def _repos(workspace):
    """Find repos created inside scratch, including linked worktrees; no symlinks."""
    found = []
    def fail(exc):
        # An unreadable directory (EACCES/EPERM) cannot hold a repo the worker
        # could have written to, so skipping it loses no survivor. Raising here
        # took down every dispatch tick for a `dir` workspace rooted at a real
        # home (2026-09-20: `var/skills-portal/caddy`, root-owned 0700, made the
        # whole default board unspawnable). Anything else is still fatal.
        if isinstance(exc, PermissionError):
            log.warning("kanban survivor: skipping unreadable dir %s (%s)", exc.filename, exc.strerror)
            return
        raise exc
    for root, dirs, files in os.walk(workspace, followlinks=False, onerror=fail):
        if ".git" in dirs or ".git" in files:
            found.append(Path(root))
        dirs[:] = sorted(d for d in dirs if d != ".git" and not (Path(root) / d).is_symlink())
    if workspace not in found and any((p / ".git").exists() for p in (workspace, *workspace.parents)):
        tracked = _git(workspace, "ls-files", "--", ".")
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
        path = path.with_name(f"{path.stem}-{digest}{path.suffix}")
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


def _temporary_roots():
    return [Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp")]


def _durable_remote(repo, remote, workspace):
    # Expand insteadOf aliases, then resolve symlinks before checking scope.
    url = _git(repo, "remote", "get-url", remote).stdout.decode().strip()
    parsed = urlsplit(url)
    if parsed.scheme in {"https", "http", "ssh", "git"}:
        return True
    if not parsed.scheme and ":" in url and not url.startswith(("/", ".")):
        return True  # scp-style SSH
    if parsed.scheme not in {"", "file"} or parsed.netloc not in {"", "localhost"}:
        return False
    path = Path(unquote(parsed.path) if parsed.scheme else url).expanduser()
    path = (repo / path).resolve()
    roots = [workspace, kb.workspaces_root(), kb.kanban_home() / "kanban" / "workspaces",
             kb.kanban_home() / "kanban" / "boards", *_temporary_roots()]
    return remote == "origin" and not any(path.is_relative_to(root.resolve()) for root in roots)


def _published_refs(repo, workspace):
    for remote in _git(repo, "remote").stdout.decode().splitlines():
        if not _durable_remote(repo, remote, workspace):
            continue
        try:
            advertised = _git(repo, "ls-remote", "--heads", remote, check=False)
        except subprocess.TimeoutExpired:
            continue
        if advertised.returncode:
            continue
        for line in advertised.stdout.decode().splitlines():
            sha, ref = line.split("\t", 1)
            yield {"remote": remote, "branch": ref.removeprefix("refs/heads/"), "sha": sha}


def _remote_survivor(repo, head, published):
    for ref in published:
        if ref["sha"] == head or _git(repo, "merge-base", "--is-ancestor", head, ref["sha"], check=False).returncode == 0:
            return dict(ref, head=head)
    return None


# A rewriting mirror (hermes-home's "isolated remote sync") republishes every
# commit under a NEW sha, so `_remote_survivor` can never match a clone of it and
# the capture falls through to a bundle of a 94 MB home tree, which always
# exceeds KANBAN_ATTACHMENT_MAX_BYTES (2026-09-20, t_e69d693a). `git patch-id`
# is the content identity that survives the rewrite: same diff, same id, any sha.
_CONTENT_SCAN_DEPTH = 25
_CONTENT_SCAN_BUDGET = 100


def _patch_id(repo, sha, env=None):
    """Content identity of one commit's diff. None when it cannot be computed."""
    diff = _git(repo, "diff-tree", "-p", "--full-index", "--no-ext-diff",
                "--no-textconv", "--no-renames", "--root", sha, env=env, check=False)
    if diff.returncode or not diff.stdout:
        return None
    result = subprocess.run(
        ["git", "-C", str(repo), "patch-id", "--stable"],
        input=diff.stdout, capture_output=True, timeout=30,
    )
    if result.returncode or not result.stdout.strip():
        return None
    return result.stdout.split()[0].decode()


def _content_survivor(repo, head, published, *, budget=_CONTENT_SCAN_BUDGET):
    """Find a published commit whose diff is byte-identical to ``head``'s.

    Fetches each durable remote head shallowly into a throwaway bare repo that
    borrows ``repo``'s objects, then walks it looking for a matching patch-id.
    The deepest fetched commit is a shallow boundary — Git would diff it against
    the empty tree and invent a bogus patch-id — so it is never scanned.
    """
    target = _patch_id(repo, head)
    if target is None:
        return None
    objects = _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects").stdout.decode().strip()
    env = dict(os.environ, GIT_ALTERNATE_OBJECT_DIRECTORIES=objects)
    with tempfile.TemporaryDirectory(prefix="kanban-content-") as tmp:
        probe = Path(tmp) / "probe.git"
        _git(repo, "init", "--bare", str(probe))
        for ref in published:
            if budget <= 0:
                return None
            url = _git(repo, "remote", "get-url", ref["remote"]).stdout.decode().strip()
            try:
                fetched = _git(
                    probe, "fetch", "--no-tags", "--depth", str(_CONTENT_SCAN_DEPTH + 1),
                    url, f"+refs/heads/{ref['branch']}:refs/heads/candidate",
                    env=env, check=False, timeout=120,
                )
            except subprocess.TimeoutExpired:
                continue
            if fetched.returncode:
                continue
            walk = _git(probe, "rev-list", "--max-count", str(_CONTENT_SCAN_DEPTH),
                        "refs/heads/candidate", env=env, check=False)
            if walk.returncode:
                continue
            for sha in walk.stdout.decode().split():
                if budget <= 0:
                    return None
                budget -= 1
                if _patch_id(probe, sha, env=env) == target:
                    return dict(ref, sha=sha, head=head, matched_by="patch-id",
                                patch_id=target)
            _git(probe, "update-ref", "-d", "refs/heads/candidate", env=env, check=False)
    return None


def _verify_landed(entries, workspace):
    """Verify an explicit `landed` claim: reachable from HEAD AND published.

    The escape hatch for work that was committed into a repo the workspace only
    mirrors. Every failure mode raises — an unverifiable claim must never be
    accepted as a survivor, because accepting it authorises deleting the only
    remaining copy of the code.
    """
    verified = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SurvivorUnavailable("survivor_unavailable: landed entry is not an object")
        repo_path, sha = entry.get("repo_path"), entry.get("sha")
        if not repo_path or not sha:
            raise SurvivorUnavailable("survivor_unavailable: landed entry needs repo_path and sha")
        repo = Path(repo_path).expanduser()
        if not repo.is_dir():
            raise SurvivorUnavailable("survivor_unavailable: landed repository missing")
        repo = repo.resolve(strict=True)
        resolved = _git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}", check=False)
        if resolved.returncode:
            raise SurvivorUnavailable("survivor_unavailable: landed commit not in repository")
        sha = resolved.stdout.decode().strip()
        if _git(repo, "merge-base", "--is-ancestor", sha, "HEAD", check=False).returncode:
            raise SurvivorUnavailable("survivor_unavailable: landed commit not reachable from HEAD")
        published = list(_published_refs(repo, workspace))
        ref = _remote_survivor(repo, sha, published) or _content_survivor(repo, sha, published)
        if not ref:
            raise SurvivorUnavailable("survivor_unavailable: landed commit is not published on a durable remote")
        verified.append({"repository": str(repo), "sha": sha, "remote": ref["remote"],
                         "branch": ref["branch"], "published_sha": ref["sha"],
                         "matched_by": ref.get("matched_by", "sha")})
    return verified


def _base(repo, published):
    candidates = []
    for ref in published:
        base = _git(repo, "merge-base", "HEAD", ref["sha"], check=False)
        if base.returncode == 0:
            sha = base.stdout.decode().strip()
            distance = int(_git(repo, "rev-list", "--count", f"{sha}..HEAD").stdout)
            candidates.append((distance, sha))
    return min(candidates)[1] if candidates else None


def _snapshot(repo, base, prefix):
    with tempfile.TemporaryDirectory(prefix="kanban-index-") as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        index = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.decode().strip())
        if index.exists():
            # Preserve the index timestamp: Git uses it to detect racy-clean entries.
            shutil.copy2(index, env["GIT_INDEX_FILE"])
        else:
            _git(repo, "read-tree", "--empty", env=env)
        _git(repo, "add", "-A", "--", ".", env=env)
        if base is None:
            tree = _git(repo, "write-tree", env=env).stdout.decode().strip()
            head = _git(repo, "rev-parse", "--verify", "HEAD", check=False)
            parents = ["-p", head.stdout.decode().strip()] if head.returncode == 0 else []
            identity = dict(env, GIT_AUTHOR_NAME="Kanban recovery", GIT_COMMITTER_NAME="Kanban recovery",
                            GIT_AUTHOR_EMAIL="kanban@localhost", GIT_COMMITTER_EMAIL="kanban@localhost",
                            GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")
            commit = _git(repo, "commit-tree", tree, *parents, "-m", "Kanban workspace recovery", env=identity).stdout.decode().strip()
            recovery = Path(tmp) / "recovery.git"
            _git(repo, "init", "--bare", str(recovery))
            objects = _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects").stdout.decode().strip()
            bundle_env = dict(os.environ, GIT_ALTERNATE_OBJECT_DIRECTORIES=objects)
            _git(recovery, "update-ref", "refs/heads/implementation", commit, env=bundle_env)
            _git(recovery, "symbolic-ref", "HEAD", "refs/heads/implementation", env=bundle_env)
            bundle = Path(tmp) / "implementation.bundle"
            _git(recovery, "bundle", "create", str(bundle), "--all", env=bundle_env)
            return bundle.read_bytes()
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


def _record(conn, task_id, survivor, previous):
    """The single publication point for a captured survivor."""
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_workspace_survivors(task_id, survivor) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET survivor = excluded.survivor, held_reason = NULL",
            (task_id, json.dumps(survivor)),
        )
        if survivor and survivor != previous:
            kb._append_event(conn, task_id, "workspace_survivor", survivor)
    return survivor


def preserve(conn, task_id, metadata=None, *, cleanup=False, workspace=None):
    """Return a verified survivor or None for non-code work; fail closed on doubt."""
    bases, held, previous = _state(conn, task_id)
    if cleanup and held:
        raise SurvivorUnavailable(held)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise SurvivorUnavailable("survivor_unavailable: task missing")
        claimed = bool((metadata or {}).get("changed_files"))
        # Review approval often has no new changed_files: inherit the implementer's claim.
        claimed = claimed or any(
            bool(json.loads(r[0]).get("changed_files"))
            for r in conn.execute("SELECT metadata FROM task_runs WHERE task_id = ? AND metadata IS NOT NULL", (task_id,))
        )
        workspace = Path(workspace or task.workspace_path) if workspace or task.workspace_path else None
        if workspace is None or not workspace.is_dir():
            if cleanup or bases or claimed:
                raise SurvivorUnavailable("survivor_unavailable: workspace missing")
            return None
        workspace = workspace.resolve(strict=True)
        repos = _repos(workspace)
        if any(a != b and a.is_relative_to(b) for a in repos for b in repos):
            # A patch cannot add a gitlink and files below the same path.
            raise SurvivorUnavailable("survivor_unavailable: nested repository requires separate recovery")
        keys = {str(r.relative_to(workspace)) for r in repos}
        if set(bases) - keys:
            raise SurvivorUnavailable("survivor_unavailable: recorded repository missing")
        landed = (metadata or {}).get("landed")
        if landed:
            if not isinstance(landed, list):
                raise SurvivorUnavailable("survivor_unavailable: landed must be a list")
            # Explicit opt-in: the worker asserts the code already lives in a
            # named repo, and we verify that claim against that repo's durable
            # remote. Verified publication makes the workspace disposable, so we
            # never pay for a snapshot of a 94 MB home clone.
            survivor = {"kind": "landed", "landed": _verify_landed(landed, workspace)}
            survivor["sidecar"] = _store(
                conn, task_id, "implementation.json",
                json.dumps(survivor, sort_keys=True).encode(), "application/json",
            )["path"]
            return _record(conn, task_id, survivor, previous)
        patches, refs, bundles, repositories = [], [], [], []
        for repo in repos:
            key = str(repo.relative_to(workspace))
            published = list(_published_refs(repo, workspace))
            head = _git(repo, "rev-parse", "--verify", "HEAD", check=False)
            dirty = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
            ref = None
            if not dirty and head.returncode == 0:
                sha = head.stdout.decode().strip()
                # A rewriting mirror republishes the same content under a new
                # sha, so sha equality can never hold; fall back to content.
                ref = _remote_survivor(repo, sha, published) or _content_survivor(repo, sha, published)
            if ref:
                refs.append(dict(ref, repository=key))
                continue
            base = _base(repo, published)
            prefix = "" if key == "." else key + "/"
            data = _snapshot(repo, base, prefix)
            if base is None:
                name = f"implementation-{len(bundles)}.bundle"
                bundle = _store(conn, task_id, name, data, "application/x-git-bundle")
                bundles.append(dict(bundle, repository=key))
            elif data:
                header = f"# kanban repository={json.dumps(key)} base={base}\n".encode()
                patches.append(header + data)
            repositories.append({"repository": key, "base_sha": base})
        if repos and len(refs) == len(repos):
            by_content = any(ref.get("matched_by") == "patch-id" for ref in refs)
            survivor = {"kind": "ref-by-content" if by_content else "ref", "refs": refs}
            if by_content:
                # The local sha exists nowhere durable; the manifest is the only
                # record of which published commit carries the same content.
                survivor["sidecar"] = _store(
                    conn, task_id, "implementation.json",
                    json.dumps(survivor, sort_keys=True).encode(), "application/json",
                )["path"]
        elif patches or bundles:
            data = b"".join(patches)
            survivor = {"kind": "bundle" if bundles else "patch", "notice": "NOT PUSHED",
                        "bundles": bundles, "refs": refs}
            if patches:
                survivor.update(_store(conn, task_id, "implementation.patch", data, "text/x-patch"))
            manifest = dict(survivor, repositories=repositories)
            sidecar = _store(conn, task_id, "implementation.json",
                             json.dumps(manifest, sort_keys=True).encode(), "application/json")
            survivor["sidecar"] = sidecar["path"]
        elif claimed:
            raise SurvivorUnavailable("survivor_unavailable: empty patch despite claimed code changes")
        else:
            survivor = None
        return _record(conn, task_id, survivor, previous)
    except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, SurvivorUnavailable) else "survivor_unavailable: capture failed"
        _hold(conn, task_id, reason)
        raise SurvivorUnavailable(reason) from exc


def remove_workspace_dir(conn, task_id, path, *, worktree_root=None, board=False):
    """The only Kanban directory deleter: inspect the exact target, then remove.

    Board hard-delete may remove only empty workspace/attachment storage. Archive
    a board with retained work instead: deleting its recovery artifacts would
    defeat survivorship. Linked worktrees retain Git's no-force dirty check.
    """
    try:
        if not path:
            raise SurvivorUnavailable("survivor_unavailable: deletion target missing")
        workspace = Path(path).expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise SurvivorUnavailable("survivor_unavailable: deletion target is not a directory")
        if board:
            if any(p.exists() and any(p.iterdir()) for p in
                   (workspace / "workspaces", workspace / "attachments")) or _repos(workspace):
                raise SurvivorUnavailable("survivor_unavailable: board has retained work; archive instead")
        elif conn is None:
            # Legacy direct worktree callers have no task connection. Never
            # capture to an invented task: allow only independently durable refs.
            repos = _repos(workspace)
            if not repos or any(
                _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
                or not _remote_survivor(repo, _git(repo, "rev-parse", "HEAD").stdout.decode().strip(),
                                        list(_published_refs(repo, workspace)))
                for repo in repos
            ):
                raise SurvivorUnavailable("survivor_unavailable: no task connection or durable ref")
        else:
            if _attachment_dir(conn, task_id).resolve().is_relative_to(workspace):
                raise SurvivorUnavailable("survivor_unavailable: recovery storage inside deletion target")
            preserve(conn, task_id, cleanup=True, workspace=workspace)
        if worktree_root is not None:
            # Preserve upstream's handle-release retry, still without --force.
            result = _git(worktree_root, "worktree", "remove", str(workspace), check=False)
            if result.returncode:
                import time
                time.sleep(0.1)
                result = _git(worktree_root, "worktree", "remove", str(workspace), check=False)
            if result.returncode:
                raise SurvivorUnavailable("survivor_unavailable: git refused workspace removal")
        else:
            shutil.rmtree(workspace)
        return True
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, SurvivorUnavailable) else "survivor_unavailable: removal failed"
        if conn is not None and task_id is not None:
            _hold(conn, task_id, reason)
        else:
            _log.warning("Workspace HELD: %s", reason)
        if board:
            raise SurvivorUnavailable(reason) from exc
        return False


def _store(conn, task_id, name, data, content_type):
    if len(data) > kb.KANBAN_ATTACHMENT_MAX_BYTES:
        raise SurvivorUnavailable("survivor_unavailable: implementation artifact exceeds attachment limit")
    path = _write_patch(_attachment_dir(conn, task_id) / name, data)
    if not any(a.stored_path == str(path) for a in kb.list_attachments(conn, task_id)):
        kb.add_attachment(conn, task_id, filename=path.name, stored_path=str(path),
                          content_type=content_type, size=len(data), uploaded_by="harness")
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
