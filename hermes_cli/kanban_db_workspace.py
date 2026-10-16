"""Task workspace lifecycle: scratch/dir/worktree resolution (incl. git worktree creation), post-completion cleanup with containment guards, worker tmux teardown and the first-use scratch-workspace tip.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""
from __future__ import annotations
import os
import shutil
import sqlite3
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Optional
from typing import TYPE_CHECKING
import contextlib
from hermes_cli.worktree_ops import release_lsp_clients
if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task
_REMOVABLE_KINDS = ("scratch", "worktree")


def _path_key(path: Path | str | None) -> str:
    """Unicode-form-insensitive identity for a filesystem path.

    macOS hands back DECOMPOSED path strings (NFD: ``o`` + U+0308) for names the
    user typed in composed form (NFC: ``ö``) — a OneDrive/FileProvider path like
    ``OneDrive-Persönlich`` round-trips through ``git rev-parse --show-toplevel``
    as NFD while the DB row holds NFC. Raw ``Path`` equality then reports a real
    repo root as "not a repo" purely on Unicode form, so every path identity
    check here goes through this key.
    """
    return unicodedata.normalize("NFC", str(path)) if path is not None else ""

RUNTIME_TREE_WORKSPACE_ERROR = (
    "workspace under the live runtime tree; use ~/.hermes/hermes-agent (fork/main)"
)


def refuse_runtime_tree_workspace(kind: Optional[str], path: Path | str | None) -> None:
    """Refuse a ``dir``/``worktree`` workspace at or under the live runtime tree.

    ``<hermes-root>/runtime/hermes-agent`` is the import path of every running
    gateway; a worktree anchored there shares its refs and index dir, and a
    worker cwd inside it is one mistake from a write into live code (10-16,
    t_3b6e4e6b / t_91b6a4c4). Realpaths on both sides so a symlink cannot alias in.
    """
    if kind not in ("dir", "worktree") or not path:
        return
    from hermes_constants import get_default_hermes_root

    runtime = (get_default_hermes_root() / "runtime" / "hermes-agent").resolve(strict=False)
    target = Path(path).expanduser().resolve(strict=False)
    if target == runtime or runtime in target.parents:
        raise ValueError(RUNTIME_TREE_WORKSPACE_ERROR)


def canon(path: Path | str) -> str:
    """THE canonical key of a workspace path (spec §2.4.1): ``~`` expanded,
    resolved without requiring existence, NFC. ``~/x`` on an unspawned card
    and ``/Users/u/x`` on its spawned twin get the same key. Raises OSError /
    RuntimeError on an unresolvable path; callers fail closed."""
    return _path_key(Path(path).expanduser().resolve(strict=False))


def _canon_within(child: str, owner: str) -> bool:
    """``child`` is ``owner`` or strictly under it (canonical keys)."""
    return child == owner or child.startswith(owner.rstrip(os.sep) + os.sep)


# Statuses after which a child no longer needs its parent's workspace artifacts.
_TERMINAL_STATUSES = ("done", "archived", "failed", "cancelled")
_TERMINAL_SQL = "('done', 'archived', 'failed', 'cancelled')"
# The link key (any kind; main before Phase 2b-i), its deferrers named for
# the retention_shadow payload.
_LINK_DEFERRERS_SQL = (
    "SELECT l.child_id, l.kind FROM task_links l "
    "JOIN tasks t ON t.id = l.child_id "
    f"WHERE l.parent_id = ? AND t.status NOT IN {_TERMINAL_SQL} "
    "ORDER BY l.child_id"
)
# The workspace_of key (spec §2.5 item 2): a declared reader of this card's dir.
_WORKSPACE_OF_ACTIVE_SQL = (
    f"SELECT 1 FROM tasks c WHERE c.workspace_of = ? AND c.status NOT IN {_TERMINAL_SQL} LIMIT 1"
)
_WORKSPACE_ROW_SQL = "SELECT workspace_kind, workspace_path, branch_name FROM tasks WHERE id = ?"


def _git(repo_root: Path, *args: str, timeout: int) -> subprocess.CompletedProcess:
    """``git -C repo_root args``; never raises on a non-zero exit."""
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True, encoding='utf-8', errors='replace',
        timeout=timeout,
        check=False,
    )

# kanban.workspace.retention_key (read per GC decision, spec §6, D-S3).
# ``links`` = main before Phase 2b-i (task_links, any kind); ``union`` (the
# default) = links OR workspace_of OR the canonical-path shared set;
# ``workspace_of`` = the link key retired (Phase 2b-ii owns the flip and its
# ``retention_dropped`` event). Unknown -> ``union``: retain more.
RETENTION_LINKS = "links"
RETENTION_UNION = "union"
RETENTION_WORKSPACE_OF = "workspace_of"
RETENTION_KEYS = (RETENTION_LINKS, RETENTION_UNION, RETENTION_WORKSPACE_OF)
RETENTION_DEFAULT = RETENTION_UNION
RETENTION_SHADOW_EVENT = "retention_shadow"


def retention_key(kanban_cfg=None) -> str:
    """``kanban.workspace.retention_key``. Placement's ``lineage_pins`` never
    feeds this (AC-25): the two switches are independent."""
    from hermes_cli import kanban_placement_reasons as _pr
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            kanban_cfg = (load_config_readonly() or {}).get("kanban")
        except Exception:
            kanban_cfg = None
    ws = kanban_cfg.get("workspace") if isinstance(kanban_cfg, dict) else None
    raw = ws.get("retention_key") if isinstance(ws, dict) else None
    if raw is None:
        return RETENTION_DEFAULT
    key = str(raw).strip().lower()
    if key in RETENTION_KEYS:
        return key
    _pr.note_unknown_switch("kanban.workspace.retention_key", key, RETENTION_UNION)
    return RETENTION_UNION


def live_path_index(conn: sqlite3.Connection) -> list:
    """``[(id, workspace_kind, workspace_path, canon)]`` of every non-terminal
    card with a path: computed once per dispatcher tick and handed to
    :func:`workspace_shared`, so canonicalisation is O(N) per tick, not per row."""
    return [(r["id"], r["workspace_kind"], r["workspace_path"], canon(r["workspace_path"]))
            for r in conn.execute(
                "SELECT id, workspace_kind, workspace_path FROM tasks "
                "WHERE workspace_path IS NOT NULL AND workspace_path != '' "
                f"AND status NOT IN {_TERMINAL_SQL}")]


def workspace_shared(conn: sqlite3.Connection, task_id: str, path: Optional[str], *,
                     role: str, index: Optional[list] = None) -> list[str]:
    """THE canonical-path sharing relation (spec §2.4.1), one function for
    placement and retention. Non-terminal cards other than ``task_id``:

    * ``role="reader"`` (placement): cards whose path EQUALS this card's, or a
      task-owned scratch/worktree path this card's path is nested under (never
      a ``dir:`` ancestor nor a managed workspaces root, Momus R-E);
    * ``role="owner"`` (retention): cards whose path equals this card's or is
      nested under it.

    Sorted ids; ``[]`` for a card with no path. Path errors propagate (the
    retention reader fails closed on them)."""
    if not path:
        return []
    mine = canon(path)
    out: list[str] = []
    for oid, okind, opath, other in (index if index is not None else live_path_index(conn)):
        if oid == task_id:
            continue
        if other == mine:
            out.append(oid)
        elif role == "owner" and _canon_within(other, mine):
            out.append(oid)
        elif (role == "reader" and _canon_within(mine, other)
              and (okind or "scratch") in _REMOVABLE_KINDS
              and (okind == "worktree" or _is_managed_scratch_path(Path(opath)))):
            out.append(oid)
    return sorted(out)


def _retention_verdict(conn: sqlite3.Connection, task_id: str, key: str) -> tuple[bool, list]:
    """``(defers, link_only_deferrers)``: whether ``key`` keeps ``task_id``'s
    directory, and the link-key deferrers the ``workspace_of`` key would NOT
    keep it for (non-empty only in ``union`` mode, the shadow population)."""
    links = [(r[0], r[1]) for r in conn.execute(_LINK_DEFERRERS_SQL, (task_id,))]
    if key == RETENTION_LINKS:
        return bool(links), []
    new_key = conn.execute(_WORKSPACE_OF_ACTIVE_SQL, (task_id,)).fetchone() is not None
    if not new_key:
        row = conn.execute("SELECT workspace_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
        new_key = bool(row and workspace_shared(conn, task_id, row[0], role="owner"))
    if key == RETENTION_WORKSPACE_OF:
        return new_key, []
    return (bool(links) or new_key), ([] if new_key else links)


def _has_active_children(conn: Optional[sqlite3.Connection], task_id: str,
                         key: Optional[str] = None) -> bool:
    """Fail closed if a child still needs its parent's handoff: THE one
    reader of ``kanban.workspace.retention_key`` (spec I-S7). Every site
    (completion, worktree lane, parent pass, the deletion choke point) calls
    it without a key, so none can pin a stale default. Any error defers."""
    if conn is None:
        try:
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing() as own:
                return _has_active_children(own, task_id, key)
        except Exception:
            return True
    try:
        return _retention_verdict(conn, task_id, key or retention_key())[0]
    except Exception:
        return True


def _retention_defers(conn: sqlite3.Connection, task_id: str) -> bool:
    """:func:`_has_active_children` at a GC DECISION site, plus the
    ``retention_shadow`` record (spec §2.5 item 4): in ``union`` mode, a
    deferral held ONLY by the link key is written once per (parent, deferral
    episode). A new episode starts when the deferring set changes or when a
    deferring child has a lifecycle event after the latest shadow
    (``PLACEMENT_WAIT_EPISODE_END_KINDS``). Never raises; errors defer."""
    try:
        key = retention_key()
        defers, link_only = _retention_verdict(conn, task_id, key)
    except Exception:
        return True
    if defers and link_only:
        try:
            _note_retention_shadow(conn, task_id, link_only)
        except Exception:
            _kb._log.debug("kanban: retention_shadow write failed for %s", task_id, exc_info=True)
    return defers


def _note_retention_shadow(conn: sqlite3.Connection, parent: str, deferrers: list) -> None:
    import json
    from hermes_cli import kanban_placement_reasons as _pr
    deferred_by = [[c, k] for c, k in deferrers]
    last = conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (parent, RETENTION_SHADOW_EVENT),
    ).fetchone()
    if last is not None:
        try:
            prev = json.loads(last[1] or "{}").get("deferred_by")
        except (TypeError, ValueError):
            prev = None
        if prev == deferred_by:
            ids = [c for c, _k in deferrers]
            kinds = sorted(_pr.PLACEMENT_WAIT_EPISODE_END_KINDS)
            boundary = conn.execute(
                f"SELECT 1 FROM task_events WHERE id > ? "
                f"AND task_id IN ({','.join('?' * len(ids))}) "
                f"AND kind IN ({','.join('?' * len(kinds))}) LIMIT 1",
                (last[0], *ids, *kinds),
            ).fetchone()
            if boundary is None:
                return
    with _kb.write_txn(conn, allow_nested=True):
        _kb._append_event(conn, parent, RETENTION_SHADOW_EVENT, {
            "parent": parent, "deferred_by": deferred_by, "new_key_would": "delete",
        })


def bind_host(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """THE host a workspace bind co-locates with (spec §2.4.2): the pool host
    of ``task_id``'s most recent run, from that run's latest ``worker_placed``
    event; None = local (never ran, or ran on the Studio). One function, so
    the swap to ``origin_host`` (affinity spec Phase 3) is one edit."""
    import json
    from hermes_cli import kanban_placement_reasons as _pr
    row = conn.execute(
        "SELECT e.payload FROM task_events e WHERE e.task_id = ? AND e.kind = ? "
        "AND e.run_id = (SELECT MAX(id) FROM task_runs WHERE task_id = ?) "
        "ORDER BY e.id DESC LIMIT 1",
        (task_id, _pr.WORKER_PLACED_EVENT, task_id),
    ).fetchone()
    if row is None:
        return None
    try:
        host = json.loads(row[0] or "{}").get("host")
    except (TypeError, ValueError, AttributeError):
        return None
    return host.lower() if isinstance(host, str) and host else None


_BIND_CHAIN_MAX = 8


def resolve_bind(conn: sqlite3.Connection, task_id: str) -> tuple[str, Optional[str]]:
    """``(owner_id, host)``: THE host-aware answer to "where is ``task_id``'s
    directory" (Apollo r1 on #2032). Follows ``workspace_of`` to the card that
    owns the directory (bounded, cycle-safe), then asks :func:`bind_host` of
    the owner first and of each reader on the chain after it: a reader that
    ran on a pool host ran where the directory is. ``host`` None = local, or
    nothing on the chain ever ran remotely. Every placement decision about a
    borrowed or shared directory reads this, never a Studio-local fact."""
    chain = [task_id]
    while len(chain) < _BIND_CHAIN_MAX:
        row = conn.execute("SELECT workspace_of FROM tasks WHERE id = ?", (chain[-1],)).fetchone()
        nxt = row[0] if row else None
        if not nxt or nxt in chain:
            break
        chain.append(nxt)
    owner = chain[-1]
    for tid in reversed(chain):
        host = bind_host(conn, tid)
        if host:
            return owner, host
    return owner, None


def _workspace_reader_of(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The card whose directory ``task_id`` borrowed (``--workspace-of``), or
    None. Ownership rule (Momus pass 3 R-1): such a card is a READER of that
    directory, never its owner, and never removes it."""
    try:
        row = conn.execute("SELECT workspace_of FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.Error:
        return None
    return (row[0] or None) if row else None


def _lexical_path(path: Path | str) -> Path:
    """Absolute, ``..``-collapsed, NFC form of *path* WITHOUT following symlinks."""
    return Path(_path_key(os.path.abspath(path)))


def _managed_scratch_path_info(p: Path) -> tuple[bool, Optional[str]]:
    """Return whether *p* is managed scratch storage and the matching board."""
    try:
        p_abs = p.resolve(strict=False)
    except OSError:
        return False, None
    # (resolved root, board, root as spelled)
    roots: list[tuple[Path, Optional[str], Path]] = []
    override = _kb._kanban_path_override("HERMES_KANBAN_WORKSPACES_ROOT")
    if override:
        try:
            roots.append((Path(override).expanduser().resolve(strict=False), None, Path(override).expanduser()))
        except OSError:
            pass
    try:
        home = _kb.kanban_home()
    except OSError:
        home = None
    # ``kanban.workspaces_root`` (e.g. a RAM-disk mount) places scratch dirs
    # at ``<root>/<board>/<task>``. Those per-board roots are managed exactly
    # like the legacy ones; the configured root itself and ``<root>/<board>``
    # stay refused by strict descendancy. Without them every scratch dir under
    # a configured root was refused forever and never reclaimed (t_bbea6686).
    try:
        from hermes_cli.kanban_workspace_policy import configured_root
        configured, _require_mount = configured_root()
    except Exception:
        configured = None
    if home is not None:
        try:
            roots.append(((home / "kanban" / "workspaces").resolve(strict=False), _kb.DEFAULT_BOARD,
                          home / "kanban" / "workspaces"))
        except OSError:
            pass
        if configured is not None:
            try:
                roots.append(((configured / _kb.DEFAULT_BOARD).resolve(strict=False), _kb.DEFAULT_BOARD,
                              configured / _kb.DEFAULT_BOARD))
            except OSError:
                pass
        try:
            boards_parent = (home / "kanban" / "boards").resolve(strict=False)
        except OSError:
            boards_parent = None
        if boards_parent is not None:
            try:
                entries = list(boards_parent.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                try:
                    roots.append(((entry / "workspaces").resolve(strict=False), entry.name, entry / "workspaces"))
                except OSError:
                    continue
                if configured is not None and entry.name != _kb.DEFAULT_BOARD:
                    try:
                        roots.append(((configured / entry.name).resolve(strict=False), entry.name, configured / entry.name))
                    except OSError:
                        continue
    memo: dict = {}
    p_lex = _lexical_path(p.expanduser())
    for root, board, root_spelled in roots:
        try:
            # Spelling-blind (card t_ee808d83 round 3): a DB row can store a
            # scratch path in another case/firmlink spelling, and a literal
            # miss refused its reclamation forever. Containment is decided by
            # kernel names, and STRICT descendancy is kept the same way: the
            # root itself, in any spelling, is never managed.
            if _kb._same_path(p_abs, root, memo):
                continue
            if _kb._same_tree(p_abs, root, memo) and _reached_through_root(p_lex, root_spelled, memo):
                return True, board
        except ValueError:
            continue
    return False, None


def _reached_through_root(p_lex: Path, root_spelled: Path, memo: dict) -> bool:
    """Upstream #28818 (symlinked root): *p* must also be spelled THROUGH a managed
    root, not merely resolve under it. When the workspaces root is a symlink to a
    broad directory, every path in the link target resolves "under" the root, so a
    scratch task naming such a path directly would be rmtree'd.

    Walk *p*'s lexical ancestors (symlinks NOT followed) looking for the root's own
    directory entry: the same lstat identity as the root as spelled (the symlink
    itself when the root is a link), or, through a differently-spelled ancestor
    path, the same resolved identity with no symlink hop. Identity, not string,
    comparison keeps the fork's case/firmlink spelling-blindness (t_ee808d83).
    """
    try:
        root_l = os.lstat(root_spelled)
    except OSError:
        return False
    root_is_link = os.path.islink(root_spelled)
    current = p_lex
    while current != current.parent:
        current = current.parent
        try:
            st = os.lstat(current)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == (root_l.st_dev, root_l.st_ino):
            return True
        if not root_is_link and not os.path.islink(current) and _kb._same_path(current, root_spelled, memo):
            return True
    return False


def _scratch_workspace(conn: sqlite3.Connection, task_id: str) -> Optional[Path]:
    """Expanded ``workspace_path`` when the task uses a scratch workspace, else ``None``."""
    row = conn.execute(
        "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row or row["workspace_kind"] != "scratch" or not row["workspace_path"]:
        return None
    return Path(row["workspace_path"]).expanduser()


def _is_managed_scratch_path(p: Path) -> bool:
    """True iff *p* is a STRICT descendant of a kanban-managed ``workspaces/``
    root (``HERMES_KANBAN_WORKSPACES_ROOT``, ``<kanban_home>/kanban/workspaces``,
    or ``<kanban_home>/kanban/boards/<slug>/workspaces``). A path equal to a
    root is not managed (deleting it would wipe every task's scratch dir);
    ``<kanban_home>/kanban``, ``.../logs`` and ``.../boards/<slug>`` hold
    Hermes' own DB and metadata. :func:`_cleanup_workspace` refuses
    ``rmtree`` outside managed storage — a board ``default_workdir`` on a real
    source tree paired with ``workspace_kind='scratch'`` would otherwise make
    task completion delete user data.

    See #28818.
    """
    return _managed_scratch_path_info(p)[0]


def _cleanup_workspace(conn: sqlite3.Connection, task_id: str) -> None:
    """Remove a task's scratch workspace dir and kill its stale tmux session.
    Called from :func:`complete_task` after the transaction commits; best-effort
    so cleanup never blocks completion. ``scratch`` is removed; ``worktree``
    only when provably free of work (clean tree, every commit reachable from a
    remote-tracking ref); ``dir`` is intentionally preserved."""
    try:
        row = conn.execute(_WORKSPACE_ROW_SQL, (task_id,)).fetchone()
        if not row:
            return
        kind: Optional[str] = row["workspace_kind"]
        path: Optional[str] = row["workspace_path"]
        from hermes_cli.kanban_survivor import remove_workspace_dir
        if kind not in ("scratch", "worktree") or not path or _workspace_reader_of(conn, task_id):
            # This task's own workspace isn't a removable scratch dir (or it is
            # a --workspace-of READER: the directory is its owner's, R-1), but
            # its completion may still unblock a deferred parent scratch
            # cleanup (e.g. a 'dir' child whose scratch parent was waiting on
            # it). #33774
            _try_cleanup_parent_workspaces(conn, task_id)
            return
        # Defer while any child is not yet terminal so it can still read
        # handoff artifacts from this workspace.
        if _retention_defers(conn, task_id):
            _kb._log.debug(
                "Deferring %s workspace cleanup for task %s: "
                "active children still need workspace at %s",
                kind, task_id, path,
            )
            return
        from hermes_cli.kanban_continuation import remote_cleanup_host
        remote_host = remote_cleanup_host(conn, task_id)
        if remote_host is not None:
            _kb.safe_remove_workspace_dir(Path(path), task_id=task_id, reason='complete_task',
                                          conn=conn, remote_host=remote_host)
        # Kill the (dead) tmux worker session BEFORE removing a worktree so a
        # lingering worker never has its cwd deleted from under it.
        if kind == "worktree":
            _cleanup_worker_tmux(conn, task_id)
            _cleanup_worktree_workspace(
                task_id, path, row["branch_name"],
                conn=conn, reason="complete_task",
            )
            _try_cleanup_parent_workspaces(conn, task_id)
            return
        wp = Path(path)
        if wp.is_dir():
            # Containment + liveness + audit all live in the choke point
            # (#28818 for containment; incident 2026-09-20 for liveness and
            # the audit trail). A board's ``default_workdir`` can pair
            # ``workspace_kind='scratch'`` with a user-supplied path pointing
            # at a real source tree, and a still-running card owns its dir.
            # safe_remove_workspace_dir performs the removal through
            # kanban_survivor.remove_workspace_dir, so survivor preservation
            # still runs underneath these gates.
            _kb.safe_remove_workspace_dir(
                wp, task_id=task_id, reason="complete_task", conn=conn,
            )
        # Also kill the tmux session for the worker that owned this task,
        # if the tmux session is now dead (worker process exited).
        _cleanup_worker_tmux(conn, task_id)
        # After cleaning up this task's workspace, check if any parent tasks now have all children done —
        # their deferred cleanup can proceed (#33774).
        _try_cleanup_parent_workspaces(conn, task_id)
    except Exception:
        pass  # best-effort — never block completion


def _cleanup_worktree_workspace(
    task_id: str,
    path: str,
    branch_name: Optional[str] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
    reason: str = "worktree_cleanup",
) -> None:
    """Remove a finished task's linked git worktree when it holds no work.

    Mirrors the safety judgment of the CLI startup pruner
    (``cli._prune_stale_worktrees``): removal requires a clean working tree
    AND every commit reachable from a remote-tracking ref. Any doubt — dirty
    files, unpushed commits, unresolvable repo, failing git — preserves the
    worktree. The task's auto-generated ``wt/<task-id>`` branch is deleted
    with it; custom branches are kept. Best-effort like the scratch path.

    Carries the same LIVENESS gate and AUDIT trail as
    :func:`safe_remove_workspace_dir`. Before this (card t_63fb42f9, review
    round 3) the worktree lane returned before ever reaching the choke point:
    it gated only on dirty/unpushed, so a ``running`` card with a clean,
    pushed worktree — run 1880's exact situation, which held a
    ``git worktree add --force --lock`` — was removed out from under the live
    process, and nothing was logged. Measured on a temp ``HERMES_HOME``:
    ``WT_STILL_EXISTS False / AUDIT_EXISTS False``.
    """
    try:
        from hermes_cli.worktree_ops import _worktree_has_unpushed_commits, _worktree_is_dirty
    except Exception:
        return  # CLI safety predicates unavailable — preserve
    try:
        wp = Path(path).expanduser()
        if not wp.is_dir():
            return
        if _retention_defers(conn, task_id) if conn is not None else _has_active_children(conn, task_id):
            _kb._audit_workspace_deletion(
                wp, task_id=task_id, reason=reason, allowed=False,
                detail="active-children-need-handoff",
            )
            return
        # Liveness FIRST: a card mid-run owns its checkout regardless of how
        # clean git thinks it is. Fail-closed on any DB error.
        if _kb._task_has_live_run(conn, task_id):
            _kb._audit_workspace_deletion(
                wp, task_id=task_id, reason=reason, allowed=False,
                detail="task-has-live-run",
            )
            _kb._log.warning(
                "Refusing to remove worktree %s: task %s is running or holds "
                "a live claim lock (reason %s)",
                wp, task_id, reason,
            )
            return
        # ...and liveness of whoever OWNS this checkout, which need not be
        # the caller. Before this, the worktree lane survived a cross-owner
        # removal only because ``git worktree remove`` refused underneath;
        # the guard did not hold, git did (review round 5).
        owners = _kb._live_owners_of_path(wp, conn=conn)
        if owners:
            _kb._audit_workspace_deletion(
                wp, task_id=task_id, reason=reason, allowed=False,
                detail="owner-has-live-run owner=%s" % ",".join(owners),
            )
            _kb._log.warning(
                "Refusing to remove worktree %s (caller task %s, reason %s): "
                "it is owned by live card(s) %s",
                wp, task_id, reason, ",".join(owners),
            )
            return
        common = _git_common_dir(wp)
        if common is None or common.name != ".git":
            _kb._audit_workspace_deletion(
                wp, task_id=task_id, reason=reason, allowed=False,
                detail="not-a-linked-worktree",
            )
            return  # not a linked worktree of a normal repo — never guess
        repo_root = common.parent
        if _path_key(wp.resolve(strict=False)) == _path_key(repo_root.resolve(strict=False)):
            _kb._audit_workspace_deletion(
                wp, task_id=task_id, reason=reason, allowed=False,
                detail="is-the-main-checkout",
            )
            return  # never remove the main checkout
        if _worktree_is_dirty(str(wp)) or _worktree_has_unpushed_commits(str(wp)):
            _kb._audit_workspace_deletion(
                wp, task_id=task_id, reason=reason, allowed=False,
                detail="dirty-or-unpushed",
            )
            _kb._log.info(
                "Preserving worktree for task %s: dirty or unpushed work at %s",
                task_id, wp,
            )
            return
        # Windows cannot delete a directory while this process has its current
        # directory inside it. Completed workers normally run from their own
        # linked worktree, so move this process back to the main checkout
        # before asking Git to remove the worktree.
        worktree_path = wp.resolve(strict=False)
        try:
            cwd = Path.cwd().resolve(strict=False)
        except OSError:
            # cwd was already deleted (a scratch-kind child's own workspace is
            # rmtree'd before this deferred parent cleanup runs, #33774). A
            # dead cwd cannot hold the worktree open, so leaving it is safe.
            cwd = None
        if cwd is None or cwd == worktree_path or cwd.is_relative_to(worktree_path):
            try:
                os.chdir(repo_root)
            except OSError as exc:
                _kb._log.warning(
                    "Preserving worktree for task %s: cannot leave %s for %s: %s",
                    task_id, cwd or "<deleted cwd>", repo_root, exc,
                )
                return
        # No --force: the dirty/unpushed checks above run before removal, so
        # git's own dirty guard re-verifies at removal time. If the tree
        # became dirty between our check and the removal (TOCTOU), removal
        # fails safe and the worktree is preserved.
        release_lsp_clients(str(worktree_path))
        from hermes_cli.kanban_survivor import remove_workspace_dir
        # Name the OWNING card, not just the caller (review round 5).
        wt_owners = _kb._live_owners_of_path(wp, conn=conn, live_only=False)
        wt_detail = "git-worktree-remove"
        if wt_owners and wt_owners != [task_id]:
            wt_detail += " owner=%s" % ",".join(wt_owners)
        _kb._audit_workspace_deletion(
            wp, task_id=task_id, reason=reason, outcome=_kb.AUDIT_ATTEMPT,
            detail=wt_detail,
        )
        if not remove_workspace_dir(conn, task_id, wp, worktree_root=repo_root):
            _kb._audit_workspace_deletion(
                wp, task_id=task_id, reason=reason, outcome=_kb.AUDIT_FAILED,
                detail="survivor-refused-or-git-remove-failed",
            )
            _kb._log.warning(
                "worktree removal refused for task %s at %s", task_id, wp,
            )
            return
        _kb._audit_workspace_deletion(
            wp, task_id=task_id, reason=reason, outcome=_kb.AUDIT_DELETE,
            detail=wt_detail,
        )
        _kb._log.debug("Removed worktree workspace: %s", wp)
        branch = (branch_name or "").strip() or f"wt/{task_id}"
        if branch.startswith("wt/"):
            _git(repo_root, "branch", "-D", branch, timeout=30)
    except Exception:
        pass  # best-effort — never block completion


def _try_cleanup_parent_workspaces(conn: sqlite3.Connection, task_id: str) -> None:
    """Run the deferred cleanup of any parent scratch/worktree workspace whose
    children are now all done/archived/failed/cancelled (called after each
    child completes).

    Candidate parents come from BOTH keys, ``task_links.parent_id`` and
    ``tasks.workspace_of``, so a parent deferred under either is found when
    its child finishes (Momus pass 2 N1 2(ii): no leak across the deploy).
    A parent that is not itself terminal keeps its directory: it is still in
    use, and its own completion runs this cleanup (Momus pass 3 R-1 (a)).

    See #33774.
    """
    try:
        parents = [r[0] for r in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? "
            "UNION SELECT workspace_of FROM tasks WHERE id = ? AND workspace_of IS NOT NULL "
            "ORDER BY 1",
            (task_id, task_id),
        ).fetchall()]
        for parent_id in parents:
            row = conn.execute(_WORKSPACE_ROW_SQL, (parent_id,)).fetchone()
            status = conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,)).fetchone()
            if (
                not row
                or status is None or status[0] not in _TERMINAL_STATUSES
                or row["workspace_kind"] not in _REMOVABLE_KINDS
                or not row["workspace_path"]
                or _retention_defers(conn, parent_id)
            ):
                continue
            from hermes_cli.kanban_survivor import remove_workspace_dir  # noqa: F401 (choke point below)
            # All children done (``_has_active_children`` above) — safe to clean up parent workspace
            if row["workspace_kind"] == "worktree":
                _cleanup_worktree_workspace(
                    parent_id, row["workspace_path"], row["branch_name"],
                    conn=conn, reason="deferred_parent_cleanup",
                )
                continue
            wp = Path(row["workspace_path"])
            if wp.is_dir():
                _kb.safe_remove_workspace_dir(
                    wp,
                    task_id=parent_id,
                    reason="deferred_parent_cleanup",
                    conn=conn,
                )
    except Exception:
        pass  # best-effort


