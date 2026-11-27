"""Task graph initialization and atomic decomposition persistence."""
from __future__ import annotations
import sqlite3
import time
from typing import Any, Optional
from hermes_cli import kanban_db as _kb  # noqa: E402  (head: decorators need it)


def inherit_creator_origin(
    conn: sqlite3.Connection, task_id: str, creator_task_id: Optional[str], *,
    created_at: int,
) -> None:
    """Copy durable origin inside creation's transaction, never adding dependencies."""
    if not creator_task_id:
        return
    from hermes_cli.kanban_db import _inherit_notify_subs

    conn.execute(
        "UPDATE tasks SET session_id = COALESCE(session_id, "
        "(SELECT session_id FROM tasks WHERE id = ?)) WHERE id = ?",
        (creator_task_id, task_id),
    )
    _inherit_notify_subs(conn, task_id, (creator_task_id,), created_at=created_at)


def initial_task_state(
    conn: sqlite3.Connection, parents: tuple[str, ...], initial_status: str,
    triage: bool, tenant: Optional[str],
) -> tuple[str, Optional[str]]:
    """Resolve state and tenant under the creator's write transaction.

    Parent order breaks ties in this soft namespace; explicit tenant wins.
    Validate parents even for parked tasks so links never dangle.
    """
    rows = {}
    if parents:
        rows = {row["id"]: row for row in conn.execute(
            "SELECT id, status, tenant FROM tasks WHERE id IN "
            "(" + ",".join("?" * len(parents)) + ")", parents,
        )}
        missing = [pid for pid in parents if pid not in rows]
        if missing:
            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
        if tenant is None:
            tenant = next((rows[pid]["tenant"] for pid in parents if rows[pid]["tenant"]), None)
    if initial_status == "blocked":
        return "blocked", tenant
    if triage:
        return "triage", tenant
    if any(row["status"] != "done" for row in rows.values()):
        return "todo", tenant
    return "ready", tenant


def _validate_children_graph(children: list) -> None:
    """DB-free shape check + Kahn's cycle check on the sibling graph (a cycle
    would deadlock every involved child in ``todo`` forever)."""
    for idx, child in enumerate(children):
        if not isinstance(child, dict):
            raise ValueError(f"child[{idx}] is not a dict")
        title = child.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"child[{idx}].title is required")
        parents_idx = child.get("parents") or []
        if not isinstance(parents_idx, list):
            raise ValueError(f"child[{idx}].parents must be a list")
        for p in parents_idx:
            if not isinstance(p, int) or p < 0 or p >= len(children):
                raise ValueError(f"child[{idx}].parents[{p}] is not a valid index into children")
            if p == idx:
                raise ValueError(f"child[{idx}] cannot list itself as a parent")

    in_deg = [0] * len(children)
    adj: list[list[int]] = [[] for _ in children]
    for i, c in enumerate(children):
        for p in (c.get("parents") or []):
            adj[p].append(i)
            in_deg[i] += 1
    queue = [i for i in range(len(children)) if in_deg[i] == 0]
    seen = 0
    while queue:
        seen += 1
        for nb in adj[queue.pop()]:
            in_deg[nb] -= 1
            if in_deg[nb] == 0:
                queue.append(nb)
    if seen != len(children):
        raise ValueError("cyclic dependency detected in decomposed children list")


