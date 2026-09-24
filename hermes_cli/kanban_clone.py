"""``hermes kanban clone`` -- reference clones against shared fleet mirrors.

Provenance (card t_dad1edd7, split from t_b99d8978): kanban workers clone the
fleet repos by hand into their scratch workspaces. On 2026-09-23 there were 159
such clones on the Studio, 39 of hermes-home alone, each carrying a full object
store. The dispatcher creates an EMPTY scratch dir and never clones, so there
is no dispatcher call site to dedupe -- the clone has to go through a helper
the worker calls instead of ``git clone``.

The helper clones with ``git clone --reference-if-able <mirror> <url>``: the
new checkout borrows objects from a shared bare mirror via
``.git/objects/info/alternates`` and only downloads what the mirror lacks. The
git-clone options workers actually use (-q, --depth, --filter, --no-checkout,
--single-branch, --no-tags, --origin, -b, ...) are forwarded unchanged.

Mirrors live at ``<hermes fleet root>/mirrors/<owner>/<repo>.git`` (one per
GitHub owner so the Kyzcreig and ANG-Ventures forks never share a remote). The
root is the FLEET root even when HERMES_HOME is a profile dir: a per-profile
mirror would defeat the dedupe. Mirrors are created lazily on first use and
refreshed by a fetch-only cron (hermes-home scripts/mirrors-refresh.sh).

INVARIANT -- a mirror is NEVER pruned. Every dependent clone reads objects
out of the mirror through its alternates file; an object the mirror drops is
an object every dependent clone silently loses (``fatal: bad object``).
Mirrors are therefore created with:

  * ``extensions.preciousObjects=true`` (``core.repositoryformatversion=1``):
    git itself refuses to delete objects -- ``git gc --prune=now``, ``git
    prune`` and ``git repack -d`` fail instead of corrupting dependents. This
    is the load-bearing guard; it covers a human or agent at a terminal.
  * ``gc.auto=0`` / ``gc.pruneExpire=never`` / never-expire reflogs /
    ``fetch.prune=false``: defence in depth for git builds or tools that do
    not honour the extension.

hermes-home's ``scripts/mirrors-never-prune-lint.py`` checks both the mirror
configs and every tracked script for that.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple

# GitHub owners whose repos get a shared mirror. Matched case-insensitively
# (GitHub owners are); the canonical spelling here names the mirror directory.
FLEET_OWNERS = ("ANG-Ventures", "Kyzcreig")

MIRRORS_ENV = "HERMES_KANBAN_MIRRORS_ROOT"

# Config written into every mirror BEFORE it holds a single object. Order
# matters: extensions.* is only honoured once repositoryformatversion is 1.
MIRROR_CONFIG = (
    ("core.repositoryformatversion", "1"),
    ("extensions.preciousObjects", "true"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("gc.pruneExpire", "never"),
    ("gc.reflogExpire", "never"),
    ("gc.reflogExpireUnreachable", "never"),
    ("fetch.prune", "false"),
    ("remote.origin.fetch", "+refs/heads/*:refs/heads/*"),
)

# Inherited git env that would re-root or redirect a clone/fetch (the
# core.bare producer class, hermes-home tests/git_env_scrub_lint.py).
_SCRUB_ENV = (
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
)

_GITHUB_URL = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/"
    r"|ssh://git@github\.com/"
    r"|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)/(?P<repo>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?/?$"
)
_HTTP_USERINFO = re.compile(r"^(https?://)[^@/]+@")

# git-clone options forwarded verbatim: (flags, takes_value). Measured on the
# 365 real clone calls the hook denies (QA r1): -q on 108, --filter 57,
# --depth 28, --no-checkout 25, --origin 6, plus --single-branch / --no-tags.
_FORWARDED_OPTIONS = (
    (("-q", "--quiet"), False),
    (("-v", "--verbose"), False),
    (("-n", "--no-checkout"), False),
    (("--single-branch",), False),
    (("--no-single-branch",), False),
    (("--no-tags",), False),
    (("--recurse-submodules",), False),
    (("--shallow-submodules",), False),
    (("--sparse",), False),
    (("-b", "--branch"), True),
    (("-o", "--origin"), True),
    (("--depth",), True),
    (("--filter",), True),
    (("--shallow-since",), True),
    (("--shallow-exclude",), True),
)


class _Forward(argparse.Action):
    """Append the option (and its value) to ``git_opts`` in command-line order."""

    def __call__(self, parser, namespace, values, option_string=None):
        opts = list(getattr(namespace, self.dest, None) or [])
        long_flag = self.option_strings[-1]
        opts.append(long_flag if self.nargs == 0 else f"{long_flag}={values}")
        setattr(namespace, self.dest, opts)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Wire ``hermes kanban clone``'s arguments onto *parser*."""
    parser.add_argument("url", help="Repository URL")
    parser.add_argument("dest", nargs="?", default=None,
                        help="Destination directory (default: repo name)")
    for flags, takes_value in _FORWARDED_OPTIONS:
        parser.add_argument(
            *flags, dest="git_opts", action=_Forward,
            nargs=None if takes_value else 0,
            help=f"forwarded to git clone as {flags[-1]}",
        )
    parser.set_defaults(git_opts=[])


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


