"""Detector + backstop: every turn_api_calls turn_id gets a parent turns row."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.turn_finalizer import emit_unfinalized_session_end
from agent.usage_pricing import CanonicalUsage
from plugins import blackbox
from plugins.blackbox import orphans, store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {
        "enabled": True, "alerts_enabled": False, "record_subagents": True,
        "retention_days": 3650, "store_text": True,
    })
    store._connect().close()
    return store._db_path()


def _orphans(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return orphans.orphan_turns(conn, since=0.0, settle_before=1e12)
    finally:
        conn.close()


def _call(turn_id, seq, status):
    blackbox.record_api_call(
        turn_id=turn_id, seq=seq, ts=100.0 + seq, provider="claude-apr",
        model="claude-opus-4-8", usage=CanonicalUsage(request_count=0),
        api_mode="anthropic_messages", sub_key=None, attribution="wire",
        http_status=status, relay_synthetic=False, route_id=None,
    )


def _route_session_end(name, **kwargs):
    if name == "on_session_end":
        blackbox._on_session_end(**kwargs)
    return []


def test_failed_turn_gets_parent_row_with_terminal_marker(db):
    turn_id = "sess:task:0badf00d"
    _call(turn_id, 1, 503)
    _call(turn_id, 2, 503)
    assert [t for t, _ts, _e in _orphans(db)] == [turn_id]

    agent = SimpleNamespace(
        _current_turn_id=turn_id, _current_task_id="task", session_id="sess",
        model="claude-opus-4-8", provider="claude-apr", platform="kanban",
        _blackbox_turn_calls=(turn_id, []),
        _turn_original_user_message=(turn_id, "do the thing"),
    )
    with patch("hermes_cli.lifecycle.invoke_hook", _route_session_end):
        assert emit_unfinalized_session_end(
            agent, turn_id,
            result={"failed": True, "completed": False, "error": "HTTP 503: no capacity"},
        )

    assert _orphans(db) == []
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT final_text, terminal_error, interrupted, user_text FROM turns WHERE turn_id=?",
        (turn_id,),
    ).fetchone()
    conn.close()
    assert row == ("", "early_return:HTTP 503: no capacity", 0, "do the thing")


def test_normal_turn_has_null_terminal_error(db):
    blackbox._on_session_end(
        session_id="s", turn_id="s:t:1", completed=True, failed=False,
        turn_exit_reason="text_response(stop)", model="m", provider="p",
        final_response="done",
    )
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT terminal_error FROM turns WHERE turn_id='s:t:1'").fetchone() == (None,)
    conn.close()


def test_detector_window_and_cli_gate(db, capsys):
    _call("s:t:old", 1, 200)  # ts 101: orphan
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert orphans.orphan_turns(conn, since=200.0, settle_before=1e12) == []
        assert orphans.orphan_turns(conn, since=0.0, settle_before=101.0) == []  # still settling
        assert len(orphans.orphan_turns(conn, since=0.0, settle_before=1e12)) == 1
    finally:
        conn.close()
    assert orphans.main([str(db), "--since", "0", "--settle-s", "0"]) == 1
    assert "1 turn(s) have turn_api_calls rows but no turns row" in capsys.readouterr().out
    assert orphans.main([str(db), "--since", "0", "--settle-s", "0", "--max", "1"]) == 0
    assert capsys.readouterr().out == ""
