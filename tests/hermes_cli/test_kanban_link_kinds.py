"""Link kinds: ``blocks`` (gates) vs ``derived-from`` (provenance only).

Regression suite for the 2026-08-08 incident: a DEPLOY card was linked as a
child of an AUDIT card purely to record where it came from. Kanban had one
edge type and it meant "blocks", so the audit going to ``triage`` held a
merged, green, shipped fix in ``todo`` forever.

The principle under test: **a DEPLOY must never be gated on a DISCOVERY.**
Every test here therefore comes in a pair — the provenance edge must NOT gate,
and the genuine dependency edge must STILL gate (the negative control that
proves nothing silently un-gated).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# Titles from the real cards, so the regression is recognisable in a failure.
AUDIT_TITLE = "AUDIT ALL 37 PROTOCOL FIELDS for the same top-level-scope bug"
DEPLOY_TITLE = (
    "DEPLOY the metadata.user_id top-level-scope fix — 8,767 reqs (47%) "
    "leaked identity into a tool schema"
)


def _incident_pair(conn, *, kind: str | None):
    """Build the exact 2026-08-08 shape: a triage AUDIT parent, DEPLOY child."""
    audit = kb.create_task(conn, title=AUDIT_TITLE, assignee="daedalus", triage=True)
    deploy = kb.create_task(
        conn,
        title=DEPLOY_TITLE,
        assignee="daedalus",
        parents=[audit],
        parents_kind=kind,
    )
    assert kb.get_task(conn, audit).status == "triage"
    return audit, deploy


# ---------------------------------------------------------------------------
# The incident itself
# ---------------------------------------------------------------------------

def test_blocks_edge_still_strands_the_deploy_behind_the_audit(kanban_home):
    """NEGATIVE CONTROL — the default edge still gates, exactly as before.

    This is the bug as it behaves today. It must keep behaving this way, or
    the fix has silently un-gated every real dependency on the board.
    """
    with kb.connect() as conn:
        audit, deploy = _incident_pair(conn, kind=None)

        assert kb.get_task(conn, deploy).status == "todo"
        kb.recompute_ready(conn)
        assert kb.get_task(conn, deploy).status == "todo", (
            "a blocking parent in triage must keep the child held"
        )
        assert kb.claim_task(conn, deploy) is None
        ok, err = kb.promote_task(conn, deploy, actor="test")
        assert ok is False
        assert audit in (err or "")

        # ...and it releases the moment the real dependency is satisfied.
        # (``complete_task`` refuses a 'triage' row, which is itself part of
        # the incident: the audit could not even be closed out from triage.)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (audit,))
        conn.commit()
        assert kb.complete_task(conn, audit, result="37 fields audited") is True
        kb.recompute_ready(conn)
        assert kb.get_task(conn, deploy).status == "ready"


def test_derived_from_edge_dispatches_the_deploy_immediately(kanban_home):
    """THE FIX — provenance recorded, deploy independently dispatchable."""
    with kb.connect() as conn:
        audit, deploy = _incident_pair(conn, kind="derived-from")

        assert kb.get_task(conn, deploy).status == "ready", (
            "a derived-from parent must never hold the child"
        )
        # Provenance survives: the edge is still there, just not gating.
        assert kb.parent_ids(conn, deploy) == [audit]
        assert kb.parent_links(conn, deploy) == [(audit, "derived-from")]

        claimed = kb.claim_task(conn, deploy)
        assert claimed is not None and claimed.id == deploy
        assert kb.get_task(conn, deploy).status == "running"


def test_relinking_the_incident_edge_releases_the_held_deploy(kanban_home):
    """The in-place repair path for a board that already has the bad edge."""
    with kb.connect() as conn:
        audit, deploy = _incident_pair(conn, kind=None)
        assert kb.get_task(conn, deploy).status == "todo"

        # Re-link the SAME pair with the right semantics — no unlink needed.
        kb.link_tasks(conn, audit, deploy, kind="derived-from")

        assert kb.parent_links(conn, deploy) == [(audit, "derived-from")]
        assert kb.get_task(conn, deploy).status == "ready", (
            "converting the edge to provenance must release the child "
            "without waiting for a dispatcher tick"
        )
        assert kb.claim_task(conn, deploy) is not None


# ---------------------------------------------------------------------------
# Every gating site, both kinds
# ---------------------------------------------------------------------------

def test_link_tasks_demotes_only_for_blocks(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        blocked_child = kb.create_task(conn, title="gated")
        derived_child = kb.create_task(conn, title="provenance")
        assert kb.get_task(conn, blocked_child).status == "ready"
        assert kb.get_task(conn, derived_child).status == "ready"

        kb.link_tasks(conn, parent, blocked_child)
        kb.link_tasks(conn, parent, derived_child, kind="derived-from")

        assert kb.get_task(conn, blocked_child).status == "todo"
        assert kb.get_task(conn, derived_child).status == "ready"


def test_claim_task_rejects_only_for_blocks(kanban_home):
    """``claim_task`` re-checks parents itself; it must use the same rule."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        gated = kb.create_task(conn, title="gated", parents=[parent])
        derived = kb.create_task(
            conn, title="derived", parents=[parent], parents_kind="derived-from",
        )
        # Force both into 'ready' behind the dispatcher's back, the racy-writer
        # case the claim-time invariant exists to catch.
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id IN (?, ?)",
            (gated, derived),
        )
        conn.commit()

        assert kb.claim_task(conn, gated) is None
        assert kb.get_task(conn, gated).status == "todo", (
            "claim must demote a task promoted past a real dependency"
        )
        assert kb.claim_task(conn, derived) is not None