def strip_userinfo(url: str) -> str:
    """Drop ``user[:token]@`` from an https URL: the mirror config is shared."""
    return _HTTP_USERINFO.sub(r"\1", url)


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


def _git_env() -> dict:
    env = dict(os.environ)
    for key in _SCRUB_ENV:
        env.pop(key, None)
    return env


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], text=True, capture_output=True, check=False, env=_git_env()
    )


def _must(res: subprocess.CompletedProcess, what: str) -> None:
    if res.returncode != 0:
        raise RuntimeError(f"{what} failed: {res.stderr.strip()}")


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


def configure_mirror(mirror: Path, origin_url: str) -> None:
    for key, value in MIRROR_CONFIG:
        _must(_git("--git-dir", str(mirror), "config", key, value),
              f"setting {key} on mirror {mirror}")
    _must(_git("--git-dir", str(mirror), "config", "remote.origin.url", origin_url),
          f"setting remote.origin.url on mirror {mirror}")


def _set_head(mirror: Path, url: str) -> None:
    """Point the mirror's HEAD at the remote default branch (best effort)."""
    res = _git("ls-remote", "--symref", url, "HEAD")
    m = re.search(r"^ref:\s+(refs/heads/\S+)\s+HEAD$", res.stdout, re.MULTILINE)
    if res.returncode == 0 and m:
        _git("--git-dir", str(mirror), "symbolic-ref", "HEAD", m.group(1))


def ensure_mirror(url: str, owner: str, repo: str) -> Path:
    """Return the mirror for owner/repo, creating it (bare, never-prune) lazily.

    Built at a fixed staging path beside the target and renamed into place, so
    a half-built mirror is never visible to a concurrent ``--reference-if-able``.
    The never-prune config is written into the staging repo BEFORE the first
    fetch, so no object ever lives in a mirror that could prune it. A failed
    build leaves the staging repo for the next caller to resume (init and
    config are idempotent, fetch is incremental) rather than deleting it here:
    kanban's one directory-deletion choke point is
    ``kanban_survivor.remove_workspace_dir``.
    """
    mirror = mirror_path(owner, repo)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    with _mirror_lock(mirror):
        if (mirror / "objects").is_dir():
            return mirror
        staging = mirror.parent / f".{repo}.git.partial"
        _must(_git("init", "--bare", "--quiet", str(staging)), f"init of {staging}")
        configure_mirror(staging, strip_userinfo(url))
        # Fetch from the URL as given (it may carry credentials) without
        # persisting it: remote.origin.url above is the stripped form.
        _must(
            _git("--git-dir", str(staging), "fetch", "--quiet", "--tags", url,
                 "+refs/heads/*:refs/heads/*"),
            f"mirror fetch of {strip_userinfo(url)}",
        )
        _set_head(staging, url)
        os.replace(staging, mirror)
    return mirror


def clone(url: str, dest: Optional[str] = None, git_opts: Sequence[str] = ()) -> int:
    """Clone *url* into *dest*, borrowing objects from a fleet mirror if any.

    *git_opts* are git-clone options forwarded verbatim (``--depth=1``,
    ``--filter=blob:none``, ``--no-checkout``, ``--branch=x``, ...).

    A mirror failure never fails the clone: ``--reference-if-able`` tolerates
    a missing mirror, so the worst case is an ordinary clone plus a warning on
    stderr.
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
    cmd += list(git_opts)
    cmd += ["--", url, dest]
    return subprocess.run(cmd, check=False, env=_git_env()).returncode
