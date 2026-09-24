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
import re
import shutil
import sqlite3
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_external_survivor as _ext
from hermes_cli.cli_hint import hint_arg

_log = logging.getLogger(__name__)


class SurvivorUnavailable(ValueError):
    """Completion/reclamation must retain the workspace for recovery.

    ``override_hint`` is deliberately NOT part of ``str(self)``. The refusal
    text is persisted as ``held_reason`` and replayed to a worker's model by
    ``kanban_show``, so anything baked into the message reaches readers the
    raising process never inspected. The hint is rendered at the CLI boundary
    instead, where the environment being tested is the one actually reading.
    """

    override_hint = ""


log = logging.getLogger(__name__)

def _git(repo, *args, env=None, check=True, input=None, timeout=30):
    # Local replace refs can substitute another tree for the only raw commit.
    # Every Git authority read must inspect raw objects before deleting work.
    git_env = dict(os.environ if env is None else env, GIT_NO_REPLACE_OBJECTS="1")
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL if input is None else None,
        input=input,
        capture_output=True, timeout=timeout, env=git_env,
    )
    if check and result.returncode:
        # Git stderr can contain credential-bearing remote URLs, so it is never
        # persisted. The subcommand and returncode are safe to include in the
        # hold; the stderr goes to the log through the existing redaction path.
        #
        # Without this, every occurrence costs an attribution pass. A failure
        # that is purely environmental (t_169d6e46: a concurrent pytest session
        # deleting this repo's tmp_path, so git exits 128 "cannot change to
        # '<path>': No such file or directory") is indistinguishable from a real
        # capture defect when the caller receives only the old bare
        # "survivor_unavailable: git inspection failed".
        log.warning(
            "kanban survivor: git %s failed rc=%s in %s: %s",
            args[0] if args else "?", result.returncode, _ext.redact(str(repo)),
            _ext.redact(result.stderr.decode("utf-8", "replace").strip()),
        )
        raise SurvivorUnavailable(
            f"survivor_unavailable: git {args[0] if args else '?'} failed (rc={result.returncode})"
        )
    return result


def _alternates(repo):
    """Object stores this repo BORROWS from, newest-first as Git reads them.

    A clone made from a local path with `--shared` or `--reference` does not
    copy the lender's objects: it records the lender here. (A plain
    `git clone <local-path>` does NOT -- measured on git 2.53.0, it hardlinks
    and writes no alternates file at all.) When the lender is later pruned or
    reaped the borrower silently becomes unreadable, which is the upstream
    cause of a broken object store inside a scratch workspace -- so the
    lender's path is the single most useful thing to name.
    """
    location = _git(repo, "rev-parse", "--path-format=absolute", "--git-path",
                    "objects/info/alternates", check=False)
    if location.returncode:
        return []
    path = location.stdout.decode("utf-8", "replace").strip()
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if path else []
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _lender(repo, workspace):
    """The first borrowed object store and whether it lives inside `workspace`.

    A lender INSIDE the same workspace is the reapable shape: the sibling clone
    it borrows from is itself disposable, so this repo is doomed the moment that
    sibling is pruned or reclaimed. That is the upstream CAUSE of the broken
    store, not the symptom.
    """
    lenders = _alternates(repo)
    if not lenders:
        return None, False
    try:
        inside = Path(lenders[0]).resolve().is_relative_to(Path(workspace).resolve())
    except (OSError, ValueError):
        inside = False
    return lenders[0], inside


def _warn_borrowed_from_workspace(repo, key, workspace):
    """Record at baseline that a repo's objects are on loan from a reapable sibling.

    Deliberately a warning, not a refusal: `record_baseline` runs before
    dispatch and a refusal here would make the card unspawnable over a clone
    layout that is usually fine. What it buys is attribution -- when the
    borrower later fails its completion, the dispatch-time log already names
    the lender that was always going to be reaped.
    """
    lender, inside = _lender(repo, workspace)
    if lender and inside:
        log.warning(
            "kanban survivor: %s borrows objects from %s inside the same workspace; "
            "reaping the lender will make this repo unreadable",
            key, _ext.redact(lender),
        )


def _advertised_head(repo, workspace, head):
    """The durable remote ref that already carries `head`, or None.

    Deliberately an EXACT tip match: ancestry (`merge-base --is-ancestor`) needs
    the local objects that are, by construction, the ones we cannot read here.
    `ls-remote` needs none of them.
    """
    for ref in _published_refs(repo, workspace):
        if ref["sha"] == head:
            return ref
    return None


def _object_store_broken(repo):
    """True when Git cannot walk the objects reachable from HEAD.

    Cheap on purpose (one commit's worth of tree walk, not a full `fsck`):
    this only runs after a capture has ALREADY failed, so it is a classifier,
    not a gate on the happy path.
    """
    if _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode:
        return False  # no commit yet: nothing borrowed can be missing
    return bool(
        _git(repo, "cat-file", "-e", "HEAD^{tree}", check=False).returncode
        or _git(repo, "rev-list", "--objects", "--no-object-names",
                "--max-count=1", "HEAD", check=False).returncode
    )


def _explain_broken_object_store(repo, key, workspace, bases=()):
    """Re-raise a failed capture with a diagnosis instead of the bare constant.

    `_git(check=True)` collapses every non-zero rc into
    "survivor_unavailable: git inspection failed" (the detail is log-only,
    because git stderr can carry credential-bearing remote URLs). For a broken
    object store that costs the operator a full manual forensics pass -- on
    t_85cc093e round 11 it cost ~15 minutes to establish facts this function
    already has: WHICH repo, WHICH lender, and whether the committed work is
    published. The repo key is workspace-relative and the lender is a local
    filesystem path; neither is credential-bearing, so both are safe to persist.

    Returns (raising nothing) when the store reads fine, so any other failure
    keeps today's constant message and today's behaviour. Fail-closed is
    preserved either way: this never skips a repo, it only explains the refusal
    and names the remedy (quarantine by MOVING, never deleting).
    """
    if not _object_store_broken(repo):
        return
    sha = _git(repo, "rev-parse", "--verify", "HEAD", check=False).stdout.decode().strip()
    where = "." if key == "." else f"./{key}"
    lender, inside = _lender(repo, workspace)
    if lender:
        scope = "sibling clone inside this workspace" if inside else "external"
        cause = f"likely a pruned `alternates` lender ({scope}: {_ext.redact(lender)})"
    else:
        cause = "objects are missing from this repository"
    published = _advertised_head(repo, workspace, sha)
    if published:
        remedy = (f"HEAD {sha[:12]} is advertised at {published['remote']}/{published['branch']}, "
                  "so its committed work is published; if the remaining local changes are "
                  f"disposable, MOVE {where} out of the workspace (never delete it) and retry")
        if key in bases:
            # `preserve` has a SECOND fail-closed gate: a repo recorded at
            # dispatch that is no longer present raises "recorded repository
            # missing", which says nothing about the quarantine that caused it.
            # Following the MOVE advice alone would just trade one refusal for
            # a more confusing one, so the remedy has to name both steps. The
            # gate itself stays closed -- only the message gets honest.
            remedy += (f" -- {where} was recorded at dispatch, so the retry must PAIR the move "
                       "with --survivor-pr <owner/repo#N> or --survivor-ref <repo-url>#<sha> "
                       "or it will refuse again with 'recorded repository missing'")
    else:
        remedy = (f"HEAD {sha[:12]} is not advertised on any durable remote -- do not delete "
                  f"{where}; restore the missing objects first")
    log.warning("kanban survivor: unreadable object store in %s (%s)", _ext.redact(str(repo)), cause)
    raise SurvivorUnavailable(
        f"survivor_unavailable: git cannot read the objects reachable from HEAD in {where} -- "
        f"broken object store, {cause}. {remedy}."
    )


# Directories that cannot hold a survivor because their contents are DERIVED:
# a package manager, an interpreter or a linter can regenerate every byte. They
# are pruned so the walk never descends into them.
#
# The set is deliberately narrow, and the narrowness is the point. Pruning by
# "this subtree is big" is what a reader reaching for a fix naturally proposes
# (t_6c46905a proposed exactly `.worktrees`, `kanban`, `var`, `wt`, `plans`,
# `skills-shared`, `sessions`, `runs`, `logs`, `backups`) and it is UNSOUND:
# measured against every `bases` row on the live board, that list drops 691 of
# the 832 recorded repository keys -- i.e. it prunes away the repositories the
# walk exists to find, silently turning a HOLD into a delete for real unpushed
# work. `kanban/workspaces/*` alone holds 422 recorded repos, and `.worktrees`
# is where the dispatcher puts every worktree card.
#
# This set instead drops 0 of those 832 keys, so it can never lose a survivor,
# and it is still worth ~10% of the entries on a real worktree workspace
# (measured 2,128/21,877 on t_dbfa8eb2, 353/4,124 on t_19f4a9d3). Anything
# added here must be re-measured the same way: coverage first, speed second.
_DERIVED_DIRS = frozenset({
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", ".venvs", "venv",
})

# Hard ceiling on directories inspected by one enumeration. A gate that cannot
# finish must SAY so rather than hang: unbounded, this walk visited 850,853
# directories in 227 s on an idle disk for a `dir` workspace rooted at the
# fleet home, and `kanban_complete` died at its 420 s tool ceiling having
# written nothing at all -- no output, no error, no terminal state (t_6c46905a,
# faulthandler caught it inside `is_symlink` under `os.walk`).
#
# 50,000 is ~6.7 s measured on that tree and leaves >2x headroom over the
# largest real workspace on the board (21,877 directories).
DEFAULT_WALK_BUDGET_ENTRIES = 50_000