def _cleanup_worker_tmux(conn: sqlite3.Connection, task_id: str) -> None:
    """Kill the tmux session associated with a task's assignee, if dead."""
    try:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row or not row["assignee"]:
            return
        # Workers named swarm1-12 use tmux sessions named swarm-swarm1 etc.
        session = f"swarm-{row['assignee']}"
        out = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_dead}"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
        )
        if out.stdout.strip() == "1":
            subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True, timeout=5)
            _kb._log.debug("Killed stale tmux session: %s", session)
    except Exception:
        pass  # best-effort — never block completion
_SCRATCH_TIP_SENTINEL_NAME = ".scratch_tip_shown"
_SCRATCH_TIP_MESSAGE = (
    "scratch workspaces are ephemeral — they're deleted when the task "
    "completes. Use --workspace worktree: (git worktree) or "
    "--workspace dir:/abs/path (existing dir) to preserve worker output."
)


def _scratch_tip_sentinel_path() -> Path:
    """Path to the per-install scratch-workspace-tip sentinel file."""
    return _kb.kanban_home() / _SCRATCH_TIP_SENTINEL_NAME


def _scratch_tip_shown() -> bool:
    """True iff the scratch-workspace tip was already emitted on this install.
    Best-effort — any error re-emits, the safer failure mode for a help message."""
    try:
        return _scratch_tip_sentinel_path().exists()
    except OSError:
        return False


