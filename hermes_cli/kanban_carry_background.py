"""Workspace carry with no size cap, off the dispatcher tick (t_f3a411c2; Ace 2026-11-12 09:25 "no cap").

Before this, ``kanban_worker_hosts.carry_workspace`` copied a local_files card's Studio scratch dir to the pool host
INSIDE the dispatcher tick, so ``CARRY_MAX_BYTES`` / ``CARRY_MAX_ENTRIES`` bounded the tick and a bigger dir made the
card wait. Here the tick only STARTS the carry (a background thread) and moves on; the card waits
``carry_pending:<host>`` and is claimed on a later tick once the copy has finished. Nothing is capped by size.

The carry, in order (all in the background thread):

1. census: bytes and entries of the source (no cap, no wall bound) and, in the same walk, every git checkout in it.
   A checkout whose git dir would not travel as files (``carried_gitdir_loss``: a linked worktree of a repo outside
   the workspace, a clone borrowing objects through alternates) is REBUILT on the pool host from origin plus one git
   bundle (see "checkouts whose git dir does not travel" below), never refused for it;
2. the destination is prepared (the same mkdir every placed run gets) and its free space read; ``need > free`` is the
   one size refusal left: ``No room on <host>: need N, free M``;
3. ``rsync -a --partial --delete`` over fleet ssh into a STAGING dir next to the workspace (``.carry-<leaf>``), never
   into the workspace itself (the rebuilt checkouts excluded, then rebuilt inside it). An interrupted copy leaves only the staging dir, which the next attempt on that host
   resumes; a later carry to another host removes it first.

At the claim the staging dir is moved onto the workspace path in one ssh call (``finalize``), and the existing
``workspace_carried`` event is written, so the card binds to that host as before. The Studio copy is not touched.

Events on the card: ``workspace_carry_started`` / ``workspace_carry_progress`` (at most one a minute) /
``workspace_carry_failed`` (``state``: clean | resumable | refused) / ``workspace_carried`` (with bytes, entries,
seconds and the CPU the Studio paid).

A one-shot tick (CLI dispatch, admit: no ``defer``) holds the board tick lock throughout, so it copies inline only a
dir within the old caps, each copy step under CARRY_TIMEOUT_SECONDS; a bigger one waits for the gateway's tick.

Kill switch: ROOT ``kanban.placement.carry_caps: true`` restores the in-tick carry and its two caps exactly.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import stat
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from hermes_cli.kanban_worker_pool import PoolHost, _ssh_argv
from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable

_log = logging.getLogger(__name__)

CAPS_KNOB = "carry_caps"
BACKGROUND = "background"          # the dispatcher's carry_out value for a card carried in the background
STARTED_EVENT = "workspace_carry_started"
PROGRESS_EVENT = "workspace_carry_progress"
FAILED_EVENT = "workspace_carry_failed"
REFUSED_EVENT = "workspace_carry_admission_refused"   # t_681a9c5e: admission refused the card; its carry is dropped
PENDING = "carry_pending"
NO_ROOM = "carry_no_room"
FAILED = "carry_failed"
RSYNC_IO_TIMEOUT_S = 600           # rsync --timeout: an I/O stall, never a total-time bound
FINALIZE_TIMEOUT_S = 120
RETRY_S = 120.0                    # a failed carry waits this long, named, before the next attempt
PROGRESS_EVERY_S = 60.0
FRESH_S = 120.0                    # a finished copy older than this is refreshed off the tick before the claim
WORKERS = 2
_run = subprocess.run              # the subprocess runner, read at call time (tests replace it)


def caps_on(kcfg: Optional[Mapping] = None) -> bool:
    """``kanban.placement.carry_caps`` in the ROOT config (default off). On = the rule before t_f3a411c2: the carry
    runs in the tick, bounded by CARRY_MAX_BYTES / CARRY_MAX_ENTRIES. Unreadable config reads as the default."""
    if kcfg is None:
        try:
            from hermes_cli import kanban_db as _kdb
            kcfg = _kdb._board_home_kanban_cfg()
        except Exception:
            kcfg = {}
    placement = kcfg.get("placement") if isinstance(kcfg, Mapping) else None
    val = placement.get(CAPS_KNOB, False) if isinstance(placement, Mapping) else False
    if isinstance(val, str):
        return val.strip().lower() not in ("off", "false", "no", "0", "")
    return bool(val)


def staging_path(workspace: str) -> str:
    w = workspace.rstrip("/")
    return f"{os.path.dirname(w)}/.carry-{os.path.basename(w)}"


class CarryNoRoom(WorkspaceUnavailable, RuntimeError):
    """The pool host has less free space than the carry needs. Not the card's fault: the claim is released
    uncharged (a WorkspaceUnavailable), the card waits and retries."""

    def __init__(self, host: str, need: int, free: int, staged: int):
        super().__init__(f"No room on {host}: need {need}, free {free}")
        self.host, self.need, self.free, self.staged = host, need, free, staged


class CarryRefused(RuntimeError):
    """Nothing can be carried at all (a path that is not shell-inert, a checkout with no commit whose git dir does
    not travel); the capped rule decides the card."""


def census(path: str, repos: Optional[Dict[str, int]] = None) -> Tuple[int, int]:
    """``(bytes, entries)`` of ``path``: every entry below it, symlinks counted and never followed. No cap.
    ``repos`` (t_f3a411c2 item 1): filled in the same walk with every checkout (a dir holding ``.git``, ``.`` = the
    root) -> the bytes below it outside its ``.git``, which is what a rebuild of it checks out on the pool host."""
    total = entries = 0
    pending = [(path, ".", None)]
    while pending:
        current, rel, owner = pending.pop()
        if current != path and os.path.islink(current):
            continue
        with os.scandir(current) as scan:
            batch = list(scan)
        if repos is not None and any(e.name == ".git" for e in batch):
            owner = rel
            repos.setdefault(owner, 0)
        for entry in batch:
            entries += 1
            info = entry.stat(follow_symlinks=False)
            total += info.st_size
            if repos is not None and owner is not None and entry.name != ".git":
                repos[owner] += info.st_size
            if stat.S_ISDIR(info.st_mode):
                child = entry.name if rel == "." else f"{rel}/{entry.name}"
                pending.append((entry.path, child, None if entry.name == ".git" else owner))
    return total, entries


def _children_cpu() -> float:
    try:
        import resource  # POSIX only (Windows has no resource module); the CPU figure is a record, never a gate
    except ImportError:
        return 0.0
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime


def _ssh(host: PoolHost, script: str, runner: Optional[Callable], timeout: float):
    return (runner or _run)(_ssh_argv(host, script), capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)


def remote_space(host: PoolHost, dest: str, runner: Optional[Callable] = None) -> Tuple[int, int]:
    """``(free bytes on the filesystem holding dest's parent, bytes already in its staging dir)``."""
    p, s = shlex.quote(os.path.dirname(dest.rstrip("/")) or "/"), shlex.quote(staging_path(dest))
    proc = _ssh(host, f"f=$(df -Pk -- {p} | awk 'NR==2{{print $4}}') && [ -n \"$f\" ] || exit 1; u=0; "
                      f"if [ -d {s} ]; then u=$(du -sk -- {s} | awk '{{print $1}}'); fi; echo \"$f $u\"",
                runner, 30)
    out = (getattr(proc, "stdout", "") or "").split()
    if getattr(proc, "returncode", 1) != 0 or len(out) != 2 or not all(x.isdigit() for x in out):
        raise RuntimeError(f"carry to {host.name}: free space unreadable: "
                           f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")
    return int(out[0]) * 1024, int(out[1]) * 1024


def remove_staging(host: PoolHost, workspace: str, runner: Optional[Callable] = None) -> bool:
    proc = _ssh(host, f"rm -rf -- {shlex.quote(staging_path(workspace.rstrip('/')))}", runner, 300)
    return getattr(proc, "returncode", 1) == 0


# -- checkouts whose git dir does not travel: rebuilt on the pool host (operator 16:40 11-12, item 1) ---------------
# A linked worktree of the Studio's home repo (``.git`` a file pointing into ~/.hermes/.git/worktrees) or a clone
# borrowing objects through objects/info/alternates cannot be copied as files: its git dir stays behind. Such a
# checkout is REBUILT on the pool host instead (the operator's suggested shape): a clone of its origin through the
# host's mirror, then ONE git bundle over the same fleet ssh holding every commit the Studio's origin-tracking refs do
# not have (unpushed work, the stash entries, and the staged index recorded as one commit), then HEAD, index and stash
# set from it, and only the files that differ from HEAD (modified, untracked, ignored minus regenerable caches) sent by
# rsync. The ~36,000 tracked files of a checkout never travel. Nothing is pushed anywhere: the pool host only READS
# origin. A checkout with no GitHub/local origin, or whose bundle lacks a prerequisite even after a fetch of origin,
# gets a FULL bundle (every commit it needs) and is cloned from that, so no git state is a reason to wait.

GIT_TIMEOUT_S = 600


def _git(repo: Optional[str], *args, env: Optional[dict] = None, input: Optional[bytes] = None,
         ok=(0,)) -> bytes:
    argv = ["git", "--no-optional-locks"] + (["-C", repo] if repo else []) + list(args)
    proc = subprocess.run(argv, capture_output=True, input=input, timeout=GIT_TIMEOUT_S,
                          env=env, **({} if input is not None else {"stdin": subprocess.DEVNULL}))
    if proc.returncode not in ok:
        raise RuntimeError(f"git {' '.join(args[:3])} in {repo}: rc={proc.returncode}: "
                           f"{proc.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return proc.stdout


def rebuilds_of(src: str, repos) -> Tuple[str, ...]:
    """The checkouts among ``repos`` (workspace-relative, ``.`` = the root) whose git dir would not travel with a
    file copy, shallowest first."""
    from hermes_cli import kanban_worker_hosts as _kwh
    out = [r for r in repos if _kwh.carried_gitdir_loss(src, (r,), (), nested=False) is not None]
    return tuple(sorted(out, key=lambda r: (0 if r == "." else r.count("/") + 1, r)))


def _origin_url(repo: str) -> Optional[str]:
    """The URL the pool host clones from: GitHub (normalised to https, as every pool clone is) or a local path.
    None: anything else (the checkout then rides a full bundle)."""
    from hermes_cli import kanban_worktree_port as _kwt
    raw = _git(repo, "remote", "get-url", "origin", ok=(0, 2, 128)).decode("utf-8", "replace").strip()
    m = _kwt._SLUG_RE.match(raw)
    if m:
        return f"https://github.com/{m.group(1)}.git"
    if raw.startswith("/") or raw.startswith("file://"):
        return raw
    return None


def plan_rebuild(src: str, rel: str, others=()) -> dict:
    """What the pool host needs to rebuild the checkout ``src/rel`` (read-only on the Studio): origin url (or None),
    branch, HEAD, the stash entries, the files that differ from HEAD and the paths deleted since HEAD. The bundle is
    built by :func:`make_bundle`. ``others``: the other rebuilt checkouts; one nested in this one is never listed as
    its files. Raises CarryRefused when there is nothing to rebuild from (no commit at HEAD)."""
    from hermes_cli import kanban_worker_hosts as _kwh
    repo = os.path.join(src, rel) if rel != "." else src
    head = _git(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}", ok=(0, 1)).decode().strip()
    if not head:
        raise CarryRefused(f"checkout {rel}: no commit at HEAD, and its git dir does not travel")
    # a tracked gitlink (submodule): the rebuild carries neither .git/modules nor the submodule's files, so the capped
    # rule decides such a card (t_681a9c5e)
    if any(ln.startswith(b"160000 ") for ln in _git(repo, "ls-files", "--stage", "-z").split(b"\0")):
        raise CarryRefused(f"checkout {rel}: tracks a submodule (gitlink), which a rebuild does not carry")
    branch = _git(repo, "symbolic-ref", "-q", "--short", "HEAD", ok=(0, 1)).decode().strip()
    # operator 18:55 11-12 item 1: the stash follows kanban.placement.worktree_stash_carry (default off), as in PR 253.
    # A linked worktree sees the whole shared list of the Studio home repo (other sessions' work): off, no entry rides
    # the bundle, the host's list stays empty and how many stayed on the Studio is recorded (stash_left).
    stash_left = None
    if _kwh.stash_carry_on():
        stash = _kwh.stash_entries(repo)
        if stash is None:
            raise RuntimeError(f"checkout {rel}: stash unreadable")
    else:
        stash, stash_left = (), _kwh.stash_count(repo)
    dirs = _kwh._gitfile_dirs(repo)
    common = dirs[1] if dirs else os.path.realpath(os.path.join(repo, ".git"))
    gitdir = dirs[0] if dirs else common
    exclude_file = os.path.join(common, "info", "exclude")
    nul = lambda b: [p for p in b.decode("utf-8", "surrogateescape").split("\0") if p]  # noqa: E731
    changed = nul(_git(repo, "diff", "HEAD", "--name-only", "-z", "--no-renames", "--ignore-submodules=none"))
    untracked = nul(_git(repo, "ls-files", "-z", "--others", "--exclude-standard", "--directory"))
    ignored = [p for p in nul(_git(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                                   "--directory"))
               if not _kwh.regenerable_cache(p.rstrip("/"))]
    pre = "" if rel == "." else rel + "/"
    nested = {o[len(pre):] for o in others if o != rel and (rel == "." or o.startswith(pre))}

    def _keep(p: str) -> bool:
        q = p.rstrip("/")
        return not any(q == n or q.startswith(n + "/") for n in nested)
    files = sorted({p for p in changed + untracked + ignored
                    if _keep(p) and os.path.lexists(os.path.join(repo, p.rstrip("/")))})
    deleted = sorted(p for p in changed if _keep(p) and not os.path.lexists(os.path.join(repo, p)))
    url = _origin_url(repo)
    return {"rel": rel, "repo": repo, "url": url, "nested": sorted(nested), "branch": branch, "head": head, "stash": stash,
            "stash_left": stash_left, "files": files, "deleted": deleted, "gitdir": gitdir, "common": common,
            "exclude": Path(exclude_file).read_bytes() if os.path.isfile(exclude_file) else None}


def make_bundle(plan: dict, full: bool = False) -> Tuple[bytes, str]:
    """``(bundle bytes, index commit)`` for ``plan``: HEAD, the stash entries and the staged index (one commit on
    HEAD), less every commit the checkout's ``refs/remotes/origin/*`` already have (the pool clone fetched those from
    origin) unless ``full``. Built in a throwaway bare repo that borrows the checkout's objects through an alternate:
    nothing in the checkout or its common dir is written."""
    import shutil
    import tempfile
    objects = os.path.realpath(os.path.join(plan["common"], "objects"))
    have = [] if full or not plan["url"] else [
        ln.split()[0] for ln in _git(plan["repo"], "for-each-ref", "--format=%(objectname) %(objecttype)",
                                     "refs/remotes/origin").decode().splitlines() if ln.endswith(" commit")]
    tmp = tempfile.mkdtemp(prefix="kanban-carry-")
    try:
        bare, out = os.path.join(tmp, "b.git"), os.path.join(tmp, "carry.bundle")
        _git(tmp, "init", "-q", "--bare", bare)
        # a WRITE (mode "w"), flagged as a read by the lint: policy is reads utf-8-sig, writes utf-8 (git reads no BOM)
        with open(os.path.join(bare, "objects", "info", "alternates"), "w", encoding="utf-8") as f:  # windows-footgun: ok
            f.write(objects + "\n")
        g = bare
        # a COPY of the index: write-tree may store its cache-tree back into the file it reads
        index = os.path.join(tmp, "index")
        if os.path.exists(os.path.join(plan["gitdir"], "index")):
            shutil.copyfile(os.path.join(plan["gitdir"], "index"), index)
        tree = _git(g, "write-tree", env=dict(os.environ, GIT_INDEX_FILE=index)).decode().strip()
        idx = _git(g, "-c", "user.name=kanban-carry", "-c", "user.email=kanban-carry@localhost",
                   "commit-tree", tree, "-p", plan["head"], "-m", f"kanban carry: index of {plan['rel']}"
                   ).decode().strip()
        refs = [("refs/carry/head", plan["head"]), ("refs/carry/index", idx)] + \
            [(f"refs/carry/stash/{i}", e[0]) for i, e in enumerate(plan["stash"])]
        for ref, sha in refs:
            _git(g, "update-ref", ref, sha)
        spec = "\n".join([r for r, _s in refs] + [f"^{h}" for h in have]) + "\n"
        _git(g, "bundle", "create", out, "--stdin", input=spec.encode())
        return Path(out).read_bytes(), idx
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


NEEDS_FULL_RC = 10


def rebuild_script(dest: str, plan: dict, idx: str, mirror: Optional[str]) -> str:
    """Remote sh, the bundle on stdin. Clone origin at ``dest`` (through the mirror; a dir that already is a clone of
    that origin is reused, so a refresh fetches nothing new), or with no origin clone the (full) bundle itself; then
    unbundle, put HEAD / index / stash where the Studio has them, and PROVE HEAD and the stash list (rc 5 / rc 8).
    rc 10: the bundle's prerequisites are missing even after one fetch of origin (the caller resends it full)."""
    d = shlex.quote(dest)
    head = shlex.quote(plan["head"])
    url = plan["url"]
    m = None
    if mirror:
        m = ('"$HOME"/' + shlex.quote(mirror[2:])) if mirror.startswith("~/") else shlex.quote(mirror)
    if url:
        # the mirror's own refspec (refs/*:refs/*) is refused whole when another workspace on the host has one of its
        # branches checked out (t_e38aba52 23:26, operator 23:40 item A), leaving the mirror stale: warm it with
        # origin's default branch only, into a ref no worktree can have checked out
        get = ((f"if [ -d {m}/objects ]; then git -c gc.auto=0 -c fetch.prune=false --git-dir={m} fetch -q "
                f"--no-write-fetch-head origin +HEAD:refs/carry/origin-head || true; fi; "
                f"git clone -q --no-checkout --reference-if-able {m} --dissociate \"$url\" \"$d\"; ") if m else
               "git clone -q --no-checkout \"$url\" \"$d\"; ")
        # an origin the host cannot clone (a Studio-local path, t_681a9c5e item 6): an empty repo instead; the bundle
        # check below then exits NEEDS_FULL_RC and the caller resends the full bundle, which needs no origin
        get = f"if ! {{ {get}}}; then rm -rf -- \"$d\"; git init -q \"$d\"; fi; "
        reuse = "[ -d \"$d/.git\" ] && [ \"$(git -C \"$d\" remote get-url origin 2>/dev/null)\" = \"$url\" ]"
    else:
        get = "git init -q \"$d\"; "   # no origin to clone: the full bundle carries every commit
        reuse = "false"
    checkout = (f"git checkout -q -f -B {shlex.quote(plan['branch'])} {head}; " if plan["branch"]
                else f"git checkout -q -f --detach {head}; ")
    stores = "".join(f"git -c user.name=kanban-carry -c user.email=kanban-carry@localhost stash store -q -m "
                     f"{shlex.quote(e[2])} {e[0]}; " for e in reversed(tuple(plan["stash"])))
    want = shlex.quote(" ".join(e[0] for e in plan["stash"]))
    exclude = ""
    if plan.get("exclude") is not None:
        exclude = (f"mkdir -p .git/info; printf %s {shlex.quote(plan['exclude'].decode('utf-8', 'surrogateescape'))}"
                   " > .git/info/exclude; ")
    return ("set -e; " + (f"export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0={m}; "
                          if m else "") +
            f"d={d}; url={shlex.quote(url or '')}; t=$(mktemp); trap 'rm -f \"$t\"' EXIT; cat >\"$t\"; "
            f"if {reuse}; then :; else rm -rf -- \"$d\"; mkdir -p -- \"$(dirname \"$d\")\"; {get}fi; cd \"$d\"; "
            "if ! git bundle verify -q \"$t\" >/dev/null 2>&1; then git fetch -q origin >/dev/null 2>&1 || true; "
            # what verify said (the prerequisite commits the host lacks) is the full_reason (operator 23:40 item B)
            "v=$(git bundle verify \"$t\" 2>&1 >/dev/null) || { printf '\\nneed-full: %s\\n' \"$(echo $v)\" >&2; "
            f"exit {NEEDS_FULL_RC}; }}; fi; "
            "git bundle unbundle \"$t\" >/dev/null; " + checkout +
            f"git reset -q --hard {head}; git clean -fdxq; git read-tree {shlex.quote(idx)}; "
            "git stash clear; " + stores + exclude +
            f"[ \"$(git rev-parse HEAD)\" = {head} ] || exit 5; "
            "have=$(git stash list --format=%H | tr '\\n' ' ' | sed 's/ $//'); "
            f"[ \"$have\" = {want} ] || exit 8")


def _err(proc) -> str:
    e = getattr(proc, "stderr", b"") or b""
    return (e.decode("utf-8", "replace") if isinstance(e, bytes) else str(e)).strip()[:200]


def carry_rebuild(host: PoolHost, staging: str, plan: dict, runner: Callable, rsync_base: list,
                  timeout: Optional[float]) -> Dict[str, object]:
    """Rebuild one checkout of ``plan`` under ``staging`` on ``host``: its git state from origin + the bundle, then
    the paths deleted since HEAD removed, then the files that differ from HEAD sent by rsync. Returns what was sent;
    raises RuntimeError naming the step."""
    from hermes_cli import kanban_worktree_port as _kwt
    kw = {"timeout": timeout} if timeout else {}
    rel = plan["rel"]
    dest = staging if rel == "." else f"{staging}/{rel}"
    slug = _kwt._SLUG_RE.match(plan["url"] or "")
    mirror = f"{_kwt.DERIVED_MIRROR_ROOT}/{slug.group(1)}.git" if slug else None
    full = plan["url"] is None
    full_reason = "no GitHub or local origin" if full else None
    while True:
        bundle, idx = make_bundle(plan, full=full)
        proc = runner(_ssh_argv(host, rebuild_script(dest, plan, idx, mirror)), input=bundle, capture_output=True,
                      **kw)
        rc = getattr(proc, "returncode", 1)
        if rc == NEEDS_FULL_RC and not full:
            full = True  # the pool clone lacks a commit the Studio's origin refs have: send every commit
            e = getattr(proc, "stderr", b"") or b""
            e = e.decode("utf-8", "replace") if isinstance(e, bytes) else str(e)
            full_reason = e.rpartition("need-full: ")[2].strip()[:300] or "bundle prerequisites missing on the host"
            continue
        if rc != 0:
            raise RuntimeError(f"rebuild of {rel} on {host.name} rc={rc}: {_err(proc)}")
        break
    if plan["deleted"]:
        payload = "".join(p + "\0" for p in plan["deleted"]).encode("utf-8", "surrogateescape")
        proc = runner(_ssh_argv(host, f"cd {shlex.quote(dest)} && xargs -0 rm -rf --"), input=payload,
                      capture_output=True, **kw)
        if getattr(proc, "returncode", 1) != 0:
            raise RuntimeError(f"rebuild of {rel} on {host.name}: deletions rc={proc.returncode}: {_err(proc)}")
    if plan["files"]:
        payload = "".join(p.rstrip("/") + "\0" for p in plan["files"]).encode("utf-8", "surrogateescape")
        # a listed dir holding a nested rebuilt checkout: that checkout is rebuilt on its own, never sent as files
        excl = [f"--exclude=/{n}/" for n in plan["nested"]]
        # -I: every listed file is known to differ from HEAD; the quick check would skip one whose size and mtime
        # equal the checked-out version's and leave HEAD's bytes on the host (t_681a9c5e item 5)
        proc = runner([*rsync_base, "-r", "-I", *excl, "--from0", "--files-from=-", "--", f"{plan['repo']}/",
                       f"{host.target}:{dest}/"], input=payload, capture_output=True, **kw)
        if getattr(proc, "returncode", 1) != 0:
            raise RuntimeError(f"rebuild of {rel} on {host.name}: files rc={proc.returncode}: {_err(proc)}")
    return {"rel": rel, "bundle_bytes": len(bundle), "full_bundle": full,
            **({"full_reason": full_reason} if full else {}), "files": len(plan["files"]),
            "deleted": len(plan["deleted"]), "stash": len(plan["stash"]),
            **({"stash_left": plan["stash_left"]} if plan.get("stash_left") else {})}


class Job:
    """One background carry. ``phase``: queued, census, space, copy, copied. ``stats`` once copied."""

    def __init__(self, task_id: str, host: PoolHost, workspace: str, stale: Optional[PoolHost] = None,
                 lock_dir: Optional[Path] = None):
        self.task_id, self.host, self.workspace, self.stale = task_id, host, workspace, stale
        self.lock_dir = lock_dir    # the (host, path) carry locks; the stale host's cleanup takes its lock there
        self.phase = "queued"
        self.bytes: Optional[int] = None
        self.entries: Optional[int] = None
        self.t0 = time.monotonic()
        self.last_progress = self.t0
        self.staged_before = 0
        self.stats: Dict[str, object] = {}
        self.future = None
        self.stale_cleaned: Optional[bool] = None
        self.fd: Optional[int] = None  # the (host, path) carry lock, held from the start through the finalize
        self.done_at: Optional[float] = None
        self.rebuilt: Tuple[str, ...] = ()
        self.timeout: Optional[float] = None  # a total bound on each copy step: one-shot (run_sync) carries only
        self.seen_done = False  # a poll already handed this finished copy to a claim (see poll's refresh rule)

    def elapsed(self) -> float:
        return time.monotonic() - self.t0


def run(job: Job, runner: Optional[Callable] = None) -> Dict[str, object]:
    """The whole carry up to (not including) the finalize; raises CarryRefused, CarryNoRoom or RuntimeError."""
    from hermes_cli import kanban_worker_hosts as _kwh
    runner = runner or _run
    t0, cpu0, ch0 = time.monotonic(), time.thread_time(), _children_cpu()
    src = job.workspace.rstrip("/")
    if not _kwh.carry_path_safe(src):
        raise CarryRefused(f"{src!r} is not a shell-inert path")
    dest = src
    job.phase = "census"
    repos: Dict[str, int] = {}
    job.bytes, job.entries = census(src, repos)
    # t_f3a411c2 item 1: a checkout whose git dir would not travel as files (a linked worktree of a repo outside the
    # workspace, a clone borrowing objects through alternates; Prism b68c58398adb: the root counts too) is rebuilt on
    # the pool host from origin + a bundle, never refused for it. The census walk above found the checkouts.
    rebuild = rebuilds_of(src, sorted(repos))
    for rel in rebuild:
        if rel != "." and not _kwh.carry_path_safe(f"{src}/{rel}"):
            raise CarryRefused(f"checkout {rel!r} is not a shell-inert path and its git dir does not travel")
    plans = [plan_rebuild(src, rel, rebuild) for rel in rebuild]
    job.rebuilt = tuple(rebuild)
    if job.stale is not None and job.stale.name != job.host.name:
        # a resumable staging dir an earlier attempt left on another host: that host is cleaned first, under that
        # host's carry lock (Prism 4d42b4210a5a); while another dispatcher holds it, the dir is left alone
        sfd = _try_lock(lock_path(job.lock_dir, job.stale.name, src)) if job.lock_dir is not None else -1
        if sfd is None:
            job.stale_cleaned = False
        else:
            try:
                job.stale_cleaned = remove_staging(job.stale, src, runner)
            finally:
                _unlock(sfd)
    job.phase = "space"
    _kwh.prepare_remote_workspace(job.host, dest, runner)
    free, staged = remote_space(job.host, dest, runner)
    job.staged_before = staged
    # what lands on the host: the workspace as counted, plus each rebuilt checkout's git dir, estimated at the size
    # of its checked-out files (a pack is smaller than the tree it holds)
    need = max(0, job.bytes + sum(repos[r] for r in rebuild) - staged)
    if need > free:
        raise CarryNoRoom(job.host.name, need, free, staged)
    job.phase = "copy"
    ssh = _ssh_argv(job.host, "")[:-2]
    base = ["rsync", "-a", "--partial", f"--timeout={RSYNC_IO_TIMEOUT_S}", "-e", " ".join(shlex.quote(a) for a in ssh)]
    if "." not in rebuild:
        # everything outside the rebuilt checkouts travels as files; --delete never touches an excluded dir, so a
        # rebuilt checkout already in the staging dir (a resumed or refreshed carry) is kept and only updated below
        excl = [f"--exclude=/{rel}/" for rel in rebuild]
        proc = runner([*base, "--delete", *excl, "--", f"{src}/", f"{job.host.target}:{staging_path(dest)}/"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                      **({"timeout": job.timeout} if job.timeout else {}))
        if getattr(proc, "returncode", 1) != 0:
            raise RuntimeError(f"rsync rc={getattr(proc, 'returncode', '?')}: "
                               f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")
    rebuilt = [carry_rebuild(job.host, staging_path(dest), plan, runner, base, job.timeout) for plan in plans]
    job.phase = "copied"
    job.stats = {"bytes": job.bytes, "entries": job.entries, "seconds": round(time.monotonic() - t0, 2),
                 "cpu_s": round((time.thread_time() - cpu0) + (_children_cpu() - ch0), 2),
                 "staging": staging_path(dest), **({"rebuilt": rebuilt} if rebuilt else {})}
    job.done_at = time.monotonic()
    return job.stats


def finalize(host: PoolHost, workspace: str, runner: Optional[Callable] = None) -> None:
    """Move the finished staging dir onto the workspace path on ``host`` (one ssh call). An empty or absent
    workspace is replaced whole; one that already holds files gets the staging merged over it with rsync, as the
    in-tick carry did (never deleted from; a destination symlink is replaced, never written through: Prism
    0e52f77edd32; ``-I`` so a file of equal size and mtime is still copied: Prism 707d9e8389dd). Raises when the staging dir is missing or the move fails (the staging dir stays)."""
    dest = workspace.rstrip("/")
    s, w = shlex.quote(staging_path(dest)), shlex.quote(dest)
    # a workspace path that is a symlink on the host loses the link itself (never its target) and gets the staging
    # dir moved into place (t_681a9c5e item 4: rsync into it would write the files into the link's target)
    proc = _ssh(host, f"[ -d {s} ] || exit 4; if [ -L {w} ]; then rm -f -- {w} || exit 6; fi; "
                      f"if rmdir -- {w} 2>/dev/null || [ ! -e {w} ]; then mv -- {s} {w}; "
                      f"else rsync -a -I -- {s}/ {w}/ && rm -rf -- {s}; fi", runner, FINALIZE_TIMEOUT_S)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"carry to {host.name}: finalize rc={getattr(proc, 'returncode', '?')}: "
                           f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")


# -- registry: one job per card, visible to every tick of this process; a flock per (host, path) for the others --
# The flock is held from the start of the copy until the staging dir is consumed (finalize) or the carry failed
# (Prism 18996b99e57f / b198d365c020): no other dispatcher can start an rsync into a staging dir being moved.

_lock = threading.Lock()
_jobs: Dict[str, Tuple[Job, Optional[int]]] = {}       # task id -> (job, lock fd)
_failed: Dict[str, Tuple[float, str]] = {}             # task id -> (monotonic, wait reason) for RETRY_S
_pool: Optional[ThreadPoolExecutor] = None


def lock_path(lock_dir: Path, host: str, workspace: str) -> Path:
    import hashlib
    return Path(lock_dir) / (hashlib.sha1(f"{host}\0{workspace}".encode()).hexdigest()[:24] + ".lock")


def _try_lock(path: Path) -> Optional[int]:
    from hermes_cli.kanban_worktree_port import _try_lock as _tl
    return _tl(path)


def _unlock(fd: Optional[int]) -> None:
    from hermes_cli.kanban_worktree_port import _unlock as _ul
    _ul(fd)


def wait_reason(exc: BaseException, host: str) -> str:
    if isinstance(exc, CarryNoRoom):
        return f"{NO_ROOM}:{host}: {exc}"
    return f"{FAILED}:{host}: {exc}"


def failure_state(job: Job, exc: BaseException) -> str:
    """What the failure left on the pool host: ``refused`` (nothing copied, the capped rule decides), ``clean``
    (nothing of this card on the host) or ``resumable`` (a staging dir the next attempt there resumes)."""
    if isinstance(exc, CarryRefused):
        return "refused"
    if job.phase in ("queued", "census"):
        return "clean"
    if job.phase == "space" and not job.staged_before:
        return "clean"
    return "resumable"


def _executor() -> ThreadPoolExecutor:
    """The carry thread pool, created on first use (caller holds ``_lock``). A job kept by :func:`run_sync` can be
    refreshed by a later poll in a process that never started a background carry (Prism 64edba28c538)."""
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="kanban-carry")
    return _pool


def poll(task_id: str, host: PoolHost, workspace: str, lock_dir: Path, *, start: bool = True,
         stale: Optional[PoolHost] = None, runner: Optional[Callable] = None) -> Tuple[str, object]:
    """One tick's look at the card's carry: ``(state, detail)``.

    * ``started``: no job was running; one was submitted now (detail: the Job);
    * ``running``: the job is still copying (detail: the Job);
    * ``done``: the copy finished (detail: the Job; :func:`take` collects it at the claim);
    * ``failed``: the job raised (detail: ``(job, exc)``); the card backs off for RETRY_S;
    * ``backoff``: an earlier failure is younger than RETRY_S (detail: its wait reason);
    * ``busy``: another dispatcher holds this carry's lock, or this card's job runs to another host;
    * ``none``: nothing recorded and ``start`` is False."""
    now = time.monotonic()
    with _lock:
        hit = _jobs.get(task_id)
        if hit is not None and (hit[0].host.name != host.name or hit[0].workspace != workspace):
            if not hit[0].future.done():
                return "busy", hit[0]
            # placed elsewhere since: that host's staging dir (finished or not) is removed by the next carry
            stale = stale or hit[0].host
            del _jobs[task_id]
            release(hit[0])
            hit = None
        if hit is not None:
            job, fd = hit
            if not job.future.done():
                return "running", job
            exc = job.future.exception()
            # Operator 16:40 item 4: a finished copy is handed to a claim at least once, whatever its age, so ticks
            # more than FRESH_S apart still claim it; only a copy a claim already passed over is refreshed.
            if exc is None and job.done_at is not None and now - job.done_at > FRESH_S and job.seen_done:
                # Prism 38c86a910163: a copy that waited (no slot, no claim) is refreshed before it is claimed; the
                # second rsync into the same staging dir sends only what changed on the Studio since, under the
                # lock this job still holds
                # Prism 64edba28c538: a submit that raises leaves the job as it was (restored below, never cleared
                # first and left so), so the next poll tries the refresh again instead of handing the stale copy over
                # as done. Cleared before the submit: the refresh thread sets done_at itself when it finishes.
                prev = job.done_at, job.seen_done
                job.done_at, job.seen_done = None, False
                try:
                    job.future = _executor().submit(run, job, runner)
                except BaseException:
                    job.done_at, job.seen_done = prev
                    raise
                return "running", job
            if exc is None:
                job.seen_done = True
                return "done", job
            del _jobs[task_id]
            release(job)
            _failed[task_id] = (now, wait_reason(exc, host.name))
            return "failed", (job, exc)
        seen = _failed.get(task_id)
        if seen is not None:
            if now - seen[0] < RETRY_S:
                return "backoff", seen[1]
            del _failed[task_id]
        if not start:
            return "none", None
        fd = _try_lock(lock_path(lock_dir, host.name, workspace))
        if fd is None:
            return "busy", None
        job = Job(task_id, host, workspace, stale, lock_dir)
        job.fd = fd
        try:
            job.future = _executor().submit(run, job, runner)
        except BaseException:
            release(job)
            raise
        _jobs[task_id] = (job, fd)
        return "started", job


def release(job: Job) -> None:
    """Drop the job's carry lock (idempotent)."""
    fd, job.fd = job.fd, None
    _unlock(fd)


def take(task_id: str, host: PoolHost, workspace: str) -> Optional[Job]:
    """At the claim: the finished job for this card (removed from the registry), or None. Re-raises its error.
    The job's carry lock stays held: the caller finalizes, then :func:`release`."""
    with _lock:
        hit = _jobs.get(task_id)
        if hit is None or hit[0].host.name != host.name or hit[0].workspace != workspace \
                or not hit[0].future.done():
            return None
        del _jobs[task_id]
    try:
        hit[0].future.result()
    except BaseException:
        release(hit[0])
        raise
    return hit[0]


def run_sync(job: Job, lock_dir: Path, runner: Optional[Callable] = None) -> bool:
    """A one-shot (CLI) tick: the same carry, run here under the same lock. False (nothing run) while another
    dispatcher holds it; raises what :func:`run` raises. On success the lock stays held on ``job`` until the
    finalize (:func:`release`). The finished job is kept in the registry, like a background one, so a claim that
    never comes (claim_task None, PauseHeld, a route failure) does not strand its lock: the card's next carry in
    this process releases it first (Prism eba22256a789)."""
    with _lock:
        prev = _jobs.get(job.task_id)
        if prev is not None and not (prev[0].future is not None and prev[0].future.done()):
            return False  # a background copy of this card is still running in this process
        _jobs.pop(job.task_id, None)
    if prev is not None:
        release(prev[0])
    fd = _try_lock(lock_path(lock_dir, job.host.name, job.workspace))
    if fd is None:
        return False
    job.fd, job.lock_dir = fd, job.lock_dir or lock_dir
    job.seen_done = True  # this tick claims it
    try:
        stats = run(job, runner)
    except BaseException:
        release(job)
        raise
    job.future = Future()
    job.future.set_result(stats)
    with _lock:
        _jobs[job.task_id] = (job, fd)
    return True


def progress_due(job: Job) -> bool:
    now = time.monotonic()
    if now - job.last_progress >= PROGRESS_EVERY_S:
        job.last_progress = now
        return True
    return False


def progress_payload(job: Job) -> dict:
    return {"host": job.host.name, "path": job.workspace, "phase": job.phase, "bytes": job.bytes,
            "entries": job.entries, "elapsed_s": round(job.elapsed(), 1)}


# -- the card's carry history (DB): what binds the card to a host while a carry is in flight --------------------

_KINDS = (STARTED_EVENT, FAILED_EVENT, REFUSED_EVENT)


def _last(conn, task_id: str, workspace: Optional[str]) -> Optional[Tuple[str, dict]]:
    """The newest started / failed / carried event of this card for ``workspace``, as (kind, payload)."""
    from hermes_cli.kanban_worker_hosts import CARRIED_EVENT
    kinds = (*_KINDS, CARRIED_EVENT)
    row = conn.execute("SELECT kind, payload FROM task_events WHERE task_id=? AND kind IN (%s) "
                       "ORDER BY id DESC LIMIT 1" % ",".join("?" * len(kinds)), (task_id, *kinds)).fetchone()
    if row is None:
        return None
    try:
        doc = json.loads(row[1] or "{}")
    except (TypeError, ValueError):
        return None
    if workspace is None or doc.get("path") != workspace:
        return None
    return row[0], doc


def inflight_host(task_id: str, workspace: Optional[str]) -> Optional[str]:
    """The host this process's carry of the card is copying to or has finished copying to: the card is pinned
    there until it is claimed. None when no such job (a restarted dispatcher places afresh; :func:`stale_host`
    then names the host whose staging dir the next carry removes)."""
    with _lock:
        hit = _jobs.get(task_id)
    if hit is None or hit[0].workspace != workspace:
        return None
    return hit[0].host.name


def refused(conn, task_id: str, workspace: Optional[str]) -> bool:
    """True when the newest carry attempt of this card refused to carry its files (``state: refused``)."""
    last = _last(conn, task_id, workspace)
    return last is not None and last[0] == FAILED_EVENT and last[1].get("state") == "refused"


def stale_host(conn, task_id: str, workspace: Optional[str], hosts: Mapping[str, PoolHost],
               host: str) -> Optional[PoolHost]:
    """The pool host that may hold a staging dir of this card from an earlier attempt (one that failed
    ``resumable``, or one started and never finished: a dispatcher restart), when that is not ``host``. The next
    carry removes it there first."""
    last = _last(conn, task_id, workspace)
    if last is None or not (last[0] == STARTED_EVENT
                            or (last[0] == FAILED_EVENT and last[1].get("state") == "resumable")):
        return None
    name = last[1].get("host")
    if not isinstance(name, str) or name.lower() == host.lower():
        return None
    return next((h for n, h in hosts.items() if n.lower() == name.lower()), None)


def drop_for_refusal(conn, task_id: str, workspace: Optional[str], hosts: Mapping[str, PoolHost], lock_dir: Path,
                     reason: str, runner: Optional[Callable] = None) -> Optional[dict]:
    """Workspace admission refused the card (t_681a9c5e, Prism 2a7982d8eadd): forget this process's finished carry
    of it and remove the staging dir a carry may have left on a pool host (the host of its live job, else of its last
    started / resumable-failed record), under that (host, path) carry lock. The returned payload is written as
    ``workspace_carry_admission_refused``, so a later refused tick finds it and does nothing. None: nothing to drop
    (no carry started, already recorded) or not now (a copy still running, another dispatcher holds the lock, the
    remove failed: the next refused tick tries again from the started record)."""
    if not workspace:
        return None
    workspace = str(Path(workspace))
    host: Optional[PoolHost] = None
    with _lock:
        hit = _jobs.get(task_id)
        if hit is not None:
            if hit[0].future is not None and not hit[0].future.done():
                return None   # the copy is running: its rsync is not stopped mid-write; dropped once it finished
            del _jobs[task_id]
            release(hit[0])
            host, workspace = hit[0].host, hit[0].workspace
        _failed.pop(task_id, None)
    if host is None:
        host = stale_host(conn, task_id, workspace, hosts, "")
        if host is None:
            return None
    fd = _try_lock(lock_path(lock_dir, host.name, workspace))
    if fd is None:
        return None
    try:
        removed = remove_staging(host, workspace, runner)
    finally:
        _unlock(fd)
    if not removed:
        return None   # Prism 8309def60720: no event, so the next refused tick retries (its started record remains)
    return {"host": host.name, "path": workspace, "staging": staging_path(workspace), "staging_removed": removed,
            "reason": reason[:300]}


def reset() -> None:
    """Tests: forget every job and backoff."""
    with _lock:
        for job, _fd in _jobs.values():
            release(job)
        _jobs.clear()
        _failed.clear()
