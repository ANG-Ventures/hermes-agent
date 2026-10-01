"""`kanban priority` / `edit --priority`: event-logged priority writes (t_4f7918ce)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    kb.init_db()
    return home


def _run(*argv):
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(parser.add_subparsers(dest="command"))
    return kc.kanban_command(parser.parse_args(["kanban", *argv]))


def _prio_events(conn, tid):
    return [e.payload for e in kb.list_events(conn, tid) if e.kind == "priority_set"]


def test_priority_verb_writes_column_and_event(kanban_home, capsys):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee=None)
    assert _run("priority", tid, "200") == 0
    assert f"{tid}: priority 0 -> 200" in capsys.readouterr().out
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).priority == 200
        events = _prio_events(conn, tid)
    assert len(events) == 1
    assert events[0]["old"] == 0 and events[0]["new"] == 200
    assert events[0].get("actor")
    # Same value again: no second event.
    assert _run("priority", tid, "200") == 0
    with kb.connect_closing() as conn:
        assert len(_prio_events(conn, tid)) == 1
    assert "priority:  200" in kc.run_slash(f"show {tid}")
    assert json.loads(kc.run_slash(f"show {tid} --json"))["task"]["priority"] == 200


def test_edit_priority_and_unknown_id(kanban_home, capsys):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee=None, priority=5)
    assert _run("edit", tid, "--priority", "-3") == 0
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).priority == -3
        assert _prio_events(conn, tid)[-1] == {
            **_prio_events(conn, tid)[-1], "old": 5, "new": -3,
        }
    assert _run("priority", "t_deadbeef", "1") == 1
    assert "unknown id" in capsys.readouterr().err


def test_priority_respects_home_session_guard(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="human")
        assert kb.set_task_session(conn, tid, "home-session-A")
    with kb.mutation_actor(session_ids=("other-session-B",), profile="x", surface="cli"):
        with kb.connect_closing() as conn:
            with pytest.raises(kb.ForeignSessionMutationError):
                kb.set_task_priority(conn, tid, 9)
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).priority == 0
        assert _prio_events(conn, tid) == []
    with kb.mutation_actor(
        session_ids=("other-session-B",), profile="x", surface="cli",
        foreign_ok="push chain past backlog",
    ):
        with kb.connect_closing() as conn:
            ok, old = kb.set_task_priority(conn, tid, 9)
    assert ok and old == 0
    with kb.connect_closing() as conn:
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert kb.get_task(conn, tid).priority == 9
    assert "priority_set" in kinds
    assert any("takeover" in k or "foreign" in k for k in kinds), kinds


def test_dry_run_dispatch_orders_by_priority_and_shows_prio(kanban_home, capsys):
    with kb.connect_closing() as conn:
        older = kb.create_task(conn, title="older", assignee="default")
        newer = kb.create_task(conn, title="newer", assignee="default")
    assert _run("priority", newer, "200") == 0
    capsys.readouterr()
    _run("dispatch", "--dry-run", "--max", "1")
    out = capsys.readouterr().out
    spawned = [ln for ln in out.splitlines() if ln.strip().startswith("- t_")]
    assert len(spawned) == 1, out
    assert newer in spawned[0] and "prio=200" in spawned[0]
    assert older not in out.split("Spawned:")[1]