def _mark_scratch_tip_shown() -> None:
    """Touch the sentinel so future scratch workspaces stay silent. Best-effort:
    a failure means the tip may appear once more, preferable to crashing dispatch."""
    try:
        path = _scratch_tip_sentinel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        pass


def _maybe_emit_scratch_tip(
    conn: sqlite3.Connection,
    task_id: str,
    workspace_kind: Optional[str],
) -> None:
    """Emit the first-use scratch-workspace tip once per install, right after a
    scratch workspace is materialized. No-op for ``worktree``/``dir`` (preserved
    by design) and once the sentinel exists."""
    if (workspace_kind or "scratch") != "scratch" or _scratch_tip_shown():
        return
    try:
        _kb._log.warning("kanban: %s (task %s)", _SCRATCH_TIP_MESSAGE, task_id)
        with _kb.write_txn(conn):
            _kb._append_event(
                conn, task_id, "tip_scratch_workspace",
                {"message": _SCRATCH_TIP_MESSAGE},
            )
    except Exception:
        # Best-effort — never block the spawn loop over a help message.
        pass
    finally:
        _mark_scratch_tip_shown()


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------
def _git_toplevel(path: Path) -> Optional[Path]:
    """Return the git toplevel containing ``path``, or ``None`` if not in a repo."""
    out = _kb._git_out(path, "rev-parse", "--show-toplevel")
    if out is None:
        return None
    try:
        return Path(out).expanduser().resolve()
    except Exception:
        return Path(out).expanduser()


