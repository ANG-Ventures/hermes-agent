"""Worktree cards on pool hosts: clone from origin + the host's git mirror.

A ``--workspace worktree:`` card used to be Studio-only (``portable()`` rule
``workspace_kind``): its checkout is a linked worktree of a Studio repo. When
the repo has a mirror on a pool host (``/srv/ci/git-mirrors/*.git`` on
ci-box / ace-ai / ace-media), the card can run there instead (t_6d9e7d90):
the dispatcher clones origin into the card's scratch path on that host with
the mirror as ``--reference --dissociate`` (~13 s for hermes-home on
ci-box), checks out the card's branch (``wt/<id>`` unless set), and converts
the card to ``scratch`` at that path. Every rerun binds to that host
(:func:`ported_host`, independent of ``placement.continuation``). Remote GC
(``kanban_survivor.remove_workspace_dir`` remote path) deletes the clone
only when it is clean and every local commit is on a remote, so unpushed
work is held, never collected.

Which repos: ``<fleet>/kanban-worktree-mirrors.json`` (schema 1)::

    {"schema": 1, "repos": {"ANG-Ventures/hermes-home":
        {"mirror": "/srv/ci/git-mirrors/hermes-home-mirror.git",
         "hosts": ["ci-box", "ace-ai", "ace-media"]}}}

Absent / unreadable / malformed file = no repo ports (fail closed; the card
stays Studio-local as before). That file is the kill switch: drop a row.

Only an ANCHOR worktree ports (``workspace_path`` unset, or naming a repo
root): a concrete target path is a place on the Studio someone chose. A card
whose ``.worktrees/<id>`` already exists on the Studio keeps it (that tree
may hold a prior run's work).

DERIVED mode (t_1d2c5939, default; ``KANBAN_MIRROR_DERIVED=0`` or a file
``"derived": false`` restores the hand list above): every GitHub repo a card
anchors on ports to EVERY pool host; there is no per-repo allowlist. The
file is only an override: schema 2 ``{"schema": 2, "pins": [{"repo": slug,
"hosts": [host, ...], "why": "..."}], "mirror_paths": {slug: path | {"*":
path, host: path}}}`` (schema 1 rows are still read as mirror paths; their
``hosts`` are ignored). ``pins`` ships EMPTY (Ace 2026-11-10, t_20fa16c7): a
row is an explicit operator choice of named hosts, never a default. The
retired ``exclude`` list and a malformed row are ignored with one loud log
line, never a silent Studio pin. A repo with no mirror path uses
``~/.hermes/mirrors/<owner>/<repo>.git`` on the host; the clone is
``--reference-if-able``, so an absent mirror is a slower clone, never a
refusal. A card branch with commits origin lacks is PUSHED at spawn
(``--force-with-lease``) and then ported (:func:`push_card_branch`). A card
whose own Studio tree (``.worktrees/<id>``, a prior run's too, t_20fa16c7)
is dirty ports as well: its changed files ride the clone's ssh call as a tar
(t_894be273, :func:`carry_payload`); nothing uncommitted is pushed anywhere,
and the Studio tree is left as it is. A tree off its branch whose HEAD
contains the branch has that HEAD pushed as the card branch tip.
The reasons a GitHub worktree card may still stay on the Studio are
:data:`LOCAL_ONLY_REASONS`, each with its justification;
``scripts/check_worktree_local_reasons.py`` fails CI on any other.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: the in-process cache is the only pending marker
    fcntl = None

MIRRORS_FILE = "kanban-worktree-mirrors.json"
DERIVED_ENV = "KANBAN_MIRROR_DERIVED"
# Per-host default mirror of a derived repo; "~/" expands on the host (clone_script).
DERIVED_MIRROR_ROOT = "~/.hermes/mirrors"
PUSH_TIMEOUT_S = 120
# Named local-only reasons (``why`` of :func:`assess`), the tail of not_portable:workspace_kind:<why>.
WHY_DIRTY = "unpushed_dirty_tree"          # hand-list mode / carry kill switch only
WHY_OFF_BRANCH = "own_tree_off_branch"     # hand-list mode / carry kill switch only
WHY_CONCRETE = "concrete_target_path"
WHY_NOT_REPO = "not_a_repo"
WHY_NOT_GITHUB = "origin_not_github"
WHY_FOREIGN_ORIGIN = "origin_not_ours"
WHY_NO_POOL = "no_pool_host"
WHY_PINNED = "operator_pin"
WHY_DIVERGED = "diverged_from_origin"
WHY_UNPUSHED = "unpushed_commits"          # hand-list mode only
WHY_NOT_CHECKOUT = "own_tree_not_a_checkout"
WHY_PROBE_FAILED = "own_tree_probe_failed"
WHY_REPO_MISMATCH = "own_tree_repo_mismatch"
WHY_DERIVED_OFF = "derived_mode_off"
# A dirty own tree is carried to the clone (t_894be273) except in these cases, where the card stays local:
WHY_NESTED_REPO = "dirty_submodule_or_nested_repo"   # a modified submodule or an embedded repository
WHY_CARRY_SIZE = "dirty_tree_over_cap"               # more than DIRTY_CARRY_MAX_BYTES of changed files
WHY_BASE_MOVED = "origin_tip_not_studio_head"        # the clone would not start at the tree's HEAD
WHY_SYMLINK_PARENT = "dirty_path_through_symlink"     # a changed path under a symlinked directory (Prism 9e03f1e94c2d)
WHY_SPECIAL_FILE = "dirty_path_not_plain_file"        # a changed path that is not a regular file or a symlink
WHY_UNENUMERATED = "unenumerated_local_reason"        # _why got a reason outside LOCAL_ONLY_REASONS (a code bug)
# Every reason a GitHub worktree card may stay on the Studio, with why it must. The lint
# (scripts/check_worktree_local_reasons.py) keeps its own copy of the keys: adding a reason
# changes both, so it shows in review (t_20fa16c7, Ace 2026-11-10: "All machines can make any checkout always").
LOCAL_ONLY_REASONS: Dict[str, str] = {
    WHY_NO_POOL: "no pool host is configured or reachable this tick; the Studio is the only host left",
    WHY_FOREIGN_ORIGIN: "the card branch needs a push and origin is an upstream we do not own (t_eac2768a): "
                        "nothing can publish the Studio's commits, so it runs where the work is",
    WHY_NOT_GITHUB: "origin is not a GitHub repo, so no pool host can clone it",
    WHY_NOT_REPO: "the workspace path is not a git checkout (nothing to clone)",
    WHY_DIVERGED: "origin's card branch has commits the Studio lacks; a push would drop them",
    WHY_PINNED: "an operator pin row in fleet/kanban-worktree-mirrors.json names the hosts (explicit, nightly-listed) "
                "and no pool host is among them; the card runs on the Studio only when the row names it, else it "
                "waits, named (t_fbe8a067)",
    WHY_CONCRETE: "a path that is neither a repo root nor this card's own .worktrees/<id> (another card's tree, "
                  "a sub-directory), or the host_preference kanban switch is off (Studio not last resort)",
    WHY_NOT_CHECKOUT: "the card's .worktrees/<id> holds files but is not a git checkout, or has no index: git "
                      "cannot see or carry them",
    WHY_PROBE_FAILED: "git could not read whether the card's own Studio tree has an index (error or timeout): "
                      "unknown state is never read as an empty tree",
    WHY_REPO_MISMATCH: "the card's .worktrees/<id> belongs to another repo than the directory it sits in (its git "
                       "common dir differs, a hand-made `worktree add` into a foreign tree): porting the parent would "
                       "clone the wrong repo, so the pin is read from the worktree's own repo and the card stays "
                       "(Prism cf6083fccb97)",
    WHY_OFF_BRANCH: "the Studio tree is off its branch and its HEAD does not contain the card branch's commits "
                    "(putting the branch at it would drop them), or the carry kill switch / hand-list mode is on",
    WHY_NESTED_REPO: "a modified submodule or an embedded repository: git status cannot see the work inside it",
    WHY_CARRY_SIZE: "the changed files exceed the 25 MiB carry cap (the survivor-capture cap for the same content)",
    WHY_BASE_MOVED: "origin's live card branch is not the tree's HEAD, so the carried changes have no base there",
    WHY_SYMLINK_PARENT: "a changed path under a symlinked directory would read files from outside the tree",
    WHY_SPECIAL_FILE: "a changed path is a FIFO, socket or device; tar cannot carry it faithfully",
    WHY_DIRTY: "kill switch (KANBAN_DIRTY_CARRY=0 / dirty_carry: false, or hand-list mode), or git status of "
               "the tree failed: uncommitted work in the Studio tree",
    WHY_UNPUSHED: "hand-list mode only (derived: false): committed work origin lacks",
    WHY_DERIVED_OFF: "hand-list mode (derived: false / KANBAN_MIRROR_DERIVED=0) and the repo has no row",
    WHY_UNENUMERATED: "a code bug: a path named a reason that is not in this table; _why records this instead and "
                      "logs the offending string, so no unlisted reason reaches the dispatcher",
}
PORTED_EVENT = "worktree_ported"
# portable() bind rule of a ported card: it runs where its clone is.
PORT_BIND_RULE = "worktree_port"
CLONE_TIMEOUT_S = 300
# Exactly github.com, in the three shapes git accepts (Prism afc85da13a79):
# a host merely ENDING in github.com, or a path containing it, is no match.
_SLUG_RE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")
_ORIGIN_TTL_S = 300.0
_origin_cache: Dict[str, Tuple[float, Optional[str]]] = {}
_log = logging.getLogger(__name__)


def read_mirrors(fleet_dir: Path) -> Dict[str, Tuple[str, Tuple[str, ...], Dict[str, str]]]:
    """``{owner/repo (lower): (mirror path, hosts, {host: mirror path})}``; {} on any doubt.

    ``mirrors`` (optional, beside ``mirror``) overrides the path per host: macOS
    hosts have a sealed root and cannot hold ``/srv`` (mbp-2026 keeps them under
    ``/Users/Shared/git-mirrors``). A malformed ``mirrors`` drops the whole row."""
    try:
        doc = json.loads((Path(fleet_dir) / MIRRORS_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    repos = doc.get("repos") if isinstance(doc, dict) and doc.get("schema") == 1 else None
    if isinstance(doc, dict) and doc.get("schema") == 2 and "repos" not in doc:
        # Frozen rollback snapshot, not an editable allowlist.
        repos = {
            slug: {"mirror": mirror, "hosts": ["ci-box", "ace-ai", "ace-media", "nas-fleet"]}
            for slug, mirror in (
                ("ANG-Ventures/hermes-home", "/srv/ci/git-mirrors/hermes-home-mirror.git"),
                ("ANG-Ventures/hermes-agent", "/srv/ci/git-mirrors/hermes-mirror.git"),
                ("ANG-Ventures/prism-router", "/home/kanbanw/.hermes/mirrors/ANG-Ventures/prism-router.git"),
                ("ANG-Ventures/fleetreview-router", "/home/kanbanw/.hermes/mirrors/ANG-Ventures/prism-router.git"))}
    out: Dict[str, Tuple[str, Tuple[str, ...], Dict[str, str]]] = {}
    if not isinstance(repos, dict):
        return {}
    for slug, row in repos.items():
        if not isinstance(slug, str) or "/" not in slug or not isinstance(row, dict):
            continue
        mirror, hosts, per_host = row.get("mirror"), row.get("hosts"), row.get("mirrors", {})
        if not (isinstance(mirror, str) and mirror.startswith("/")) or not isinstance(hosts, list):
            continue
        if not isinstance(per_host, dict) or not all(
                isinstance(h, str) and h and isinstance(p, str) and p.startswith("/")
                for h, p in per_host.items()):
            continue
        hs = tuple(h.lower() for h in hosts if isinstance(h, str) and h)
        if hs:
            out[slug.lower()] = (mirror, hs, {h.lower(): p for h, p in per_host.items()})
    return out


def _read_doc(fleet_dir: Path):
    """The parsed override file; ``None`` when absent, ``False`` when unreadable/malformed."""
    path = Path(fleet_dir) / MIRRORS_FILE
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError:
        return False
    try:
        doc = json.loads(text)
    except ValueError:
        return False
    return doc if isinstance(doc, dict) else False


def derived_enabled(fleet_dir: Path) -> bool:
    """Derived mode (t_1d2c5939) unless ``KANBAN_MIRROR_DERIVED=0`` or the file says ``"derived": false``."""
    if os.environ.get(DERIVED_ENV, "").strip().lower() in ("0", "false", "no", "off"):
        return False
    doc = _read_doc(fleet_dir)
    return not (isinstance(doc, dict) and doc.get("derived") is False)


_fault_logged: Dict[str, str] = {}


def _config_fault(fleet_dir: Path, what: str) -> None:
    """One loud log line per distinct fault (t_20fa16c7): a bad override row is IGNORED, never a Studio pin."""
    key = str(fleet_dir)
    if _fault_logged.get(key + what) is None:
        if len(_fault_logged) > 256:
            _fault_logged.clear()
        _fault_logged[key + what] = what
        _log.error("kanban worktree port: fleet/%s config fault, IGNORED (no repo is pinned by it): %s",
                   MIRRORS_FILE, what)


def read_overrides(fleet_dir: Path) -> Tuple[Dict[str, Tuple[str, ...]], Dict[str, Tuple[Optional[str], Dict[str, str]]]]:
    """Derived-mode overrides: ``({slug (lower): pinned hosts}, {slug (lower): (default mirror | None, {host: mirror})})``.

    Absent file = no overrides. A file that cannot be read, the retired ``exclude`` list, schema-1 ``hosts``,
    and any malformed row are config faults: logged once, loudly, and ignored, so the repo ports to every pool
    host (Ace 2026-11-10, t_20fa16c7: "my default no pins"). Schema 1 rows count as mirror paths only."""
    doc = _read_doc(fleet_dir)
    if doc is None:
        return {}, {}
    if doc is False:
        _config_fault(fleet_dir, "unreadable or not a JSON object")
        return {}, {}
    if doc.get("schema") == 1:
        _config_fault(fleet_dir, "schema 1 per-repo `hosts` are retired (read as mirror paths only)")
        return {}, {s: (row[0], row[2]) for s, row in read_mirrors(fleet_dir).items()}
    if doc.get("schema") != 2:
        _config_fault(fleet_dir, f"schema {doc.get('schema')!r} (want 2)")
        return {}, {}
    if doc.get("exclude"):
        _config_fault(fleet_dir, "`exclude` is retired; use a `pins` row naming hosts")
    pins: Dict[str, Tuple[str, ...]] = {}
    rows = doc.get("pins", [])
    for row in rows if isinstance(rows, list) else [rows]:
        ok = (isinstance(row, dict) and isinstance(row.get("repo"), str) and "/" in row["repo"]
              and isinstance(row.get("hosts"), list) and row["hosts"]
              and all(isinstance(h, str) and h.strip() for h in row["hosts"])
              and isinstance(row.get("why"), str) and row["why"].strip())
        if not ok:
            _config_fault(fleet_dir, f"pin row without repo / non-empty hosts / why: {str(row)[:120]}")
            continue
        pins[row["repo"].lower()] = tuple(dict.fromkeys(h.strip().lower() for h in row["hosts"]))
    paths = doc.get("mirror_paths", {})
    out: Dict[str, Tuple[Optional[str], Dict[str, str]]] = {}
    for slug, val in (paths.items() if isinstance(paths, dict) else ()):
        if isinstance(val, str):
            val = {"*": val}
        if not (isinstance(slug, str) and "/" in slug and isinstance(val, dict) and val and all(
                isinstance(h, str) and h and isinstance(p, str) and (p.startswith("/") or p.startswith("~/"))
                for h, p in val.items())):
            _config_fault(fleet_dir, f"mirror_paths row ignored: {str(slug)[:80]}")
            continue
        out[slug.lower()] = (val.get("*"), {h.lower(): p for h, p in val.items() if h != "*"})
    if not isinstance(paths, dict):
        _config_fault(fleet_dir, "mirror_paths is not an object")
    return pins, out


def pin_names_studio(pinned) -> bool:
    """An operator pin row admits the Studio only when it names it (t_fbe8a067)."""
    return bool({str(h).lower() for h in (pinned or ())} & {"mac-studio", "studio"})


def repo_pin(task_id: str, workspace_path: Optional[str], board_default: Optional[str], fleet_dir: Path,
             runner: Callable = subprocess.run) -> Optional[Tuple[str, ...]]:
    """The hosts of the operator pin row for the repo a worktree card's workspace is in, or None (t_fbe8a067).

    Read before and independently of the rest of :func:`assess` (Prism 345896e571e6): an early refusal there
    (off-branch tree, failed probe, not a checkout) must not drop the pin. The repo is :func:`workspace_repo`'s,
    the same one the port reads (Prism 881a62c60cff: an own linked worktree is re-anchored to its parent repo, whose
    ``origin`` is the one ported). When no repo can be resolved (no path, not a git checkout, a non-GitHub origin)
    there is no pin row to read and the card routes as it does without one."""
    if not derived_enabled(fleet_dir):
        return None  # hand-list mode has no pin rows (its rows name mirror hosts only)
    pins = read_overrides(fleet_dir)[0]
    if not pins:
        return None
    repo = workspace_repo(task_id, workspace_path, board_default, allow_studio_tree=True)[0]
    slug = origin_slug(repo, runner) if repo is not None else None
    return pins.get(slug.lower()) if slug else None


def _toplevel(path: Path) -> Optional[Path]:
    # Once per board tick per path (t_97a78e19): N worktree cards on one repo forked 3 git each per tick.
    from hermes_cli import kanban_db_workspace as _kbw
    from hermes_cli.kanban_tick_steps import tick_memo
    return tick_memo(("wt_toplevel", str(path)), lambda: _kbw._git_toplevel(path), inner=True)


def _linked(path: Path) -> bool:
    from hermes_cli import kanban_db_workspace as _kbw
    from hermes_cli.kanban_tick_steps import tick_memo
    return tick_memo(("wt_linked", str(path)), lambda: _kbw._is_linked_worktree_checkout(path), inner=True)


def _common_dir(path: Path) -> Optional[Path]:
    """``path``'s resolved git common dir, or None (unreadable); memoized per tick like :func:`_toplevel`."""
    from hermes_cli import kanban_db_workspace as _kbw
    from hermes_cli.kanban_tick_steps import tick_memo

    def _read() -> Optional[Path]:
        common = _kbw._git_common_dir(path)
        return common.resolve() if common is not None else None
    return tick_memo(("wt_common", str(path)), _read, inner=True)