def _walk_budget():
    """Entry ceiling for one enumeration; invalid env values fall back silently."""
    raw = os.environ.get("HERMES_KANBAN_SURVIVOR_WALK_BUDGET", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return DEFAULT_WALK_BUDGET_ENTRIES


def _over_budget(what, workspace, visited, budget):
    """The refusal for a tree too large to enumerate, naming count and path.

    Fail-closed and LEGIBLE: the count and the path are what tell an operator
    which tree to point the workspace at instead, and neither is
    credential-bearing, so both are safe to persist as ``held_reason``.
    """
    return SurvivorUnavailable(
        f"survivor_unavailable: {what} exceeded the {budget} directory budget after "
        f"{visited} entries under {workspace} -- the workspace is too large to "
        "enumerate; point the card at the specific repository or worktree instead of "
        "a whole home directory, or raise HERMES_KANBAN_SURVIVOR_WALK_BUDGET"
    )


def _subdirs(here):
    """Immediate non-symlink subdirectory names, and whether ``.git`` is present.

    ``os.scandir`` answers "is this a directory" and "is this a symlink" from
    the dirent the readdir already returned, so the per-child ``is_symlink()``
    stat the previous ``os.walk`` form issued -- 853,413 of them on the fleet
    home -- is not paid at all.

    An unreadable directory (EACCES/EPERM) cannot hold a repo the worker could
    have written to, so skipping it loses no survivor. Raising took down every
    dispatch tick for a `dir` workspace rooted at a real home (2026-09-20:
    `var/skills-portal/caddy`, root-owned 0700, made the whole default board
    unspawnable). Anything else is still fatal.
    """
    try:
        with os.scandir(here) as it:
            entries = list(it)
    except PermissionError as exc:
        log.warning("kanban survivor: skipping unreadable dir %s (%s)", here, exc.strerror)
        return [], False
    names = set()
    children = []
    for entry in entries:
        names.add(entry.name)
        if entry.name in (".git", *_DERIVED_DIRS):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                children.append(Path(entry.path))
        except OSError:
            continue
    return children, ".git" in names


def _walk(workspace):
    """Directories under ``workspace``, pruned and bounded; depth-first.

    Yields ``(path, is_repo)``. Raises :class:`SurvivorUnavailable` rather than
    running forever when the tree exceeds the entry budget.
    """
    budget = _walk_budget()
    visited = 0
    stack = [Path(workspace)]
    while stack:
        here = stack.pop()
        visited += 1
        if visited > budget:
            raise _over_budget("repository enumeration", workspace, visited, budget)
        children, is_repo = _subdirs(here)
        yield here, is_repo
        stack.extend(children)


def _dangling_gitfile(path):
    """True when ``path/.git`` is a gitfile whose ``gitdir:`` target is absent.

    Git stops its upward discovery at the FIRST ``.git`` it meets, so a
    dangling gitfile there means no repository encloses anything below it:
    there are no objects anywhere that could hold a survivor. This is the
    deliberate tripwire shape (``kanban/workspaces/.git`` -> ``/nonexistent``,
    incident t_82a5c853) that keeps scratch-workspace git off the live repo.
    A ``.git`` DIRECTORY, or a gitfile whose target exists, is a repository
    that may hold work, and stays fail-closed.
    """
    marker = path / ".git"
    try:
        if not marker.is_file():
            return False
        first = marker.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    except OSError:
        return False
    if not first or not first[0].startswith("gitdir:"):
        return False
    target = Path(first[0][len("gitdir:"):].strip())
    if not target.is_absolute():
        target = path / target
    try:
        return not target.exists()
    except OSError:
        return False


def _repos(workspace):
    """Find repos created inside scratch, including linked worktrees; no symlinks."""
    found = [here for here, is_repo in _walk(workspace) if is_repo]
    enclosing = next((p for p in (workspace, *workspace.parents) if (p / ".git").exists()), None)
    if workspace not in found and enclosing is not None:
        # Probe before inspecting: a scratch workspace under the tripwire
        # gitfile makes every git call exit 128 "not a git repository", and
        # that means NO enclosing repository -- not an unreadable one. Only
        # the structural dangling-gitfile shape is excused; anything else
        # that fails the probe still raises below.
        probe = _git(workspace, "rev-parse", "--show-toplevel", check=False)
        if probe.returncode and _dangling_gitfile(enclosing):
            return found
        tracked = _git(workspace, "ls-files", "--", ".")
        if tracked.returncode == 0 and tracked.stdout:
            found.insert(0, workspace)
    return found


def _is_repo_on_disk(path):
    """A registry entry is proof only if the path is a repository ON DISK.

    THE discriminator for both registry sources. Git's registries record what
    git *knows about*, not what is *present*, and both of them lie in the same
    direction:

      * a `160000` gitlink stays in the index of a linked worktree even though
        `git worktree add` does NOT check submodules out -- the path on disk is
        an empty directory (measured: `~/.hermes/.worktrees/t_01322a3f` carries
        18 gitlinks, 0 of them materialised);
      * `git worktree list` keeps listing a worktree whose directory was
        deleted but never pruned -- which is exactly what a failed
        `git worktree remove` leaves behind.

    Trusting either unfiltered made `preserve()` refuse on a repository that is
    not there, and the raise precedes every survivor-consulting branch, so not
    even `--survivor-unbound` could reach it: 26/28 `~/.hermes/.worktrees/t_*`
    and 49/50 `ace-media-homelab/.worktrees/t_*` cards were fail-closed HELD
    where they complete on main.

    `.git` may be a directory (clone, checked-out submodule) or a file (linked
    worktree, modern submodule), so `exists()` is the right test and costs one
    stat per registry hit -- 0.07-0.12 s over the whole fleet home.
    """
    try:
        return (path / ".git").exists()
    except OSError:
        return False


def _registered_nested(workspace):
    """Nested repos Git ALREADY knows about, without walking the filesystem.

    Git's own registries answer this in O(refs): `worktree list` enumerates
    every linked worktree of the repo at ``workspace``, and a `160000` index
    entry is a gitlink (a submodule). Both are read from metadata, so the cost
    does not scale with the tree -- measured 1.2 s and 0.05 s on the fleet home,
    against 227 s for the walk that finds the same thing. The registry path is
    the arm that satisfies the card's <10 s acceptance bar; re-measured after
    review at 2.34 s cold / 0.10--0.14 s warm. The bounded fallback remains
    disk-contention sensitive (21.47--23.14 s in the same measurement), but it
    terminates with a named refusal instead of hanging silently.

    Every hit is confirmed present via :func:`_is_repo_on_disk` before it is
    returned; see there for why a bare registry entry is not proof.

    Used ONLY as a positive: a path Git lists AND that is a repository on disk
    really is a nested repository, so finding one is proof. The converse does
    not hold and must not be inferred -- a repo a worker cloned by hand is in
    neither registry (measured: these two registries account for 47 of the 813
    repositories recorded for t_f5ebd9db), which is why this supplements the
    walk's refusal rather than replacing the walk.
    """
    if not (workspace / ".git").exists():
        return []
    nested = []
    listing = _git(workspace, "worktree", "list", "--porcelain", check=False)
    if listing.returncode == 0:
        for line in listing.stdout.decode("utf-8", "replace").splitlines():
            if not line.startswith("worktree "):
                continue
            try:
                path = Path(line[len("worktree "):]).resolve()
            except OSError:
                continue
            if path != workspace and path.is_relative_to(workspace) and _is_repo_on_disk(path):
                nested.append(path)
    index = _git(workspace, "ls-files", "--stage", check=False)
    if index.returncode == 0:
        for line in index.stdout.decode("utf-8", "replace").splitlines():
            meta, _, path = line.partition("\t")
            if meta.split(" ", 1)[0] == "160000" and path and _is_repo_on_disk(workspace / path):
                nested.append(workspace / path)
    return nested


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
    try:
        discovered = _repos(workspace)
    except SurvivorUnavailable as exc:
        # This runs at dispatch, before the worker exists. A refusal here would
        # make the card UNSPAWNABLE -- the same failure mode the EACCES skip
        # exists to avoid (2026-09-20, `var/skills-portal/caddy`). An empty
        # `bases` is the honest record for a tree we could not enumerate, and it
        # costs no survivor: `preserve` re-enumerates at completion under the
        # same budget and fails closed there, where there is a run to hold.
        log.warning("kanban survivor: baseline skipped for %s: %s", task_id, exc)
        discovered = []
    for repo in discovered:
        key = str(repo.relative_to(workspace))
        if key not in bases:
            head = _git(repo, "rev-parse", "--verify", "HEAD", check=False)
            bases[key] = head.stdout.decode().strip() if head.returncode == 0 else None
        _warn_borrowed_from_workspace(repo, key, workspace)
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


def _excluded_roots(workspace):
    """Roots a repo must NOT live under to count as durable/canonical."""
    # Never the fail-closed resolver: a worker spawned before
    # kanban.workspaces_root changed carries a stale pin, and raising here
    # turned every in-flight completion into survivor_unavailable (HELD).
    return [workspace, *kb.workspace_root_candidates(), kb.kanban_home() / "kanban" / "workspaces",
            kb.kanban_home() / "kanban" / "boards", *_temporary_roots()]


def _independent_storage(repo, workspace):
    """Require local ref authority to retain its Git storage after disposal.

    Gitfiles and linked worktrees are fine when BOTH their private and common
    directories are durable. Reject alternates and internal storage symlinks
    conservatively: proving a checkout path alone says nothing about the objects
    it borrows. Ordinary hardlinked clones remain independent on unlink.
    """
    if any(os.environ.get(key) for key in (
        "GIT_DIR", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    )):
        return False
    try:
        excluded = [root.resolve() for root in _excluded_roots(workspace)]

        def durable(path):
            return not any(path.resolve(strict=True).is_relative_to(root) for root in excluded)

        if not durable(repo):
            return False
        config = _git(repo, "config", "--null", "--get-regexp",
                      r"^(extensions\.partialclone|remote\..*\.promisor)$", check=False)
        if config.returncode not in (0, 1):
            return False
        for field in config.stdout.split(b"\0"):
            key, _, value = field.partition(b"\n")
            if key.lower() == b"extensions.partialclone" and value:
                return False
            if key.lower().endswith(b".promisor") and value.lower() in (b"true", b"yes", b"on", b"1"):
                return False
        directories = set()
        result = _git(repo, "rev-parse", "--path-format=absolute", "--git-dir",
                      "--git-common-dir", "--git-path", "objects", check=False)
        paths = result.stdout.splitlines()
        if result.returncode or len(paths) != 3:
            return False
        for raw in paths:
            path = Path(os.fsdecode(raw).strip())
            if not path.is_dir() or not durable(path):
                return False
            directories.add(path.resolve(strict=True))

        def fail(exc):
            raise exc

        for directory in directories:
            for root, dirs, files in os.walk(directory, followlinks=False, onerror=fail):
                for name in dirs + files:
                    path = Path(root) / name
                    if path.is_symlink():
                        return False
                    if name.endswith(".promisor"):
                        return False
                    if name in {"alternates", "http-alternates"} and path.is_file() and path.stat().st_size:
                        return False
        complete = _git(
            repo, "rev-list", "--objects", "--missing=print", "HEAD", check=False,
        )
        if complete.returncode or any(
            line.startswith(b"?") for line in complete.stdout.splitlines()
        ):
            return False
        return True
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return False


def _durable_remote(repo, remote, workspace, storage_cache=None):
    # Expand insteadOf aliases, then resolve symlinks before checking scope.
    url = _git(repo, "remote", "get-url", remote).stdout.decode().strip()
    if storage_cache is not None:
        storage_cache[("url", remote)] = url
    parsed = urlsplit(url)
    if parsed.scheme in {"https", "http", "ssh", "git"}:
        return True
    if not parsed.scheme and ":" in url and not url.startswith(("/", ".")):
        return True  # scp-style SSH
    if parsed.scheme not in {"", "file"} or parsed.netloc not in {"", "localhost"}:
        return False
    path = Path(unquote(parsed.path) if parsed.scheme else url).expanduser()
    path = (repo / path).resolve()
    if remote != "origin":
        return False
    if storage_cache is not None:
        if path not in storage_cache:
            storage_cache[path] = _independent_storage(path, workspace)
        return storage_cache[path]
    return _independent_storage(path, workspace)


def _published_refs(repo, workspace, storage_cache=None):
    remotes = _git(repo, "remote").stdout.decode().splitlines()
    if storage_cache is not None:
        storage_cache[("remotes",)] = remotes
    for remote in remotes:
        if not _durable_remote(repo, remote, workspace, storage_cache):
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


def _present_commits(repo, shas):
    """The subset of ``shas`` this repository actually holds, in ONE git call.

    Both ref scans below ask git a reachability question per published ref.
    That is fine for a handful of refs and ruinous for a real fork: the
    workspace of card t_a49e8a28 had 5 remotes advertising 7,937 heads, and at
    a measured 90.9 ms per `git` spawn the two scans projected to 721 s and
    718 s -- each one alone past the 420 s tool-call ceiling, which is why the
    `kanban_complete` TOOL timed out three times while the CLI (same DB, same
    code) closed the card in 1.8 s. The cost tracks REMOTE REF COUNT, not
    workspace size: a seeded 39,995-file workspace with one remote completes
    in 1.52 s.

    A sha advertised by a remote need not be in the local object store (the
    measured workspace was missing 1 of 2,917). The per-ref loops pass
    ``check=False`` and simply skip those, so the set-based forms must filter
    them out first or `rev-list` aborts with "fatal: bad object" and takes the
    whole scan with it. This filter is what makes bulk EXACTLY match per-ref,
    not an approximation of it.
    """
    if not shas:
        return []
    probe = _git(
        repo, "cat-file", "--batch-check=%(objectname) %(objecttype)", "--buffer",
        check=False, input=b"".join(f"{sha}\n".encode() for sha in shas),
    )
    if probe.returncode:
        return []
    present = []
    for line in probe.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "commit":
            present.append(parts[0])
    return present


def _rev_list(repo, head, shas, *args):
    """``git rev-list <args> head ^sha ^sha …`` over a set of refs, in one call.

    The negative revs go on stdin because 7,937 of them overflow ARG_MAX. Note
    that ``--not --stdin`` does NOT negate stdin revs -- measured, it returned
    52,090 (the whole positive union) where the explicit ``^`` form returned 6
    -- so each line is written pre-negated.
    """
    payload = f"{head}\n".encode() + b"".join(f"^{sha}\n".encode() for sha in shas)
    return _git(repo, "rev-list", *args, "--stdin", check=False, input=payload)


def _first_containing(repo, head, candidates):
    """The first ref in ``candidates`` that contains ``head``, in ~log2(N) calls.

    Naming the ref is where the per-ref loop survived its first removal: the
    containment QUESTION was already set-based, but the ANSWER still walked
    `merge-base --is-ancestor` per ref, so a clean workspace parked behind a
    branch tip (HEAD contained in the set but not itself a tip) paid the full
    O(N) cost the card was filed about. Measured on the real board at 5 -> 41
    refs: 24 -> 96 git spawns, slope 2.000/ref, projecting 1,424 s at the real
    workspace's 7,937 refs -- half of it BEFORE the durable write.

    `contained in candidates[:k]` is MONOTONE in k, so the first containing ref
    is a binary search over prefixes using the same `_rev_list ... --count`
    test, not a scan. Order is preserved, so the ref named is bit-for-bit the
    one the per-ref loop named.
    """
    low, high = 0, len(candidates)  # invariant: not contained in [:low]
    while low < high:
        mid = (low + high) // 2
        counted = _rev_list(repo, head, [c["sha"] for c in candidates[:mid + 1]], "--count")
        if counted.returncode == 0 and counted.stdout.strip() == b"0":
            high = mid
        else:
            low = mid + 1
    return candidates[low] if low < len(candidates) else None


def _remote_survivor(repo, head, published):
    # Reachability from a SET is the union of reachability from each member,
    # so "HEAD is contained in some published ref" == "HEAD has no commit that
    # is not in the set". One rev-list answers that for all refs at once
    # (measured 0.04 s vs 721 s projected for the per-ref loop).
    covered = [ref for ref in published if ref["sha"] == head]
    if not covered:
        present = set(_present_commits(repo, sorted({ref["sha"] for ref in published})))
        if not present:
            return None
        # A sha the local store lacks can never contain HEAD; the per-ref loop
        # skipped those via check=False, so dropping them here keeps the same
        # answer AND keeps `rev-list ^<unknown>` from aborting the whole walk.
        candidates = [ref for ref in published if ref["sha"] in present]
        counted = _rev_list(repo, head, [ref["sha"] for ref in candidates], "--count")
        if counted.returncode or counted.stdout.strip() != b"0":
            return None
        ref = _first_containing(repo, head, candidates)
        return dict(ref, head=head) if ref else None
    return dict(covered[0], head=head)


# A rewriting mirror (hermes-home's "isolated remote sync") republishes every
# commit under a NEW sha, so `_remote_survivor` can never match a clone of it and
# the capture falls through to a bundle of a 94 MB home tree, which always
# exceeds KANBAN_ATTACHMENT_MAX_BYTES (2026-09-20, t_e69d693a).
#
# `git patch-id` survives the rewrite, but it is the identity of a commit's
# DIFF, not of its content: two commits on different parents can produce
# byte-identical diffs over different trees, and patch-id normalises whitespace
# away. The live hermes-home mirror demonstrates this on the very pair the fix
# was designed around — 9f25d7cce and e747d18db share a patch-id while their
# trees differ by 58 files. So a patch-id hit is ADVISORY ANNOTATION ONLY and
# never authority to delete a workspace (Apollo ruling, 2026-09-20).
#
# The durable target for a home-clone workspace is the LIVE CANONICAL TREE it
# was cloned from — a local repository outside every kanban/temp root — which is
# covered by the fleet-backup tier. The rewriting mirror is not a faithful copy
# and must not be the bar.
_CONTENT_SCAN_DEPTH = 25
_CONTENT_SCAN_BUDGET = 100


def _canonical_repos(repo, workspace, storage_cache=None):
    """Local repositories this repo's remotes resolve to, outside every disposable root.

    A `file:`/path remote pointing at a live checkout (``~/.hermes``) is the
    canonical tree: it holds the real objects, it is what the workspace was
    cloned from, and it is what the fleet-backup tier covers. Remotes inside a
    kanban workspace, board, or temp root are disposable and never canonical.
    """
    remotes = (storage_cache.get(("remotes",)) if storage_cache is not None else None)
    if remotes is None:
        remotes = _git(repo, "remote").stdout.decode().splitlines()
    for remote in remotes:
        raw = storage_cache.get(("url", remote)) if storage_cache is not None else None
        if raw is None:
            url = _git(repo, "remote", "get-url", remote, check=False)
            if url.returncode:
                continue
            raw = url.stdout.decode().strip()
        parsed = urlsplit(raw)
        if parsed.scheme not in {"", "file"} or parsed.netloc not in {"", "localhost"}:
            continue
        if not parsed.scheme and ":" in raw and not raw.startswith(("/", ".")):
            continue  # scp-style SSH: not a local path
        path = Path(unquote(parsed.path) if parsed.scheme else raw).expanduser()
        try:
            path = (repo / path).resolve(strict=True)
        except OSError:
            continue
        if storage_cache is not None and path in storage_cache:
            independent = storage_cache[path]
        else:
            independent = _independent_storage(path, workspace)
            if storage_cache is not None:
                storage_cache[path] = independent
        if not independent:
            continue
        yield {"remote": remote, "repository_path": str(path)}


def _canonical_survivor(repo, head, workspace, storage_cache=None):
    """Accept ``head`` when a LIVE canonical tree can reach it from its HEAD.

    This is the deletion authority for a home-clone workspace: the commit is not
    only present in a durable local repo, it is on that repo's current line of
    work. Reachability (not sha equality) is the predicate, so the rewriting
    mirror never enters into it.
    """
    for candidate in _canonical_repos(repo, workspace, storage_cache):
        live = Path(candidate["repository_path"])
        if _git(live, "merge-base", "--is-ancestor", head, "HEAD", check=False).returncode == 0:
            return dict(candidate, sha=head, head=head, matched_by="canonical",
                        branch=_git(live, "rev-parse", "--abbrev-ref", "HEAD",
                                    check=False).stdout.decode().strip() or "HEAD")
    return None


def _patch_id(repo, sha, env=None):
    """Content identity of one commit's diff. None when it cannot be computed."""
    diff = _git(repo, "diff-tree", "-p", "--full-index", "--no-ext-diff",
                "--no-textconv", "--no-renames", "--root", sha, env=env, check=False)
    if diff.returncode or not diff.stdout:
        return None
    result = subprocess.run(
        ["git", "-C", str(repo), "patch-id", "--stable"],
        input=diff.stdout, capture_output=True, timeout=30,
        env=dict(os.environ if env is None else env, GIT_NO_REPLACE_OBJECTS="1"),
    )
    if result.returncode or not result.stdout.strip():
        return None
    return result.stdout.split()[0].decode()


def _exact_commit_diff(repo, sha):
    """Full blob-identified diff; unlike patch-id this retains whitespace and bytes."""
    diff = _git(repo, "diff-tree", "-p", "--binary", "--no-commit-id",
                "--full-index", "--no-ext-diff", "--no-textconv",
                "--no-renames", "--root", sha, check=False)
    return diff.stdout if not diff.returncode and diff.stdout else None


def _landed_contains_history(workspace_repo, landed_repo, landed_sha):
    """Return how ``landed_sha`` contains the workspace's final committed tree."""
    workspace_head = _git(
        workspace_repo, "rev-parse", "--verify", "HEAD^{commit}", check=False,
    )
    if workspace_head.returncode:
        return None
    workspace_head = workspace_head.stdout.decode().strip()
    # Historical ancestry and identical commit diffs can both be undone by a
    # later canonical commit. Only the current live tree can authorize deletion.
    workspace_history = _git(workspace_repo, "rev-list", workspace_head, check=False)
    landed_history = _git(landed_repo, "rev-list", landed_sha, check=False)
    if workspace_history.returncode or landed_history.returncode:
        return None
    landed_commits = set(landed_history.stdout.decode().split())
    work_commits = workspace_history.stdout.decode().split()
    shared = next((commit for commit in work_commits if commit in landed_commits), None)
    # Compare net worker changes, not paths touched by inherited history.
    # A shared HEAD still needs its path guard against later live reverts.
    if shared and shared != workspace_head:
        touched = _git(workspace_repo, "diff", "--no-renames", "--name-only", "-z", shared, workspace_head, check=False)
    else:
        touched = _git(workspace_repo, "log", "--no-renames", "--name-only", "-z", "--format=", workspace_head, check=False)
    live_head = _git(landed_repo, "rev-parse", "--verify", "HEAD^{commit}", check=False)
    if touched.returncode or live_head.returncode or not touched.stdout:
        return None
    work_tree = _git(workspace_repo, "ls-tree", "-rz", "--full-tree", workspace_head, check=False)
    live_tree = _git(landed_repo, "ls-tree", "-rz", "--full-tree", live_head.stdout.decode().strip(), check=False)
    if work_tree.returncode or live_tree.returncode:
        return None
    def entries(tree):
        return dict(item.split(b"\t", 1)[::-1] for item in tree.stdout.split(b"\0") if item)
    work_entries, live_entries = entries(work_tree), entries(live_tree)
    if any(work_entries.get(path) != live_entries.get(path) for path in set(touched.stdout.split(b"\0")) - {b""}):
        return None
    if _git(
        landed_repo, "merge-base", "--is-ancestor", workspace_head, landed_sha,
        check=False,
    ).returncode == 0:
        return "ancestor"

    missing = [
        commit for commit in work_commits
        if commit not in landed_commits
    ]
    if not missing:
        return "ancestor"

    needed = set()
    for commit in missing:
        diff = _exact_commit_diff(workspace_repo, commit)
        if diff is None:
            return None
        needed.add(diff)
    for commit in landed_commits:
        diff = _exact_commit_diff(landed_repo, commit)
        if diff in needed:
            needed.remove(diff)
            if not needed:
                return "exact-diff"
    return None


def _content_advisory(repo, head, published, *, budget=_CONTENT_SCAN_BUDGET):
    """ADVISORY ONLY: a published commit with the same normalized patch-id.

    Recorded in the sidecar as a recovery hint ("the mirror's <sha> looks like
    this work"), NEVER as authority to delete a workspace: patch-id is diff
    identity, not content identity, and the live hermes-home mirror produces
    false positives on exactly the pair this feature was designed around.

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
            fetch_env = dict(env, KANBAN_FETCH_URL=url)
            try:
                fetched = _git(
                    probe, "--config-env=remote.candidate.url=KANBAN_FETCH_URL",
                    "fetch", "--no-tags", "--depth", str(_CONTENT_SCAN_DEPTH + 1),
                    "candidate", f"+refs/heads/{ref['branch']}:refs/heads/candidate",
                    env=fetch_env, check=False, timeout=120,
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
                                advisory=True, patch_id=target)
            _git(probe, "update-ref", "-d", "refs/heads/candidate", env=env, check=False)
    return None


def _verify_landed(entries, workspace):
    """Verify an explicit `landed` claim against a LIVE CANONICAL repository.

    The escape hatch for work that was committed into a repo the workspace only
    mirrors. The bar (Apollo ruling, 2026-09-20): the named repository must be a
    real repository OUTSIDE every disposable root — a live tree covered by the
    fleet-backup tier — and the sha must resolve there AND be reachable from that
    tree's HEAD. Publication on a durable remote is NOT required, because the
    hermes-home mirror rewrites trees and is not a faithful copy; requiring it
    would make the claim unsatisfiable by construction.

    Every repository in the workspace must be clean and its committed history
    must be represented by a named landed commit, either by exact ancestry or
    byte-exact commit diffs. Patch-id remains advisory for remote
    mirrors; neither it nor a normalized diff can bind workspace commits.

    Every failure mode raises — an unverifiable claim must never be accepted,
    because accepting it authorises deleting the only remaining copy of the code.
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
        if _git(repo, "rev-parse", "--git-dir", check=False).returncode:
            raise SurvivorUnavailable("survivor_unavailable: landed path is not a repository")
        if not _independent_storage(repo, workspace):
            # A workspace/board/temp tree is itself disposable: pointing `landed`
            # at one would let the capture authorise deleting its own only copy.
            raise SurvivorUnavailable("survivor_unavailable: landed repository is not a durable tree")
        resolved = _git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}", check=False)
        if resolved.returncode:
            raise SurvivorUnavailable("survivor_unavailable: landed commit not in repository")
        sha = resolved.stdout.decode().strip()
        if _git(repo, "merge-base", "--is-ancestor", sha, "HEAD", check=False).returncode:
            raise SurvivorUnavailable("survivor_unavailable: landed commit not reachable from HEAD")
        record = {"repository": str(repo), "sha": sha, "matched_by": "canonical",
                  "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD",
                                 check=False).stdout.decode().strip() or "HEAD"}
        published = list(_published_refs(repo, workspace))
        ref = _remote_survivor(repo, sha, published)
        if ref:
            record["published"] = {"remote": ref["remote"], "branch": ref["branch"],
                                   "sha": ref["sha"], "matched_by": "sha"}
        else:
            hint = _content_advisory(repo, sha, published)
            if hint:
                # Advisory: same diff on the mirror. Not why we accepted.
                record["published"] = {"remote": hint["remote"], "branch": hint["branch"],
                                       "sha": hint["sha"], "matched_by": "patch-id",
                                       "advisory": True}
        verified.append(record)

    workspace_repos = _repos(workspace)
    if not workspace_repos or _loose_files(workspace, workspace_repos):
        raise SurvivorUnavailable("survivor_unavailable: landed workspace has uncaptured files")
    used_entries = set()
    for repo in workspace_repos:
        status = _git(repo, "status", "--porcelain", "--untracked-files=all", check=False)
        if status.returncode or status.stdout:
            raise SurvivorUnavailable("survivor_unavailable: landed workspace is not clean")
        workspace_head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}", check=False)
        if workspace_head.returncode:
            raise SurvivorUnavailable("survivor_unavailable: workspace repository has no commit")
        binding = None
        for index, record in enumerate(verified):
            matched_by = _landed_contains_history(repo, Path(record["repository"]), record["sha"])
            if matched_by:
                binding = (index, record, matched_by)
                break
        if binding is None:
            raise SurvivorUnavailable(
                "survivor_unavailable: landed commit does not contain workspace history"
            )
        index, record, matched_by = binding
        used_entries.add(index)
        record.setdefault("workspace_repositories", []).append({
            "repository": str(repo.relative_to(workspace)),
            "head": workspace_head.stdout.decode().strip(),
            "matched_by": matched_by,
        })
    if used_entries != set(range(len(verified))):
        raise SurvivorUnavailable("survivor_unavailable: landed claim is unrelated to workspace")
    return verified