def _git_branch_exists(repo_root: Path, branch_name: str) -> bool:
    try:
        result = _git(repo_root, "show-ref", "--verify", f"refs/heads/{branch_name}", timeout=30)
    except Exception:
        return False
    return result.returncode == 0


def _git_abs_path(path: Path, flag: str) -> Optional[Path]:
    out = _kb._git_out(path, "rev-parse", "--path-format=absolute", flag)
    return Path(out).expanduser().resolve(strict=False) if out else None


def _git_common_dir(path: Path) -> Optional[Path]:
    return _git_abs_path(path, "--git-common-dir")


def _git_dir(path: Path) -> Optional[Path]:
    return _git_abs_path(path, "--git-dir")


def _git_current_branch(path: Path) -> Optional[str]:
    return _kb._git_out(path, "branch", "--show-current")


def _is_linked_worktree_checkout(path: Path) -> bool:
    git_dir = _git_dir(path)
    common_dir = _git_common_dir(path)
    return git_dir is not None and common_dir is not None and git_dir != common_dir


def _nearest_existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _repo_root_for_worktree_target(path: Path) -> Optional[Path]:
    current = _nearest_existing_path(path).resolve(strict=False)
    while True:
        repo_root = _git_toplevel(current)
        if repo_root is not None:
            return repo_root
        if current == current.parent:
            return None
        current = current.parent


