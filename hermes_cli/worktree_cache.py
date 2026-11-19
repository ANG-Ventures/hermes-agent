"""``hermes worktree-cache`` -- copy-on-write checkouts from a per-host template.

Spec: ANG-Ventures/hermes-content plans/fleet-worktree-cache/SPEC.md (card
t_940636ed); this module is build card 1 (t_90fccfb9), CLONE MODE first.

Every new checkout of a fleet repo writes the whole working tree again (about
190 MB for hermes-home). Each host instead keeps one TEMPLATE checkout per repo
on the same filesystem as its workspaces, and a new checkout copies the
template's files with copy-on-write clones (macOS ``clonefile(2)``, Linux
``FICLONE`` through ``cp --reflink=always``), verifies them against the commit
in a staging dir, then registers the result with git. A clone shares the
template's blocks until a file is edited.

Layout (spec 3.1)::

    <kanban.workspaces_root>/.templates/<owner>__<repo>/.lock      flock (3.3)
    <kanban.workspaces_root>/.templates/<owner>__<repo>/gen-<N>/   a normal clone, detached
    <kanban.workspaces_root>/.templates/<owner>__<repo>/gen-<N>/template.json

``<kanban.workspaces_root>/.templates`` is the default root. A host whose checkouts land on
another volume lists a root there in ``worktree_cache.templates_roots``; each create uses the
root on its destination's volume (t_259301d7: nas-fleet's card workspaces are on /srv/fleet,
a different ZFS dataset from the default root on the container rootfs).

Rules this module keeps:

* OFF by default (``worktree_cache.enabled: false``). Off, out of scope, or
  ``HERMES_WORKTREE_CACHE=0`` runs the plain command and writes no ledger row.
* Every fallback to plain git writes one ``clone-fallback.jsonl`` row with a
  ``reason`` (``REASONS``), never silent (D3). A fallback still succeeds through
  plain git; a non-zero exit is always a real git failure.
* No byte copy is ever attempted across volumes: ``st_dev`` is compared before
  copying (``cross_volume``), and the copy primitive only clones
  (``--reflink=always`` / ``clonefile``), never ``cp`` without them, never a
  hardlink.
* The template is only read by a create (``LOCK_SH``); refresh/rebuild hold
  ``LOCK_EX``.

Worktree mode (``add``) is not built in this card; nothing calls it yet.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple

from hermes_cli import kanban_clone

ENV_SWITCH = "HERMES_WORKTREE_CACHE"                 # "0" forces plain git for this process
ENV_TEMPLATES = "HERMES_WORKTREE_CACHE_TEMPLATES"    # override <workspaces_root>/.templates
ENV_LOG = "HERMES_WORKTREE_CACHE_LOG"                # override the per-create log path
LEDGER_ENV = "CLONE_FALLBACK_LEDGER"                  # same knob as hermes-home live_sqlite_snapshot
LEDGER_MAX_B = 4 << 20

DEFAULT_REPOS = (
    "ANG-Ventures/hermes-home",
    "ANG-Ventures/hermes-agent",
    "ANG-Ventures/hermes-skills",
    "ANG-Ventures/hermes-content",
    "ANG-Ventures/pipecat-house-voice",
    "ANG-Ventures/prism-router",
)
KNOWN_SCOPES = ("dispatcher_worktree", "kanban_clone", "port_clone", "manual", "deploy_selftest")

REASONS = (
    "no_template",       # repo not in worktree_cache.repos, or no generation built yet
    "cross_volume",      # template and destination on different st_dev
    "no_cow_fs",         # the filesystem failed the clone probe
    "unsupported_tree",  # .gitmodules, core.sparseCheckout, or a filter/ident/working-tree-encoding attribute
    "template_dirty",    # template has modified/untracked/ignored files
    "template_moved",    # template HEAD differs from template.json
    "lock_timeout",      # LOCK_SH not granted within CREATE_LOCK_TIMEOUT_S
    "clone_errno",       # the CoW copy or another pre-register step raised (errno when an OSError)
    "verify_failed",     # the staged copy does not equal the commit
    "far_commit",        # target commit differs from the template in > max_diff_files files, or T unreachable
    "unsupported_opts",  # `hermes kanban clone` options the cache does not reproduce (--depth, --no-checkout, ...)
)

CREATE_LOCK_TIMEOUT_S = 30.0
REFRESH_LOCK_TIMEOUT_S = 120.0
META = "template.json"
BAD_MARKER = "hermes-template-bad"      # inside <gen>/.git: generation retired
_GEN_RE = re.compile(r"^gen-(\d+)$")
_GEN_ANY_RE = re.compile(r"^gen-(\d+)(?:\.bad-\d+)?$")

# Keys the template must carry (spec 3.1); refresh re-asserts them every run.
TEMPLATE_CONFIG = (
    ("core.repositoryformatversion", "1"),
    ("extensions.worktreeConfig", "true"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("core.hooksPath", "/dev/null"),
    ("fetch.prune", "false"),
)


# --------------------------------------------------------------------------- config


@dataclass
class Settings:
    enabled: bool = False
    scopes: Tuple[str, ...] = ()
    repos: Tuple[str, ...] = DEFAULT_REPOS
    max_lag_s: float = 900.0
    max_diff_files: int = 2000


def host_config() -> dict:
    """The host's ROOT config.yaml, never a profile's (t_969164a0). ``worktree_cache`` and
    ``kanban.workspaces_root`` are host switches, and a kanban worker runs with
    ``HERMES_HOME=<root>/profiles/<name>``: on the Studio no worker profile carries either key
    (29 profiles read 2026-11-17), so a profile read left the cache off for every worker and put
    its templates root on the Data volume. Unreadable -> {} (OFF)."""
    try:
        from hermes_cli.config import read_host_root_config

        return read_host_root_config()
    except Exception:  # noqa: BLE001 - an unreadable config means OFF
        return {}


def settings(cfg: Optional[dict] = None) -> Settings:
    """``worktree_cache`` from the host's root config.yaml (read per call: no restart needed)."""
    if cfg is None:
        cfg = host_config().get("worktree_cache") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    repos = cfg.get("repos")
    return Settings(
        enabled=cfg.get("enabled") is True,
        scopes=tuple(str(s) for s in (cfg.get("scopes") or ()) if s),
        repos=tuple(str(r) for r in repos) if isinstance(repos, (list, tuple)) else DEFAULT_REPOS,
        max_lag_s=float(cfg.get("max_lag_s") or 900),
        max_diff_files=int(cfg.get("max_diff_files") or 2000),
    )