def _base(repo, published):
    # The nearest published ancestor of HEAD is a BOUNDARY commit of
    # `rev-list HEAD ^<every ref>`: git stops there precisely because the
    # commit is contained in the excluded set. That collapses one spawn per
    # ref into one spawn total, then ranks the (handful of) boundary commits
    # by distance exactly as the per-ref loop did. Verified against the
    # per-ref result on the real workspace: same base, same distance.
    present = _present_commits(repo, sorted({ref["sha"] for ref in published}))
    if not present:
        return None
    walked = _rev_list(repo, "HEAD", present, "--boundary")
    if walked.returncode:
        return None
    candidates = []
    for line in walked.stdout.decode("utf-8", "replace").split():
        if not line.startswith("-"):
            continue
        sha = line[1:]
        distance = int(_git(repo, "rev-list", "--count", f"{sha}..HEAD").stdout)
        candidates.append((distance, sha))
    if not candidates:
        # An EMPTY walk is not "no base" -- it is the strongest possible base.
        # git printed nothing because HEAD itself is contained in the excluded
        # set, which is exactly the per-ref case `merge-base HEAD <ref>` == HEAD
        # at distance 0 (the minimum, so it always won). Returning None here
        # instead downgrades a pushed-but-dirty repo from a `patch` survivor to
        # a whole-tree `bundle`.
        head = _git(repo, "rev-parse", "--verify", "HEAD", check=False)
        if head.returncode:
            return None
        return head.stdout.decode().strip() or None
    return min(candidates)[1]


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