def _ensure_git_worktree(repo_root: Path, target: Path, branch_name: str) -> None:
    """Materialize ``target`` as a linked git worktree under ``repo_root``.

    When ``target`` already exists as a linked worktree of ``repo_root``,
    reuse it — but only after confirming it is checked out on
    ``branch_name``. A previous failed/interrupted dispatch of the same
    task can leave the canonical ``.worktrees/<id>`` path on a stale or
    unrelated branch; silently reusing it would run this task's work on
    the wrong branch. If the branch differs, switch it back (creating the
    branch if needed) so the reused checkout matches the requested branch.
    """
    target = target.expanduser()
    repo_common = _git_common_dir(repo_root)
    if (
        target.exists() and repo_common is not None
        and _path_key(_git_common_dir(target)) == _path_key(repo_common)
    ):
        if _git_current_branch(target) == branch_name:
            return
        # Reused worktree is on the wrong branch — realign it.
        if _git_branch_exists(repo_root, branch_name):
            switch = _git(target, "checkout", branch_name, timeout=60)
        else:
            switch = _git(target, "checkout", "-b", branch_name, timeout=60)
        if switch.returncode != 0:
            stderr = (switch.stderr or switch.stdout or "").strip()
            raise RuntimeError(
                f"git checkout {branch_name} failed for reused worktree "
                f"{target}: {stderr}"
            )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if _git_branch_exists(repo_root, branch_name):
        args = ["worktree", "add", str(target), branch_name]
    else:
        # Branch off the freshly fetched remote trunk, never the anchor's
        # local HEAD: a long-lived anchor checkout lags its remote by hundreds
        # of commits and can carry local-only commits (t_18e781d0). --no-track
        # so a bare `git push` can never target the trunk.
        from hermes_cli.kanban_branch_base import fresh_trunk_ref

        base = fresh_trunk_ref(repo_root)
        if base is None:
            _kb._log.warning(
                "kanban worktree %s: no remote trunk in %s; branching from local HEAD",
                target, repo_root,
            )
        args = ["worktree", "add", "--no-track", "-b", branch_name, str(target), base or "HEAD"]
    result = _git(repo_root, *args, timeout=60)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"git worktree add failed for {target} on branch {branch_name}: {stderr}"
        )