def _own_tree_of(tree: Path, anchor: Path) -> Optional[str]:
    """THE check that ``tree`` is a card's own tree of the repo ``anchor`` (Prism cf6083fccb97, b7d84f5e8135):
    None when it is a linked worktree, its own toplevel, with ``anchor``'s git common dir. Else the reason:
    :data:`WHY_NOT_CHECKOUT` (not a linked checkout of its own) or :data:`WHY_REPO_MISMATCH` (a worktree of
    another repo placed at ``anchor/.worktrees/<id>``; an unreadable common dir is never read as a match).
    :func:`workspace_repo` (both persisted-path forms) and :func:`assess`'s discovery call it, nothing else."""
    if not (_linked(tree) and _toplevel(tree) == tree.resolve()):
        return WHY_NOT_CHECKOUT
    common = _common_dir(tree)
    if common is None or common != _common_dir(anchor):
        return WHY_REPO_MISMATCH
    return None


def _main_tree(top: Path) -> Path:
    """A linked worktree's main working tree (its common dir's parent, read from the repo config git itself keeps,
    so a worktree-local url rewrite cannot change the answer), else ``top`` (not linked, bare, unreadable)."""
    if not _linked(top):
        return top
    from hermes_cli import kanban_db_workspace as _kbw
    common = _kbw._git_common_dir(top)
    main = common.parent if common is not None and common.name == ".git" else None
    return main if main is not None and _toplevel(main) == main.resolve() else top


def workspace_repo(task_id: str, workspace_path: Optional[str], board_default: Optional[str], *,
                   allow_studio_tree: bool = False) -> Tuple[Optional[Path], Optional[Path], Optional[str]]:
    """THE answer to "which repo is this worktree card's workspace in" (t_fbe8a067, Prism 881a62c60cff):
    ``(repo | None, own linked tree | None, refusal | None)``. :func:`assess` (the port) and :func:`repo_pin` (the
    pin) both call it, so they read ``origin`` from the same repo. ``repo`` does not depend on
    ``allow_studio_tree``; only ``refusal`` (the reason :func:`assess` keeps the card local) does.

    A not-yet-made ``<repo>/.worktrees/<x>`` is its repo (t_e604c0f9; refused unless it is this card's and the
    knob allows it, Prism 7eba47409a75). The card's own linked ``<repo>/.worktrees/<id>`` is its parent repo
    (refused with the knob off). Any other concrete path or linked worktree: the repo it is in, refused."""
    raw = (workspace_path or "").strip() or (board_default or "").strip()
    if not raw:
        return None, None, WHY_NOT_REPO
    p = Path(raw).expanduser()
    target = worktree_target_anchor(p)
    if target is not None:
        try:
            repo = _toplevel(target) if target.is_dir() else None
        except OSError:
            repo = None
        if repo is None:
            return None, None, WHY_NOT_REPO
        if not (allow_studio_tree and p.name == task_id):
            return repo, None, WHY_NOT_REPO  # knob off or another card's target: today's Studio pin
        if repo != target.resolve() or _linked(repo):
            return repo, None, WHY_CONCRETE
        return repo, None, None
    try:
        if not p.is_absolute() or not p.is_dir():
            return None, None, WHY_NOT_REPO
    except OSError:
        return None, None, WHY_NOT_REPO
    top = _toplevel(p)
    if top is None:
        return None, None, WHY_NOT_REPO
    if top != p.resolve() or _linked(top):
        if not (top == p.resolve() and top.name == task_id and top.parent.name == ".worktrees" and _linked(top)):
            return _main_tree(top), None, WHY_CONCRETE
        anchor = top.parent.parent.resolve()
        if _toplevel(anchor) != anchor or _linked(anchor):
            return _main_tree(top), None, WHY_CONCRETE
        bad = _own_tree_of(top, anchor)
        if bad is not None:
            # A worktree of repo A placed at B/.worktrees/<id> (Prism cf6083fccb97): the repo is A, never B.
            return _main_tree(top), None, WHY_REPO_MISMATCH if bad == WHY_REPO_MISMATCH else WHY_CONCRETE
        return anchor, top, None if allow_studio_tree else WHY_CONCRETE
    own = top / ".worktrees" / task_id
    if own.exists() and _own_tree_of(own, top) == WHY_REPO_MISMATCH:
        # The path names B and B/.worktrees/<id> is A's (Prism b7d84f5e8135): the repo is A here too.
        return _main_tree(own), None, WHY_REPO_MISMATCH
    return top, None, None


def derived_route(slug: str, fleet_dir: Path, pool_hosts, why: Optional[List[str]] = None
                  ) -> Optional[Tuple[str, Tuple[str, ...], Dict[str, str]]]:
    """``(mirror, hosts, {host: mirror})`` for ``slug`` in derived mode: every pool host (or the hosts an operator
    pin row names), the override path or ``~/.hermes/mirrors/<slug>.git``. None (reason appended to ``why``)
    when no pool host is left."""
    pins, paths = read_overrides(fleet_dir)
    hosts = tuple(dict.fromkeys(str(h).lower() for h in (pool_hosts or ()) if h))
    if not hosts:
        _why(why, WHY_NO_POOL)
        return None
    pinned = pins.get(slug.lower())
    if pinned is not None:
        hosts = tuple(h for h in hosts if h in pinned)
        if not hosts:
            _why(why, WHY_PINNED)  # pinned to hosts outside the pool: local only when the pin names the Studio
            return None
    default, per_host = paths.get(slug.lower(), (None, {}))
    return (default or f"{DERIVED_MIRROR_ROOT}/{slug}.git"), hosts, dict(per_host)


def _why(why: Optional[List[str]], reason: str) -> None:
    """The ONLY writer of a ``why`` list (Prism b4c7ade40781): a reason outside :data:`LOCAL_ONLY_REASONS` is
    recorded as :data:`WHY_UNENUMERATED` with one loud log line naming it. Never raises (the dispatcher tick)."""
    if why is not None and not why:
        if not isinstance(reason, str) or reason not in LOCAL_ONLY_REASONS:
            _log.error("kanban worktree port: unenumerated local-only reason %r recorded as %s (add it to "
                       "LOCAL_ONLY_REASONS and scripts/check_worktree_local_reasons.py ENUMERATED, or fix the "
                       "caller)", reason, WHY_UNENUMERATED)
            reason = WHY_UNENUMERATED
        why.append(reason)


