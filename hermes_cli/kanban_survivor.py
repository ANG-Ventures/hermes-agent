"""Durable Git survivors shared by completion and workspace reclamation.

A remote HEAD only protects a CLEAN tree. Otherwise save a binary Git patch
against a published ancestor, or a self-contained bundle when none survives.
"""
from __future__ import annotations

import getpass
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
from hermes_cli import kanban_external_survivor as _ext

_log = logging.getLogger(__name__)


class SurvivorUnavailable(ValueError):
    """Completion/reclamation must retain the workspace for recovery."""


log = logging.getLogger(__name__)

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


def _verified_explicit(task_id, survivor_ref, survivor_pr, *, unbound=False):
    """An operator-named survivor is a claim: verify it is real AND is THIS card's work.

    "Live on GitHub" is not evidence about this card. Any unrelated OPEN or
    MERGED PR satisfies existence, and ``preserve`` treats a verified explicit
    survivor as authority for the branch that protects UNPUSHED implementation
    work -- so an unbound claim authorises deleting a workspace whose bytes may
    exist nowhere else. The claim therefore carries the same task-id binding
    the text-mined path carries: the PR branch (or the remote ref) must name
    the task.

    The legitimate operator case -- a human who knows the work landed on a
    differently-named branch -- keeps a path: ``--survivor-unbound`` accepts
    the claim without the binding and records the override on the survivor, so
    the authorisation is auditable rather than invisible. It is a CLI flag, so
    a worker calling the ``kanban_complete`` tool cannot self-certify an
    unrelated survivor.
    """
    for claim, flag, verify, extra in (
        (survivor_ref, "--survivor-ref", _ext.verify_ref, {}),
        (survivor_pr, "--survivor-pr", _ext.verify_pr,
         {"corroborate": ("headRefName", "title", "body")}),
    ):
        if not claim:
            continue
        ref = verify(claim, mined_for=None if unbound else task_id, **extra)
        if ref is None:
            # The claim is unverified and may carry a token: echo it redacted only.
            if not unbound and verify(claim, **extra) is not None:
                raise SurvivorUnavailable(
                    f"survivor_unavailable: {flag} {_ext.redact(claim)} is live but does not "
                    f"name {task_id}, so it is not evidence of THIS card's work; re-run with "
                    f"--survivor-unbound if the work really did land on an unrelated-looking branch"
                )
            raise SurvivorUnavailable(
                f"survivor_unavailable: could not verify {flag} {_ext.redact(claim)} against the remote"
            )
        if unbound:
            # Record WHO authorised an unbound claim: _record replays the
            # survivor into the task's event log, so the override is auditable.
            ref = dict(ref, unbound=True, claimed_by=_claimant())
            _log.warning("Unbound survivor accepted for task %s by %s: %s",
                         task_id, ref["claimed_by"], _ext.redact(claim))
        return ref
    return None


def _claimant():
    try:
        return getpass.getuser()
    except (KeyError, OSError):  # no passwd entry (containers, CI)
        return "unknown"


def _loose_files(workspace, repos):
    """True when the workspace holds entries outside every repository.

    A remote ref vouches only for the repositories it was resolved from; files
    beside them (notes, scripts, a tarball) are captured by nothing, so an
    inferred survivor must never trade them for a delete.
    """
    for root, dirs, files in os.walk(workspace, followlinks=False):
        here = Path(root)
        if here in repos:
            dirs[:] = []
            continue
        if files or any((here / d).is_symlink() for d in dirs):
            return True
        dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink())
    return False


def _remote_urls(repos):
    urls = []
    for repo in repos:
        for remote in _git(repo, "remote", check=False).stdout.decode().splitlines():
            url = _git(repo, "remote", "get-url", remote, check=False)
            if url.returncode == 0:
                urls.append(url.stdout.decode().strip())
    return urls


def _external(conn, task_id, metadata, evidence, urls, explicit, *, discover, cleanup, previous):
    """Infer a survivor only for work that claims one.

    An operator-named flag is authority and always applies. Text mining is a
    guess: it stays behind the claim test that decides whether the absence of
    a survivor is an error, and it never runs during reclamation -- cleanup
    reuses the survivor recorded at completion or holds. Re-mining a hint
    there would turn a fail-closed HOLD into a delete.
    """
    if explicit:
        ref = explicit
    elif cleanup:
        return previous
    else:
        ref = _ext.discover(conn, task_id, metadata, evidence, urls) if discover else None
    return {"kind": "ref", "refs": [dict(ref, repository=".")]} if ref else None


