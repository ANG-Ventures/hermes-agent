"""Post-#951 follow-ups: worker-created / parented cards inherit the parent's
HOME session + origin line (never the worker run's per-run session id)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

HOME = "20260922_000000_home"
WORKER_RUN = "20260924_999999_workerrun"
ORIGIN = "origin: discord #sub-vps-n (123) \u00b7 session " + HOME + " \u00b7 2026-09-24"
KT = "HERMES_KANBAN_TASK"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (KT, "HERMES_SESSION_ID", "HERMES_PROFILE", "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


def _parent(conn, session_id=HOME, body=ORIGIN + "\n\nparent body"):
    return kb.create_task(conn, title="parent", assignee="apollo",
                          session_id=session_id, body=body)


def test_parented_card_inherits_home_and_origin(kanban_home):
    with kb.connect_closing() as conn:
        p = _parent(conn)
        c = kb.create_task(conn, title="child", assignee="w", parents=(p,),
                           session_id=WORKER_RUN, body="do the thing")
        child = kb.get_task(conn, c)
        assert child.session_id == HOME
        assert child.body.splitlines()[0] == ORIGIN
        assert child.body.endswith("do the thing")


def test_existing_origin_line_is_not_duplicated(kanban_home):
    with kb.connect_closing() as conn:
        p = _parent(conn)
        own = "origin: cli \u00b7 session x"
        c = kb.create_task(conn, title="child", assignee="w", parents=(p,),
                           body=own + "\n\nbody")
        assert kb.get_task(conn, c).body == own + "\n\nbody"


def test_worker_run_card_inherits_dispatched_cards_home(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        p = _parent(conn)
        monkeypatch.setenv(KT, p)
        c = kb.create_task(conn, title="followup", assignee="w",
                           session_id=WORKER_RUN)
        child = kb.get_task(conn, c)
        assert child.session_id == HOME
        assert child.body == ORIGIN


def test_worker_run_with_unstamped_lineage_stays_unstamped(kanban_home, monkeypatch):
    """Never stamp the worker run's own per-run session id."""
    with kb.connect_closing() as conn:
        p = _parent(conn, session_id=None, body="no origin")
        monkeypatch.setenv(KT, p)
        c = kb.create_task(conn, title="followup", assignee="w",
                           session_id=WORKER_RUN)
        assert kb.get_task(conn, c).session_id is None


def test_first_stamped_parent_wins(kanban_home):
    with kb.connect_closing() as conn:
        unstamped = _parent(conn, session_id=None, body="x")
        stamped = _parent(conn)
        c = kb.create_task(conn, title="c", assignee="w",
                           parents=(unstamped, stamped), session_id=WORKER_RUN)
        assert kb.get_task(conn, c).session_id == HOME


def test_unparented_non_worker_create_keeps_its_own_session(kanban_home):
    with kb.connect_closing() as conn:
        c = kb.create_task(conn, title="c", assignee="w", session_id=HOME)
        assert kb.get_task(conn, c).session_id == HOME


def test_decomposed_children_inherit_root_home_and_origin(kanban_home):
    with kb.connect_closing() as conn:
        root_id = kb.create_task(conn, title="root", assignee="apollo",
                                 session_id=HOME, body=ORIGIN + "\n\nroot",
                                 triage=True)
        kids = kb.decompose_triage_task(
            conn, root_id, root_assignee="apollo",
            children=[{"title": "a", "assignee": "w", "body": "A"},
                      {"title": "b", "assignee": "w"}],
        )
        assert kids and len(kids) == 2
        for k in kids:
            t = kb.get_task(conn, k)
            assert t.session_id == HOME
            assert t.body.splitlines()[0] == ORIGIN