def _capture(repo, key, workspace):
    """One repository's survivor material: (remote ref, base, snapshot bytes).

    Extracted verbatim from `preserve`'s loop so the object-reading steps sit
    inside a single `try` the caller can classify. Behaviour is unchanged.
    """
    storage_cache = {}
    published = list(_published_refs(repo, workspace, storage_cache))
    head = _git(repo, "rev-parse", "--verify", "HEAD", check=False)
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
    if not dirty and head.returncode == 0:
        sha = head.stdout.decode().strip()
        # A rewriting mirror republishes the same content under a new sha, so
        # sha equality can never hold for a home clone. The fallback is the
        # LIVE CANONICAL TREE the workspace was cloned from — never a patch-id
        # hit, which is diff identity only.
        ref = (_remote_survivor(repo, sha, published)
               or _canonical_survivor(repo, sha, workspace, storage_cache))
        if ref and ref.get("matched_by") == "canonical":
            hint = _content_advisory(repo, sha, published)
            if hint:
                ref = dict(ref, mirror_hint={
                    "remote": hint["remote"], "branch": hint["branch"],
                    "sha": hint["sha"], "matched_by": "patch-id", "advisory": True,
                })
        if ref:
            return ref, None, None
    base = _base(repo, published)
    prefix = "" if key == "." else key + "/"
    return None, base, _snapshot(repo, base, prefix)


_QUALIFIER = re.compile(r"[^:?#=]+=")


def _split_qualifier(claim):
    """Split an optional ``<workspace-relative-repo>=`` prefix off a claim.

    One `--survivor-pr` names ONE remote, so a multi-repository loss needs a
    way to say WHICH repository each claim covers. A bare claim keeps its
    historical meaning (the single lost repository).

    The grammar is defined by EXCLUSION, not by a whitelist of tidy directory
    names: every verifiable claim shape carries a `#` (`verify_ref` requires
    `<url>#<sha>`, `verify_pr` requires `owner/repo#N` or a `/pull/N` URL), and
    every URL shape additionally carries a `:` before any `=` it may hold in a
    query string. Excluding `:`, `?` and `#` from the prefix therefore cannot
    swallow a bare claim -- while admitting any real directory name, including
    one with a space, `@`, `+`, `~` or non-ASCII characters. A whitelist could
    not: it made the remedy the refusals PRINT (`--survivor-pr <key>=...`)
    unparseable for exactly those names, leaving that state HELD with no
    accepted input -- the unreachable-remedy bug this module exists to close.
    """
    match = _QUALIFIER.match(claim)
    if not match:
        return None, claim
    return claim[:match.end() - 1], claim[match.end():]