def mirror_for(port: tuple, host: str) -> str:
    """The mirror path ``host`` holds for a :func:`port_for` result."""
    overrides = port[3] if len(port) > 3 else {}
    return overrides.get(str(host).lower(), port[1])


def origin_slug(repo: Path, runner: Callable = subprocess.run) -> Optional[str]:
    """``owner/repo`` of ``repo``'s ``origin`` (GitHub URLs only), cached."""
    key = str(repo)
    hit = _origin_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _ORIGIN_TTL_S:
        return hit[1]
    slug = None
    try:
        proc = runner(["git", "-C", key, "remote", "get-url", "origin"], capture_output=True,
                      text=True, encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
        if getattr(proc, "returncode", 1) == 0:
            m = _SLUG_RE.match((proc.stdout or "").strip())
            slug = m.group(1) if m else None
    except (OSError, subprocess.SubprocessError):
        slug = None
    _origin_cache[key] = (time.monotonic(), slug)
    return slug


def _clean_on_branch(tree: Path, branch: str, runner: Callable) -> bool:
    """``tree`` is checked out on ``branch`` with nothing uncommitted or
    untracked (ignored files excepted): a clone of the pushed branch loses
    nothing that only this tree holds."""
    def _out(*args):
        try:
            # --no-optional-locks: a status probe never refreshes the Studio tree's index.
            proc = runner(["git", "--no-optional-locks", "-C", str(tree), *args], capture_output=True, text=True,
                          encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if getattr(proc, "returncode", 1) == 0 else None
    cur = _out("branch", "--show-current")
    if cur is None or cur.strip() != branch:
        return False
    status = _out("status", "--porcelain", "--untracked-files=all")
    return status is not None and status.strip() == ""


def worktree_target_anchor(path: Path, name: Optional[str] = None) -> Optional[Path]:
    """The repo root a NOT-YET-CREATED ``<repo>/.worktrees/<name>`` target names, else None (t_e604c0f9). An
    existing target is never re-anchored: it may hold a prior run's work. ``name`` None accepts any leaf (the
    ``kanban create`` rewrite, before the card has an id); else the leaf must be that card id."""
    try:
        if not path.is_absolute() or path.exists() or path.parent.name != ".worktrees":
            return None
    except OSError:
        return None
    if name is not None and path.name != name:
        return None
    return path.parent.parent


def anchor_repo(task_id: str, workspace_path: Optional[str],
                board_default: Optional[str], branch: Optional[str] = None,
                runner: Callable = subprocess.run, *,
                allow_studio_tree: bool = False) -> Optional[Path]:
    """The repo root a worktree card would anchor on, or None (not portable).

    None for: a concrete target path; a LINKED worktree (a prior Studio run
    persisted ``<repo>/.worktrees/<id>``, whose toplevel is itself, Prism
    4a24c6d61776); an existing ``<repo>/.worktrees/<id>``; and a card branch
    that exists locally with commits origin does not have (or no origin ref
    at all), since the clone could not start from them (Prism 044612a3554e).

    ``allow_studio_tree`` (the Studio is a ``host_preference`` last-resort host
    for kanban in ``fleet/allocation-policy.json``, t_c2fce87e): the
    card's OWN ``<repo>/.worktrees/<id>`` (persisted path or found under the
    anchor) no longer pins it to the Studio when that tree is on the card's
    branch, clean, and the branch is fully on origin (the check below). Any
    other concrete path still keeps the card local.

    The hand-list semantics: committed-but-unpushed work keeps the card local.
    :func:`assess` with ``push_ok=True`` is the derived mode's push-then-port."""
    top, _push = assess(task_id, workspace_path, board_default, branch, runner,
                        allow_studio_tree=allow_studio_tree, push_ok=False)
    return top


def assess(task_id: str, workspace_path: Optional[str], board_default: Optional[str],
           branch: Optional[str] = None, runner: Callable = subprocess.run, *,
           allow_studio_tree: bool = False, push_ok: bool = False,
           why: Optional[List[str]] = None,
           carry_out: Optional[list] = None) -> Tuple[Optional[Path], Optional[Tuple[str, str, str, str]]]:
    """``(anchor repo | None, push)``: :func:`anchor_repo`'s checks, with the reason a card stays local
    appended to ``why``. ``push_ok`` (derived mode, t_1d2c5939): a card branch whose commits origin lacks is
    no longer a refusal; ``push`` = ``(repo, branch, expected origin sha or "", assessed local sha)`` and the spawn pushes it
    (:func:`push_card_branch`) before the clone. Still local: a branch that diverged from origin (a push would
    drop origin's commits).

    ``carry_out`` (with ``push_ok``, t_894be273): a dirty own tree no longer stays local. Its changed files are
    copied straight from the Studio into the pool clone (:func:`carry_payload`), never pushed anywhere;
    ``(own tree, HEAD sha, digest)`` is appended to ``carry_out`` and the clone is pinned to that HEAD. Still
    local, named: a modified submodule or nested repository, changed files over the 25 MiB cap, or origin's
    live card branch (``ls-remote``) not at the tree's HEAD (nor about to be, by the leased push).

    t_20fa16c7 (Ace: "All machines can make any checkout always"): a PRIOR RUN's own ``.worktrees/<id>`` is no
    longer a pin by itself; it is assessed like any own tree. An own tree OFF the card branch (detached, or on
    another branch) ports when its HEAD contains the card branch's commits: that HEAD's COMMITS become the
    pushed branch tip (committed work, like any unpushed card branch), its uncommitted files ride the carry."""
    top, own_tree, refusal = workspace_repo(task_id, workspace_path, board_default,
                                            allow_studio_tree=allow_studio_tree)
    if refusal is not None or top is None:
        _why(why, WHY_CONCRETE if refusal == WHY_CONCRETE else  # workspace_repo's three refusals
             WHY_REPO_MISMATCH if refusal == WHY_REPO_MISMATCH else WHY_NOT_REPO)
        return None, None
    b = (branch or "").strip() or f"wt/{task_id}"
    if own_tree is None and (top / ".worktrees" / task_id).exists():
        own_tree = top / ".worktrees" / task_id
        if not allow_studio_tree:
            _why(why, WHY_CONCRETE)
            return None, None  # host_preference off: a prior run's local worktree keeps the card (old rule)
        bad = _own_tree_of(own_tree, top)
        if bad == WHY_REPO_MISMATCH:
            _why(why, WHY_REPO_MISMATCH)
            return None, None
        if bad is not None:
            if not _dir_empty(own_tree):
                _why(why, WHY_NOT_CHECKOUT)
                return None, None
            own_tree = None  # an empty leftover directory holds no work: port, leave it where it is
    if own_tree is not None:
        initialized = _tree_initialized(own_tree, runner)
        if initialized is None:
            # The probe itself failed (git error, timeout): unknown is not "never worked in" (Prism b7868d8b43cd).
            _why(why, WHY_PROBE_FAILED)
            return None, None
        if not initialized:
            # No index: git cannot see this tree's files (status reads every tracked file as deleted and an
            # untracked one is not carried). Only a tree holding nothing but `.git` was never worked in
            # (t_20fa16c7's own first spawn timed out): port, leave it as it is. Any file: fail closed
            # (Prism 9afe97134805).
            if not _only_git(own_tree):
                _why(why, WHY_NOT_CHECKOUT)
                return None, None
            # Its HEAD may still hold commits (Prism 34f9c3bc9c4c: `worktree add --no-checkout --detach <p> B`,
            # B ahead of the card branch): drop it only when HEAD is the card branch tip or an ancestor of it.
            off = _uninitialized_head_off_branch(own_tree, b, runner)
            if off is None:
                _why(why, WHY_PROBE_FAILED)
                return None, None
            if off:
                _why(why, WHY_OFF_BRANCH)
                return None, None
            own_tree = None
    carry = False
    off_head = None
    if own_tree is not None and not _clean_on_branch(own_tree, b, runner):
        # uncommitted/untracked work, or off its branch: carried into the clone when allowed (t_894be273)
        on_branch = _on_branch(own_tree, b, runner)
        if carry_out is None or not push_ok:
            _why(why, WHY_DIRTY if on_branch else WHY_OFF_BRANCH)
            return None, None
        if not on_branch:
            off_head = _tree_head(own_tree, runner)
            if not off_head:
                _why(why, WHY_OFF_BRANCH)
                return None, None
        census = dirty_census(own_tree, runner)
        if isinstance(census, str):
            _why(why, census)
            return None, None
        if census != ((), ()):
            carry = carry_digest(own_tree, census)

    def _git(*args):
        try:
            return runner(["git", "-C", str(top), *args], capture_output=True, text=True, encoding="utf-8",
                          timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None

    # The real runner only (the module-load default): an injected runner is a test seam and reads per ref.
    refs = _ref_table(top) if runner is _DEFAULT_RUNNER else None

    def _sha(ref):
        if refs is not None:
            return refs.get(ref, "")
        proc = _git("rev-parse", "-q", "--verify", ref)
        if proc is None:
            return None
        return (proc.stdout or "").strip() if getattr(proc, "returncode", 1) == 0 else ""

    remote = _sha(f"refs/remotes/origin/{b}")
    local = _sha(f"refs/heads/{b}")
    if refs is not None and (local or own_tree is not None):
        # The tick's table answers "no branch here" for the many fresh cards. A branch that exists, or a card with
        # its own tree, is re-read live before any port is accepted (Prism ce53b51a0bc2, 4ceddf92a18f): the tree
        # (or the branch itself) may be newer than the table, and a stale tip or a cached absence would port
        # origin's commit with no push and drop the tree's commits.
        refs = None
        remote = _sha(f"refs/remotes/origin/{b}")
        local = _sha(f"refs/heads/{b}")
    if remote is None or local is None:
        _why(why, WHY_NOT_REPO)
        return None, None
    if off_head is not None:
        # The tree's HEAD becomes the card branch tip: it must contain what the branch holds, locally and on
        # origin, or the push would drop commits (t_20fa16c7). The Studio's own branch ref is left as it is.
        for base, reason in ((local, WHY_OFF_BRANCH), (remote, WHY_DIVERGED)):
            if base and base != off_head:
                anc = _git("merge-base", "--is-ancestor", base, off_head)
                if anc is None or getattr(anc, "returncode", 1) != 0:
                    _why(why, reason)
                    return None, None
        local = off_head
    if carry:
        # The carried files are changes against the tree's HEAD (= local): the clone must start exactly there.
        # The tracking ref may be stale or the branch pushed from elsewhere, so read origin's real tip: it must
        # be local already, or be the lease's expected sha (the push then puts it at local).
        top_ret, push = _assess_branch(top, b, remote, local, push_ok, why, _git, own_tree)
        if top_ret is None:
            return None, None
        live = _origin_tip(top, b, runner)
        # Origin already at the assessed source tip = the push happened (a prefetch pushed it and the tracking
        # ref, never updated by that push, still shows the old sha): keep the same plan, so the key still
        # collects that clone (Prism 97743f997e44). Re-pushing it is a no-op.
        if not local or live is None or live not in ((push[2], local) if push is not None else (local,)):
            _why(why, WHY_BASE_MOVED)
            return None, None
        carry_out.append((str(own_tree), local, carry))
        return top_ret, push
    return _assess_branch(top, b, remote, local, push_ok, why, _git, own_tree)


def _assess_branch(top: Path, b: str, remote: str, local: str, push_ok: bool, why: Optional[List[str]],
                   _git: Callable, own_tree: Optional[Path]):
    """The branch half of :func:`assess`: ``(top, push)`` from the local and origin-tracking shas of ``b``."""
    if own_tree is not None and not remote and not push_ok:
        _why(why, WHY_UNPUSHED)
        return None, None  # the tree's commits must be on origin: no origin ref = local only
    if not local:
        return top, None  # no local branch at all: the clone makes it
    ahead = _git("rev-list", "--count", f"{remote}..{local}") if remote else None
    if remote and (ahead is None or getattr(ahead, "returncode", 1) != 0):
        _why(why, WHY_NOT_REPO)
        return None, None
    if remote and (ahead.stdout or "").strip() == "0":
        return top, None  # in step with origin
    if not push_ok:
        _why(why, WHY_UNPUSHED)
        return None, None  # local commits (or no origin ref): only the Studio has the baseline
    if remote:
        anc = _git("merge-base", "--is-ancestor", remote, local)
        if anc is None or getattr(anc, "returncode", 1) != 0:
            _why(why, WHY_DIVERGED)
            return None, None  # origin has commits the Studio lacks: a push would drop them
    return top, (str(top), b, remote, local)


_DEFAULT_RUNNER = subprocess.run


def _ref_table(top: Path) -> Optional[Dict[str, str]]:
    """``{refname: sha}`` of ``top``'s local branches and origin's remote-tracking refs, read with ONE
    ``for-each-ref`` per repo per board tick (t_97a78e19) instead of two ``rev-parse`` per card. None outside a
    tick, or when git fails (the caller then reads per ref, as before). A missing ref is ``""``, as ``rev-parse
    -q --verify`` answers it."""
    from hermes_cli import kanban_tick_steps as _kts
    if not _kts.active():
        return None

    def _read():
        try:
            proc = subprocess.run(["git", "-C", str(top), "for-each-ref", "--format=%(objectname) %(refname)",
                                   "refs/heads", "refs/remotes/origin"], capture_output=True, text=True,
                                  encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        if getattr(proc, "returncode", 1) != 0:
            return None
        return dict(line.split(" ", 1)[::-1] for line in (proc.stdout or "").splitlines() if " " in line)

    table = _kts.tick_memo(("wt_refs", str(top)), _read, inner=True)
    return dict(table) if table is not None else None


def _dir_empty(path: Path) -> bool:
    try:
        return not any(path.iterdir())
    except OSError:
        return False


def _tree_git(tree: Path, runner: Callable, *args):
    """``git --no-optional-locks -C tree args``: never refreshes or locks the tree's own index."""
    try:
        return runner(["git", "--no-optional-locks", "-C", str(tree), *args], capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def _git_path(tree: Path, runner: Callable, name: str) -> Optional[Path]:
    proc = _tree_git(tree, runner, "rev-parse", "--path-format=absolute", "--git-path", name)
    if proc is None or getattr(proc, "returncode", 1) != 0 or not (proc.stdout or "").strip():
        return None
    return Path(proc.stdout.strip())


def _only_git(path: Path) -> bool:
    """``path`` holds nothing but its ``.git`` entry. An unreadable directory is not empty (fail closed)."""
    try:
        return all(p.name == ".git" for p in path.iterdir())
    except OSError:
        return False


def _tree_initialized(tree: Path, runner: Callable) -> Optional[bool]:
    """The worktree's checkout completed: it has an index. ``worktree add`` writes the index last (via
    ``index.lock``), so a killed add leaves none. Its ``initializing`` lock is not the signal: an operator may
    finish such a tree by hand and work in it. None = the probe failed (unknown, never read as False)."""
    index = _git_path(tree, runner, "index")
    if index is None:
        return None
    try:
        index.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return True


def _uninitialized_head_off_branch(tree: Path, branch: str, runner: Callable) -> Optional[bool]:
    """False when the no-index tree's HEAD is ``branch``'s tip or an ancestor of it (nothing the branch lacks),
    True when it is not (or the branch does not exist), None when git cannot read it (unknown, never False)."""
    head = _tree_head(tree, runner)
    tip = _tree_git(tree, runner, "rev-parse", "-q", "--verify", f"refs/heads/{branch}")
    if not head or tip is None:
        return None
    tip_sha = (tip.stdout or "").strip() if getattr(tip, "returncode", 1) == 0 else ""
    if not tip_sha:
        return True
    if head == tip_sha:
        return False
    anc = _tree_git(tree, runner, "merge-base", "--is-ancestor", head, tip_sha)
    if anc is None or getattr(anc, "returncode", 1) not in (0, 1):
        return None
    return anc.returncode != 0


def _tree_head(tree: Path, runner: Callable) -> str:
    proc = _tree_git(tree, runner, "rev-parse", "-q", "--verify", "HEAD")
    return (proc.stdout or "").strip() if proc is not None and getattr(proc, "returncode", 1) == 0 else ""


def _on_branch(tree: Path, branch: str, runner: Callable) -> bool:
    try:
        proc = runner(["git", "-C", str(tree), "branch", "--show-current"], capture_output=True, text=True,
                      encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(proc, "returncode", 1) == 0 and (proc.stdout or "").strip() == branch


# -- stale branch_name (t_0a0c9ddc) ------------------------------------------------------------------------------
# t_1e785f52 (2026-11-12, 2 h in ready): tasks.branch_name still named the archived parent's branch (commits the
# tree lacks) while the card's own tree sat on its PR branch == origin. Every tick judged own_tree_off_branch, a
# structural hold studio-override cannot clear. A tree on another NAMED branch whose HEAD is that branch's origin
# tip holds nothing origin lacks: branch_name follows the tree. Anything else keeps today's refusal.
BRANCH_REBOUND_EVENT = "branch_rebound"
_NEVER_REBIND = frozenset({"main", "master"})


def stale_branch(task_id: str, workspace_path: Optional[str], board_default: Optional[str], branch: str,
                 runner: Callable = subprocess.run, *, allow_studio_tree: bool = False
                 ) -> Optional[Tuple[str, str, str]]:
    """``(own tree, the branch it is on, HEAD)`` when the card's own tree is on a named branch other than
    ``branch`` and its HEAD is ``origin/<that branch>``; else None. Never a default branch. Read-only."""
    top, own, refusal = workspace_repo(task_id, workspace_path, board_default, allow_studio_tree=allow_studio_tree)
    if top is None or refusal is not None:
        return None
    if own is None:
        cand = top / ".worktrees" / task_id
        if not cand.exists() or _own_tree_of(cand, top) is not None:
            return None
        own = cand
    cur = _tree_git(own, runner, "branch", "--show-current")
    cur = (cur.stdout or "").strip() if cur is not None and getattr(cur, "returncode", 1) == 0 else ""
    if not cur or cur == branch or cur in _NEVER_REBIND:
        return None
    head = _tree_head(own, runner)
    if not head:
        return None
    # The remote itself, never a local ref (Prism 9c711a6dd8f7, aef8b937133f): its default branch must resolve and
    # differ from ``cur``, and its live tip of ``cur`` must be HEAD. Unreadable or unresolved = keep the refusal.
    ls = _tree_git(own, runner, "ls-remote", "--symref", "origin", "HEAD", f"refs/heads/{cur}")
    if ls is None or getattr(ls, "returncode", 1) != 0:
        return None
    dflt, tip = None, ""
    for line in (ls.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "ref:" and parts[2] == "HEAD":
            dflt = parts[1]
        elif len(parts) == 2 and parts[1] == f"refs/heads/{cur}":
            tip = parts[0]
    if not dflt or dflt == f"refs/heads/{cur}" or head != tip:
        return None
    return str(own), cur, head


def rebind_branch(conn, task_id: str, old: str, tree: str, new: str, head: str) -> bool:
    """Point ``branch_name`` at ``new`` (CAS on ``old``, unclaimed only) and record ``branch_rebound``."""
    from hermes_cli import kanban_db as _kb
    with _kb.write_txn(conn):
        cur = conn.execute("UPDATE tasks SET branch_name=? WHERE id=? AND branch_name IS ? AND claim_lock IS NULL",
                           (new, task_id, old))
        if cur.rowcount != 1:
            return False
        _kb._append_event(conn, task_id, BRANCH_REBOUND_EVENT, {"from": old, "to": new, "head": head, "tree": tree})
    return True


def _origin_tip(repo: Path, branch: str, runner: Callable) -> Optional[str]:
    """Origin's current sha of ``refs/heads/<branch>``, read from the remote itself ("" = absent), None on error."""
    try:
        proc = runner(["git", "-C", str(repo), "ls-remote", "origin", f"refs/heads/{branch}"], capture_output=True,
                      text=True, encoding="utf-8", errors="replace", timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return ((proc.stdout or "").split() or [""])[0]


# Dirty-tree carry (t_894be273): the card's own dirty Studio tree is copied, as a tar stream over the pool ssh
# path, straight into the pool clone. Nothing is pushed to GitHub. Cap: the changed files' bytes may not exceed
# kanban_db.KANBAN_ATTACHMENT_MAX_BYTES (25 MiB), the cap the survivor capture applies to the same content.
DIRTY_CARRY_ENV = "KANBAN_DIRTY_CARRY"
DIRTY_CARRY_TIMEOUT_S = 300
DIRTY_CARRY_MAX_DELETE_ARGV = 64 * 1024


def _carry_cap() -> int:
    from hermes_cli.kanban_db import KANBAN_ATTACHMENT_MAX_BYTES
    return KANBAN_ATTACHMENT_MAX_BYTES


def dirty_carry_enabled(fleet_dir: Path) -> bool:
    """Kill switch: ``"dirty_carry": false`` in the overrides file, or ``KANBAN_DIRTY_CARRY=0``. Off = a dirty own
    tree stays local as ``unpushed_dirty_tree``, exactly as before."""
    if os.environ.get(DIRTY_CARRY_ENV, "").strip().lower() in ("0", "false", "no", "off"):
        return False
    doc = _read_doc(fleet_dir)
    return not (isinstance(doc, dict) and doc.get("dirty_carry") is False)


def dirty_census(tree: Path, runner: Callable = subprocess.run):
    """``(copy, delete)`` relative paths that make a clean checkout of ``tree``'s HEAD byte-equal to ``tree`` for
    every path git reports changed (ignored files excepted), or a ``WHY_*`` string when the tree cannot be carried:
    a modified submodule or an embedded repository (``git status`` cannot see inside it), or more changed bytes
    than the cap. A path present in the tree is copied; one absent is deleted."""
    try:
        proc = runner(["git", "-C", str(tree), "status", "--porcelain=v2", "-z", "--untracked-files=all",
                       "--ignore-submodules=none"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return WHY_DIRTY
    if getattr(proc, "returncode", 1) != 0:
        return WHY_DIRTY
    out = proc.stdout if isinstance(proc.stdout, bytes) else (proc.stdout or "").encode("utf-8", "surrogateescape")
    fields = out.split(b"\0")
    paths: List[str] = []
    i = 0
    while i < len(fields):
        rec = fields[i].decode("utf-8", "surrogateescape")
        i += 1
        if not rec:
            continue
        kind = rec[0]
        if kind in ("1", "2", "u"):
            parts = rec.split(" ", {"1": 8, "2": 9, "u": 10}[kind])
            if len(parts) < 3 or parts[2] != "N...":
                return WHY_NESTED_REPO        # a submodule entry (S...): its own work is not in this status
            paths.append(parts[-1])
            if kind == "2" and i < len(fields):  # rename/copy: the original path follows
                paths.append(fields[i].decode("utf-8", "surrogateescape"))
                i += 1
        elif kind == "?":
            p = rec[2:]
            if p.endswith("/"):
                return WHY_NESTED_REPO        # an embedded repository: git lists the directory, not its files
            paths.append(p)
    copy, delete, total = [], [], 0
    try:
        root = tree.resolve()
    except OSError:
        return WHY_DIRTY
    for rel in dict.fromkeys(paths):
        if not rel or rel.startswith("/") or any(c in ("", ".", "..", ".git") for c in rel.split("/")):
            return WHY_NESTED_REPO
        # No parent component may be a symlink (Prism 9e03f1e94c2d): lstat follows a symlinked PARENT, so a
        # tracked dir replaced by a link to elsewhere would read files from outside the tree. A parent that is
        # absent or a plain file (a tracked dir replaced by a file) means the path itself is gone: a deletion.
        gone = False
        for i in range(1, len(rel.split("/"))):
            try:
                pst = (tree / "/".join(rel.split("/")[:i])).lstat()
            except (FileNotFoundError, NotADirectoryError):
                gone = True
                break
            except OSError:
                return WHY_DIRTY
            if stat.S_ISLNK(pst.st_mode):
                return WHY_SYMLINK_PARENT
            if not stat.S_ISDIR(pst.st_mode):
                gone = True
                break
        if gone:
            delete.append(rel)
            continue
        try:
            st = (tree / rel).lstat()
        except (FileNotFoundError, NotADirectoryError):
            delete.append(rel)
            continue
        except OSError:
            return WHY_DIRTY
        try:
            (tree / rel).parent.resolve().relative_to(root)
        except (OSError, ValueError):
            return WHY_SYMLINK_PARENT
        if stat.S_ISDIR(st.st_mode):
            # git lists files, never a directory (an untracked dir or embedded repo was refused above): a
            # directory here replaced the listed tracked file; its own files are listed on their own.
            delete.append(rel)
            continue
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
            return WHY_SPECIAL_FILE
        total += st.st_size
        if total > _carry_cap():
            return WHY_CARRY_SIZE
        copy.append(rel)
    if 2 * sum(len(p.encode("utf-8", "surrogateescape")) + 3 for p in delete) > DIRTY_CARRY_MAX_DELETE_ARGV:
        return WHY_CARRY_SIZE                 # the deletions (and their parents) ride the remote command line
    return tuple(copy), tuple(delete)


def carry_digest(tree: Path, census: tuple) -> str:
    """A digest of a :func:`dirty_census` result: each copied path with its lstat size, mtime_ns, ctime_ns, mode
    and inode, and each deleted path. Part of :func:`clone_key` via the port's carry tuple (Prism b4ca10cadc5b):
    a prefetched clone whose carried files changed since is a different key, so it is never collected and the
    clone job (which carries the files as they are then) runs again."""
    h = hashlib.sha256()
    copy, delete = census
    for rel in copy:
        try:
            st = (tree / rel).lstat()
            sig = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode, st.st_ino)
        except OSError:
            sig = None
        h.update(f"c\0{rel}\0{sig}\0".encode("utf-8", "surrogateescape"))
    for rel in delete:
        h.update(f"d\0{rel}\0".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def carry_payload(own_tree: str, head: str, runner: Callable = subprocess.run) -> Tuple[bytes, tuple]:
    """``(tar bytes, deleted paths)`` that bring a clean checkout of ``head`` to ``own_tree``'s current state
    (t_894be273). :func:`clone_on_host` sends them in the clone's own ssh call (one admitted spawn site; the
    carry adds none). The census is taken here, so files changed since routing are carried as they are now.
    Raises (a spawn failure; nothing on the Studio is touched) when the tree is off ``head`` or not carriable.

    Snapshot race (Prism d9d8d2d38b45): HEAD is read before the census AND after the tar is built, and the census
    digest (paths, sizes, mtimes, inodes) is compared across the build. A commit, or any listed path changing or
    changing shape (a dir swapped for a symlink, a file appearing or going) while the tar is built, raises instead
    of sending a tar that does not match the clone's pinned HEAD."""
    tree = Path(own_tree)

    def _head_is(where: str) -> None:
        cur = runner(["git", "-C", own_tree, "rev-parse", "HEAD"], capture_output=True, text=True,
                     encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
        if getattr(cur, "returncode", 1) != 0 or (cur.stdout or "").strip() != head:
            raise RuntimeError(f"dirty carry of {own_tree}: HEAD moved off {head[:12]} {where}")

    _head_is("since routing")
    census = dirty_census(tree, runner)
    if isinstance(census, str):
        raise RuntimeError(f"dirty carry of {own_tree}: {census}")
    before = carry_digest(tree, census)
    payload = _carry_tar(tree, census[0])
    _head_is("while the carry was built")
    after = dirty_census(tree, runner)
    if isinstance(after, str) or after != census or carry_digest(tree, after) != before:
        raise RuntimeError(f"dirty carry of {own_tree}: the tree changed while the carry was built")
    return payload, census[1]


# The clone-side record of what the last carry wrote (Prism 23f7819e68e3): NUL-separated relative paths, carried
# as the tar's first member. The next carry removes exactly those before its reset, so a carried file that HEAD
# ignores never survives, and a worker's own ignored build output is never touched.
CARRY_MANIFEST = ".git/kanban-carry.manifest"


def _carry_tar(tree: Path, copy) -> bytes:
    """An uncompressed tar of ``copy`` (paths relative to ``tree``; symlinks stored as links, modes kept), led by
    :data:`CARRY_MANIFEST` listing them. Members are opened without following a symlink in any parent component
    (the census already refused one; this closes the window between census and read). Bounded by the census cap,
    so it is built in memory."""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        listing = b"".join(rel.encode("utf-8", "surrogateescape") + b"\0" for rel in copy)
        info = tarfile.TarInfo(CARRY_MANIFEST)
        info.size, info.mode, info.mtime = len(listing), 0o600, int(time.time())
        tar.addfile(info, io.BytesIO(listing))
        for rel in copy:
            _tar_add_nofollow(tar, tree, rel)
    return buf.getvalue()


def _tar_add_nofollow(tar, tree: Path, rel: str) -> None:
    """Add ``tree/rel`` to ``tar``, walking each parent with O_NOFOLLOW|O_DIRECTORY so no symlinked parent is
    traversed. A symlink member is stored as a link; a regular file is read through its own O_NOFOLLOW fd."""
    import io
    import tarfile
    parts = rel.split("/")
    flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tree), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for comp in parts[:-1]:
            nfd = os.open(comp, flags_dir, dir_fd=fd)
            os.close(fd)
            fd = nfd
        st = os.lstat(parts[-1], dir_fd=fd)
        info = tar.tarinfo(rel)
        info.mode, info.mtime, info.uid, info.gid = stat.S_IMODE(st.st_mode), int(st.st_mtime), 0, 0
        if stat.S_ISLNK(st.st_mode):
            info.type, info.linkname = tarfile.SYMTYPE, os.readlink(parts[-1], dir_fd=fd)
            tar.addfile(info)
            return
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"dirty carry: {rel} is not a regular file or a symlink")
        ffd = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
        with os.fdopen(ffd, "rb") as fh:
            data = fh.read()
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    finally:
        os.close(fd)


def carry_script(workspace: str, head: str, delete=()) -> str:
    """Remote sh of :func:`carry_dirty`. Inside the clone (a git checkout, rc 3 otherwise; HEAD proved to be
    ``head``, rc 5 otherwise): the paths the previous carry wrote (:data:`CARRY_MANIFEST`) are removed, the
    working tree is reset to HEAD (``reset --hard`` + ``clean -fd``; other ignored files are kept), the Studio's
    deleted paths are removed and then their now-empty parent directories, deepest first (a tracked directory the
    Studio replaced by a file), and the tar on stdin is unpacked. So the clone's non-ignored state equals the
    Studio tree's on every port, including a REUSED clone that an earlier carry left dirty (Prism 6d57e664864b,
    23f7819e68e3). The reset never costs a worker's work: a clone a run has used belongs to a card already
    converted to ``scratch``, which is never ported (or carried) again."""
    w = shlex.quote(workspace.rstrip("/"))
    rm = (("rm -f -- " + " ".join(shlex.quote(p) for p in delete) + "; ") if delete else "")
    parents = sorted({"/".join(p.split("/")[:i]) for p in delete for i in range(1, len(p.split("/")))},
                     key=lambda d: (-d.count("/"), d))
    rmdir = (("rmdir -- " + " ".join(shlex.quote(d) for d in parents) + " 2>/dev/null || true; ")
             if parents else "")
    return (f"set -e; cd {w}; [ -d .git ] || exit 3; [ \"$(git rev-parse HEAD)\" = {shlex.quote(head)} ] || exit 5; "
            f"{_carry_undo()}{rm}{rmdir}tar -xf -")


def _carry_undo() -> str:
    """Remote sh (cwd = the clone): remove the paths the previous carry wrote (:data:`CARRY_MANIFEST`), then reset
    the working tree to HEAD (``reset --hard`` + ``clean -fd``; other ignored files are kept). Run by
    :func:`clone_script` BEFORE a reused clone's checkout and fast-forward (a carried file the new HEAD also
    touches would refuse the merge, rc 5 on every retry; Apollo 20:4x), and again by :func:`carry_script`."""
    m = shlex.quote(CARRY_MANIFEST)
    return (f"if [ -f {m} ]; then xargs -0 rm -f -- < {m}; rm -f {m}; fi; "
            "git reset -q --hard HEAD; git clean -fdq; ")


class ForeignOriginPush(RuntimeError):
    """The card branch would be pushed to an origin the fleet does not own (t_eac2768a). Permanent: no retry
    changes the checkout's origin, so the dispatcher blocks on the first failure (``permanent_spawn_wall``)."""
    permanent_spawn_wall = True


def _own_destination(url: str) -> bool:
    """``url`` is a GitHub repo whose owner is in the fleet's own-owner list
    (:data:`hermes_cli.kanban_open_pr.FLEET_OWNERS`), or a local path (nothing is published)."""
    from hermes_cli.kanban_open_pr import FLEET_OWNERS
    m = _SLUG_RE.match(url)
    if m:
        return m.group(1).split("/", 1)[0].lower() in FLEET_OWNERS
    return url.startswith("/") or url.startswith("file://")


def _redact_url(url: str) -> str:
    """Drop URL userinfo (``https://user:token@host/...`` -> ``https://host/...``): the refusal is stored on the card."""
    return re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^@/]*@", r"\1", url)


def push_card_branch(push: Tuple[str, str, str, str], runner: Callable = subprocess.run) -> None:
    """Push the card branch to origin before the port (t_1d2c5939): ``--force-with-lease`` pinned to the
    origin sha the routing saw ("" = the branch must not exist on origin), so a branch someone moved since
    is refused, never overwritten. Raises on failure (a spawn failure; the card stays where it is).
    An origin the fleet does not own (an upstream such as NousResearch) is never pushed to:
    :class:`ForeignOriginPush`, before any push (t_eac2768a)."""
    repo, branch, expected, source = push
    # Push to the fetch URL explicitly: a remote's pushurl may name another repo.
    dest = runner(["git", "-C", repo, "remote", "get-url", "origin"], capture_output=True,
                  text=True, encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
    if dest.returncode != 0 or not dest.stdout.strip():
        raise RuntimeError("cannot resolve clone source before push")
    # An explicit remote pushurl suppresses pushInsteadOf; passing the URL
    # directly does not. Command-local configuration leaves the checkout intact.
    destination = dest.stdout.strip()
    remote_cfg = ["-c", f"remote.kanban-port-destination.url={destination}",
                  "-c", f"remote.kanban-port-destination.pushurl={destination}"]
    # pushurl is multivalued: -c APPENDS to any checkout-configured pushurl of the synthetic remote, and the
    # push goes to every one of them. Each is also rewritten by url.<base>.insteadOf at push time
    # (ls-remote --get-url applies that rewrite). Every effective destination must be ours.
    got = runner(["git", "-C", repo, *remote_cfg, "config", "--get-all", "remote.kanban-port-destination.pushurl"],
                 capture_output=True, text=True, encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
    urls = (got.stdout or "").split() if getattr(got, "returncode", 1) == 0 else []
    bad = [] if urls else [destination]
    for url in urls:
        eff = runner(["git", "-C", repo, "ls-remote", "--get-url", url], capture_output=True,
                     text=True, encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
        effective = (eff.stdout or "").strip() if getattr(eff, "returncode", 1) == 0 else ""
        if not (_own_destination(url) and effective and _own_destination(effective)):
            bad.append(_redact_url(url) if effective in ("", url)
                       else f"{_redact_url(url)} (rewritten to {_redact_url(effective)})")
    if bad or not _own_destination(destination):
        shown = ", ".join(bad or [_redact_url(destination)])
        raise ForeignOriginPush(
            f"refused to push card branch {branch}: origin {shown} of {repo} is not a fleet-owned repo, "
            "and NO retry can fix it. The card needs a checkout whose origin is ours (re-create it with "
            f"--workspace worktree:<a checkout whose origin is ANG-Ventures/* or Kyzcreig/*>). Nothing was pushed.")
    proc = runner(["git", "-C", repo, *remote_cfg,
                   "push", "--porcelain",
                   f"--force-with-lease=refs/heads/{branch}:{expected}",
                   "kanban-port-destination", f"{source}:refs/heads/{branch}"],
                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=PUSH_TIMEOUT_S, stdin=subprocess.DEVNULL)
    rc = getattr(proc, "returncode", 1)
    if rc != 0:
        raise RuntimeError(f"push of {branch} from {repo} before the port failed rc={rc}: "
                           f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")


def port_for(task_id: str, workspace_path: Optional[str], board_default: Optional[str],
             fleet_dir: Path, runner: Callable = subprocess.run, *,
             branch: Optional[str] = None,
             allow_studio_tree: bool = False,
             pool_hosts=None, why: Optional[List[str]] = None) -> Optional[tuple]:
    """``(owner/repo, mirror, hosts, {host: mirror}, push)`` when this worktree card
    may run on a pool host, else None (:func:`mirror_for` picks the path).

    Derived mode (:func:`derived_enabled`): any GitHub repo, every host in ``pool_hosts``, ``push`` set when
    the card branch must be pushed first. Hand-list mode: the file's rows only, ``push`` always None."""
    if pool_hosts is not None and derived_enabled(fleet_dir):
        carry: Optional[list] = [] if dirty_carry_enabled(fleet_dir) else None
        repo, push = assess(task_id, workspace_path, board_default, branch, runner,
                            allow_studio_tree=allow_studio_tree, push_ok=True, why=why, carry_out=carry)
        if repo is None:
            return None
        slug = origin_slug(repo, runner)
        if not slug:
            _why(why, WHY_NOT_GITHUB)
            return None
        if push is not None and not _own_destination(_origin_url(repo, runner)):
            # Named at routing, not a spawn wall (t_20fa16c7): the Studio's work cannot be published to an
            # upstream we do not own, so the card runs where the work is. push_card_branch still re-checks.
            _why(why, WHY_FOREIGN_ORIGIN)
            return None
        route = derived_route(slug, fleet_dir, pool_hosts, why)
        if not route:
            return None
        # A 6th element only for a dirty own tree to carry (t_894be273): (own tree, HEAD sha, census digest).
        return (slug, route[0], route[1], route[2], push) + ((carry[0],) if carry else ())
    mirrors = read_mirrors(fleet_dir)
    if not mirrors:
        _why(why, WHY_DERIVED_OFF)
        return None
    repo = anchor_repo(task_id, workspace_path, board_default, branch, runner,
                       allow_studio_tree=allow_studio_tree)
    slug = origin_slug(repo, runner) if repo is not None else None
    row = mirrors.get(slug.lower()) if slug else None
    if row is None:
        _why(why, WHY_DERIVED_OFF)
    return (slug, row[0], row[1], row[2], None) if row else None


def _origin_url(repo: Path, runner: Callable) -> str:
    try:
        proc = runner(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True,
                      encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip() if getattr(proc, "returncode", 1) == 0 else ""


def mint_rewrite(workspace_kind: Optional[str], workspace_path: Optional[str], fleet_dir: Path,
                 runner: Callable = subprocess.run) -> Tuple[Optional[str], Optional[str]]:
    """``kanban create`` half of t_e604c0f9: ``(workspace_path, note)``. A ``worktree:<repo>/.worktrees/<x>``
    target that does not exist yet, on a repo with a row in ``kanban-worktree-mirrors.json``, is rewritten to the
    anchor form ``worktree:<repo>`` (the dispatcher can then port it: FWA places it and the worktree is built on
    the chosen host). ``note`` says what happened, or why the card stays Studio-local; None for non-worktree cards
    and whenever the host_preference switch (t_c2fce87e) does not make the Studio a last resort: then ``kanban
    create`` stores and prints exactly what it did before (Prism 7eba47409a75)."""
    if workspace_kind != "worktree" or not workspace_path:
        return workspace_path, None
    try:
        from hermes_cli import allocation_policy as _ppol
        from hermes_cli.kanban_worker_pool import STUDIO_HOST
        if not _ppol.load(Path(fleet_dir)).is_last_resort(STUDIO_HOST):
            return workspace_path, None
    except Exception:
        return workspace_path, None
    from hermes_cli.kanban_db_workspace import _git_toplevel
    p = Path(workspace_path).expanduser()
    target = worktree_target_anchor(p)
    repo = target if target is not None else p
    top = _git_toplevel(repo) if repo.is_dir() else None
    slug = origin_slug(top, runner) if top is not None else None
    if slug and derived_enabled(fleet_dir):
        # t_1d2c5939: every GitHub repo ports to every pool host (an operator pin narrows the hosts).
        excl: List[str] = []
        pinned = read_overrides(fleet_dir)[0].get(slug.lower())
        row = derived_route(slug, fleet_dir, pinned or ("every pool host",), excl)
        if row is None:
            return workspace_path, f"worktree card stays Studio-local: {slug} {excl[0] if excl else 'not portable'}"
    else:
        row = read_mirrors(fleet_dir).get(slug.lower()) if slug else None
    if row is None:
        why = (f"{slug}: derived mode is off (kill switch) and the frozen rollback list has no row for it" if slug
               else f"{workspace_path} is not a GitHub repo checkout")
        return workspace_path, f"worktree card stays Studio-local: {why}"
    hosts = ", ".join(row[1])
    if target is not None and top == target.resolve():
        return str(top), (f"workspace rewritten: worktree:{workspace_path} -> worktree:{top} ({slug} has a "
                          f"pool clone on {hosts}; FWA places the card and the worktree is built on that host)")
    if top == p.resolve():
        return workspace_path, f"worktree card is portable ({slug} has a pool clone on {hosts}; FWA places it)"
    return workspace_path, ("worktree card stays Studio-local: a concrete existing target path "
                            f"({workspace_path}) is kept where it is")


def clone_script(workspace: str, slug: str, mirror: str, branch: str, *,
                 url: Optional[str] = None, sha: Optional[str] = None, exact: bool = False,
                 carried: bool = False) -> str:
    """Remote sh: clone origin into the (empty) workspace, then put HEAD on
    ``branch`` and PROVE it (rc 4 otherwise). The mirror only accelerates the
    clone: ``--dissociate`` copies what was borrowed, so a later mirror prune
    cannot take objects from a running card (Prism 02dfd9094b97). A never-prune
    mirror (``extensions.preciousObjects``) cannot drop them, so its clone keeps
    the alternates instead of a ~440 MB object store of its own (t_969164a0).
    With ``worktree_cache`` on for ``port_clone`` on the host, the fresh clone is
    ``hermes worktree-cache clone`` (copy-on-write from the host's template). The branch
    baseline is the clone's own ``refs/remotes/origin/<branch>`` (fetched by
    the clone), never a second network call whose failure would read as
    "absent" (Prism 9b397aa5183d); else a new branch off origin/HEAD,
    --no-track. Re-run safe: an existing checkout of THIS origin finishes
    the checkout instead of returning early (Prism f43e48b43fc3). rc 3 = the
    workspace holds something else (never overwritten). ``sha`` (the pushed
    tip, Prism 3e2f8506c8ff): a reused checkout behind it is fetched and
    fast-forwarded, and HEAD is PROVED to be it (rc 5 otherwise). ``exact``
    (a fresh re-clone of a carried scratch clone, t_9965b8d5): reset to
    ``sha`` even when it is behind the branch tip, then PROVE it (rc 5); a reused clone with uncommitted edits
    (rc 7) or commits no remote holds (rc 6) is refused before the reset, nothing reset. ``carried`` (a dirty carry follows,
    t_894be273): an earlier carry's files are undone (:func:`_carry_undo`) before the checkout and fast-forward.
    The two combine: a reused clone is undone, then put on the exact ``sha``."""
    w, m, b = shlex.quote(workspace), shlex.quote(mirror), shlex.quote(branch)
    if mirror.startswith("~/"):
        # A derived per-host default (t_1d2c5939): the host's own $HOME, whatever user/OS it runs.
        m = '"$HOME"/' + shlex.quote(mirror[2:])
    url = shlex.quote(url or f"https://github.com/{slug}.git")
    from hermes_cli.kanban_worker_hosts import WORKSPACE_MODE  # t_69e47673: pool workspace, never world-searchable
    return (
        f"set -e; w={w}; mkdir -p -m {WORKSPACE_MODE} -- \"$w\"; "
        # the mirror is owned by the runner user: trust that one path only
        f"export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0={m}; "
        "if [ -d \"$w/.git\" ]; then "
        f"[ \"$(git -C \"$w\" remote get-url origin)\" = {url} ] || exit 3; "
        + (f"( cd \"$w\" && {_carry_undo()}); " if carried else "") +
        "elif [ -n \"$(ls -A -- \"$w\")\" ]; then exit 3; "
        f"else if [ -d {m}/objects ]; then "
        f"git -c gc.auto=0 -c maintenance.auto=false -c fetch.prune=false --git-dir={m} "
        "fetch -q --no-write-fetch-head origin || true; fi; "
        # Worktree cache, scope port_clone (t_969164a0): asked on THIS host, whose config holds the switch.
        "v=\"$HOME/.local/bin/hermes\"; [ -x \"$v\" ] || v=$(command -v hermes || true); "
        "if [ \"${HERMES_WORKTREE_CACHE:-}\" != 0 ] && [ -n \"$v\" ] && \"$v\" worktree-cache active --caller port_clone >/dev/null 2>&1; then "
        f"\"$v\" worktree-cache clone --url {url} --dest \"$w\" --caller port_clone; "
        # a fallback inside the helper may re-create the dir: keep it never world-searchable (t_69e47673)
        f"chmod {WORKSPACE_MODE} -- \"$w\"; "
        # --dissociate only when the mirror can prune: a never-prune mirror (preciousObjects) is safe to
        # borrow from, and dissociating gave every port clone its own ~440 MB object store.
        # git honours extensions.* only at core.repositoryformatversion 1 (Prism 7848e18134f2)
        f"else d=--dissociate; [ \"$(git --git-dir={m} config --bool --get extensions.preciousObjects 2>/dev/null)\" != true ] "
        f"|| [ \"$(git --git-dir={m} config --get core.repositoryformatversion 2>/dev/null)\" != 1 ] || d=; "
        f"git clone -q --reference-if-able {m} $d {url} \"$w\"; fi; fi; "
        "cd \"$w\"; "
        f"if [ \"$(git branch --show-current)\" != {b} ]; then "
        f"if git rev-parse -q --verify refs/heads/{b} >/dev/null; then git checkout -q {b}; "
        f"elif git rev-parse -q --verify refs/remotes/origin/{b} >/dev/null; then "
        f"git checkout -q -b {b} --track origin/{b}; "
        f"else git checkout -q --no-track -b {b} origin/HEAD; fi; fi; "
        f"[ \"$(git branch --show-current)\" = {b} ] || exit 4"
        + (f"; if [ \"$(git rev-parse HEAD)\" != {shlex.quote(sha)} ]; then "
           f"{{ git fetch -q origin {b} && git merge -q --ff-only {shlex.quote(sha)}; }} || exit 5; fi; "
           f"[ \"$(git rev-parse HEAD)\" = {shlex.quote(sha)} ] || exit 5" if sha and not exact else "")
        # t_9965b8d5: a re-cloned scratch clone lands on the Studio's exact commit, even behind the tip (rc 5 otherwise)
        # Prism 30f81cba3dfc: never reset away a reused clone's commits that no remote holds (rc 6)
        # Apollo F1 01:0x: nor its uncommitted edits (tracked changes, untracked non-ignored files: rc 7)
        + (f"; s={shlex.quote(sha)}; if [ \"$(git rev-parse HEAD)\" != \"$s\" ]; then "
           "[ -z \"$(git status --porcelain)\" ] || exit 7; "
           "[ -z \"$(git rev-list -n 1 HEAD --not --remotes)\" ] || exit 6; git reset -q --hard \"$s\"; fi; "
           "[ \"$(git rev-parse HEAD)\" = \"$s\" ] || exit 5" if sha and exact else "")
    )


def clone_on_host(host, workspace: str, slug: str, mirror: str, branch: str,
                  runner: Callable = subprocess.run, sha: Optional[str] = None,
                  carry: Optional[tuple] = None, exact: bool = False) -> None:
    """Run :func:`clone_script` on ``host`` (a ``PoolHost``); raise on failure. ``carry`` = the port's
    ``(own tree, HEAD sha, digest)`` (t_894be273): the dirty tree's :func:`carry_payload` rides the SAME ssh call
    on stdin, applied by :func:`carry_script` once the clone is proved at that HEAD. ``exact`` (t_9965b8d5): the
    clone lands on ``sha`` even behind the branch tip (see :func:`clone_script`)."""
    from hermes_cli.kanban_worker_pool import _ssh_argv
    kw: dict = {"sha": sha}
    if carry:
        kw["carried"] = True
    if exact:
        kw["exact"] = True
    script = clone_script(workspace, slug, mirror, branch, **kw)
    io_kw: dict = {"stdin": subprocess.DEVNULL}
    timeout = CLONE_TIMEOUT_S
    if carry:
        if sha != carry[1]:
            raise RuntimeError(f"dirty carry of {carry[0]}: clone pinned to {str(sha)[:12]}, tree at {carry[1][:12]}")
        payload, delete = carry_payload(carry[0], carry[1], runner)
        # the clone half never reads stdin; the carry's tar -xf - gets all of it
        script = f"{{ {script}; }} </dev/null; {carry_script(workspace, carry[1], delete)}"
        io_kw, timeout = {"input": payload}, CLONE_TIMEOUT_S + DIRTY_CARRY_TIMEOUT_S
    proc = runner(_ssh_argv(host, script), capture_output=True, timeout=timeout, **io_kw)
    rc = getattr(proc, "returncode", 1)
    if rc != 0:
        err = getattr(proc, "stderr", "") or ""
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else err
        raise RuntimeError(f"worktree clone of {slug} on {host.name} failed rc={rc}: {err.strip()[:200]}")


# Pool-host clone off the dispatcher tick (t_915b8d2d). A gateway tick measured 5 of 6 py-spy samples in
# clone_on_host: the ssh fetch + clone ran inside the per-row loop and every other ready row waited on it.
# The gateway tick now STARTS the push+clone here and moves on. The card waits ``clone_pending`` and is claimed
# and launched on a later tick, once the clone has finished. A failed clone is raised at the old call site, so
# the claim release, spillover refund and failure accounting stay the existing exception path. Only the
# long-lived gateway tick uses this (``defer_pool_clone``): a one-shot CLI process would exit with the clone
# thread still running.
#
# Two guards (Prism 3e2f8506c8ff, d4847ef87c43). The key carries the push plan, i.e. the source tip sha: a tip
# that moved after the prefetch is a new key, the stale result is dropped and never collected. The pending
# clone is visible to EVERY dispatcher: a per-workspace flock (``clone_lock_path``) is taken in the tick before
# the job is submitted and held until it finishes. Any tick, deferred or not, in any process, that finds it held
# skips the card (:func:`clone_busy`), and the synchronous clone refuses to start under it
# (:func:`run_clone`). The lock dies with its process, so a crashed dispatcher never strands the card.
CLONE_PENDING = "clone_pending"
CLONE_PREFETCH_WORKERS = 4
CLONE_PREFETCH_TTL_S = 3600.0
_prefetch_lock = threading.Lock()
_prefetch: Dict[tuple, tuple] = {}   # key -> (Future, started monotonic)
_prefetch_pool: Optional[ThreadPoolExecutor] = None


def clone_key(task_id: str, host_name: str, workspace: str, slug: str, mirror: str, branch: str,
              push: Optional[tuple] = None, carry: Optional[tuple] = None) -> tuple:
    """``push`` = :func:`assess`'s push plan (repo, branch, origin sha, source tip sha) or None. ``carry`` = the
    dirty own tree to copy in (t_894be273), so a clone made without it is never collected for a carry."""
    return (task_id, host_name, str(workspace), slug, mirror, branch, tuple(push) if push else None) + (
        (tuple(carry),) if carry else ())


def clone_lock_path(lock_dir: Path, key: tuple) -> Path:
    """The per-workspace pending marker: one file per (host, workspace), whatever the tip."""
    return Path(lock_dir) / (hashlib.sha1(f"{key[1]}\0{key[2]}".encode()).hexdigest()[:24] + ".lock")


def _try_lock(path: Path) -> Optional[int]:
    """An fd holding ``path``'s exclusive flock, or None when another holder has it."""
    if fcntl is None:
        return -1
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _unlock(fd: Optional[int]) -> None:
    if fd is not None and fd >= 0:
        try:
            os.close(fd)   # closing the only fd releases the flock
        except OSError:
            pass


def _same_workspace(a: tuple, b: tuple) -> bool:
    return a[:3] == b[:3]


def _drop_stale_locked(key: tuple) -> bool:
    """Under ``_prefetch_lock``: forget finished entries of ``key``'s workspace under another key (the tip moved:
    never collected). True while one of them still runs (a second writer must not start)."""
    running = False
    for k, (fut, _t0) in list(_prefetch.items()):
        if k != key and _same_workspace(k, key):
            if fut.done():
                del _prefetch[k]
            else:
                running = True
    return running


def clone_ready(key: tuple, job: Callable[[], None], lock_path: Path) -> bool:
    """True once ``job`` (push + clone for ``key``) has finished, ok or failed. On the first call it takes the
    workspace lock, starts the job on a small background pool and returns False. While it runs, or while any
    other clone of the same workspace runs (this process or another), this returns False. The caller collects
    the outcome with :func:`run_clone`."""
    global _prefetch_pool
    now = time.monotonic()
    with _prefetch_lock:
        for k, (fut, t0) in list(_prefetch.items()):
            if fut.done() and now - t0 > CLONE_PREFETCH_TTL_S:
                del _prefetch[k]    # finished, never collected (the card moved on): forget it
        entry = _prefetch.get(key)
        if entry is not None:
            return entry[0].done()
        if _drop_stale_locked(key):
            return False
        fd = _try_lock(lock_path)
        if fd is None:
            return False            # another dispatcher's clone of this workspace is live
        if _prefetch_pool is None:
            _prefetch_pool = ThreadPoolExecutor(max_workers=CLONE_PREFETCH_WORKERS,
                                                thread_name_prefix="kanban-clone")
        try:
            fut = _prefetch_pool.submit(job)
        except BaseException:
            _unlock(fd)
            raise
        fut.add_done_callback(lambda _f, _fd=fd: _unlock(_fd))
        _prefetch[key] = (fut, now)
        return False


def clone_busy(key: tuple, lock_path: Path) -> bool:
    """For a tick that clones synchronously: True while a clone of ``key``'s workspace is pending anywhere
    (this process's cache, or the workspace lock held by another dispatcher)."""
    with _prefetch_lock:
        if any(_same_workspace(k, key) and not fut.done() for k, (fut, _t0) in _prefetch.items()):
            return True
    fd = _try_lock(lock_path)
    if fd is None:
        return True
    _unlock(fd)
    return False


def take_clone(key: tuple) -> bool:
    """Collect a finished prefetched clone: True when it succeeded, False when none is recorded for ``key``
    (the caller clones synchronously). Re-raises the clone's own exception when it failed. A finished clone of
    the same workspace under another key (an older tip) is dropped, never collected."""
    with _prefetch_lock:
        _drop_stale_locked(key)
        entry = _prefetch.get(key)
        if entry is None or not entry[0].done():
            return False
        del _prefetch[key]
    entry[0].result()
    return True


def run_clone(key: tuple, job: Callable[[], None], lock_path: Path) -> None:
    """The claim-time clone: collect the finished prefetch for ``key``, else run ``job`` under the workspace
    lock. Refuses (raises) rather than start a second writer while another clone of the workspace is live."""
    if take_clone(key):
        return
    with _prefetch_lock:
        if any(_same_workspace(k, key) and not fut.done() for k, (fut, _t0) in _prefetch.items()):
            raise RuntimeError(f"{CLONE_PENDING}: a clone of {key[2]} on {key[1]} is still running")
    fd = _try_lock(lock_path)
    if fd is None:
        raise RuntimeError(f"{CLONE_PENDING}: another dispatcher holds the clone of {key[2]} on {key[1]}")
    try:
        job()
    finally:
        _unlock(fd)


def convert(conn, task, workspace: str, branch: str, *, host: str, slug: str,
            mirror: str) -> None:
    """Persist the port: the card is now a scratch card at ``workspace``."""
    from hermes_cli import kanban_db as _kb
    with _kb.write_txn(conn):
        # t_7eb1cbbb: the row still holds the declared workspace (the caller only re-pointed the in-memory task);
        # record it so a later hand-over off this host restores it instead of leaving the card scratch.
        kind, path = declared_workspace(conn, task.id)
        # Fenced to the launching run: the tick has the board lock handed back here (t_f60f502f).
        cur = conn.execute("UPDATE tasks SET workspace_kind='scratch', workspace_path=?, branch_name=? "
                           "WHERE id=? AND status='running' AND current_run_id IS ?",
                           (workspace, branch, task.id, task.current_run_id))
        if cur.rowcount == 0:
            raise _kb.ClaimLost(f"{task.id}: run {task.current_run_id} no longer owns the card")
        _kb._append_event(conn, task.id, PORTED_EVENT,
                          {"host": host, "repo": slug, "mirror": mirror, "branch": branch,
                           "workspace": workspace, "declared_kind": kind, "declared_path": path},
                          run_id=task.current_run_id)
    task.workspace_kind, task.workspace_path, task.branch_name = "scratch", workspace, branch


def declared_workspace(conn, task_id: str) -> Tuple[Optional[str], Optional[str]]:
    """``(kind, path)`` the card was given before any port made it scratch (t_7eb1cbbb): the row while it is not
    ported yet, else the newest port event's ``declared_*``, else the ``created`` event (ports written before
    the port recorded it)."""
    row = conn.execute("SELECT workspace_kind, workspace_path FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is not None and (row[0] or "scratch") != "scratch":
        return row[0], row[1]
    _id, port = _newest_event(conn, task_id, PORTED_EVENT)
    if port.get("declared_kind"):
        return port.get("declared_kind"), port.get("declared_path")
    _id, created = _newest_event(conn, task_id, "created")
    if _id is not None and created.get("workspace_kind"):
        return created.get("workspace_kind"), created.get("workspace_path")
    return (row[0], row[1]) if row is not None else (None, None)


def board_default_workdir(board: Optional[str]) -> Optional[str]:
    from hermes_cli import kanban_db as _kb
    try:
        slug = board if board else _kb.get_current_board()
        return (_kb.read_board_metadata(slug).get("default_workdir") or "").strip() or None
    except Exception:
        return None


def ported_host(conn, task_id: str, workspace_path: Optional[str]) -> Optional[str]:
    """The pool host holding this card's ported clone, or None. Only the
    newest port event counts, and only while the card still points at that
    workspace (an operator who re-pointed it has moved it on)."""
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? "
                       "ORDER BY id DESC LIMIT 1", (task_id, PORTED_EVENT)).fetchone()
    if row is None:
        return None
    try:
        doc = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return None
    host = doc.get("host")
    if not isinstance(host, str) or not host or doc.get("workspace") != workspace_path:
        return None
    return host.lower()


# -- hand-over off a port host that cannot serve the card any more (t_f914c529) ---------------------------------
# t_cf13ff3b (2026-11-12): ported to ci-box, blocked kind=capability there, re-routed with `requires: studio-state`.
# The port bind kept it on ci-box, which cannot serve studio-state, so it waited not_portable:requires:studio-state
# for 4.5 h and never reached the Studio default-deny check (no studio_default_deny code, no wake, admit/override
# inert). The bind ends ONLY for that case: a hard requirement the bound host cannot serve. And only after the clone
# there is proven clean and fully pushed (the remote-GC predicate, kanban_survivor._REMOTE_PROOF in check mode): the
# card is re-pointed to a FRESH local scratch path, which the spawn fills from origin at the clone's head
# (:func:`materialize_handover`). A clone with local work keeps the card held, named, on that host.
HANDOVER_EVENT = "worktree_handed_over"
STRANDED_PREFIX = "port_stranded"
PROBE_RETRY_S = 300
PROBE_TIMEOUT_S = 10.0  # the whole probe ssh, connect included: it runs inside the tick (operator 11-12 13:36)
PROBE_LEASE_S = 60.0    # the router lease one clone probe runs under (released after; the TTL bounds a leak)
_probe_memo: Dict[tuple, tuple] = {}   # (task id, port event id) -> (monotonic, wait reason)


def handover_url(slug: str) -> str:
    return f"https://github.com/{slug}.git"


def _newest_event(conn, task_id: str, kind: str):
    row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                       (task_id, kind)).fetchone()
    if row is None:
        return None, {}
    try:
        doc = json.loads(row[1] or "{}")
    except (TypeError, ValueError):
        doc = {}
    return row[0], doc if isinstance(doc, dict) else {}


def hard_missing(row, caps, host: str) -> List[str]:
    """The card's HARD capability requirements that ``host`` cannot serve AND no pool host serves, i.e. only the
    Studio can ([] with no capability map: the bind stands). Operator ruling 11-12 13:36 (Prism d6cfdbb53981): the
    hand-over goes to the Studio only; a requirement some other pool host serves keeps the bind (no pool-to-pool
    hand-over: all 179 placement_rerouted events of the 7 days before named to_host studio)."""
    if caps is None or getattr(caps, "cmap", None) is None:
        return []
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_host_capabilities as _khc
    skills = [s for s in (_kb._json_or(row["skills"]) or []) if isinstance(s, str)]
    reqs, _none = _khc.derive(row["body"], skills, caps.cmap)
    pool = tuple(getattr(caps, "pool_hosts", ()) or ()) + (host,)
    return sorted({r.cap for r in reqs if r.strength == "hard" and not _khc._satisfied(r.cap, caps, host)
                   and not any(r.cap in _khc._host_caps(caps, h.lower()) for h in pool)})


def probe_clone(host, path: str, runner: Callable = subprocess.run) -> Tuple[bool, str]:
    """``(True, head sha or "" when the path is gone)`` when the clone on ``host`` passes remote GC's proof (clean,
    no stash, every local commit on a remote, HEAD on a remote ref), else ``(False, why)``. Read-only.

    The ssh runs under a router reservation on ``host`` (zero footprint, short TTL, released after): the router sees
    every ssh the fleet opens on a host (spawn-site lint). A refusal keeps the card held, named."""
    from hermes_cli.fleet_placement import alloc_client
    from hermes_cli.kanban_survivor import _REMOTE_PROOF
    from hermes_cli.kanban_worker_pool import _ssh_argv
    cmd = "python3 -c " + shlex.quote(_REMOTE_PROOF) + " " + shlex.quote(path) + " check"
    try:
        grant = alloc_client.admit("kanban", job_id=f"port-probe:{host.name}:{path}", ttl_s=PROBE_LEASE_S,
                                   footprint={"ram_gb": 0.0, "threads": 0.0}, candidates=[host.name])
    except Exception as exc:  # Refused (token), a table with no such host: held, named
        return False, f"probe not admitted: {exc}"[:200]
    if not grant.host or not grant.reservation_id:
        return False, f"probe not admitted on {host.name}: {grant.reason or 'refused'}"[:200]
    env = dict(os.environ, ALLOC_RESERVATION=str(grant.reservation_id))
    try:
        proc = runner(_ssh_argv(host, cmd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                      timeout=PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL, env=env)
    except Exception as exc:  # unreachable host, timeout: held, named
        return False, f"probe failed: {exc}"[:200]
    finally:
        try:
            alloc_client.release(grant.reservation_id, consumer=grant.consumer or "kanban")
        except Exception as exc:  # the TTL bounds an unreleased probe lease
            _log.warning("kanban port probe: release of %s failed: %s", grant.reservation_id, exc)
    if proc.returncode != 0:
        return False, ((proc.stderr or "").strip() or f"probe rc={proc.returncode}")[:200]
    return True, (proc.stdout or "").strip()


def release_port_bind(conn, row, host_name: str, missing: List[str], pool_hosts, *, dry_run: bool = False,
                      runner: Callable = subprocess.run) -> Optional[str]:
    """Hand a ported card back from ``host_name``, which cannot serve ``missing``. None = handed over (re-pointed,
    ``worktree_handed_over`` written; simulated under ``dry_run``), else the wait reason that keeps it held."""
    from hermes_cli import kanban_db as _kb
    tid, old = row["id"], row["workspace_path"]
    port_id, port = _newest_event(conn, tid, PORTED_EVENT)
    # no str.format: a brace in the path, host or a capability must not raise inside the tick (Prism 1ca2affda3f7)
    def held(why: str) -> str:
        return (f"{STRANDED_PREFIX}:{host_name} ({old}: {why}; the card needs {', '.join(missing)}, which "
                f"{host_name} cannot serve. Push or drop that clone's work and the next tick hands the card over to a "
                f"fresh clone)")
    memo = _probe_memo.get((tid, port_id))
    if memo is not None and time.monotonic() - memo[0] < PROBE_RETRY_S:
        return memo[1]
    host = next((h for h in pool_hosts or () if h.name.lower() == host_name.lower()), None)
    ok, got = (False, "port host is not in the worker registry") if host is None else probe_clone(host, old, runner)
    # t_7eb1cbbb: a card declared ``worktree:<repo>`` goes back to that worktree (the spawn puts its branch at the
    # clone's head first, :func:`materialize_handover`); only a card declared scratch gets a fresh scratch path.
    kind, new = declared_workspace(conn, tid)
    if kind != "worktree":
        kind, new = "scratch", f"{old}-from-{host_name.lower()}"
    if ok and kind == "scratch" and os.path.lexists(new):
        ok, got = False, f"hand-over target {new} already exists here; it is never reused"
    if not ok:
        reason = held(got)
        _probe_memo[(tid, port_id)] = (time.monotonic(), reason)
        return reason
    if dry_run:
        return None
    payload = {"from_host": host_name, "workspace": old, "path": new, "kind": kind, "repo": port.get("repo"),
               "mirror": port.get("mirror"), "branch": port.get("branch") or f"wt/{tid}", "head": got or None,
               "missing": missing}
    if not got:
        payload["head_note"] = "no clone at the port path: the branch on origin is used"
    with _kb.write_txn(conn):
        cur = conn.execute("UPDATE tasks SET workspace_kind=?, workspace_path=?, origin_host=CASE WHEN "
                           "lower(COALESCE(origin_host,''))=? THEN NULL ELSE origin_host END "
                           "WHERE id=? AND workspace_path IS ? AND claim_lock IS NULL",
                           (kind, new, host_name.lower(), tid, old))
        if cur.rowcount != 1:
            return held("the card changed under the hand-over; retried next tick")
        _kb._append_event(conn, tid, HANDOVER_EVENT, payload)
    if kind == "scratch":
        _make_scratch(new)
    return None


def _make_scratch(path: str) -> bool:
    """Create the scratch path a hand-over recorded, so admission never reads it as a lost mount (t_7eb1cbbb).
    A failure is logged; :func:`recover_handover` retries it on the next admission."""
    from hermes_cli.kanban_worker_hosts import WORKSPACE_MODE
    try:
        os.makedirs(path, mode=int(WORKSPACE_MODE, 8), exist_ok=True)
    except OSError as exc:
        _log.warning("kanban hand-over: could not create %s: %s", path, exc)
        return False
    return True


HANDOVER_NEVER_CREATED = "handover_target_never_created"


def recover_handover(conn, task) -> Optional[str]:
    """Re-point a card whose persisted path is the newest hand-over's own target that never came to exist (no run
    spawned on it since): back to its declared worktree, or the scratch path created (t_7eb1cbbb, t_6e7909d1 sat
    28 min refused ``stranded_by_mount_loss`` on a mounted volume). Returns the recovery label, else None (any
    other missing path is not this case and stays fail-closed). The caller re-admits."""
    from hermes_cli import kanban_db as _kb
    ev_id, doc = _newest_event(conn, task.id, HANDOVER_EVENT)
    path = task.workspace_path
    if ev_id is None or not path or doc.get("path") != path or os.path.lexists(path):
        return None
    if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='spawned' AND id>? LIMIT 1",
                    (task.id, ev_id)).fetchone():
        return None
    kind, new = declared_workspace(conn, task.id)
    label = f"{HANDOVER_NEVER_CREATED}: {path}"
    if kind != "worktree":
        # Scratch: the row already holds the path, so recovery IS creating it. Failing that is no recovery
        # (Prism 3d337a9df692: a truthy label with the dir still missing re-admitted without end).
        if not _make_scratch(path) or not os.path.isdir(path):
            return None
        kind, new = "scratch", path
    # Conditional on the row still being this hand-over's, for both kinds (Prism 3d337a9df692 r2): a newer
    # hand-over that lands meanwhile wins, and its document stays the newest one materialize_handover reads.
    with _kb.write_txn(conn):
        cur = conn.execute("UPDATE tasks SET workspace_kind=?, workspace_path=? WHERE id=? AND workspace_path IS ? "
                           "AND claim_lock IS NULL", (kind, new, task.id, path))
        if cur.rowcount != 1:
            return None
        _kb._append_event(conn, task.id, HANDOVER_EVENT, {**doc, "kind": kind, "path": new, "recovered": label})
    _log.warning("kanban dispatch: %s task=%s -> %s:%s", label, task.id, kind, new)
    return label


def materialize_handover(conn, task, workspace: str, placed_host=None, runner: Callable = subprocess.run, *,
                         board: Optional[str] = None) -> bool:
    """Fill a handed-over card's fresh workspace with its branch at the head the port clone held, once (no ``spawned``
    after the hand-over), here on the Studio. Raises on failure or a pool placement (a spawn failure). True = cloned."""
    ev_id, doc = _newest_event(conn, task.id, HANDOVER_EVENT)
    if ev_id is None or doc.get("path") != task.workspace_path or not doc.get("repo"):
        return False
    if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='spawned' AND id>? LIMIT 1",
                    (task.id, ev_id)).fetchone():
        return False
    mirror, branch, sha = doc.get("mirror") or "/nonexistent-mirror", doc.get("branch") or f"wt/{task.id}", doc.get("head")
    if placed_host is not None:
        # The hand-over is to the Studio only (hard_missing: no pool host serves the need; operator ruling 13:36,
        # Prism d6cfdbb53981). A pool placement here would leave no port binding for its clone: refuse it.
        raise RuntimeError(f"hand-over of {task.id} is to the Studio only; refusing a start on {placed_host.name}")
    if doc.get("kind") == "worktree":
        return _prepare_handover_branch(task, doc, branch, sha, runner, board=board)
    script = clone_script(workspace, doc["repo"], mirror, branch, url=handover_url(doc["repo"]), sha=sha,
                          exact=bool(sha))
    proc = runner(["sh", "-c", script], capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=CLONE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"hand-over clone of {doc['repo']} into {workspace} failed rc={proc.returncode}: "
                           f"{(proc.stderr or '').strip()[:200]}")
    return True


def _prepare_handover_branch(task, doc: dict, branch: str, sha: Optional[str], runner: Callable, *,
                             board: Optional[str] = None) -> bool:
    """A worktree card handed back to its declared repo (t_7eb1cbbb): put the card's local branch at the head the
    port clone held (``sha``; else origin's branch tip) BEFORE the worktree is made, so the Studio run starts where
    the pool run stopped. A local branch that does not contain that head raises (a spawn failure: its work is never
    reset). No head and no origin branch: False, the worktree branches off the trunk as a fresh card does."""
    # A pathless worktree card's repo is the board's default_workdir, as at port time (Prism e1c1afcd8647).
    repo = workspace_repo(task.id, task.workspace_path, board_default_workdir(board), allow_studio_tree=True)[0]
    if repo is None:
        raise RuntimeError(f"hand-over of {task.id}: {task.workspace_path} is not in a git repo")
    slug = origin_slug(repo, runner)
    # Exact: doc["repo"] was written by origin_slug() on this same repo at port time.
    if slug and doc.get("repo") and slug != str(doc["repo"]):
        raise RuntimeError(f"hand-over of {task.id}: {repo} is {slug}, the port cloned {doc['repo']}")

    def git(*args):
        return runner(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                      errors="replace", timeout=CLONE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    fetched = git("fetch", "-q", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    if fetched.returncode != 0 and not sha:
        # Without a recorded head only origin knows where the branch is (Prism 50ba052e20b3): an absent branch
        # (ls-remote rc 2) branches off the trunk; a transport failure must not, nor may a stale cached ref.
        if git("ls-remote", "--exit-code", "--heads", "origin", branch).returncode != 2:
            raise RuntimeError(f"hand-over of {task.id}: could not fetch {branch} from origin: "
                               f"{(fetched.stderr or '').strip()[:200]}")
        return False
    target = sha or f"refs/remotes/origin/{branch}"
    if git("rev-parse", "-q", "--verify", f"{target}^{{commit}}").returncode != 0:
        if sha:
            raise RuntimeError(f"hand-over of {task.id}: head {sha} of {branch} is not in {repo} or on origin")
        return False
    if git("rev-parse", "-q", "--verify", f"refs/heads/{branch}").returncode == 0:
        if git("merge-base", "--is-ancestor", target, f"refs/heads/{branch}").returncode != 0:
            raise RuntimeError(f"hand-over of {task.id}: local {branch} in {repo} does not contain {target}; "
                               "nothing reset, operator: reconcile it")
        return True
    made = git("branch", "--no-track", branch, target)
    if made.returncode != 0:
        raise RuntimeError(f"hand-over of {task.id}: git branch {branch} {target} failed: "
                           f"{(made.stderr or '').strip()[:200]}")
    return True