def cache_active(caller: str, s: Settings) -> bool:
    """Spec 5.4 step 1: the switch, the scope, the per-process env override."""
    if os.environ.get(ENV_SWITCH, "").strip() == "0":
        return False
    return s.enabled and caller in s.scopes


def _default_templates_root(cfg: dict) -> Path:
    kanban = cfg.get("kanban")
    raw = kanban.get("workspaces_root") if isinstance(kanban, dict) else None
    if isinstance(raw, str) and raw and Path(raw).expanduser().is_absolute():
        return Path(raw).expanduser() / ".templates"
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "kanban" / "workspaces" / ".templates"


def templates_roots() -> list:
    """Every templates root on this host, in preference order (t_259301d7).

    ``HERMES_WORKTREE_CACHE_TEMPLATES`` (tests, one-off runs) is the only root when set. Else the
    host root config's ``worktree_cache.templates_roots`` (absolute dirs, one per volume the
    checkouts land on), then the default ``<kanban.workspaces_root>/.templates``. A clone can only
    share blocks within one filesystem, so each create uses the root on its destination's volume
    (:func:`templates_root`); ``refresh`` keeps a template in every root."""
    override = os.environ.get(ENV_TEMPLATES, "").strip()
    if override:
        return [Path(override).expanduser()]
    cfg = host_config()
    wcfg = cfg.get("worktree_cache")
    raw = wcfg.get("templates_roots") if isinstance(wcfg, dict) else None
    out: list = []
    for item in raw if isinstance(raw, (list, tuple)) else ():
        if isinstance(item, str) and item.strip() and Path(item.strip()).expanduser().is_absolute():
            out.append(Path(item.strip()).expanduser())
    out.append(_default_templates_root(cfg))
    return list(dict.fromkeys(out))


def _volume_dev(path: Path) -> Optional[int]:
    """``st_dev`` of *path*, or of its nearest existing ancestor (a root or a destination that is
    not built yet lives on the volume of the directory it will be created in)."""
    p = Path(path).absolute()
    for cand in (p, *p.parents):
        dev = _dev(cand)
        if dev is not None:
            return dev
    return None


def templates_root(dest: Optional[Path] = None) -> Path:
    """The templates root for a checkout at *dest*: the first of :func:`templates_roots` on the
    same volume as *dest*, else the default root (the create then falls back ``cross_volume``).
    No *dest*: the default root (``status``, the operator scripts' precheck)."""
    roots = templates_roots()
    if dest is not None:
        want = _volume_dev(dest)
        for root in roots:
            if want is not None and _volume_dev(root) == want:
                return root
    return roots[-1]


def repo_slug(url: str) -> Optional[str]:
    fleet = kanban_clone.parse_fleet_repo(url)
    if fleet is None:
        return None
    owner, repo = fleet
    return f"{owner}/{repo[:-4] if repo.endswith('.git') else repo}"


def repo_dir(slug: str, root: Optional[Path] = None) -> Path:
    owner, repo = slug.split("/", 1)
    return (root or templates_root()) / f"{owner}__{repo}"


# --------------------------------------------------------------------------- ledger + log


def ledger_path() -> Optional[Path]:
    env = os.environ.get(LEDGER_ENV)
    if env == "0":
        return None
    return Path(env) if env else Path.home() / ".hermes" / "state" / "clone-fallback.jsonl"


def log_path() -> Optional[Path]:
    env = os.environ.get(ENV_LOG)
    if env == "0":
        return None
    return Path(env) if env else Path.home() / ".hermes" / "state" / "worktree-cache.jsonl"


