"""``reopen`` must walk back the children it promoted.

``reopen_task`` voids a false terminal completion and returns the card to the
queue. Children linked ``blocks`` to that parent were promoted to ``ready`` on
the premise that the parent was ``done`` — a premise the reopen explicitly
withdraws.

``recompute_ready`` ONLY ever promotes (todo/blocked -> ready); there is no
demotion path anywhere else. So without this, a reopened parent leaves its
children dispatchable, and the very next dispatcher tick spawns a worker on
work whose parent has been taken back. That also violates the invariant
``promote_task`` enforces on the way in: a ``ready`` task has all blocking
parents ``done``/``archived``.

A child that is already claimed or past ``ready`` is REPORTED rather than
demoted — yanking a card out from under a live worker would orphan its run.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        yield kb, conn
    finally:
        conn.close()


def _falsely_completed_parent(kb, conn):
    tid = kb.create_task(
        conn, title="parent", assignee="w", workspace_kind="scratch"
    )
    kb.claim_task(conn, tid, claimer="host:1")
    kb.complete_task(conn, tid, summary="BOGUS completion by a non-owner")
    assert kb.get_task(conn, tid).status == "done"
    return tid


def test_reopen_demotes_a_child_promoted_on_the_withdrawn_premise(board):
    kb, conn = board
    parent = _falsely_completed_parent(kb, conn)
    child = kb.create_task(
        conn, title="downstream", assignee="w2", workspace_kind="scratch"
    )
    kb.link_tasks(conn, parent, child)
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "ready"

    ok, err = kb.reopen_task(
        conn, parent, actor="operator", reason="false completion"
    )
    assert (ok, err) == (True, None)

    assert kb.get_task(conn, parent).status == "ready"
    assert kb.get_task(conn, child).status == "todo", (
        "child left dispatchable behind a parent that is no longer done — the "
        "dispatcher will spawn a worker on a withdrawn premise"
    )

    # And it must STAY demoted: recompute_ready only promotes, and the parent
    # is no longer terminal, so the child must not bounce straight back.
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "todo"


def test_reopen_does_not_yank_a_child_that_is_already_running(board):
    kb, conn = board
    parent = _falsely_completed_parent(kb, conn)
    child = kb.create_task(
        conn, title="downstream", assignee="w2", workspace_kind="scratch"
    )
    kb.link_tasks(conn, parent, child)
    kb.recompute_ready(conn)
    kb.claim_task(conn, child, claimer="host:99")
    assert kb.get_task(conn, child).status == "running"

    ok, _ = kb.reopen_task(conn, parent, actor="operator", reason="wrong writer")
    assert ok

    assert kb.get_task(conn, child).status == "running", (
        "a claimed child was demoted out from under its live worker"
    )
    fanout = [
        e for e in kb.list_events(conn, parent) if e.kind == "reopen_child_fanout"
    ]
    assert fanout, "an un-demoted live child must still be surfaced"
    assert fanout[-1].payload["not_demoted"] == [child]
    assert fanout[-1].payload["demoted"] == []


def test_reopen_records_which_children_it_demoted(board):
    kb, conn = board
    parent = _falsely_completed_parent(kb, conn)
    child = kb.create_task(
        conn, title="downstream", assignee="w2", workspace_kind="scratch"
    )
    kb.link_tasks(conn, parent, child)
    kb.recompute_ready(conn)

    kb.reopen_task(conn, parent, actor="operator", reason="false completion")

    fanout = [
        e for e in kb.list_events(conn, parent) if e.kind == "reopen_child_fanout"
    ]
    assert fanout and fanout[-1].payload["demoted"] == [child]

    demoted_events = [
        e for e in kb.list_events(conn, child) if e.kind == "demoted"
    ]
    assert demoted_events, "the demotion must be auditable on the child too"
    assert parent in demoted_events[-1].payload["reason"]


def test_reopen_with_no_children_is_unchanged(board):
    """No links ⇒ no fanout event, and the core reopen contract still holds."""
    kb, conn = board
    parent = _falsely_completed_parent(kb, conn)

    ok, err = kb.reopen_task(conn, parent, actor="operator", reason="no kids")
    assert (ok, err) == (True, None)

    after = kb.get_task(conn, parent)
    assert after.status == "ready"
    assert after.result is None
    assert not [
        e for e in kb.list_events(conn, parent) if e.kind == "reopen_child_fanout"
    ]