def test_promote_task_counts_only_blocking_parents(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        gated = kb.create_task(conn, title="gated", parents=[parent])
        derived = kb.create_task(
            conn, title="derived", parents=[parent], parents_kind="derived-from",
        )
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (derived,))
        conn.commit()

        ok, err = kb.promote_task(conn, gated, actor="test")
        assert ok is False and parent in (err or "")

        ok, err = kb.promote_task(conn, derived, actor="test")
        assert ok is True and err is None
        assert kb.get_task(conn, derived).status == "ready"


def test_unblock_task_counts_only_blocking_parents(kanban_home):
    """Unblock re-gates on parents; a provenance parent must not park it."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        gated = kb.create_task(conn, title="gated", parents=[parent])
        derived = kb.create_task(
            conn, title="derived", parents=[parent], parents_kind="derived-from",
        )
        for tid in (gated, derived):
            conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,)
            )
        conn.commit()

        assert kb.unblock_task(conn, gated) is True
        assert kb.get_task(conn, gated).status == "todo"

        assert kb.unblock_task(conn, derived) is True
        assert kb.get_task(conn, derived).status == "ready"


def test_recompute_ready_ignores_derived_from_parents(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        derived = kb.create_task(
            conn, title="derived", parents=[parent], parents_kind="derived-from",
        )
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (derived,))
        conn.commit()

        assert kb.recompute_ready(conn) >= 1
        assert kb.get_task(conn, derived).status == "ready"


def test_mixed_parents_gate_on_the_blocking_one_only(kanban_home):
    """A task with both edge kinds waits for the dependency, not the audit."""
    with kb.connect() as conn:
        audit = kb.create_task(conn, title="audit", triage=True)
        dependency = kb.create_task(conn, title="real dependency")
        child = kb.create_task(conn, title="child", parents=[dependency])
        kb.link_tasks(conn, audit, child, kind="derived-from")

        assert kb.get_task(conn, child).status == "todo"
        kb.complete_task(conn, dependency, result="done")
        assert kb.get_task(conn, dependency).status == "done"
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready", (
            "the still-open derived-from audit must not keep holding the child"
        )


# ---------------------------------------------------------------------------
# Validation / API surface
# ---------------------------------------------------------------------------

def test_normalize_link_kind_defaults_and_rejects(kanban_home):
    assert kb.normalize_link_kind(None) == "blocks"
    assert kb.normalize_link_kind("") == "blocks"
    assert kb.normalize_link_kind("  BLOCKS ") == "blocks"
    # Underscore spelling is accepted; hyphenated is canonical on disk.
    assert kb.normalize_link_kind("derived_from") == "derived-from"
    assert kb.normalize_link_kind("Derived-From") == "derived-from"
    with pytest.raises(ValueError, match="unknown link kind"):
        kb.normalize_link_kind("spawned-by")


def test_bad_kind_is_rejected_before_any_row_is_written(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        with pytest.raises(ValueError, match="unknown link kind"):
            kb.create_task(
                conn, title="child", parents=[parent], parents_kind="nonsense",
            )
        assert [t.title for t in kb.list_tasks(conn)] == ["parent"]

        child = kb.create_task(conn, title="child")
        with pytest.raises(ValueError, match="unknown link kind"):
            kb.link_tasks(conn, parent, child, kind="nonsense")
        assert kb.parent_links(conn, child) == []


def test_cycle_and_self_link_rejected_for_derived_from_too(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b", parents=[a])
        with pytest.raises(ValueError, match="itself"):
            kb.link_tasks(conn, a, a, kind="derived-from")
        with pytest.raises(ValueError, match="cycle"):
            kb.link_tasks(conn, b, a, kind="derived-from")


def test_child_links_and_created_event_report_kind(kanban_home):
    with kb.connect() as conn:
        audit = kb.create_task(conn, title="audit")
        deploy = kb.create_task(
            conn, title="deploy", parents=[audit], parents_kind="derived-from",
        )
        assert kb.child_links(conn, audit) == [(deploy, "derived-from")]

        created = [e for e in kb.list_events(conn, deploy) if e.kind == "created"]
        assert created and created[0].payload["parents_kind"] == "derived-from"

        linked = [e for e in kb.list_events(conn, deploy) if e.kind == "linked"]
        kb.link_tasks(conn, audit, deploy, kind="blocks")
        linked = [e for e in kb.list_events(conn, deploy) if e.kind == "linked"]
        assert linked[-1].payload["kind"] == "blocks"


def test_default_edge_is_blocks_everywhere(kanban_home):
    """No caller opts in accidentally: every default path stores 'blocks'."""
    with kb.connect() as conn:
        p = kb.create_task(conn, title="p")
        via_create = kb.create_task(conn, title="c1", parents=[p])
        via_link = kb.create_task(conn, title="c2")
        kb.link_tasks(conn, p, via_link)

        rows = dict(
            conn.execute(
                "SELECT child_id, kind FROM task_links WHERE parent_id = ?",
                (p,),
            ).fetchall()
        )
        assert rows == {via_create: "blocks", via_link: "blocks"}


# ---------------------------------------------------------------------------
# Migration: a pre-``kind`` DB must keep gating
# ---------------------------------------------------------------------------

def test_legacy_links_backfill_to_blocks_and_still_gate(tmp_path, monkeypatch):
    """A DB written before the column existed must not silently un-gate.

    Backfilling to anything but 'blocks' would release every held child on the
    fleet's boards the first time the new code opened them.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="legacy")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    # Hand-write the pre-migration schema: task_links with no ``kind``.
    raw = sqlite3.connect(str(db_path))
    raw.executescript(kb.SCHEMA_SQL)
    raw.executescript(
        "DROP TABLE task_links;"
        "CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL,"
        " PRIMARY KEY (parent_id, child_id));"
    )
    raw.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('t_audit', 'audit', 'triage', 1000)"
    )
    raw.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('t_deploy', 'deploy', 'todo', 1000)"
    )
    raw.execute("INSERT INTO task_links VALUES ('t_audit', 't_deploy')")
    raw.commit()
    raw.close()
    assert "kind" not in _link_columns(db_path)

    with kb.connect(db_path) as conn:
        assert "kind" in _link_columns(db_path)
        assert conn.execute(
            "SELECT kind FROM task_links WHERE child_id = 't_deploy'"
        ).fetchone()["kind"] == "blocks"
        assert kb.parent_links(conn, "t_deploy") == [("t_audit", "blocks")]

        kb.recompute_ready(conn)
        assert kb.get_task(conn, "t_deploy").status == "todo", (
            "a legacy edge must keep gating after migration"
        )
        assert kb.claim_task(conn, "t_deploy") is None


