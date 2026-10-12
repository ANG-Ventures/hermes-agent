"""``kanban complete --actor <closer>`` stamps ``task_events.actor_profile`` (t_7de359d9).

A cron closer has no chat session, so its events carried actor_profile=NULL and the
notify-suppress filter guessed the closer from the completion summary's prose.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

HOME_SID = "20261008_184838_dbe0955f"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_PROFILE", "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(var, raising=False)
    # a cron closer: no chat-session identity
    monkeypatch.setattr(kc, "_caller_session_id", lambda: None)
    kb.init_db()
    return home


def _parse(argv):
    sub = argparse.ArgumentParser().add_subparsers(dest="_top")
    return kc.build_parser(sub).parse_args(argv)


def _completed_event(tid):
    with kb.connect_closing() as conn:
        rows = [e for e in kb.list_events(conn, tid) if e.kind == "completed"]
    assert len(rows) == 1, rows
    return rows[0]


def _card(conn):
    return kb.create_task(conn, title="card", assignee="worker-a", session_id=HOME_SID)


def test_complete_with_actor_stamps_actor_profile(kanban_home, capsys):
    with kb.connect_closing() as conn:
        tid = _card(conn)
    rc = kc.kanban_command(_parse(["complete", tid, "--summary", "landed by kanban-review-merge-pass (CI gate, x)",
                                   "--actor", "kanban-review-merge-pass"]))
    assert rc == 0, capsys.readouterr()
    ev = _completed_event(tid)
    assert ev.actor_profile == "kanban-review-merge-pass"
    assert ev.actor_session_id is None


def test_complete_without_actor_is_unstamped(kanban_home, capsys):
    """Control: the pre-fix shape (no --actor, no session) still writes NULL."""
    with kb.connect_closing() as conn:
        tid = _card(conn)
    assert kc.kanban_command(_parse(["complete", tid, "--summary", "x"])) == 0, capsys.readouterr()
    assert _completed_event(tid).actor_profile is None


def test_actor_refuses_operator_labels(kanban_home, capsys):
    with kb.connect_closing() as conn:
        tid = _card(conn)
    rc = kc.kanban_command(_parse(["complete", tid, "--summary", "x", "--actor", "apollo"]))
    assert rc != 0
    assert "operator label" in capsys.readouterr().err
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status != "done"


def test_bound_session_actor_wins_over_flag(kanban_home):
    """An already-bound chat actor is the identity; --actor never overrides it."""
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=(HOME_SID,), profile="default"):
            with kb.event_actor("kanban-review-merge-pass"):
                kb.complete_task(conn, tid, summary="x")
    ev = _completed_event(tid)
    assert (ev.actor_profile, ev.actor_session_id) == ("default", HOME_SID)