def _record(conn, task_id, survivor, previous):
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_workspace_survivors(task_id, survivor) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET survivor = excluded.survivor, held_reason = NULL",
            (task_id, json.dumps(survivor)),
        )
        if survivor and survivor != previous:
            kb._append_event(conn, task_id, "workspace_survivor", survivor)
    return survivor


def _hold(conn, task_id, reason):
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_workspace_survivors(task_id, held_reason) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET held_reason = excluded.held_reason",
            (task_id, reason),
        )
        kb._append_event(conn, task_id, "workspace_held", {"reason": reason})
    _log.warning("Workspace HELD for task %s: %s", task_id, reason)


def preserve(conn, task_id, metadata=None, *, cleanup=False, workspace=None,
             survivor_ref=None, survivor_pr=None, survivor_unbound=False, evidence=()):
    """Return a verified survivor or None for non-code work; fail closed on doubt."""
    bases, held, previous = _state(conn, task_id)
    if cleanup and held:
        raise SurvivorUnavailable(held)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise SurvivorUnavailable("survivor_unavailable: task missing")
        explicit = _verified_explicit(task_id, survivor_ref, survivor_pr,
                                      unbound=survivor_unbound)
        claimed = bool((metadata or {}).get("changed_files"))
        # Review approval often has no new changed_files: inherit the implementer's claim.
        claimed = claimed or any(
            bool(json.loads(r[0]).get("changed_files"))
            for r in conn.execute("SELECT metadata FROM task_runs WHERE task_id = ? AND metadata IS NOT NULL", (task_id,))
        )
        workspace = Path(workspace or task.workspace_path) if workspace or task.workspace_path else None
        if workspace is None or not workspace.is_dir():
            external = _external(conn, task_id, metadata, evidence, (), explicit,
                                 discover=bool(bases or claimed), cleanup=cleanup, previous=previous)
            if external:
                return _record(conn, task_id, external, previous)
            if cleanup or bases or claimed:
                raise SurvivorUnavailable(
                    "survivor_unavailable: workspace missing and no verifiable external "
                    f"survivor; {_ext.HINT}"
                )
            return None
        workspace = workspace.resolve(strict=True)
        repos = _repos(workspace)
        if any(a != b and a.is_relative_to(b) for a in repos for b in repos):
            # A patch cannot add a gitlink and files below the same path.
            raise SurvivorUnavailable("survivor_unavailable: nested repository requires separate recovery")
        keys = {str(r.relative_to(workspace)) for r in repos}
        if set(bases) - keys:
            raise SurvivorUnavailable("survivor_unavailable: recorded repository missing")
        patches, refs, bundles, repositories = [], [], [], []
        for repo in repos:
            key = str(repo.relative_to(workspace))
            published = list(_published_refs(repo, workspace))
            head = _git(repo, "rev-parse", "--verify", "HEAD", check=False)
            dirty = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
            ref = None
            if not dirty and head.returncode == 0:
                ref = _remote_survivor(repo, head.stdout.decode().strip(), published)
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
            survivor = {"kind": "ref", "refs": refs}
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
            # Nothing in-tree to capture: the survivor must live elsewhere. An
            # inferred one vouches only for the repositories it came from, so
            # files beside them make the inference worthless -- HOLD instead.
            loose = _loose_files(workspace, repos)
            external = _external(conn, task_id, metadata, evidence, _remote_urls(repos), explicit,
                                 discover=not loose, cleanup=cleanup, previous=None if loose else previous)
            if external:
                return _record(conn, task_id, dict(external, refs=external["refs"] + refs), previous)
            if loose:
                raise SurvivorUnavailable(
                    "survivor_unavailable: workspace holds files outside any repository that "
                    f"no inferred survivor vouches for; {_ext.HINT}"
                )
            raise SurvivorUnavailable(
                "survivor_unavailable: empty patch despite claimed code changes and no "
                f"verifiable external survivor; {_ext.HINT}"
            )
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