def test_null_kind_is_treated_as_blocks_by_the_scheduler(tmp_path, monkeypatch):
    """Defense in depth: a hand-edited NULL must not read as non-gating.

    The migration backfills 'blocks', but the scheduling queries COALESCE too —
    so a nullable column left behind by a manual repair still gates. Modelled
    by writing the legacy (nullable, un-backfilled) shape directly.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="nullkind")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    raw = sqlite3.connect(str(db_path))
    raw.executescript(kb.SCHEMA_SQL)
    raw.executescript(
        "DROP TABLE task_links;"
        "CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL,"
        " kind TEXT, PRIMARY KEY (parent_id, child_id));"
    )
    raw.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('t_parent', 'parent', 'running', 1000)"
    )
    raw.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('t_child', 'child', 'todo', 1000)"
    )
    # NULL kind: the column exists, so the additive migration won't touch it.
    raw.execute("INSERT INTO task_links VALUES ('t_parent', 't_child', NULL)")
    raw.commit()
    raw.close()

    with kb.connect(db_path) as conn:
        assert conn.execute(
            "SELECT kind FROM task_links WHERE child_id = 't_child'"
        ).fetchone()["kind"] is None, "migration must not rewrite an existing column"

        kb.recompute_ready(conn)
        assert kb.get_task(conn, "t_child").status == "todo"
        assert kb.claim_task(conn, "t_child") is None
        ok, _err = kb.promote_task(conn, "t_child", actor="test")
        assert ok is False
        assert kb.parent_links(conn, "t_child") == [("t_parent", "blocks")]


def _link_columns(db_path: Path) -> set[str]:
    raw = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in raw.execute("PRAGMA table_info(task_links)")}
    finally:
        raw.close()
