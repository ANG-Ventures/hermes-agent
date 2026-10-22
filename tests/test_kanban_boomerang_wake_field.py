"""Boomerang wake level: ``tasks.boomerang_wake`` is the ONLY setting (Ace 2026-10-22, t_7be18632).

Covers the additive migration from the pre-rename ``auto_handle`` column (copy + one-shot
delivery-mode upgrade), the kernel create/edit path, the ``kanban_create`` tool, the
``hermes kanban create/edit --boomerang-wake`` CLI and ``kanban show``'s level line, the
emergency stop file behind ``enabled()``, and the removal of ``/autohandle``.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_KEY"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _cols(conn) -> dict:
    return {r["name"]: r for r in conn.execute("PRAGMA table_info(tasks)")}


def _sub(conn, tid, chat, mode):
    kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=chat, chat_type="group",
                       delivery_mode=mode, also=True)


def _modes(conn, tid) -> list:
    return sorted(s["delivery_mode"] for s in kbn.list_notify_subs(conn, tid))


def test_fresh_board_has_nullable_boomerang_wake(kanban_home):
    with kbc.connect_closing() as conn:
        col = _cols(conn)["boomerang_wake"]
        assert col["notnull"] == 0 and col["dflt_value"] is None
        assert "auto_handle" not in _cols(conn)
        tid = kb.create_task(conn, title="plain", assignee="peer")
        assert kb.get_task(conn, tid).boomerang_wake is None


def test_migration_copies_legacy_column_and_upgrades_only_the_on_card(kanban_home, caplog):
    """AC-5: auto_handle true/false/null -> boomerang_wake 1/0/null; only the true card's
    wake subscription moves to the boomerang level, once."""
    with kbc.connect_closing() as conn:
        on = kb.create_task(conn, title="on", assignee="peer")
        off = kb.create_task(conn, title="off", assignee="peer")
        unset = kb.create_task(conn, title="unset", assignee="peer")
        for tid in (on, off, unset):
            _sub(conn, tid, "c1", "notify+wake")
        _sub(conn, on, "c2", "notify")
    # Rewind to a pre-rename board: the legacy column carries the values, the new one is gone.
    raw = sqlite3.connect(kb.kanban_db_path())
    raw.execute("ALTER TABLE tasks DROP COLUMN boomerang_wake")
    raw.execute("ALTER TABLE tasks ADD COLUMN auto_handle INTEGER")
    raw.executemany("UPDATE tasks SET auto_handle = ? WHERE id = ?", [(1, on), (0, off), (None, unset)])
    raw.execute("DROP INDEX IF EXISTS idx_notify_one_waker")
    raw.execute("CREATE UNIQUE INDEX idx_notify_one_waker ON kanban_notify_subs(task_id) "
                "WHERE delivery_mode IN ('notify+wake', 'wake') AND platform != 'api_server'")
    raw.commit()
    raw.close()

    with caplog.at_level(logging.INFO):
        for _ in range(2):   # the second open must be a no-op
            kb._INITIALIZED_PATHS.clear()
            kb.init_db()
    with kbc.connect_closing() as conn:
        got = {r["id"]: r["boomerang_wake"] for r in conn.execute("SELECT id, boomerang_wake FROM tasks")}
        assert got == {on: 1, off: 0, unset: None}
        assert _modes(conn, on) == ["notify", "notify+boomerang"]
        assert _modes(conn, off) == ["notify+wake"] and _modes(conn, unset) == ["notify+wake"]
        assert kb.get_task(conn, on).boomerang_wake is True
        # the one-waker index now covers the boomerang modes too
        idx = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (kbn.ONE_WAKER_INDEX,)).fetchone()[0]
        assert "boomerang" in idx
    lines = [r.getMessage() for r in caplog.records if "boomerang_wake copied" in r.getMessage()]
    assert lines == ["kanban migration: boomerang_wake copied from auto_handle on 2 card(s); "
                     "1 subscription(s) upgraded to the boomerang level"], lines


def test_set_level_moves_the_waker_mode_both_ways(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="c", assignee="peer")
        _sub(conn, tid, "c1", "notify+wake")
        assert kb.set_task_boomerang_wake(conn, tid, True)
        assert kb.get_task(conn, tid).boomerang_wake is True and _modes(conn, tid) == ["notify+boomerang"]
        assert kbn.card_level(True, kbn.list_notify_subs(conn, tid)) == "boomerang wake"
        assert kb.set_task_boomerang_wake(conn, tid, False)
        assert _modes(conn, tid) == ["notify+wake"]
        assert kbn.card_level(False, kbn.list_notify_subs(conn, tid)) == "wake"
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert kinds.count("boomerang_wake_set") == 2
        assert kb.set_task_boomerang_wake(conn, "t_nope", True) is False


def test_subscribing_a_boomerang_mode_sets_the_card_level(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="c", assignee="peer")
        _sub(conn, tid, "c1", "boomerang")
        assert kb.get_task(conn, tid).boomerang_wake is True and _modes(conn, tid) == ["boomerang"]


def test_kanban_create_tool_accepts_boomerang_wake(kanban_home):
    from tools import kanban_tools as kt
    from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA

    props = KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    assert props["boomerang_wake"]["type"] == "boolean" and "auto_handle" not in props
    on = json.loads(kt._handle_create({"title": "a", "assignee": "peer", "boomerang_wake": True,
                                       "unhomed": True}))
    unset = json.loads(kt._handle_create({"title": "b", "assignee": "peer", "unhomed": True}))
    assert on["ok"] and unset["ok"], (on, unset)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, on["task_id"]).boomerang_wake is True
        assert kb.get_task(conn, unset["task_id"]).boomerang_wake is None
    shown = json.loads(kt._handle_show({"task_id": on["task_id"]}))
    assert shown["task"]["boomerang_wake"] is True


def _cli():
    from hermes_cli import kanban as kcli
    from hermes_cli.kanban_parser import build_parser

    root = argparse.ArgumentParser()
    build_parser(root.add_subparsers(dest="cmd"))

    def run(*argv):
        return kcli.kanban_command(root.parse_args(["kanban", *argv]))
    return run


def test_cli_create_with_boomerang_wake_subscribes_and_shows_level(kanban_home, monkeypatch, capsys):
    """AC-2: --boomerang-wake -> boomerang_wake=1, a notify+boomerang subscription, and
    ``kanban show`` prints the level."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "c1")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "group")
    run = _cli()
    assert run("create", "cli card", "--assignee", "peer", "--boomerang-wake", "--home", "operator") == 0
    with kbc.connect_closing() as conn:
        tid = conn.execute("SELECT id FROM tasks WHERE title='cli card'").fetchone()["id"]
        assert kb.get_task(conn, tid).boomerang_wake is True
        assert _modes(conn, tid) == ["notify+boomerang"]
    capsys.readouterr()
    assert run("show", tid) == 0
    assert "level:     boomerang wake" in capsys.readouterr().out
    assert run("edit", tid, "--no-boomerang-wake") == 0
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).boomerang_wake is False and _modes(conn, tid) == ["notify+wake"]
    with pytest.raises(SystemExit):
        run("edit", tid, "--auto-handle", "true")