def _qualified_hint(keys):
    """The qualified remedy for `keys`, or an honest admission one has none.

    The refusals interpolate a real repository key into the form the operator
    is told to type. A key the qualifier grammar cannot express would print a
    remedy that does not parse -- `_split_qualifier` would hand the whole
    string to `verify_*`, the match would fail, and the state would be HELD
    with no accepted input at all. Take the WHOLE set the operator must cover,
    not just its first element: every one of them needs its own claim, so a
    single unrepresentable key elsewhere in the set makes the state
    unsatisfiable no matter how tidy the example is.
    """
    unrepresentable = sorted(k for k in keys if _split_qualifier(f"{k}=x") != (k, "x"))
    if unrepresentable:
        return (f"no qualified form exists for repository {unrepresentable[0]!r} (its name "
                "contains ':', '?', '#' or '='); rename or re-clone it under a plain path "
                "to recover")
    # Expressible by the grammar is necessary but NOT sufficient: the remedy is
    # consumed by a shell and then by argparse before `_split_qualifier` ever
    # sees it. A key starting with `-` reads as an option (`expected one
    # argument`) and a key holding a space splits into two words, so the bare
    # `--flag value` form printed an unreachable remedy for exactly those names
    # -- the same unreachable-remedy bug one layer out. `hint_arg` owns the
    # spelling that survives both.
    return hint_arg("--survivor-pr", f"{sorted(keys)[0]}=owner/repo#N")


def _unbound_keys(survivor_unbound):
    """Normalise the override into ``(bare, keys)`` -- the claims it covers.

    ``--survivor-unbound`` used to be a single invocation-wide boolean threaded
    straight into :func:`_verified_explicit`, where it stripped the task-id
    binding from EVERY claim. The multi-repository remedy this module works
    hard to keep reachable is several claims in one invocation
    (``--survivor-pr kept=owner/repo#1 --survivor-pr gone=owner/repo#2``), so
    an operator overriding the ONE claim that landed on an unrelated-looking
    branch silently accepted the others with no check that they name the card
    -- a typo, a stale number or a pasted unrelated PR recorded as that
    repository's provenance. That is the same recovery-index corruption this
    file refuses elsewhere (one claim may not vouch for several missing
    repositories; a qualifier may not name a repository still on disk).

    ``bare`` is a BARE ``--survivor-unbound`` (the historical ``store_true``
    spelling), which keeps its meaning for the single-claim shape it was
    designed for and is refused by the caller on a multi-claim invocation.
    ``keys`` are qualifier keys, where ``None`` is the key of the historical
    unqualified claim.
    """
    if survivor_unbound is True:
        return True, frozenset()
    if not survivor_unbound:
        return False, frozenset()
    if isinstance(survivor_unbound, str):
        survivor_unbound = [survivor_unbound]
    entries = list(survivor_unbound)
    if entries == [True]:
        return True, frozenset()
    return False, frozenset(
        None if entry in (None, True, "") else str(entry) for entry in entries
    )


def _live(verify, value, extra):
    """Is this claim real at all, with the binding set aside?

    Only ever asked to DISCRIMINATE a refusal that has already happened: a
    claim the remote does not know at all and a claim it knows but that does
    not name the card are different facts and deserve different diagnostics,
    and only the second has the override as a remedy.

    Asked without ``mined_for``, so :class:`_ext.AmbiguousRef` cannot arise
    (the unbound path resolves a multi-tip SHA rather than refusing it). It is
    caught anyway and read as live, because that is what it means -- the remote
    answered and the refs exist -- and because a future narrowing of the
    unbound path must not silently turn this discrimination into a crash.
    :class:`_ext.RemoteUnavailable` is deliberately NOT caught: it propagates to
    the caller's handler, which reports "could not verify", rather than being
    flattened into "not live" -- a blip must never be published as a verdict.
    """
    try:
        return verify(value, **extra) is not None
    except _ext.AmbiguousRef:
        return True


def _verified_explicit(task_id, survivor_ref, survivor_pr, *, unbound=False):
    """An operator-named survivor is a claim: verify it is real AND is THIS card's work.

    Returns ``{repository-key-or-None: ref}``. Both flags are repeatable and
    each value may be qualified as ``<repo>=<claim>`` so a multi-repository
    loss has a reachable remedy that records TRUE provenance per repository.
    An unqualified claim maps to ``None`` -- "the one lost repository" -- and
    is resolved by the caller, which is the only place that knows which one.

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
    the authorisation is auditable rather than invisible.

    The override is NOT an identity gate, and must not be mistaken for one.
    Argus's round-1 review asked for one: refuse the flag unless the caller is
    not a dispatched worker, plus an interactive tty. Measured on this host
    (kanban card t_de2e348e, probe_r2_signals.py) that is not implementable -- a worker
    runs as the SAME uid as the operator (502) and can forge every
    process-local signal available:

      * ``is_dispatcher_owned_worker_context()`` fails OPEN with no
        ``HERMES_KANBAN_OWNER_PID``, so dropping the grant (``env -u``) reads
        as an operator: predicate False inherited, True stripped;
      * process ancestry survives one double-fork: the daemonized grandchild
        reports ``ppid = 1``, so nothing above it looks like a worker;
      * ``sys.stdin.isatty()`` is False in this worker and True in a
        ``pty.fork`` child it spawns itself.

    So the flag cannot be bounded by WHO calls it, and pretending otherwise
    would ship a gate that reads as security and is not. It is bounded by
    CAPABILITY instead -- see :func:`_reusable`: an unbound claim authorises
    THIS completion and never becomes standing authority that a later
    reclamation reuses without re-testing.

    NOT every corroboration is the same strength, and a weak one is NOT a
    binding. ``verify_pr`` reports WHICH field answered: a head branch or a
    claimed SHA ties the PR's *content* to the card, but a substring in the PR
    title or body is only a MENTION -- an umbrella changelog, a dependency
    note, even "does not address t_..." satisfies it.

    A mention therefore takes the SAME refusal an unrelated live claim takes,
    and needs the SAME explicit override to proceed. Marking it `unbound` and
    letting it complete anyway (the shape this function shipped with) closed
    only half the hole: :func:`_reusable` guards the RECORDED row on the
    `cleanup=True` pass, so it never sees a claim that reaches the completion
    path through :func:`_external`'s ``explicit`` arm. ``preserve`` treats
    that verified explicit survivor as authority on the workspace-missing exit
    -- so "follow-up to t_...; does not address it" could delete unpushed work
    in that moment, without any operator ever authorising it. The two paths
    now answer "is this a tie to the card's work?" identically, and the
    operator case keeps the one documented door.

    The override is PER-CLAIM. It used to be a single invocation-wide boolean
    applied to every claim, so an operator overriding the one repository whose
    work landed on an odd branch silently accepted the others with no check
    that they name the card -- and cost the correctly-bound ones their
    reclamation authority, since all of them were stamped ``unbound``. See
    :func:`_unbound_keys`.
    """
    bare_override, override = _unbound_keys(unbound)
    pending = [
        (flag, verify, extra, claim)
        for claims, flag, verify, extra in (
            (survivor_ref, "--survivor-ref", _ext.verify_ref, {}),
            (survivor_pr, "--survivor-pr", _ext.verify_pr,
             {"corroborate": ("headRefName", "title", "body")}),
        )
        for claim in (claims if isinstance(claims, (list, tuple)) else [claims] if claims else [])
        if claim
    ]
    if bare_override and len(pending) > 1:
        # A bare flag cannot say WHICH claim is being vouched for, and guessing
        # would reinstate exactly the invocation-wide behaviour this replaced.
        raise SurvivorUnavailable(
            "survivor_unavailable: --survivor-unbound is per-claim; with more than one "
            "claim, name the repository it applies to (--survivor-unbound <repository>)"
        )
    unclaimed = override - {_split_qualifier(claim)[0] for _, _, _, claim in pending}
    if unclaimed:
        # A no-op override is an operator typo, and a silent one would leave
        # the claim they meant to vouch for still refused -- or, worse, read
        # as though the binding had been relaxed when it was not.
        raise SurvivorUnavailable(
            "survivor_unavailable: --survivor-unbound names no claim in this invocation "
            f"({', '.join(sorted(str(k) for k in unclaimed))})"
        )
    resolved, unqualified = {}, None
    for flag, verify, extra, claim in pending:
        key, value = _split_qualifier(claim)
        claim_unbound = bare_override or key in override
        # A remote that did not ANSWER is not evidence about the claim. Keep
        # the two outcomes apart at the seam: ``None`` is a verdict from the
        # remote, ``RemoteUnavailable`` is the absence of one. Collapsing them
        # let a rate limit or network blip be reported -- and persisted to
        # ``held_reason`` and the ``workspace_held`` event that ``kanban_show``
        # replays -- as the false statement "is live but does not name <card>",
        # whose offered remedy is to drop the very binding this path adds
        # (kanban card t_de2e348e, Argus round 3).
        try:
            ref = verify(value, mined_for=None if claim_unbound else task_id, **extra)
            if ref is None:
                # The claim is unverified and may carry a token: echo it redacted only.
                if not claim_unbound and _live(verify, value, extra):
                    raise _refusal(
                        f"survivor_unavailable: {flag} {_ext.redact(claim)} is live but does not "
                        f"name {task_id}, so it is not evidence of THIS card's work",
                        hint=True,
                    )
                raise SurvivorUnavailable(
                    f"survivor_unavailable: could not verify {flag} {_ext.redact(claim)} "
                    f"against the remote"
                )
        except _ext.AmbiguousRef as exc:
            # A THIRD outcome, and reporting it as either of the other two lies.
            # The remote answered, and it answered with several refs that all
            # name the card -- so "could not verify against the remote" is
            # false, and "does not name <card>" is its opposite. Name the tips
            # and keep the hint: the override is a real remedy here, because it
            # drops the binding that is doing the narrowing.
            raise _refusal(
                f"survivor_unavailable: {flag} {_ext.redact(claim)} is the tip of "
                f"{len(exc.tips)} refs that all name {task_id} "
                f"({', '.join(_ext.redact(tip) for tip in exc.tips)}), so which one carries "
                f"THIS card's work cannot be established; name it with --survivor-pr",
                hint=True,
            ) from exc
        except _ext.RemoteUnavailable as exc:
            raise SurvivorUnavailable(
                f"survivor_unavailable: could not verify {flag} {_ext.redact(claim)} "
                f"against the remote ({_ext.redact(str(exc))})"
            ) from exc
        if not claim_unbound and ref.get("corroborated_by") in _WEAK_CORROBORATION:
            # Same refusal as an unrelated live claim, because it is the same
            # fact: the remote answered, and what it said is not a tie to this
            # card's work. Naming the field keeps the diagnostic honest -- the
            # operator can read the PR's own prose and decide.
            raise _refusal(
                f"survivor_unavailable: {flag} {_ext.redact(claim)} names {task_id} only in "
                f"its {ref['corroborated_by']}, which is a mention, not a tie to THIS "
                f"card's work",
                hint=True,
            )
        if claim_unbound:
            # Record WHO authorised an unbound claim and WHY it is unbound:
            # _record replays the survivor into the task's event log, so the
            # authorisation is auditable.
            ref = dict(ref, unbound=True, claimed_by=_claimant())
            _log.warning("Unbound survivor accepted for task %s by %s (%s): %s",
                         task_id, ref["claimed_by"],
                         ref.get("corroborated_by") or "operator override",
                         _ext.redact(claim))
        if key is None:
            # An unqualified claim means "the one lost repository", so two
            # of them are ambiguous in exactly the way the qualified/
            # unqualified mix below is. Before the flags became repeatable
            # this was unreachable (one value each), and keeping the first
            # silently recorded A as the provenance for a repository whose
            # survivor may have been B -- a false entry in the recovery
            # index, with no diagnostic, after a remote verification had
            # already been spent on both.
            if unqualified is not None:
                raise SurvivorUnavailable(
                    "survivor_unavailable: two unqualified operator survivors are ambiguous; "
                    "qualify each claim as <repository>=<claim>"
                )
            unqualified = ref
            continue
        if key in resolved:
            raise SurvivorUnavailable(
                f"survivor_unavailable: two operator survivors name the same repository ({key})"
            )
        resolved[key] = ref
    if unqualified is not None:
        if resolved:
            raise SurvivorUnavailable(
                "survivor_unavailable: mixing qualified and unqualified operator survivors is "
                "ambiguous; qualify every claim as <repository>=<claim>"
            )
        return {None: unqualified}
    return resolved