def _append_jsonl(path: Optional[Path], row: dict) -> None:
    """Append one row, rotating to ``.1`` past 4 MiB. Never raises (accounting only)."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            if path.stat().st_size > LEDGER_MAX_B:
                os.replace(path, str(path) + ".1")
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(row, sort_keys=True) + "\n").encode())
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001
        pass


def _dev(path: Path) -> Optional[int]:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def record_fallback(caller: str, reason: str, slug: Optional[str], src: Optional[Path], dest: Path,
                    *, err: Optional[int] = None, nbytes: Optional[int] = None) -> None:
    """One ``clone-fallback.jsonl`` row (the hermes-home row shape + ``reason`` and ``repo``)."""
    row = {
        "ts": round(time.time(), 3), "caller": f"worktree_cache:{caller}", "reason": reason, "repo": slug,
        "errno": errno.errorcode.get(err, str(err)) if err is not None else None,
        "bytes": nbytes, "src_dev": _dev(src) if src else None, "dst_dev": _dev(dest.parent),
        "src": str(src) if src else None, "dst": str(dest), "pid": os.getpid(),
    }
    _append_jsonl(ledger_path(), row)


# --------------------------------------------------------------------------- git helpers


def _git(*args: str, cwd: Optional[Path] = None, env: Optional[dict] = None,
         input_: Optional[bytes] = None) -> subprocess.CompletedProcess:
    full_env = kanban_clone._git_env()
    if env:
        full_env.update(env)
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, env=full_env,
                          capture_output=True, input=input_, check=False)


def _out(res: subprocess.CompletedProcess) -> str:
    return res.stdout.decode("utf-8", "replace").strip()


def _must(res: subprocess.CompletedProcess, what: str) -> str:
    if res.returncode != 0:
        raise RuntimeError(f"{what} failed: {res.stderr.decode('utf-8', 'replace').strip()}")
    return _out(res)


# --------------------------------------------------------------------------- locking


@contextlib.contextmanager
def _flock(path: Path, mode: int, timeout: float) -> Iterator[bool]:
    """Yield True with *mode* held, or False when not granted within *timeout* s."""
    import fcntl  # POSIX only; nothing on Windows reaches the cache (no CoW primitive there)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    got = False
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --------------------------------------------------------------------------- generations


def _gens(rdir: Path) -> list:
    out = []
    with contextlib.suppress(FileNotFoundError):
        for child in rdir.iterdir():
            m = _GEN_RE.match(child.name)
            if m and child.is_dir() and not child.is_symlink():
                out.append((int(m.group(1)), child))
    return [p for _, p in sorted(out)]


def current_gen(rdir: Path) -> Optional[Path]:
    for gen in reversed(_gens(rdir)):
        if not (gen / ".git" / BAD_MARKER).exists():
            return gen
    return None


def read_meta(gen: Path) -> Optional[dict]:
    try:
        meta = json.loads((gen / META).read_text(encoding="utf-8-sig"))
        return meta if isinstance(meta, dict) else None
    except (OSError, ValueError):
        return None


def template_problem(gen: Path) -> Optional[str]:
    """Spec 3.5: ``template_moved`` / ``template_dirty`` / None. A stat walk, no writes."""
    meta = read_meta(gen)
    head = _git("rev-parse", "HEAD", cwd=gen)
    if meta is None or head.returncode != 0 or _out(head) != meta.get("head"):
        return "template_moved"
    st = _git("--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all",
              "--ignored=matching", cwd=gen)
    if st.returncode != 0:
        return "template_dirty"
    entries = [e for e in st.stdout.decode("utf-8", "replace").split("\0") if e]
    entries = [e for e in entries if e != f"!! {META}"]
    return "template_dirty" if entries else None


def _linked_worktrees(gen: Path) -> int:
    res = _git("worktree", "list", "--porcelain", cwd=gen)
    return _out(res).count("\nworktree ") if res.returncode == 0 else 0


def mark_bad(gen: Path, why: str) -> Optional[Path]:
    """Retire *gen*: marker always; rename to ``gen-N.bad-<ts>`` when no linked worktree
    still points into it (renaming would break their gitdir links). Caller holds LOCK_EX."""
    marker = gen / ".git" / BAD_MARKER
    with contextlib.suppress(OSError):
        marker.write_text(f"{time.time():.0f} {why}\n", encoding="utf-8")
    if _linked_worktrees(gen):
        return gen
    target = gen.with_name(f"{gen.name}.bad-{int(time.time())}")
    try:
        os.rename(gen, target)
        return target
    except OSError:
        return gen


def _tree_bytes(gen: Path, rev: str = "HEAD") -> Optional[int]:
    res = _git("ls-tree", "-r", "-l", rev, cwd=gen)
    if res.returncode != 0:
        return None
    total = 0
    for line in _out(res).splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4 and parts[3].isdigit():
            total += int(parts[3])
    return total


def unsupported_reason(gen: Path, rev: str = "HEAD") -> Optional[str]:
    """Spec 5.4 step 2: submodules, sparse checkout, filter / ident / working-tree-encoding."""
    if _git("cat-file", "-e", f"{rev}:.gitmodules", cwd=gen).returncode == 0:
        return "submodules"
    if _out(_git("config", "--bool", "--get", "core.sparseCheckout", cwd=gen)) == "true":
        return "sparse_checkout"
    files = _git("ls-files", "-z", cwd=gen)
    if files.returncode != 0:
        return "ls_files_failed"
    return _attr_problem(gen, files.stdout)


def unsupported_at(repo: Path, rev: str) -> Optional[str]:
    """``unsupported_reason`` for commit *rev* read from the object store (no working tree):
    the target of a create can add ``.gitmodules`` or a filter/ident attribute that the
    template's commit does not have."""
    if _git("cat-file", "-e", f"{rev}:.gitmodules", cwd=repo).returncode == 0:
        return "submodules"
    files = _git("ls-tree", "-r", "-z", "--name-only", rev, cwd=repo)
    if files.returncode != 0:
        return "ls_files_failed"
    return _attr_problem(repo, files.stdout, source=rev)


def _attr_problem(cwd: Path, files_z: bytes, source: Optional[str] = None) -> Optional[str]:
    src = (f"--source={source}",) if source else ()
    attrs = _git("check-attr", *src, "-z", "--stdin", "filter", "ident", "working-tree-encoding",
                 cwd=cwd, input_=files_z)
    if attrs.returncode != 0:
        return "check_attr_failed"
    fields = attrs.stdout.decode("utf-8", "replace").split("\0")
    for i in range(0, len(fields) - 2, 3):
        attr, value = fields[i + 1], fields[i + 2]
        if value not in ("unspecified", "unset"):
            return f"attr:{attr}"
    return None


def _attributes_changed(repo: Path, a: str, b: str) -> bool:
    """Does a..b touch any ``.gitattributes``? A two-tree move does not re-convert files
    whose blobs are unchanged, so new eol/text/ident rules would not reach them."""
    res = _git("diff", "--name-only", "-z", a, b, "--", ":(glob)**/.gitattributes", cwd=repo)
    return res.returncode != 0 or bool(res.stdout.strip(b"\0"))