def test_notify_subscribe_boomerang_wake_shorthand(kanban_home):
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="c", assignee="peer")
    assert run("notify-subscribe", tid, "--platform", "discord", "--chat-id", "c9",
               "--boomerang-wake") == 0
    with kbc.connect_closing() as conn:
        assert _modes(conn, tid) == ["notify+boomerang"] and kb.get_task(conn, tid).boomerang_wake is True


def test_enabled_is_the_stop_file_with_one_logs_line_per_transition(kanban_home, monkeypatch):
    """AC-3: no config key -> on; the stop file -> off; one #logs line per transition."""
    from gateway import kanban_boomerang_wake as bw

    posted: list = []
    monkeypatch.setattr(bw, "post_logs", posted.append)
    monkeypatch.setattr(bw, "_LAST_ENABLED", {})
    # A leftover config key from yesterday must not matter either way.
    (kanban_home / "config.yaml").write_text(
        "delegation:\n  boomerang:\n    auto_handle_enabled: false\n")
    assert bw.enabled() is True and bw.enabled() is True and posted == []
    stop = kanban_home / "state" / "boomerang-wake.disabled"
    assert bw.stop_file_path() == stop
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("")
    assert bw.enabled() is False and bw.enabled() is False
    assert len(posted) == 1 and "DISABLED" in posted[0] and str(stop) in posted[0]
    stop.unlink()
    assert bw.enabled() is True
    assert len(posted) == 2 and "re-enabled" in posted[1]


def test_autohandle_command_is_gone(kanban_home):
    """AC-4: not in the registry, not a gateway command, not in the desktop dump."""
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.slash_commands import GatewaySlashCommandsMixin
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command

    assert resolve_command("autohandle") is None
    assert "autohandle" not in GATEWAY_KNOWN_COMMANDS
    assert "autohandle" not in GatewayBusySessionMixin._PLAIN_COMMANDS
    assert not hasattr(GatewaySlashCommandsMixin, "_handle_autohandle_command")
    dump = Path(__file__).resolve().parents[1] / "apps/desktop/src/lib/desktop-slash-registry.json"
    assert "/autohandle" not in json.loads(dump.read_text())