#: Corroboration that is only a MENTION of the card, never a tie to its work.
_WEAK_CORROBORATION = frozenset({"title", "body"})


def _refusal(message, *, hint=False):
    """Build the refusal, keeping the override out of the PERSISTED text.

    ``preserve`` writes the refusal to ``held_reason`` and to a
    ``workspace_held`` event, and ``kanban_show`` replays events to a worker's
    model. A hint baked into the message therefore reaches readers that the
    raising process never inspected -- including a worker redispatched onto a
    card an OPERATOR refused earlier. So the flag name is not in the string at
    all; it rides on the exception and is rendered at the CLI boundary, where
    the environment being tested belongs to the caller actually reading it.
    """
    exc = SurvivorUnavailable(message)
    if hint:
        exc.override_hint = (
            "re-run with --survivor-unbound if the work really did land on an "
            "unrelated-looking branch"
        )
    return exc


def render_override_hint(exc):
    """Append an exception's override hint for a caller entitled to read it.

    A worker's environment carries the dispatcher's grant, so treat the mere
    PRESENCE of that grant as "someone other than an operator is reading",
    without asking whether the grant belongs to this process: for a hint,
    unlike for an authority check, over-suppressing costs only an operator one
    ``--help``.
    """
    hint = getattr(exc, "override_hint", "")
    if not hint or any(
        os.environ.get(key) for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID")
    ):
        return str(exc)
    return f"{exc}; {hint}"


def _claimant():
    """Attribute an unbound claim to the OS user, not to a chosen string.

    ``getpass.getuser()`` consults ``LOGNAME``/``USER``/``LNAME``/``USERNAME``
    before the passwd database, so anything that can invoke the CLI can also
    choose the name recorded against a workspace delete -- including a worker
    writing ``claimed_by="operator"``. That is not attribution; the whole
    safety argument for the override is that the authorisation is auditable.
    Resolve the real uid instead, and record the number alongside the name so
    the audit trail survives a host with no passwd entry at all.
    """
    uid = os.getuid() if hasattr(os, "getuid") else None
    if uid is None:
        return "unknown"
    try:
        import pwd

        return f"{pwd.getpwuid(uid).pw_name} (uid {uid})"
    except (KeyError, OSError, ImportError):  # containers, CI, Windows
        return f"uid {uid}"


def _bound(survivor):
    """True when EVERY ref in ``survivor`` is bound to the card.

    The single place "is this survivor standing delete authority?" is decided,
    so the completion path and the reclamation path cannot answer it
    differently -- which is exactly what happened: :func:`_reusable` guarded
    `cleanup=True` against a RECORDED row, and an unbound-by-MENTION claim
    reached the completion path through :func:`_external`'s ``explicit`` arm,
    which `_reusable` never sees.

    The ``isinstance`` guard is not defensive decoration. Every other reader of
    ``previous["refs"]`` in this module applies it (``_unshrunk``, the cleanup
    carry-forward, ``_merge_refs``, ``_vouched_repositories``) because a
    recorded row can hold a non-dict entry -- a hand-edited board, a partially
    written or legacy row, a future writer. ``ref.get(...)`` on one raises
    ``AttributeError``, which is NOT in ``preserve()``'s ``except`` tuple nor
    in ``remove_workspace_dir``'s, so it escapes past ``_hold()`` and skips the
    fail-closed contract entirely -- the same class of outage as the ``_repos``
    PermissionError incident. A malformed entry vouches for nothing, so it is
    treated as UNBOUND rather than skipped: fail closed on junk.
    """
    refs = (survivor or {}).get("refs") or ()
    return not any(not isinstance(ref, dict) or ref.get("unbound") for ref in refs)


def _reusable(previous):
    """Reclamation may reuse a RECORDED survivor only if it was bound to the card.

    This is the bound on the override, and the reason it needs no identity
    check. An unbound claim is a human assertion that work landed somewhere the
    kernel cannot corroborate; ``preserve(cleanup=True)`` does not re-verify,
    it reuses whatever completion recorded. So an unbound claim recorded once
    would otherwise become STANDING authority to discard this workspace on
    every later reclamation, without the claim ever being re-tested.

    Measured on the workspace-missing reclamation branch, where ``previous`` is
    the SOLE authority (kanban card t_de2e348e, probe_r2_branches.py, P1): an unbound
    recorded claim HELDs, a bound one is REUSED, and no claim HELDs -- so the
    gate discriminates on exactly the field it names and can still say yes.

    Refusing the reuse cuts the override down to what the legitimate operator
    case needs -- closing a card whose workspace is already gone -- and keeps
    the fail-closed HOLD otherwise. A caller who wants a workspace discarded
    must re-assert against the tree in front of them rather than inherit a
    stale authorisation.
    """
    return previous if _bound(previous) else None