def _assert_template_config(gen: Path) -> list:
    drift = []
    for key, value in TEMPLATE_CONFIG:
        if _out(_git("config", "--get", key, cwd=gen)) != value:
            drift.append(key)
            _must(_git("config", key, value, cwd=gen), f"setting {key} on template {gen}")
    exclude = gen / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8-sig") if exclude.exists() else ""
    if f"/{META}" not in text.splitlines():
        exclude.write_text(text + ("" if text.endswith("\n") or not text else "\n") + f"/{META}\n",
                           encoding="utf-8")
    return drift


def _origin_head(gen: Path) -> str:
    ref = _git("rev-parse", "--verify", "-q", "refs/remotes/origin/HEAD^{commit}", cwd=gen)
    if ref.returncode != 0:
        _git("remote", "set-head", "origin", "--auto", cwd=gen)
        ref = _git("rev-parse", "--verify", "-q", "refs/remotes/origin/HEAD^{commit}", cwd=gen)
    return _must(ref, f"resolving origin/HEAD in {gen}")


def _write_meta(gen: Path, slug: str, n: int) -> dict:
    head = _must(_git("rev-parse", "HEAD", cwd=gen), "rev-parse HEAD")
    meta = {
        "repo": slug, "gen": n, "head": head, "refreshed_at": round(time.time(), 3),
        "host": socket.gethostname(), "fs_dev": _dev(gen),
        "unsupported": unsupported_reason(gen), "tree_bytes": _tree_bytes(gen),
    }
    tmp = gen / ".git" / f"{META}.tmp"      # under .git: the repo cannot track a symlink here
    tmp.write_text(json.dumps(meta, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, gen / META)
    return meta


def _default_url(slug: str) -> str:
    return f"https://github.com/{slug}.git"


def _mirror_for(url: str) -> Optional[Path]:
    fleet = kanban_clone.parse_fleet_repo(url)
    if fleet is None:
        return None
    try:
        return kanban_clone.ensure_mirror(url, *fleet)
    except Exception as exc:  # noqa: BLE001 - degrade to no reference
        print(f"worktree-cache: mirror unavailable ({exc})", file=sys.stderr)
        return None


# kanban_clone.MIRROR_CONFIG minus the fetch refspec (an existing mirror keeps its own), in its order:
# extensions.* is honoured only once core.repositoryformatversion is 1.
_NEVER_PRUNE = tuple(kv for kv in kanban_clone.MIRROR_CONFIG if kv[0] != "remote.origin.fetch")


def _is_never_prune(mirror: Path) -> bool:
    fmt = _git("--git-dir", str(mirror), "config", "--get", "core.repositoryformatversion")
    pre = _git("--git-dir", str(mirror), "config", "--bool", "--get", "extensions.preciousObjects")
    return fmt.returncode == 0 and _out(fmt) == "1" and pre.returncode == 0 and _out(pre) == "true"


def _write_never_prune_keys(mirror: Path) -> None:
    for k, v in _NEVER_PRUNE:
        _must(_git("--git-dir", str(mirror), "config", k, v), f"config {k} on {mirror}")


def borrow_safe(mirror: Path) -> bool:
    """True when a clone may keep an alternate to *mirror*: it is never-prune (format 1 AND
    extensions.preciousObjects). A mirror that is not is repaired first, under the same flock its
    builder and both refresh jobs take (kanban_clone._mirror_lock), so no fetch is mid-write.
    False (the caller dissociates) when the repair cannot be made (Prism 1484b4563429, t_6de39491)."""
    if _is_never_prune(mirror):
        return True
    try:
        with kanban_clone._mirror_lock(mirror):
            if not _is_never_prune(mirror):
                _write_never_prune_keys(mirror)
                print(f"worktree-cache: never-prune keys set on {mirror}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - an unrepairable mirror is still usable with --dissociate
        print(f"worktree-cache: cannot make {mirror} never-prune ({exc}); dissociating", file=sys.stderr)
    return _is_never_prune(mirror)


def _clone_cmd(url: str, dest: Path, extra: Sequence[str] = ()) -> list:
    cmd = ["clone", "--quiet", "--no-checkout"]
    if kanban_clone.local_source(url) is not None:
        cmd.append("--no-local")          # never hard-link a local source's packs
    mirror = _mirror_for(url)
    if mirror is not None:
        cmd += ["--reference-if-able", str(mirror)]
        if not borrow_safe(mirror):
            cmd.append("--dissociate")    # a prune of the mirror must not take this clone's objects
    return cmd + list(extra) + ["--", url, str(dest)]


def _build_generation(rdir: Path, slug: str, url: str) -> Tuple[Path, dict]:
    nums = [int(m.group(1)) for c in rdir.iterdir() if (m := _GEN_ANY_RE.match(c.name))]
    n = max(nums, default=0) + 1
    staging = rdir / f".gen-{n}.build-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        _must(_git(*_clone_cmd(url, staging)), f"template clone of {slug}")
        _assert_template_config(staging)
        _must(_git("checkout", "-q", "--detach", _origin_head(staging), cwd=staging), "template checkout")
        _write_meta(staging, slug, n)
        gen = rdir / f"gen-{n}"
        os.rename(staging, gen)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return gen, read_meta(gen) or {}


def refresh(slug: str, *, url: Optional[str] = None, lock_timeout: float = REFRESH_LOCK_TIMEOUT_S,
            root: Optional[Path] = None) -> dict:
    """Bring the template of *slug* under *root* (default: the default root) to origin's default
    branch (spec 3.2), building a new generation when there is none or the current one is
    dirty/moved (3.5)."""
    url = url or _default_url(slug)
    rdir = repo_dir(slug, root)
    rdir.mkdir(parents=True, exist_ok=True)
    import fcntl

    with _flock(rdir / ".lock", fcntl.LOCK_EX, lock_timeout) as got:
        if not got:
            return {"repo": slug, "status": "lock_timeout"}
        gen = current_gen(rdir)
        retired = None
        if gen is not None:
            problem = template_problem(gen)
            if problem:
                retired = {"gen": str(mark_bad(gen, problem)), "reason": problem}
                gen = None
        if gen is None:
            gen, meta = _build_generation(rdir, slug, url)
            return {"repo": slug, "status": "built", "gen": str(gen), "head": meta.get("head"), "retired": retired}
        drift = _assert_template_config(gen)
        _must(_git("fetch", "--quiet", "origin", cwd=gen), f"template fetch of {slug}")
        target = _origin_head(gen)
        before = _out(_git("rev-parse", "HEAD", cwd=gen))
        if target != before and _attributes_changed(gen, before, target):
            # git leaves unchanged blobs alone on a move, so an eol/text rule change
            # would not reach the files: write the new commit fresh instead.
            retired = {"gen": str(mark_bad(gen, "attributes_changed")), "reason": "attributes_changed"}
            gen, meta = _build_generation(rdir, slug, url)
            return {"repo": slug, "status": "built", "gen": str(gen), "head": meta.get("head"), "retired": retired}
        if target != before:
            _must(_git("checkout", "-q", "--detach", target, cwd=gen), "template checkout")
        meta = _write_meta(gen, slug, int(_GEN_RE.match(gen.name).group(1)))
        problem = template_problem(gen)
        if problem:                       # a checkout that leaves residue retires the generation
            mark_bad(gen, problem)
            return {"repo": slug, "status": problem, "gen": str(gen)}
        return {"repo": slug, "status": "refreshed", "gen": str(gen), "from": before, "head": meta["head"],
                "config_drift": drift}


# --------------------------------------------------------------------------- copy primitives


_LIBC = None
CLONE_NOFOLLOW = 0x0001
_ERRNO_TEXT = (
    ("Invalid cross-device link", errno.EXDEV),
    ("Operation not supported", errno.EOPNOTSUPP),
    ("Not supported", errno.ENOTSUP),
)


def _clonefile(src: Path, dst: Path) -> None:
    """macOS clonefile(2); a directory is cloned recursively in one call."""
    global _LIBC
    import ctypes
    import ctypes.util

    if _LIBC is None:
        _LIBC = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    if _LIBC.clonefile(os.fsencode(str(src)), os.fsencode(str(dst)), CLONE_NOFOLLOW) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(src))


def _clone_entries(src_dir: Path, names: Sequence[str], dst_dir: Path) -> None:
    """The copy step of a create. Injectable: guard tests (9.3, 9.5) replace it;
    the filesystem probe keeps using ``_cow_copy`` directly."""
    _cow_copy(src_dir, names, dst_dir)


def _cow_copy(src_dir: Path, names: Sequence[str], dst_dir: Path) -> None:
    """Copy-on-write clone of ``src_dir/<name>`` into ``dst_dir`` for every name.

    Never a byte copy: Linux runs ``cp -a --reflink=always`` (FICLONE per file),
    macOS ``clonefile(2)``. Raises OSError with the errno on any failure.
    """
    if not names:
        return
    if sys.platform == "darwin":
        for name in names:
            _clonefile(src_dir / name, dst_dir / name)
        return
    res = subprocess.run(["cp", "-a", "--reflink=always", "--", *[str(src_dir / n) for n in names],
                          str(dst_dir) + "/"], capture_output=True, check=False)
    if res.returncode != 0:
        text = res.stderr.decode("utf-8", "replace")
        err = next((code for needle, code in _ERRNO_TEXT if needle in text), errno.EIO)
        raise OSError(err, text.strip().splitlines()[-1] if text.strip() else "cp failed", str(src_dir))


_PROBE_CACHE: dict = {}


def cow_probe(directory: Path) -> bool:
    """Can this filesystem clone? One 4 KiB file cloned once per ``st_dev`` per process."""
    dev = _dev(directory)
    if dev in _PROBE_CACHE:
        return _PROBE_CACHE[dev]
    src = directory / f".cow-probe-{os.getpid()}"
    dst = directory / f".cow-probe-{os.getpid()}.clone"
    ok = False
    try:
        src.mkdir(exist_ok=True)
        (src / "p").write_bytes(b"\0" * 4096)
        dst.mkdir(exist_ok=True)
        _cow_copy(src, ["p"], dst)
        ok = (dst / "p").stat().st_size == 4096
    except OSError:
        ok = False
    finally:
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)
    _PROBE_CACHE[dev] = ok
    return ok


