"""complete_task refusals name the real reason (t_ef1ba08b).

A worker retrying a timed-out complete used to read "unknown id or terminal
state" for both an unknown id and a card that was already closed.
"""
from __future__ import annotations

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


def test_unknown_id(kanban_home):
    with kb.connect() as conn:
        assert kb.explain_complete_refusal(conn, "t_doesnotexist") == "unknown id"


def test_already_done_names_who_and_when(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="daedalus")
        claimed = kb.claim_task(conn, t)
        assert claimed is not None
        assert kb.complete_task(conn, t, summary="done", expected_run_id=claimed.current_run_id)
        assert not kb.complete_task(conn, t, summary="again")
        msg = kb.explain_complete_refusal(conn, t)
    assert msg.startswith("already done by daedalus at 20"), msg
    assert "outcome completed" in msg
    assert "unknown id" not in msg


def test_stale_run_is_named(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="daedalus")
        run_id = kb.claim_task(conn, t).current_run_id
        stale = run_id + 1000
        assert not kb.complete_task(conn, t, summary="s", expected_run_id=stale)
        msg = kb.explain_complete_refusal(conn, t, expected_run_id=stale)
    assert f"run {stale} is no longer the current run (current: {run_id})" in msg