def _loose_files(workspace, repos):
    """True when the workspace holds entries outside every repository.

    A remote ref vouches only for the repositories it was resolved from; files
    beside them (notes, scripts, a tarball) are captured by nothing, so an
    inferred survivor must never trade them for a delete.

    Bounded by the same budget as `_repos` for the same reason, and pruned by
    the same derived-directory set -- a `node_modules` tree is not evidence a
    survivor has to vouch for, and descending into one is what made this walk
    unbounded.
    """
    budget = _walk_budget()
    visited = 0
    stack = [Path(workspace)]
    while stack:
        here = stack.pop()
        visited += 1
        if visited > budget:
            raise _over_budget("loose-file scan", workspace, visited, budget)
        if here in repos:
            continue
        try:
            with os.scandir(here) as it:
                entries = list(it)
        except PermissionError as exc:
            log.warning("kanban survivor: skipping unreadable dir %s (%s)", here, exc.strerror)
            continue
        children = []
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                # Unreadable metadata cannot be classified as "no evidence".
                return True
            if entry.is_symlink():
                # A symlink is an entry no repository's ref vouches for. The
                # previous form reported it via `is_symlink` on the child; the
                # scandir form has to test it before the directory branch, or a
                # symlink to a directory would be silently skipped.
                return True
            if is_dir:
                if entry.name not in _DERIVED_DIRS:
                    children.append(Path(entry.path))
                continue
            return True
        stack.extend(children)
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

    The recorded survivor is filtered through :func:`_reusable`: an UNBOUND
    claim authorised one completion and is not standing authority to delete a
    workspace a later reclamation can still see. This is the single place
    ``previous`` becomes a delete authorisation, so the bound lives here rather
    than at the two ``preserve`` call sites that pass it.
    """
    if explicit:
        return {"kind": "ref", "refs": [dict(ref, repository=key or ".")
                                        for key, ref in sorted(explicit.items(), key=lambda i: i[0] or "")]}
    if cleanup:
        return _reusable(previous)
    ref = _ext.discover(conn, task_id, metadata, evidence, urls) if discover else None
    return {"kind": "ref", "refs": [dict(ref, repository=".")]} if ref else None


_PATCH_KEYS = ("path", "sha256", "bytes", "sidecar")

def _carries_recovery_artifact(survivor):
    """Does this row's OWN sidecar stand beside recoverable bytes?

    `sidecar` alone does not make a row a recovery pointer. Two kinds write an
    `implementation.json` that is pure METADATA -- `landed` (which repo/sha a
    live canonical tree holds the work at) and the canonical `ref` arm (which
    LIVE tree holds a sha the published remote does not carry). Neither stores
    a byte of the implementation: the durable copy is the named repository.

    A genuine recovery row does store bytes, and always alongside a pointer to
    them -- `path` for a concatenated `implementation.patch`, `bundles` for
    git bundles. Discriminating on the ARTIFACT rather than on the `kind`
    label covers both new metadata-only kinds at once and keeps covering any
    later one, and it cannot be fooled by a row `_unshrunk` has already
    relabelled.

    Getting this wrong corrupts the durable recovery index rather than merely
    mislabelling it: a metadata sidecar treated as a patch makes `_unshrunk`
    relabel the row `kind: "patch"` with `notice: "NOT PUSHED"` and a
    `patches` list of JSON manifests holding no patch bytes, after the
    workspace has already been deleted (2026-09-23 review of #796).
    """
    entry = survivor or {}
    return bool(entry.get("path") or entry.get("bundles"))


def _patch_pointers(survivor):
    """Every stored patch a survivor row points at, top-level slot first.

    A row carries ONE patch in its top-level `path`/`sha256`/`bytes`/`sidecar`
    slot, because a capture concatenates every repository's diff into a single
    `implementation.patch`. Two captures cannot share that slot, so a row that
    must retain a patch from an earlier capture as well keeps the extras in
    `patches`. Reading through one accessor keeps every consumer -- the
    non-shrink guard and the sidecar manifest scan -- seeing all of them.

    The top-level slot is gated by :func:`_carries_recovery_artifact` so a
    metadata-only `landed`/canonical-`ref` sidecar is not mistaken for stored
    patch bytes. Entries already displaced into `patches` are NOT re-gated:
    they were vetted when they were displaced, and a bundle row's pointer
    legitimately carries only a `sidecar` once its `bundles` have been merged
    into the fresh row.
    """
    pointers = []
    row = survivor if isinstance(survivor, dict) else {}
    for index, entry in enumerate((row, *(row.get("patches") or ()))):
        if not isinstance(entry, dict):
            continue
        pointer = {key: entry.get(key) for key in _PATCH_KEYS}
        if index == 0 and not _carries_recovery_artifact(entry):
            continue
        if pointer["path"] or pointer["sidecar"]:
            pointers.append(pointer)
    return pointers


def _sidecar_repositories(survivor):
    """Repository keys a stored PATCH accounts for, read from its own manifest.

    A patch keys no repository in the recorded row -- only the
    `implementation.json` sidecar this module writes beside it carries the
    `repositories` list. Excluding patches entirely made an already-completed
    card re-HOLD forever at reclamation: `absent` stayed empty, the relaxation
    never ran, `missing` survived and the `if not explicit` raise fired on a
    claim that a patch had already honoured -- and it is precisely the
    uncommitted-work case where the patch is the ONLY copy.

    Read the manifest rather than letting a patch vouch for anything: an
    unreadable or malformed sidecar vouches for NOTHING, so the guard that
    gates the relaxation stays keyed and fails closed.
    """
    path = (survivor or {}).get("sidecar")
    if not path:
        return set()
    try:
        manifest = json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        return set()
    return {entry["repository"] for entry in (manifest.get("repositories") or ())
            if isinstance(entry, dict) and isinstance(entry.get("repository"), str)}


def _all_sidecar_repositories(survivor):
    """`_sidecar_repositories` over EVERY patch pointer the row carries.

    A patch displaced into `patches` still vouches for the repositories its
    own manifest names -- that is the whole reason it was kept -- so the
    non-shrink comparison and the `missing <= absent` coverage test must read
    it too, or retaining the pointer would buy nothing.
    """
    keys = set()
    for pointer in _patch_pointers(survivor):
        keys |= _sidecar_repositories(pointer)
    return keys


def _vouched_repositories(survivor):
    """Repository keys a RECORDED survivor actually accounts for.

    A survivor vouches for a repository through a ref (remote-verified, or an
    operator flag), through a stored bundle, or through a stored patch whose
    sidecar manifest names it.
    """
    keys = set()
    for shape in ("refs", "bundles"):
        for entry in ((survivor or {}).get(shape) or ()):
            if isinstance(entry, dict) and isinstance(entry.get("repository"), str):
                keys.add(entry["repository"])
    return keys | _all_sidecar_repositories(survivor)


def _patch_carry(previous, survivor):
    """Recorded patch pointers the fresh row does not already hold.

    Identity is the stored path (or the sidecar's, for a bundle row that has
    only that), so re-recording an unchanged row carries nothing and a row
    cannot accumulate duplicate pointers to one artifact across repeated
    reclamation passes.
    """
    held = {p[key] for p in _patch_pointers(survivor) for key in ("path", "sidecar") if p[key]}
    return [p for p in _patch_pointers(previous)
            if not any(p[key] in held for key in ("path", "sidecar") if p[key])]


def _unshrunk(previous, survivor):
    """No write of the survivor row may shrink the recovery index.

    `_record()` does `SET survivor = excluded.survivor`, so every write stores
    exactly what its caller captured. The repository-keyed carry-forward in
    `preserve()` only fires for repositories ABSENT from disk, so it cannot
    save either of the two shapes a re-capture can silently drop for a repo
    that is STILL there:

    - a PATCH keys no repository in the recorded row at all (only the
      `implementation.json` sidecar's `repositories` list does), and
    - a recorded BUNDLE for a repository whose re-capture now finds a
      published base, so it emits a ref/patch and `bundles` comes back empty.

    Either way the row would be relabelled `kind: "ref"` -- "everything is
    pushed" -- while the only copy of that unpushed work is an orphaned
    attachment. Carry both across instead.

    A fresh capture that produced its OWN patch occupies the single top-level
    `path`/`sha256`/`bytes`/`sidecar` slot -- any surviving repository being
    dirty is enough -- so a recorded pointer cannot simply be merged into it:
    testing `not survivor.get(key)` dropped exactly the case where the recorded
    patch is the only copy of a VANISHED repository's work. Keep BOTH, with
    the already-recorded pointer holding the slot: its path and sha256 were
    published in the completion's own `result` line and are what `kanban_db`
    reports, so relocating it would break a pointer already handed out, while
    the fresh one has not been published yet. The displaced pointers live in
    `patches`, which `_patch_pointers()` reads back, so a colliding slot costs
    an entry in a list rather than the artifact.
    """
    if not survivor or not previous:
        return survivor
    carry = _patch_carry(previous, survivor)
    # The recorded pointers keep the slot in recorded order, then the fresh
    # ones; `carry` is empty whenever the fresh row already holds everything
    # the previous row did, and this is then a no-op.
    union = carry + _patch_pointers(survivor) if carry else []
    carried = {k: v for k, v in union[0].items() if v} if union else {}
    displaced = union[1:]
    kept = {b.get("repository") for b in (survivor.get("bundles") or ())
            if isinstance(b, dict)}
    bundles = [b for b in (previous.get("bundles") or ())
               if isinstance(b, dict) and b.get("repository") not in kept]
    # A repository the fresh row does not name at all is one this write would
    # drop. The fresh entry always wins for a repository BOTH name -- it was
    # derived from the checkout as it is now, and `_merge_refs()` resolves the
    # same way -- so this can only extend.
    refs = [r for r in (previous.get("refs") or ())
            if isinstance(r, dict) and r.get("repository") not in _vouched_repositories(survivor)]
    if not carried and not bundles and not refs:
        return survivor
    if carried:
        # Replace the slot wholesale: a partial overwrite would leave the
        # fresh capture's `sha256` beside the recorded `path`, i.e. a pointer
        # that fails its own integrity check.
        survivor = {k: v for k, v in survivor.items() if k not in _PATCH_KEYS}
        survivor.update(carried)
    else:
        survivor = dict(survivor)
    if carried or bundles:
        survivor["notice"] = "NOT PUSHED"
    if displaced:
        survivor["patches"] = [{k: v for k, v in pointer.items() if v}
                               for pointer in displaced]
    elif carried:
        survivor.pop("patches", None)
    if bundles:
        survivor["bundles"] = list(survivor.get("bundles") or ()) + bundles
    if refs:
        survivor["refs"] = list(survivor.get("refs") or ()) + refs
    if survivor.get("bundles"):
        survivor["kind"] = "bundle"
    elif _patch_pointers(survivor):
        survivor["kind"] = "patch"
    return survivor


def _merge_refs(fresh, recorded):
    """One entry per repository, preferring the FRESHLY derived one.

    The carried entries seed `refs` and are re-added by the external merge on
    the `elif claimed:` arm, so a repository could be recorded twice -- and
    when the re-capture also derived a current ref for a repository the
    recorded survivor knew at a stale SHA, first-match resolution picked the
    stale one.
    """
    seen = {ref.get("repository") for ref in fresh if isinstance(ref, dict)}
    return list(fresh) + [ref for ref in recorded
                          if not isinstance(ref, dict) or ref.get("repository") not in seen]


def _record(conn, task_id, survivor, previous):
    """The ONLY writer of the survivor column -- and therefore the only place
    the non-shrink invariant can be enforced for every exit at once.

    `_unshrunk()` used to be applied at the call sites, and only on the
    `cleanup` pass. Three of `preserve()`'s exits write this row and two of
    them were unguarded: re-completing a card (`cleanup=False`) whose recorded
    patch covered a now-published repository, and the workspace-MISSING exit,
    which records the operator's `explicit` ref and dropped the recorded
    patch, bundles and other repositories' refs outright. The invariant is not
    "reclamation may not shrink the index" but "nothing may", so it belongs on
    the write, not on one caller's pass.
    """
    survivor = _unshrunk(previous, survivor)
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
        landed = (metadata or {}).get("landed")
        if cleanup and not landed and previous and previous.get("kind") == "landed":
            # Revalidate the recorded live commits before removing the workspace.
            landed = [{"repo_path": entry["repository"], "sha": entry["sha"]}
                      for entry in previous["landed"]]
        if landed:
            if not isinstance(landed, list):
                raise SurvivorUnavailable("survivor_unavailable: landed must be a list")
            survivor = {"kind": "landed", "landed": _verify_landed(landed, workspace)}
            present = {entry["repository"] for claim in survivor["landed"]
                       for entry in claim["workspace_repositories"]}
            missing = set(bases) - present
            if missing:
                # A landed commit binds only repositories still on disk. A
                # replacement root can ignore the files of a vanished child
                # repository and appear clean while its work is still here.
                refs = {}
                if cleanup and _reusable(previous):
                    refs = {ref["repository"]: ref for ref in (previous or {}).get("refs", ())
                            if isinstance(ref, dict) and ref.get("repository") in missing}
                if explicit:
                    if None in explicit:
                        if len(missing) != 1 or len(explicit) != 1:
                            raise SurvivorUnavailable(
                                "survivor_unavailable: qualify each missing repository survivor"
                            )
                        explicit = {next(iter(missing)): explicit[None]}
                    if set(explicit) - missing:
                        raise SurvivorUnavailable(
                            "survivor_unavailable: operator survivors name repositories still present"
                        )
                    refs.update({key: dict(ref, repository=key) for key, ref in explicit.items()})
                if missing - refs.keys():
                    raise SurvivorUnavailable(
                        f"survivor_unavailable: recorded repository missing; {_ext.HINT}"
                    )
                survivor["refs"] = [refs[key] for key in sorted(missing)]
            survivor["sidecar"] = _store(
                conn, task_id, "implementation.json",
                json.dumps(survivor, sort_keys=True).encode(), "application/json",
            )["path"]
            return _record(conn, task_id, survivor, previous)
        # Git's registries answer "is there a nested repository here" in O(refs).
        # For the `dir`-workspace-rooted-at-a-home shape this is the whole fix:
        # that tree ALWAYS reaches the nested-repository refusal below, so the
        # walk spent 227 s (measured on ~/.hermes: 850,853 directories) deriving
        # a verdict Git could state in 1.2 s. Asking first turns a silent hang
        # into an immediate, legible refusal.
        #
        # Only a positive short-circuits: the absence of a registered nested
        # repo proves nothing (a hand-made clone is in neither registry), so
        # that case falls through to the bounded walk exactly as before.
        registered = _registered_nested(workspace)
        if registered:
            raise SurvivorUnavailable(
                "survivor_unavailable: nested repository requires separate recovery "
                f"({str(registered[0].relative_to(workspace))})"
            )
        repos = _repos(workspace)
        if any(a != b and a.is_relative_to(b) for a in repos for b in repos):
            # A patch cannot add a gitlink and files below the same path.
            raise SurvivorUnavailable("survivor_unavailable: nested repository requires separate recovery")
        keys = {str(r.relative_to(workspace)) for r in repos}
        carried, carried_bundles = [], []
        missing = set(bases) - keys
        if cleanup:
            # Reclamation is not a second chance to re-derive evidence. The
            # survivor recorded at completion is NEWER and better than the
            # checkout that has since vanished, and `bases` is never rewritten
            # on success -- so re-running this check on the cleanup pass
            # re-raised on a claim that had already been honoured, and `_hold()`
            # overwrote the `held_reason` `_record()` had just cleared. A card
            # completed via a verified `--survivor-pr` was left `done` and
            # permanently HELD, citing the very remedy the operator had used.
            #
            # Key the carry-forward on what the RECORDED survivor vouches for
            # and disk no longer holds, not on `bases`. The re-capture below can
            # only see repositories that still exist and `_record()` overwrites
            # the row with exactly what it captured, so anything absent is
            # dropped from the recovery index unless carried across -- and
            # `bases` is written once, before dispatch, so it does not know
            # about a repository the worker cloned afterwards.
            absent = _vouched_repositories(previous) - keys
            # `missing <= absent` is the coverage test: every repository `bases`
            # says vanished must be one the recorded survivor actually accounts
            # for. A survivor vouching for some OTHER repository buys nothing.
            if absent and missing <= absent:
                if _loose_files(workspace, repos):
                    # The relaxation may honour a satisfied claim; it may never
                    # buy a delete for evidence no survivor covers. On a PARTIAL
                    # loss the surviving repos resolve their own refs and the
                    # capture returns before the `elif claimed:` arm below --
                    # the one and only other `_loose_files()` call site -- so the
                    # guard has to be applied on THIS exit too, or reclamation
                    # `rmtree`s loose files that previously forced a HOLD.
                    raise SurvivorUnavailable(
                        "survivor_unavailable: workspace holds files outside any repository "
                        f"that no inferred survivor vouches for; {_ext.HINT}"
                    )
                if repos:
                    # Same reason as the explicit branch below: on a PARTIAL
                    # loss the surviving repos would satisfy the completion on
                    # their own and silently drop the recorded survivor for the
                    # one that is gone. A bundle vouches just as a ref does
                    # (see `_vouched_repositories`), so it must be carried too
                    # -- otherwise `_record()` rewrites the row as `kind: ref`
                    # for a repo whose only unpushed history is that bundle.
                    # With NO repo left the `elif claimed:` arm reinstates the
                    # whole recorded survivor on its own; seeding here too would
                    # record a second copy of every carried entry.
                    carried = [ref for ref in (previous or {}).get("refs") or ()
                               if isinstance(ref, dict) and ref.get("repository") in absent]
                    carried_bundles = [b for b in (previous or {}).get("bundles") or ()
                                       if isinstance(b, dict) and b.get("repository") in absent]
                missing = set()
                # `bases` still attests that code work was expected here. Keep
                # the claim so an empty in-tree capture routes through the
                # `_loose_files()` guard below rather than the silent
                # no-survivor `else` -- which would both erase the recorded
                # survivor and hand loose, unvouched evidence to the reaper.
                claimed = True
        if missing:
            # The repository recorded at dispatch is gone while the directory
            # survived (a reaped clone leaving evidence behind). Without an
            # operator survivor this must stay fail-closed: it is what protects
            # unpushed implementation work. But the workspace-MISSING branch
            # above already treats a remote-verified `--survivor-pr`/`ref` as
            # authority, and an operator-named, remote-verified survivor is
            # strictly better evidence than the checkout we lost -- so consult
            # it here too, or this state has no reachable remedy at all.
            if not explicit:
                raise SurvivorUnavailable(
                    f"survivor_unavailable: recorded repository missing; {_ext.HINT}"
                )
            uncovered = missing - set(explicit)
            if None in explicit:
                # The historical unqualified shape names exactly one remote, so
                # it can only stand for a single lost repository. Stamping it
                # onto each of them would record provenance that is false for
                # all but one -- the same recovery-index corruption as dropping
                # an entry, written deliberately.
                if len(missing) > 1:
                    raise SurvivorUnavailable(
                        "survivor_unavailable: one operator survivor cannot vouch for multiple "
                        f"missing repositories ({', '.join(sorted(missing))}); qualify each claim "
                        f"as <repository>=<claim>, e.g. {_qualified_hint(missing)}"
                    )
                explicit = {sorted(missing)[0]: explicit[None]}
            else:
                if uncovered:
                    # Qualified claims are per-repository provenance: every vanished
                    # repository needs its own, or the claim is incomplete and the
                    # uncovered ones would be silently dropped from the index.
                    raise SurvivorUnavailable(
                        "survivor_unavailable: no operator survivor names the missing "
                        f"repositories ({', '.join(sorted(uncovered))}); qualify each claim "
                        f"as <repository>=<claim>, e.g. {_qualified_hint(uncovered)}"
                    )
                # ...and the converse, which the coverage test alone does not
                # catch: a qualifier naming a repository that is STILL on disk
                # (a plausible reading of the "qualify every claim" remedy the
                # refusals above print). `carried` stamps every key in
                # `explicit`, so the capture loop would then derive its own ref
                # for that repo and record TWO contradictory provenances for
                # it -- and this exit does not pass through `_merge_refs()`.
                # An operator claim is authority about a repository we LOST; it
                # is not a licence to overwrite one we can still read.
                intruding = set(explicit) - missing
                if intruding:
                    raise SurvivorUnavailable(
                        "survivor_unavailable: operator survivors name repositories that are "
                        f"still present ({', '.join(sorted(intruding))}); qualify only the "
                        f"missing ones ({', '.join(sorted(missing))})"
                    )
            # A vanished recorded repository is an unmet claim. Force the
            # external path below so the verified survivor is actually recorded,
            # and so failing to produce one still HOLDS rather than completing
            # with no survivor at all. `carried` additionally puts it onto
            # the in-tree paths: when only SOME recorded repos vanished, the
            # survivors of the rest would otherwise satisfy the completion on
            # their own and silently drop the operator's ref for the lost one.
            claimed = True
            if repos:
                # Only a PARTIAL loss needs this. With no repo left at all the
                # external path below records `explicit` on its own; seeding it
                # here too would duplicate the ref.
                carried = [dict(ref, repository=key) for key, ref in sorted(explicit.items())]
        patches, refs, bundles, repositories = [], list(carried), list(carried_bundles), []
        for repo in repos:
            key = str(repo.relative_to(workspace))
            try:
                ref, base, data = _capture(repo, key, workspace)
            except SurvivorUnavailable:
                # Every object-reading step above (`status`, `diff`, and
                # `_snapshot`'s `git bundle` inside pack-objects) raises the
                # bare constant. Classify it before it escapes: if this repo's
                # store is broken, say WHICH repo, WHICH lender and what to do.
                # Anything else re-raises unchanged.
                _explain_broken_object_store(repo, key, workspace, bases)
                raise
            if ref:
                refs.append(dict(ref, repository=key))
                continue
            if base is None:
                name = f"implementation-{len(bundles)}.bundle"
                bundle = _store(conn, task_id, name, data, "application/x-git-bundle")
                bundles.append(dict(bundle, repository=key))
            elif data:
                header = f"# kanban repository={json.dumps(key)} base={base}\n".encode()
                patches.append(header + data)
            repositories.append({"repository": key, "base_sha": base})
        if repos and not bundles and len(refs) == len(repos) + len(carried):
            survivor = {"kind": "ref", "refs": refs}
            if any(ref.get("matched_by") == "canonical" for ref in refs):
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
            # Nothing in-tree to capture: the survivor must live elsewhere. An
            # inferred one vouches only for the repositories it came from, so
            # files beside them make the inference worthless -- HOLD instead.
            loose = _loose_files(workspace, repos)
            external = _external(conn, task_id, metadata, evidence, _remote_urls(repos), explicit,
                                 discover=not loose, cleanup=cleanup, previous=None if loose else previous)
            if external:
                # A `kind: "bundle"` row often carries no `refs` key at all --
                # unpushed history lives only in `bundles` -- and `KeyError` is
                # not in this function's `except` tuple, so it would escape
                # past `_hold()` and skip the fail-closed contract entirely.
                merged = dict(external, refs=_merge_refs(refs, external.get("refs") or []))
                return _record(conn, task_id, merged, previous)
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
            # Nothing in-tree, nothing claimed. With no survivor ever recorded
            # that is genuinely non-code work and None is correct. Otherwise it
            # is not, on EITHER pass: `_record()` does `SET survivor =
            # excluded.survivor`, so writing None here erases a recovery index
            # a previous completion already published and returns a
            # survivor-less verdict that lets the caller `rmtree` the
            # directory. Reclamation was guarded; the completion pass was not,
            # and it is reachable -- re-complete a card whose workspace was
            # missing (empty `bases`, `claimed` False, the dir since re-created
            # empty) and the external ref that is the only pointer to that work
            # became JSON `null` while `held_reason` was cleared. The invariant
            # is not "reclamation may not shrink the index" but "nothing may".
            survivor = previous
        return _record(conn, task_id, survivor, previous)
    except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, SurvivorUnavailable) else "survivor_unavailable: capture failed"
        # `reason` is what gets PERSISTED (held_reason + a workspace_held event
        # kanban_show replays to workers), so it must stay hint-free. The hint
        # rides the re-raised exception instead, for the CLI to render.
        _hold(conn, task_id, reason)
        raise _refusal(reason, hint=bool(getattr(exc, "override_hint", ""))) from exc


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