def verify_staging(gen: Path, staging: Path, rev: str, index: Path) -> bool:
    """Spec 6.1: the staged files equal *rev* (content through git's clean path,
    mode bits, symlink targets) and nothing extra is present. Writes only *index*."""
    env = {"GIT_DIR": str(gen / ".git"), "GIT_WORK_TREE": str(staging), "GIT_INDEX_FILE": str(index)}
    base = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false")
    if _git(*base, "read-tree", rev, cwd=staging, env=env).returncode != 0:
        return False
    _git(*base, "update-index", "-q", "--really-refresh", cwd=staging, env=env)
    if _git(*base, "diff-files", "--quiet", cwd=staging, env=env).returncode != 0:
        return False
    for extra in (("--others", "--exclude-standard"), ("--others", "--ignored", "--exclude-standard")):
        res = _git(*base, "ls-files", *extra, "-z", cwd=staging, env=env)
        if res.returncode != 0 or res.stdout:
            return False
    return True


# --------------------------------------------------------------------------- clone mode


def _log(row: dict) -> None:
    _append_jsonl(log_path(), row)


def plain_clone(url: str, dest: str, branch: Optional[str] = None, rev: Optional[str] = None,
                quiet: bool = True, extra: Sequence[str] = ()) -> int:
    """Today's path: ``hermes kanban clone`` (+ ``checkout --detach <rev>``)."""
    opts = (["--quiet"] if quiet else []) + ([f"--branch={branch}"] if branch else []) + list(extra)
    # kanban_clone.clone borrows from the same mirror (a local source's origin names it): only when
    # that mirror is never-prune does the clone keep the alternate (Prism 1484b4563429).
    local = kanban_clone.local_source(url)
    mirror = _mirror_for(kanban_clone._source_origin(local) if local is not None else url)
    if mirror is not None and not borrow_safe(mirror):
        opts.append("--dissociate")
    rc = kanban_clone.clone(url, dest, opts)
    if rc == 0 and rev:
        rc = subprocess.run(["git", "-C", dest, "checkout", "-q", "--detach", rev],
                            env=kanban_clone._git_env(), check=False).returncode
    return rc


