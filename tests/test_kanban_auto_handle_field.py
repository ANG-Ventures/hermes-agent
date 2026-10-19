"""Boomerang v2 P3a data model: ``tasks.auto_handle`` nullable tri-state (D-15).

Covers the additive migration (idempotent on an existing board DB), the kernel create/edit
path, the ``kanban_create`` tool, the ``hermes kanban create/update --auto-handle`` CLI, and
the ``/autohandle`` gateway command persisting into session metadata.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _col(conn):
    return {r["name"]: r for r in conn.execute("PRAGMA table_info(tasks)")}


def test_fresh_board_has_nullable_auto_handle(kanban_home):
    with kbc.connect_closing() as conn:
        col = _col(conn)["auto_handle"]
        assert col["notnull"] == 0 and col["dflt_value"] is None
        tid = kb.create_task(conn, title="plain", assignee="peer")
        assert kb.get_task(conn, tid).auto_handle is None


def test_migration_adds_column_idempotently_on_existing_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="old card", assignee="peer")
    # Turn it into a pre-P3a board: drop the column, keep the row.
    db = kb.kanban_db_path()
    raw = sqlite3.connect(db)
    raw.execute("ALTER TABLE tasks DROP COLUMN auto_handle")
    raw.commit()
    assert "auto_handle" not in {r[1] for r in raw.execute("PRAGMA table_info(tasks)")}
    raw.close()

    for _ in range(2):   # second pass must be a no-op, not an error
        kb._INITIALIZED_PATHS.clear()
        kb.init_db()
        with kbc.connect_closing() as conn:
            assert "auto_handle" in _col(conn)
            task = kb.get_task(conn, tid)
            assert task.title == "old card" and task.auto_handle is None
            conn.execute("SELECT auto_handle FROM tasks").fetchall()


@pytest.mark.parametrize("value", [True, False, None])
def test_create_and_set_round_trip(kanban_home, value):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="c", assignee="peer", auto_handle=value)
        assert kb.get_task(conn, tid).auto_handle is value
        assert kb.set_task_auto_handle(conn, tid, not value if value is not None else True)
        assert kb.get_task(conn, tid).auto_handle is (not value if value is not None else True)
        assert kb.set_task_auto_handle(conn, tid, None)
        assert kb.get_task(conn, tid).auto_handle is None
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert kinds.count("auto_handle_set") == 2
        assert kb.set_task_auto_handle(conn, "t_nope", True) is False


def test_kanban_create_tool_accepts_auto_handle(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA

    assert KANBAN_CREATE_SCHEMA["parameters"]["properties"]["auto_handle"]["type"] == "boolean"
    on = json.loads(kt._handle_create({"title": "a", "assignee": "peer", "auto_handle": True,
                                       "unhomed": True}))
    unset = json.loads(kt._handle_create({"title": "b", "assignee": "peer", "unhomed": True}))
    off = json.loads(kt._handle_create({"title": "c", "assignee": "peer", "auto_handle": "false",
                                        "unhomed": True}))
    assert on["ok"] and unset["ok"] and off["ok"], (on, unset, off)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, on["task_id"]).auto_handle is True
        assert kb.get_task(conn, unset["task_id"]).auto_handle is None
        assert kb.get_task(conn, off["task_id"]).auto_handle is False
    shown = json.loads(kt._handle_show({"task_id": on["task_id"]}))
    assert shown["task"]["auto_handle"] is True


def test_cli_create_and_update_auto_handle(kanban_home, capsys):
    from hermes_cli import kanban as kcli
    from hermes_cli.kanban_parser import build_parser

    import argparse
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="cmd")
    build_parser(sub)

    def run(*argv):
        args = root.parse_args(["kanban", *argv])
        return kcli.kanban_command(args)

    assert run("create", "cli card", "--assignee", "peer", "--auto-handle", "true",
               "--home", "operator") == 0
    with kbc.connect_closing() as conn:
        tid = conn.execute("SELECT id FROM tasks WHERE title='cli card'").fetchone()["id"]
        assert kb.get_task(conn, tid).auto_handle is True
    assert run("update", tid, "--auto-handle", "false") == 0
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).auto_handle is False
    assert run("edit", tid, "--auto-handle", "inherit") == 0
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).auto_handle is None
    with pytest.raises(SystemExit):
        run("update", tid, "--auto-handle", "maybe")


def test_autohandle_slash_command_persists_in_session_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.config import GatewayConfig, Platform
    from gateway.kanban_auto_handle import SESSION_METADATA_KEY, session_setting
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource, SessionStore
    from gateway.platforms.event import MessageEvent
    from hermes_cli.commands import resolve_command

    assert resolve_command("autohandle").name == "autohandle"
    assert "autohandle" in GatewayRunner._PLAIN_COMMANDS

    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = None
    source = SessionSource(platform=Platform.DISCORD, chat_id="c1", chat_type="group", user_id="u1")
    entry = store.get_or_create_session(source)

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._session_key_for_source = lambda src: entry.session_key

    def send(text):
        ev = MessageEvent(text=text, source=source, message_id="m1")
        reply = asyncio.run(runner._handle_autohandle_command(ev))
        return str(getattr(reply, "text", reply))

    assert "off" in send("/autohandle status")
    send("/autohandle on")
    assert store.get_session_metadata(entry.session_key, SESSION_METADATA_KEY) is True
    assert "on" in send("/autohandle")
    # The wake paths read it per home session by the card's session_id, across a restart.
    reloaded = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    reloaded._db = None
    assert session_setting(reloaded, entry.session_id) is True
    send("/autohandle off")
    assert store.get_session_metadata(entry.session_key, SESSION_METADATA_KEY) is False
    assert "Usage" in send("/autohandle sideways")
    assert store.get_session_metadata(entry.session_key, SESSION_METADATA_KEY) is False
    assert session_setting(store, "no-such-session") is None
