"""``hermes kanban clone`` -- reference clones against shared fleet mirrors.

Provenance (card t_dad1edd7, split from t_b99d8978): kanban workers clone the
fleet repos by hand into their scratch workspaces. On 2026-09-23 there were 159
such clones on the Studio, 39 of hermes-home alone, each carrying a full object
store. The dispatcher creates an EMPTY scratch dir and never clones, so there
is no dispatcher call site to dedupe -- the clone has to go through a helper
the worker calls instead of ``git clone``.

The helper clones with ``git clone --reference-if-able <mirror> <url>``: the
new checkout borrows objects from a shared bare mirror via
``.git/objects/info/alternates`` and only downloads what the mirror lacks.

Mirrors live at ``<hermes root>/mirrors/<owner>/<repo>.git`` (one per GitHub
owner so the Kyzcreig and ANG-Ventures forks never share a remote). They are
created lazily on first use and refreshed by a fetch-only cron.

INVARIANT -- a mirror is NEVER pruned. Every dependent clone reads objects
out of the mirror through its alternates file; an object the mirror drops is
an object every dependent clone silently loses (``fatal: bad object``).
Mirrors are therefore created with ``gc.auto=0`` and ``gc.pruneExpire=never``
and must never see ``git gc --prune``, ``git prune`` or ``git repack -d``
without ``-k``. hermes-home's ``scripts/mirrors-never-prune-lint.py`` checks
both the mirror configs and every tracked script for that.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator, Optional, Tuple

# GitHub owners whose repos get a shared mirror. Matched case-insensitively
# (GitHub owners are); the canonical spelling here names the mirror directory.
FLEET_OWNERS = ("ANG-Ventures", "Kyzcreig")

MIRRORS_ENV = "HERMES_KANBAN_MIRRORS_ROOT"

# Config written into every mirror. gc.auto=0 stops opportunistic gc from
# ever running; gc.pruneExpire=never keeps an explicit `git gc` from pruning
# unreachable objects that a dependent clone may still point at.
MIRROR_CONFIG = (
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("gc.pruneExpire", "never"),
    ("gc.reflogExpire", "never"),
    ("gc.reflogExpireUnreachable", "never"),
    ("fetch.prune", "false"),
    ("remote.origin.fetch", "+refs/heads/*:refs/heads/*"),
)

_GITHUB_URL = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/"
    r"|ssh://git@github\.com/"
    r"|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)/(?P<repo>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?/?$"
)


def parse_fleet_repo(url: str) -> Optional[Tuple[str, str]]:
    """Return ``(owner, repo)`` for a fleet GitHub URL, else ``None``."""
    m = _GITHUB_URL.match((url or "").strip())
    if not m:
        return None
    owner = m.group("owner")
    for canonical in FLEET_OWNERS:
        if owner.lower() == canonical.lower():
            return canonical, m.group("repo")
    return None


def mirrors_root() -> Path:
    override = os.environ.get(MIRRORS_ENV, "").strip()
    if override:
        return Path(override)
    from hermes_constants import get_default_hermes_root

    # The fleet root, not the profile home: workers run with
    # HERMES_HOME=<root>/profiles/<name>, and a per-profile mirror would
    # defeat the dedupe.
    return get_default_hermes_root() / "mirrors"


def mirror_path(owner: str, repo: str) -> Path:
    return mirrors_root() / owner / (repo + ".git")


def default_dest(url: str) -> str:
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _git(*args: str, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=False
    )


@contextlib.contextmanager
def _mirror_lock(mirror: Path) -> Iterator[None]:
    """Serialise lazy creation of one mirror across concurrent workers."""
    import fcntl

    lock_path = mirror.parent / (mirror.name + ".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def configure_mirror(mirror: Path) -> None:
    for key, value in MIRROR_CONFIG:
        res = _git("--git-dir", str(mirror), "config", key, value)
        if res.returncode != 0:
            raise RuntimeError(
                f"could not set {key} on mirror {mirror}: {res.stderr.strip()}"
            )


def ensure_mirror(url: str, owner: str, repo: str) -> Path:
    """Return the mirror for owner/repo, creating it (bare, never-prune) lazily.

    Built in a temp dir beside the target and renamed into place, so a
    half-cloned mirror is never visible to a concurrent ``--reference-if-able``.
    """
    mirror = mirror_path(owner, repo)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    with _mirror_lock(mirror):
        if (mirror / "objects").is_dir():
            return mirror
        tmp = Path(tempfile.mkdtemp(prefix=f".{repo}.", suffix=".tmp", dir=mirror.parent))
        try:
            res = _git("clone", "--bare", "--quiet", url, str(tmp / "m.git"))
            if res.returncode != 0:
                raise RuntimeError(f"mirror clone of {url} failed: {res.stderr.strip()}")
            configure_mirror(tmp / "m.git")
            os.replace(tmp / "m.git", mirror)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return mirror


def clone(url: str, dest: Optional[str] = None, branch: Optional[str] = None) -> int:
    """Clone *url* into *dest*, borrowing objects from a fleet mirror if any.

    A mirror failure never fails the clone: ``--reference-if-able`` tolerates
    a missing mirror, so the worst case is an ordinary full clone plus a
    warning on stderr.
    """
    dest = dest or default_dest(url)
    cmd = ["git", "clone"]
    fleet = parse_fleet_repo(url)
    if fleet is not None:
        owner, repo = fleet
        try:
            mirror = ensure_mirror(url, owner, repo)
            cmd += ["--reference-if-able", str(mirror)]
        except Exception as exc:  # noqa: BLE001 - degrade to a plain clone
            print(
                f"kanban clone: mirror unavailable ({exc}); cloning without it",
                file=sys.stderr,
            )
    if branch:
        cmd += ["--branch", branch]
    cmd += ["--", url, dest]
    return subprocess.run(cmd, check=False).returncode