def _rm(path: Optional[Path]) -> None:
    if path is not None and path.exists():
        shutil.rmtree(path, ignore_errors=True)


def clone(url: str, dest: Optional[str] = None, *, caller: str, branch: Optional[str] = None,
          rev: Optional[str] = None, quiet: bool = True, s: Optional[Settings] = None,
          served_opts: Sequence[str] = ()) -> int:
    """Clone mode (spec 5.3/5.4): an independent clone borrowing from the mirror whose
    working files come from the template. Same exit code as the git command it replaces.

    *served_opts* are caller options a full template copy already satisfies (``--filter=blob:none``):
    the cached path ignores them and logs them, a plain-git fallback forwards them unchanged."""
    s = s or settings()
    dest = dest or kanban_clone.default_dest(url)
    if not cache_active(caller, s):
        return plain_clone(url, dest, branch, rev, quiet, served_opts)

    t0 = time.monotonic()
    dest_p = Path(dest).absolute()
    slug = repo_slug(url)
    row = {"ts": round(time.time(), 3), "caller": caller, "repo": slug, "mode": "clone", "dest": str(dest_p),
           "cow": False, "reason": None, "T": None, "C": None, "bytes_logical": None}
    if served_opts:
        row["opts_served"] = list(served_opts)
    gen: Optional[Path] = None
    staging: Optional[Path] = None

    def fallback(reason: str, *, err: Optional[int] = None) -> int:
        _rm(staging)
        meta = read_meta(gen) if gen else None
        nbytes = (meta or {}).get("tree_bytes")
        record_fallback(caller, reason, slug, gen, dest_p, err=err, nbytes=nbytes)
        rc = plain_clone(url, dest, branch, rev, quiet, served_opts)
        row.update(reason=reason, bytes_logical=nbytes, rc=rc, ms=round((time.monotonic() - t0) * 1000))
        _log(row)
        return rc

    if dest_p.exists() and (not dest_p.is_dir() or any(dest_p.iterdir())):
        return plain_clone(url, dest, branch, rev, quiet, served_opts)     # git reports the real error
    if slug is None or slug not in s.repos:
        return fallback("no_template")
    troot = templates_root(dest_p.parent)       # the root on the destination's volume (t_259301d7)
    rdir = repo_dir(slug, troot)
    meta: dict = {}
    T = ""

    def prepare() -> Optional[Tuple[str, Optional[int]]]:
        """Steps 2-4 under LOCK_SH: staging holds a verified copy of T, or a fallback reason."""
        nonlocal gen, staging, meta, T
        import fcntl

        gen = current_gen(rdir)
        if gen is None:
            return "no_template", None
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        if _dev(gen) != _dev(dest_p.parent):
            return "cross_volume", None                      # before any copy (9.1)
        meta = read_meta(gen) or {}
        if time.time() - float(meta.get("refreshed_at") or 0) > s.max_lag_s:
            with contextlib.suppress(Exception):
                refresh(slug, url=url, lock_timeout=CREATE_LOCK_TIMEOUT_S, root=troot)
            gen = current_gen(rdir)
            if gen is None:
                return "no_template", None
        retire: Optional[str] = None
        reason: Optional[str] = None
        err: Optional[int] = None
        with _flock(rdir / ".lock", fcntl.LOCK_SH, CREATE_LOCK_TIMEOUT_S) as got:
            if not got:
                return "lock_timeout", None
            gen = current_gen(rdir)
            if gen is None:
                reason = "no_template"
            else:
                reason = template_problem(gen)
                retire = reason
                meta = read_meta(gen) or {}
            if reason is None and meta.get("unsupported"):
                reason = "unsupported_tree"
            if reason is None and not cow_probe(gen.parent):
                reason = "no_cow_fs"
            if reason is None:
                T = meta["head"]
                row["T"] = T
                staging = dest_p.parent / f".{dest_p.name}.cow-{os.getpid()}"
                _rm(staging)
                staging.mkdir()
                names = sorted(n for n in os.listdir(gen) if n not in (".git", META))
                try:
                    _clone_entries(gen, names, staging)
                except OSError as exc:
                    reason, err = "clone_errno", exc.errno
                else:
                    if not verify_staging(gen, staging, T, staging.with_name(staging.name + ".idx")):
                        reason = "verify_failed"
        if retire:
            with _flock(rdir / ".lock", fcntl.LOCK_EX, 2.0) as ex:
                if ex and gen is not None and gen == current_gen(rdir):
                    mark_bad(gen, retire)
        return (reason, err) if reason is not None else None

    try:
        pre = prepare()
    except Exception as exc:  # noqa: BLE001 - a create never dies where plain git would work
        print(f"worktree-cache: {exc!r}; falling back to plain git", file=sys.stderr)
        pre = ("clone_errno", exc.errno if isinstance(exc, OSError) else None)
    if pre is not None:
        _rm(staging)
        staging = None
        with contextlib.suppress(OSError):
            os.unlink(str(dest_p.parent / f".{dest_p.name}.cow-{os.getpid()}.idx"))
        return fallback(pre[0], err=pre[1])

    # Register (step 5): an ordinary --no-checkout clone of the real remote.
    index_src = staging.with_name(staging.name + ".idx")
    try:
        res = _git(*_clone_cmd(url, dest_p, [f"--branch={branch}"] if branch else []))
        if res.returncode != 0:
            sys.stderr.write(res.stderr.decode("utf-8", "replace"))
            _rm(staging)
            return res.returncode
        with contextlib.suppress(Exception):
            kanban_clone.arm_fleet_prepush(dest_p, url)
        head = _git("rev-parse", "--verify", "-q", f"{rev or 'HEAD'}^{{commit}}", cwd=dest_p)
        if head.returncode != 0:
            # An unknown --rev: let plain git produce its own error from a fresh clone.
            _rm(staging)
            _rm(dest_p)
            return fallback("far_commit")
        C = _out(head)
        row["C"] = C
        far = _git("cat-file", "-e", f"{T}^{{commit}}", cwd=dest_p).returncode != 0
        if not far and C != T:
            diff = _git("diff", "--name-only", "-z", T, C, cwd=dest_p)
            far = diff.returncode != 0 or diff.stdout.count(b"\0") > s.max_diff_files
        if not far and C != T and (_attributes_changed(dest_p, T, C) or unsupported_at(dest_p, C)):
            # C changes attributes or adds a submodule / filter / ident / encoding T lacks:
            # the copied files would skip git's conversion, so plain git writes this tree.
            _rm(staging)
            staging = None
            _rm(dest_p)
            return fallback("unsupported_tree")
        if far:
            # Finish in place exactly as plain git would: write the whole tree of C.
            _rm(staging)
            staging = None
            _must(_git("read-tree", "-u", "--reset", C, cwd=dest_p), "checkout")
            if rev:
                _must(_git("update-ref", "--no-deref", "HEAD", C, cwd=dest_p), "detach HEAD")
            record_fallback(caller, "far_commit", slug, gen, dest_p, nbytes=meta.get("tree_bytes"))
            row.update(reason="far_commit", rc=0, ms=round((time.monotonic() - t0) * 1000))
            _log(row)
            return 0
        for name in os.listdir(staging):
            os.rename(staging / name, dest_p / name)
        staging.rmdir()
        staging = None
        os.replace(index_src, dest_p / ".git" / "index")     # same filesystem: a rename, not a copy
        _git("update-index", "-q", "--refresh", cwd=dest_p)
        if C != T:
            _must(_git("read-tree", "-m", "-u", T, C, cwd=dest_p), f"move {T[:10]} -> {C[:10]}")
            _git("update-index", "-q", "--refresh", cwd=dest_p)
        if rev:
            _must(_git("update-ref", "--no-deref", "HEAD", C, cwd=dest_p), "detach HEAD")
        st = _git("status", "--porcelain=v1", "--untracked-files=all", cwd=dest_p)
        if st.returncode != 0 or st.stdout.strip():
            raise RuntimeError(f"post-register status not clean: {_out(st)[:200]}")
    except Exception as exc:  # noqa: BLE001 - never leave a half-built tree behind
        print(f"worktree-cache: {exc}; falling back to plain git", file=sys.stderr)
        _rm(staging)
        staging = None
        _rm(dest_p)
        return fallback("verify_failed")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(index_src)
    row.update(cow=True, bytes_logical=meta.get("tree_bytes"), rc=0, ms=round((time.monotonic() - t0) * 1000))
    _log(row)
    return 0


