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
        # Git stderr can contain credential-bearing remote URLs, so it is never
        # persisted: the raised message stays constant and the detail goes to
        # the log, redacted by the same helper that guards an echoed claim.
        #
        # Without this, every occurrence costs an attribution pass. A failure
        # that is purely environmental (t_169d6e46: a concurrent pytest session
        # deleting this repo's tmp_path, so git exits 128 "cannot change to
        # '<path>': No such file or directory") is indistinguishable from a real
        # capture defect once it reaches the caller as a bare
        # "survivor_unavailable: git inspection failed".
        log.warning(
            "kanban survivor: git %s failed rc=%s in %s: %s",
            args[0] if args else "?", result.returncode, _ext.redact(str(repo)),
            _ext.redact(result.stderr.decode("utf-8", "replace").strip()),
        )
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
    return f"--survivor-pr {sorted(keys)[0]}=owner/repo#N"


def _verified_explicit(survivor_ref, survivor_pr):
    """Operator-named survivors are claims: verify each or refuse the completion.

    Returns ``{repository-key-or-None: ref}``. Both flags are repeatable and
    each value may be qualified as ``<repo>=<claim>`` so a multi-repository
    loss has a reachable remedy that records TRUE provenance per repository.
    An unqualified claim maps to ``None`` -- "the one lost repository" -- and
    is resolved by the caller, which is the only place that knows which one.
    """
    resolved, unqualified = {}, None
    for claims, flag, verify in (
        (survivor_ref, "--survivor-ref", _ext.verify_ref),
        (survivor_pr, "--survivor-pr", _ext.verify_pr),
    ):
        for claim in (claims if isinstance(claims, (list, tuple)) else [claims] if claims else []):
            if not claim:
                continue
            key, value = _split_qualifier(claim)
            ref = verify(value)
            if ref is None:
                # The claim is unverified and may carry a token: echo it redacted only.
                raise SurvivorUnavailable(
                    f"survivor_unavailable: could not verify {flag} {_ext.redact(claim)} against the remote"
                )
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
        return {"kind": "ref", "refs": [dict(ref, repository=key or ".")
                                        for key, ref in sorted(explicit.items(), key=lambda i: i[0] or "")]}
    if cleanup:
        return previous
    ref = _ext.discover(conn, task_id, metadata, evidence, urls) if discover else None
    return {"kind": "ref", "refs": [dict(ref, repository=".")]} if ref else None


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
    return keys | _sidecar_repositories(survivor)


def _unshrunk(previous, survivor):
    """Reclamation may KEEP or EXTEND the recovery index, never shrink it.

    `_record()` does `SET survivor = excluded.survivor`, so the cleanup pass
    writes exactly what it re-captured. The repository-keyed carry-forward in
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
    """
    if not survivor or not previous:
        return survivor
    carried = {key: previous[key] for key in ("path", "sha256", "bytes", "sidecar")
               if previous.get(key) and not survivor.get(key)}
    kept = {b.get("repository") for b in (survivor.get("bundles") or ())
            if isinstance(b, dict)}
    bundles = [b for b in (previous.get("bundles") or ())
               if isinstance(b, dict) and b.get("repository") not in kept]
    if not carried and not bundles:
        return survivor
    survivor = dict(survivor, **carried, notice="NOT PUSHED")
    if bundles:
        survivor["bundles"] = list(survivor.get("bundles") or ()) + bundles
    survivor["kind"] = "bundle" if survivor.get("bundles") else "patch"
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
             survivor_ref=None, survivor_pr=None, evidence=()):
    """Return a verified survivor or None for non-code work; fail closed on doubt."""
    bases, held, previous = _state(conn, task_id)
    if cleanup and held:
        raise SurvivorUnavailable(held)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise SurvivorUnavailable("survivor_unavailable: task missing")
        explicit = _verified_explicit(survivor_ref, survivor_pr)
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
        if repos and not bundles and len(refs) == len(repos) + len(carried):
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
                # A `kind: "bundle"` row often carries no `refs` key at all --
                # unpushed history lives only in `bundles` -- and `KeyError` is
                # not in this function's `except` tuple, so it would escape
                # past `_hold()` and skip the fail-closed contract entirely.
                merged = dict(external, refs=_merge_refs(refs, external.get("refs") or []))
                return _record(conn, task_id, _unshrunk(previous, merged) if cleanup else merged,
                               previous)
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
        return _record(conn, task_id, _unshrunk(previous, survivor) if cleanup else survivor,
                       previous)
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