def _anchored_worktree(repo_root: Path, task_id: str, branch_name: str) -> tuple[Path, str]:
    """Materialize the canonical ``<repo>/.worktrees/<task-id>`` worktree."""
    target = repo_root / ".worktrees" / task_id
    _ensure_git_worktree(repo_root, target, branch_name)
    return target, branch_name


def _resolve_worktree_workspace(task: Task, *, board: Optional[str] = None) -> tuple[Path, str]:
    """Resolve + materialize a linked git worktree for ``task``. With no
    ``task.workspace_path`` the anchor is the board's ``default_workdir`` so
    every worktree lands under a board-owned repo (``<repo>/.worktrees/<id>``)
    instead of the dispatcher's incidental CWD (whatever dir the gateway was
    launched from); with no anchor configured we fail loudly rather than guess."""
    branch_name = (task.branch_name or "").strip() or f"wt/{task.id}"
    if not task.workspace_path:
        board_slug = board if board else _kb.get_current_board()
        board_default = (_kb.read_board_metadata(board_slug).get("default_workdir") or "").strip()
        if not board_default:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but no workspace_path, "
                f"and board {board_slug!r} has no default_workdir set. Set a board "
                "default workdir (a git repo) or create the task with "
                "--workspace worktree:<absolute-repo-path>."
            )
        anchor = Path(board_default).expanduser()
        refuse_runtime_tree_workspace("worktree", anchor)
        if not anchor.is_absolute():
            raise ValueError(
                f"board {board_slug!r} default_workdir {board_default!r} is not "
                "absolute; use an absolute path to a git repo"
            )
        repo_root = _git_toplevel(anchor)
        if repo_root is None:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but board "
                f"{board_slug!r} default_workdir {board_default!r} is not inside a git repo"
            )
        return _anchored_worktree(repo_root, task.id, branch_name)

    requested = Path(task.workspace_path).expanduser()
    refuse_runtime_tree_workspace("worktree", requested)
    if not requested.is_absolute():
        raise ValueError(
            f"task {task.id} has non-absolute worktree path "
            f"{task.workspace_path!r}; use an absolute path"
        )
    requested_resolved = requested.resolve(strict=False)

    if requested.exists() and _is_linked_worktree_checkout(requested):
        actual_branch = _git_current_branch(requested)
        if actual_branch == branch_name:
            return requested_resolved, actual_branch
        # The requested path is an existing checkout of a DIFFERENT task's
        # branch (decompose children inherit the root's workspace_path
        # verbatim, so siblings all point here). Reusing it would run this task
        # on the other task's branch — silent cross-task provenance corruption,
        # unsafe under concurrency — so fall back to our own worktree.
        fallback_root = _repo_root_for_worktree_target(requested.parent)
        if fallback_root is not None:
            fallback = fallback_root / ".worktrees" / task.id
            if _path_key(fallback.resolve(strict=False)) != _path_key(requested_resolved):
                _ensure_git_worktree(fallback_root, fallback, branch_name)
                return fallback.resolve(strict=False), branch_name
        # No repo to anchor a fallback on (or the occupied path IS this task's
        # own canonical worktree): keep the legacy reuse rather than fail dispatch.
        return requested_resolved, actual_branch or branch_name

    repo_root = _git_toplevel(requested)
    if repo_root is not None and _path_key(requested_resolved) == _path_key(repo_root):
        return _anchored_worktree(repo_root, task.id, branch_name)

    repo_root = _repo_root_for_worktree_target(requested.parent)
    if repo_root is None:
        raise ValueError(
            f"task {task.id} worktree path {task.workspace_path!r} is not inside a git repo "
            "and does not point at a git repo root"
        )
    _ensure_git_worktree(repo_root, requested, branch_name)
    return requested, branch_name