@_kb._home_session_guarded("decompose")
def decompose_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    root_assignee: Optional[str],
    children: list[dict],
    author: Optional[str] = None,
    auto_promote: bool = True,
    board: Optional[str] = None,
) -> Optional[list[str]]:
    """Fan a triage task out into children and move the root to ``todo``; the root
    waits on every child and wakes (``ready``) when all are done.

    ``children``: dicts of ``title`` (required), ``body``, ``assignee``,
    ``parents`` (indices into this list), optional workspace overrides.
    Returns child ids in input order, or None when the root is missing / not
    in triage, or has already decomposed. Atomic: malformed entries abort fan-out.
    """
    from hermes_cli.kanban_db import (
        _canonical_assignee, _link, _append_event, _insert_comment,
        write_txn, recompute_ready,
    )

    if not children:
        return None
    if root_assignee is not None:
        root_assignee = _canonical_assignee(root_assignee)
    _validate_children_graph(children)
    from hermes_cli.kanban_ruling_citation import validate_fields
    for child in children:
        validate_fields(conn, [("title", child["title"]), ("body", child.get("body"))])

    # ONE txn so the fan-out is atomic; helpers that open their own write_txn
    # (create_task, link_tasks, add_comment) must not be called in here.
    now = int(time.time())
    from hermes_cli import kanban_boomerang_default as _kbd
    from hermes_cli.kanban_db_connect import write_card_boomerang_wake

    policy_boomerang = _kbd.policy_default()
    with write_txn(conn):
        root_row = conn.execute(
            "SELECT id, status, tenant, workspace_kind, workspace_path, "
            "session_id, body, boomerang_wake FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if root_row is None or root_row["status"] != "triage":
            return None
        # Dependency links alone do not imply lineage. The completion event is
        # committed with the graph, and survives re-triage or unlinking.
        if conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'decomposed' LIMIT 1",
            (task_id,),
        ).fetchone():
            return None
        tenant = root_row["tenant"]
        root_home = (root_row["session_id"] or "").strip() or None
        root_origin = _kb._origin_line(root_row["body"])
        # Children carry the root's boomerang choice (an opt-out included); a root
        # that predates the creation default gets the policy default.
        if root_row["boomerang_wake"] is None:
            child_boomerang, boomerang_source = policy_boomerang, _kbd.SOURCE_DEFAULT
        else:
            child_boomerang, boomerang_source = bool(root_row["boomerang_wake"]), _kbd.SOURCE_INHERITED
        # Children inherit the root's workspace by default so a fan-out
        # of a code-gen task lands in the parent's project dir/worktree
        # rather than throwaway scratch tmp dirs. A child dict can still
        # override with its own 'workspace_kind' / 'workspace_path'.
        root_ws_kind = root_row["workspace_kind"] or "scratch"
        root_ws_path = root_row["workspace_path"]
        # When the root task was created with the default scratch workspace
        # (no explicit path), fall back to the board's default_workdir so
        # children land in the project directory rather than a throwaway
        # kanban scratch dir. The root itself is also upgraded so its
        # orchestration run shares the same directory.
        _board_ws_upgraded = False
        if root_ws_kind == "scratch" and not root_ws_path:
            # Resolve the board ONCE and thread it explicitly, matching
            # every other board-aware function here. Falling back to the
            # ambient get_current_board() only when no board was passed
            # would let an outer scope point the lookup at a
            # different board than the caller's connection is scoped to,
            # silently upgrading children into an unrelated project dir.
            _resolved_board = board if board is not None else _kb.get_current_board()
            _board_meta = _kb.read_board_metadata(_resolved_board)
            _board_default = _board_meta.get("default_workdir")
            if _board_default:
                root_ws_kind = "dir"
                root_ws_path = str(_board_default)
                _board_ws_upgraded = True

        # Create children. Status is 'todo' regardless of parents — we
        # link them under the root AFTER creation so the dispatcher
        # sees a coherent state, and recompute_ready() at the end
        # promotes parent-free children to 'ready'.
        child_ids: list[str] = []
        for idx, child in enumerate(children):
            new_id = _kb._new_task_id()
            title = child["title"].strip()
            body = child.get("body")
            assignee = _canonical_assignee(child.get("assignee"))
            # Per-child override wins; otherwise inherit the root's
            # workspace. A child that sets workspace_kind without a path
            # falls back to the root path only when kinds match (so a
            # child can't accidentally point a 'dir' at the root's
            # worktree path or vice versa).
            child_ws_kind = child.get("workspace_kind") or root_ws_kind
            if child.get("workspace_path"):
                child_ws_path = child.get("workspace_path")
            elif child_ws_kind == "worktree":
                # Never share one worktree checkout between siblings: the
                # root's literal path would put every child in the same
                # directory on the first-dispatched sibling's branch, with
                # no lock — siblings can be promoted and dispatched
                # concurrently. Leave the path unset so dispatch
                # materializes a fresh <repo>/.worktrees/<child-id> per
                # child from the board anchor.
                child_ws_path = None
            elif child_ws_kind == root_ws_kind:
                child_ws_path = root_ws_path
            else:
                child_ws_path = None
            # Decomposed children belong to the root's HOME session, and
            # carry its origin line, exactly like any other parented card.
            child_body = body if isinstance(body, str) else None
            if root_origin and not _kb._origin_line(child_body):
                child_body = (
                    f"{root_origin}\n\n{child_body}"
                    if (child_body or "").strip() else root_origin
                )
            # The one task INSERT, with the stored-body check (t_41763e36).
            _kb._insert_task_row(conn, {
                'id': new_id, 'title': title, 'body': child_body, 'assignee': assignee,
                'status': 'todo', 'workspace_kind': child_ws_kind,
                'workspace_path': child_ws_path, 'tenant': tenant, 'created_at': now,
                'created_by': (author or "decomposer"), 'session_id': root_home,
                'boomerang_wake': 1 if child_boomerang else 0,
            })
            write_card_boomerang_wake(conn, new_id, 1 if child_boomerang else 0)
            _append_event(
                conn, new_id, "created",
                {"by": author or "decomposer", "from_decompose_of": task_id,
                 "boomerang_wake": child_boomerang, "boomerang_wake_source": boomerang_source},
            )
            _kb._inherit_notify_subs(conn, new_id, (task_id,), created_at=now)
            child_ids.append(new_id)

        # Link children to their sibling parents (within the decomposed graph).
        for idx, child in enumerate(children):
            for p_idx in child.get("parents") or []:
                parent_id, child_id = child_ids[p_idx], child_ids[idx]
                _link(conn, parent_id, child_id)
                _append_event(conn, child_id, "linked", {"parent": parent_id, "child": child_id})
        # Root waits for the whole graph: link it under EVERY child (simpler
        # than computing leaves; cycle-free since the root is only ever a child).
        for cid in child_ids:
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) "
                "VALUES (?, ?)",
                (cid, task_id),
            )

        # Flip the root: triage -> todo, set assignee to the orchestrator.
        # Also persist the resolved board workspace so the orchestration
        # run lands in the project directory (not a kanban scratch dir).
        sets = ["status = 'todo'"]
        params: list[Any] = []
        if root_assignee is not None:
            sets.append("assignee = ?")
            params.append(root_assignee)
        if _board_ws_upgraded:
            sets.append("workspace_kind = ?")
            sets.append("workspace_path = ?")
            params.append(root_ws_kind)
            params.append(root_ws_path)
        params.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", tuple(params))
        if author and author.strip():
            _insert_comment(
                conn, task_id, author.strip(),
                "Decomposed into " + ", ".join(child_ids)
                + ". Root will wake when all children complete.",
                now,
            )
        _append_event(
            conn, task_id, "decomposed", {"child_ids": child_ids, "root_assignee": root_assignee},
        )
    # Outside the txn (own IMMEDIATE txn). ``auto_promote=False`` leaves the
    # children in ``todo`` for manual-review-first workflows.
    if auto_promote:
        recompute_ready(conn)
    return child_ids