# --------------------------------------------------------------------------- callers (build card 3)

# `hermes kanban clone` options the cache reproduces exactly: everything else (a shallow clone, a
# filter other than blob:none, --no-checkout, --single-branch, --origin, ...) asks for a .git the cache does
# not make, so it runs plain git with an ``unsupported_opts`` row.
_KANBAN_CLONE_OK = ("--quiet",)
# Options a full template copy satisfies: a blob:none partial clone is a strict subset of the
# full-object clone the cache registers (same refs, same history; blobs present, not lazy).
# Served from the cache and logged as ``opts_served``; a fallback forwards them to plain git.
_KANBAN_CLONE_SERVED = ("--filter=blob:none",)


def kanban_clone_entry(url: str, dest: Optional[str], git_opts: Sequence[str] = (),
                       s: Optional[Settings] = None) -> int:
    """``hermes kanban clone`` (scope ``kanban_clone``). Off or out of scope: exactly
    ``kanban_clone.clone`` as before, no ledger row."""
    s = s or settings()
    if not cache_active("kanban_clone", s):
        return kanban_clone.clone(url, dest, git_opts)
    branch: Optional[str] = None
    other = []
    served = []
    for opt in git_opts:
        if opt.startswith("--branch="):
            branch = opt.split("=", 1)[1]
        elif opt in _KANBAN_CLONE_SERVED:
            served.append(opt)
        elif opt not in _KANBAN_CLONE_OK:
            other.append(opt)
    if other:
        target = Path(dest or kanban_clone.default_dest(url)).absolute()
        record_fallback("kanban_clone", "unsupported_opts", repo_slug(url), None, target)
        _log({"ts": round(time.time(), 3), "caller": "kanban_clone", "repo": repo_slug(url), "mode": "clone",
              "dest": str(target), "cow": False, "reason": "unsupported_opts", "opts": other})
        return kanban_clone.clone(url, dest, git_opts)
    return clone(url, dest, caller="kanban_clone", branch=branch, quiet="--quiet" in git_opts, s=s,
                 served_opts=served)


# --------------------------------------------------------------------------- status + CLI