def resolve_workspace(task: Task, *, board: Optional[str] = None) -> Path:
    """Resolve (and create if needed) the workspace for a task.

    - ``scratch``: a fresh dir under ``<board-root>/workspaces/<id>/``,
      where ``<board-root>`` is the active board's root. The path is the
      same for the dispatcher and every profile worker, so handoff is
      path-stable.
    - ``dir:<path>``: the path stored in ``workspace_path``.  Created
      if missing.  MUST be absolute — relative paths are rejected to
      prevent confused-deputy traversal where ``../../../tmp/attacker``
      resolves against the dispatcher's CWD instead of a meaningful
      root.  Users who want a kanban-root-relative workspace should
      compute the absolute path themselves.
    - ``worktree``: a real linked git worktree. If ``workspace_path`` names
      a repo root, Hermes treats it as an anchor and materializes a linked
      worktree at ``<repo>/.worktrees/<task-id>``. If ``workspace_path`` names
      a concrete target path, Hermes creates/reuses that linked worktree. With
      no ``workspace_path``, Hermes anchors on the board's ``default_workdir``
      and materializes ``<repo>/.worktrees/<task-id>`` per task; if no
      ``default_workdir`` is configured it raises rather than guessing from the
      dispatcher's CWD. When ``branch_name`` is empty, Hermes uses
      ``wt/<task-id>``.

    Persist the resolved path back to the task row via ``set_workspace_path``
    so subsequent runs reuse the same directory.
    """
    protected = _kb._validate_workspace_admission(task, board=board)
    kind = task.workspace_kind or "scratch"
    if kind == "worktree":
        return _resolve_worktree_workspace(task, board=board)[0]
    if kind == "scratch" and not task.workspace_path:
        p = _kb.workspaces_root(board=board) / task.id
        if protected is not None:
            from hermes_cli.kanban_workspace_policy import create_scratch
            create_scratch(
                protected.root, p, expected_mount=protected.mount_path,
            )
            return p
    elif kind == "scratch":
        # Legacy explicit-path scratch tasks get the same absolute-path guard
        # as dir: — same threat model.
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; workspace paths must be absolute"
            )
        if protected is not None:
            return p
    elif kind == "dir":
        if not task.workspace_path:
            raise ValueError(f"task {task.id} has workspace_kind=dir but no workspace_path")
        p = Path(task.workspace_path).expanduser()
        refuse_runtime_tree_workspace("dir", p)
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; use an absolute path "
                f"(relative paths are ambiguous against the dispatcher's CWD)"
            )
    else:
        raise ValueError(f"unknown workspace_kind: {kind}")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _set_task_column(conn: sqlite3.Connection, task_id: str, column: str, value: str) -> None:
    with _kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, task_id))


def set_workspace_path(conn: sqlite3.Connection, task_id: str, path: Path | str) -> None:
    _set_task_column(conn, task_id, "workspace_path", str(path))
    from hermes_cli.kanban_survivor import record_baseline
    record_baseline(conn, task_id, path)


def set_branch_name(conn: sqlite3.Connection, task_id: str, branch_name: str) -> None:
    _set_task_column(conn, task_id, "branch_name", str(branch_name))

# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb  # noqa: E402
