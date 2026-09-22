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
from hermes_cli import kanban_external_survivor as _ext

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

    NOT every corroboration is the same strength, and the weak ones are held
    to the same bound as the override. ``verify_pr`` reports WHICH field
    answered: a head branch or a claimed SHA ties the PR's *content* to the
    card, but a substring in the PR title or body is only a MENTION -- an
    umbrella changelog, a dependency note, even "does not address t_..."
    satisfies it. Accepting a mention as a bound survivor would make it
    standing delete authority via :func:`_reusable`, which is the very thing
    this card closed. So a title/body match is recorded as an unbound claim:
    it still completes the card, and it still never buys a later delete.
    """
    for claim, flag, verify, extra in (
        (survivor_ref, "--survivor-ref", _ext.verify_ref, {}),
        (survivor_pr, "--survivor-pr", _ext.verify_pr,
         {"corroborate": ("headRefName", "title", "body")}),
    ):
        if not claim:
            continue
        # A remote that did not ANSWER is not evidence about the claim. Keep
        # the two outcomes apart at the seam: ``None`` is a verdict from the
        # remote, ``RemoteUnavailable`` is the absence of one. Collapsing them
        # let a rate limit or network blip be reported -- and persisted to
        # ``held_reason`` and the ``workspace_held`` event that ``kanban_show``
        # replays -- as the false statement "is live but does not name <card>",
        # whose offered remedy is to drop the very binding this path adds
        # (kanban card t_de2e348e, Argus round 3).
        try:
            ref = verify(claim, mined_for=None if unbound else task_id, **extra)
            if ref is None:
                # The claim is unverified and may carry a token: echo it redacted only.
                if not unbound and verify(claim, **extra) is not None:
                    raise _refusal(
                        f"survivor_unavailable: {flag} {_ext.redact(claim)} is live but does not "
                        f"name {task_id}, so it is not evidence of THIS card's work",
                        hint=True,
                    )
                raise SurvivorUnavailable(
                    f"survivor_unavailable: could not verify {flag} {_ext.redact(claim)} "
                    f"against the remote"
                )
        except _ext.RemoteUnavailable as exc:
            raise SurvivorUnavailable(
                f"survivor_unavailable: could not verify {flag} {_ext.redact(claim)} "
                f"against the remote ({_ext.redact(str(exc))})"
            ) from exc
        if unbound or ref.get("corroborated_by") in _WEAK_CORROBORATION:
            # Record WHO authorised an unbound claim and WHY it is unbound:
            # _record replays the survivor into the task's event log, so the
            # authorisation is auditable.
            ref = dict(ref, unbound=True, claimed_by=_claimant())
            _log.warning("Unbound survivor accepted for task %s by %s (%s): %s",
                         task_id, ref["claimed_by"],
                         ref.get("corroborated_by") or "operator override",
                         _ext.redact(claim))
        return ref
    return None


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
    refs = (previous or {}).get("refs") or ()
    return previous if not any(ref.get("unbound") for ref in refs) else None


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

    The recorded survivor is filtered through :func:`_reusable`: an UNBOUND
    claim authorised one completion and is not standing authority to delete a
    workspace a later reclamation can still see. This is the single place
    ``previous`` becomes a delete authorisation, so the bound lives here rather
    than at the two ``preserve`` call sites that pass it.
    """
    if explicit:
        ref = explicit
    elif cleanup:
        return _reusable(previous)
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
        recovered = None
        if set(bases) - keys:
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
            # A vanished recorded repository is an unmet claim. Force the
            # external path below so the verified survivor is actually recorded,
            # and so failing to produce one still HOLDS rather than completing
            # with no survivor at all. `recovered` additionally carries it onto
            # the in-tree paths: when only SOME recorded repos vanished, the
            # survivors of the rest would otherwise satisfy the completion on
            # their own and silently drop the operator's ref for the lost one.
            claimed = True
            if repos:
                # Only a PARTIAL loss needs this. With no repo left at all the
                # external path below records `explicit` on its own; seeding it
                # here too would duplicate the ref.
                recovered = dict(explicit, repository=sorted(set(bases) - keys)[0])
        patches, refs, bundles, repositories = [], [recovered] if recovered else [], [], []
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
        if repos and len(refs) == len(repos) + (1 if recovered else 0):
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