def dest_volume(dest: Path) -> dict:
    """The same-volume precondition for checkouts at *dest* (t_259301d7): the templates root a
    create there would use, and whether it shares the destination's ``st_dev``."""
    root = templates_root(Path(dest))
    d, r = _volume_dev(Path(dest)), _volume_dev(root)
    return {"dest": str(dest), "dest_dev": d, "templates_root": str(root), "root_dev": r,
            "same_volume": d is not None and d == r}


def status(s: Optional[Settings] = None, *, fallbacks: int = 10, dest: Optional[str] = None) -> dict:
    s = s or settings()
    root = templates_root()
    roots = templates_roots()
    repos = []
    for troot in roots:
        with contextlib.suppress(FileNotFoundError):
            for rdir in sorted(p for p in troot.iterdir() if p.is_dir() and "__" in p.name):
                gen = current_gen(rdir)
                meta = read_meta(gen) if gen else None
                repos.append({
                    "repo": rdir.name.replace("__", "/", 1),
                    "root": str(troot),
                    "current": gen.name if gen else None,
                    "generations": sorted(c.name for c in rdir.iterdir() if _GEN_ANY_RE.match(c.name)),
                    "head": (meta or {}).get("head"),
                    "lag_s": round(time.time() - float(meta["refreshed_at"])) if meta else None,
                    "unsupported": (meta or {}).get("unsupported"),
                })
    rows = []
    ledger = ledger_path()
    if ledger is not None:
        with contextlib.suppress(OSError):
            for line in ledger.read_text(encoding="utf-8-sig").splitlines()[-2000:]:
                with contextlib.suppress(ValueError):
                    row = json.loads(line)
                    if str(row.get("caller", "")).startswith("worktree_cache:"):
                        rows.append(row)
    out = {"enabled": s.enabled, "scopes": list(s.scopes), "repos_configured": list(s.repos),
           "templates_root": str(root), "templates": repos, "last_fallbacks": rows[-fallbacks:],
           "templates_roots": [{"root": str(r), "dev": _volume_dev(r)} for r in roots]}
    if dest:
        out["dest"] = dest_volume(Path(dest))
    return out


def cmd_clone(args) -> int:
    return clone(args.url, args.dest, caller=args.caller, branch=args.branch, rev=args.rev,
                 quiet=not args.verbose)


def cmd_refresh(args) -> int:
    s = settings()
    if not s.enabled and not args.force:
        print("worktree-cache: disabled (worktree_cache.enabled is false); --force to build anyway",
              file=sys.stderr)
        return 0
    slugs = list(s.repos) if args.all else [args.repo]
    rc = 0
    for root in templates_roots():          # one template per root: each volume its checkouts land on
        for slug in slugs:
            try:
                print(json.dumps(dict(refresh(slug, root=root), root=str(root)), sort_keys=True))
            except Exception as exc:  # noqa: BLE001 - report and continue with the next repo
                print(json.dumps({"repo": slug, "root": str(root), "status": "error", "error": str(exc)}),
                      file=sys.stderr)
                rc = 1
    return rc


def cmd_status(args) -> int:
    data = status(dest=getattr(args, "dest", None))
    if args.json:
        print(json.dumps(data, indent=1, sort_keys=True))
        return 0
    print(f"enabled={data['enabled']} scopes={','.join(data['scopes']) or '-'} root={data['templates_root']}")
    for r in data["templates_roots"]:
        print(f"  root {r['root']} dev={r['dev']}")
    if "dest" in data:
        d = data["dest"]
        print(f"  dest {d['dest']} dev={d['dest_dev']} -> root {d['templates_root']} dev={d['root_dev']} "
              f"same_volume={d['same_volume']}")
    for t in data["templates"]:
        print(f"  {t['repo']}: {t['current'] or 'no generation'} head={str(t['head'])[:10]} "
              f"lag={t['lag_s']}s unsupported={t['unsupported'] or '-'} gens={len(t['generations'])} "
              f"root={t['root']}")
    for r in data["last_fallbacks"]:
        print(f"  fallback {r.get('ts')} {r.get('caller')} {r.get('repo')} {r.get('reason')} {r.get('errno') or ''}")
    return 0


def cmd_active(args) -> int:
    """Exit 0 when the cache is on for ``--caller`` on this host, else 1 (the port clone
    script asks this on the pool host, whose config holds the switch)."""
    return 0 if cache_active(args.caller, settings()) else 1


def add_arguments(parser) -> None:
    sub = parser.add_subparsers(dest="worktree_cache_action")
    a = sub.add_parser("active", help="Exit 0 when the cache is on for --caller here, else 1")
    a.add_argument("--caller", required=True)
    a.set_defaults(func=cmd_active)
    c = sub.add_parser("clone", help="Clone a repo; CoW from the template when enabled for --caller")
    c.add_argument("--url", required=True)
    c.add_argument("--dest", default=None)
    c.add_argument("-b", "--branch", default=None)
    c.add_argument("--rev", default=None, help="Check out this commit detached")
    c.add_argument("--caller", required=True, help="Scope: " + ", ".join(KNOWN_SCOPES) + ", script:<name>")
    c.add_argument("-v", "--verbose", action="store_true")
    c.set_defaults(func=cmd_clone)
    r = sub.add_parser("refresh", help="Build or refresh templates (the timers call this)")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--repo", help="owner/repo")
    g.add_argument("--all", action="store_true", help="every repo in worktree_cache.repos")
    r.add_argument("--force", action="store_true", help="run even when worktree_cache.enabled is false")
    r.set_defaults(func=cmd_refresh)
    st = sub.add_parser("status", help="Templates, generations, lag, last fallbacks")
    st.add_argument("--json", action="store_true")
    st.add_argument("--dest", default=None,
                    help="Also report the templates root a checkout at DEST uses and whether it is on DEST's volume")
    st.set_defaults(func=cmd_status)
