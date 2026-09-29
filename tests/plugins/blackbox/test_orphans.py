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


def _turn_row(path, turn_id, cols):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT {cols} FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
    finally:
        conn.close()


def _priced_call(turn_id, seq, *, inp, out):
    blackbox.record_api_call(
        turn_id=turn_id, seq=seq, ts=100.0 + seq, provider="claude-bpr",
        model="claude-opus-4-8", usage=CanonicalUsage(input_tokens=inp, output_tokens=out),
        api_mode="anthropic_messages", sub_key=None, attribution="wire",
        http_status=200, relay_synthetic=False, route_id=None,
    )


def test_repair_writes_flagged_row_from_ledger_aggregates(db, capsys):
    """A killed worker's orphan gets ONE flagged row carrying its ledger totals."""
    turn_id = "20260929_081700_ab12cd:t_a9b4f1ad:deadbeef"
    _priced_call(turn_id, 1, inp=1000, out=50)
    _priced_call(turn_id, 2, inp=2000, out=70)
    assert [t for t, _ts, _e in _orphans(db)] == [turn_id]

    assert orphans.main([str(db), "--since", "0", "--settle-s", "0", "--repair", "--dry-run"]) == 1
    assert "would repair 1 orphan turn(s)" in capsys.readouterr().out
    assert [t for t, _ts, _e in _orphans(db)] == [turn_id]
    assert _turn_row(db, turn_id, "turn_id") is None, "dry-run must write nothing"

    assert orphans.main([str(db), "--since", "0", "--settle-s", "0", "--repair"]) == 0
    assert "repaired 1/1 orphan turn(s)" in capsys.readouterr().out
    assert _orphans(db) == []
    row = _turn_row(
        db, turn_id,
        "interrupted, terminal_error, api_calls, input_tokens, output_tokens, "
        "ts_start, ts_end, provider, model, profile, cost_status, user_text",
    )
    assert row[:5] == (1, orphans.REPAIR_MARKER, 2, 3000, 120)
    assert (row[5], row[6]) == (101.0, 102.0)
    assert (row[7], row[8]) == ("claude-bpr", "claude-opus-4-8")
    assert row[9] == "default"  # tmp store is not under profiles/<name>/
    assert row[10] != "unknown", "a repaired row must be priced, not an unpriced-drift page"
    assert row[11] == ""
    # Idempotent and silent once clean.
    assert orphans.main([str(db), "--since", "0", "--settle-s", "0", "--repair"]) == 0
    assert capsys.readouterr().out == ""
    assert orphans.main([str(db), "--since", "0", "--settle-s", "0"]) == 0


def test_repair_never_touches_an_existing_row(db):
    """The flagged insert is DO NOTHING: a real row present at write time wins."""
    turn_id = "s:t:real"
    _priced_call(turn_id, 1, inp=10, out=5)
    assert [t for t, _ts, _e in _orphans(db)] == [turn_id]
    record, fence = orphans.repair_record(str(db), turn_id)
    assert record.terminal_error == orphans.REPAIR_MARKER and record.interrupted
    assert fence == (101.0, 1)
    # The real on_session_end lands between the scan and the write.
    blackbox._on_session_end(
        session_id="s", turn_id=turn_id, completed=True, failed=False,
        turn_exit_reason="text_response(stop)", model="claude-opus-4-8",
        provider="claude-bpr", final_response="done", platform="cli", chat_id="c1",
    )
    assert store.insert_turn(record, provisional=True, db_path=str(db), ledger_fence=fence) is False
    assert _turn_row(db, turn_id, "terminal_error, interrupted, final_text") == (None, 0, "done")


def test_repair_is_fenced_on_the_ledger_it_was_aggregated_from(db):
    """Prism P1 on #1504 (stale repair): a call that lands between the
    aggregate read and the insert must not be summarised away."""
    turn_id = "s:t:still-running"
    _priced_call(turn_id, 1, inp=10, out=5)
    record, fence = orphans.repair_record(str(db), turn_id)
    assert (record.api_calls, fence) == (1, (101.0, 1))
    _priced_call(turn_id, 2, inp=20, out=5)  # the worker was only parked on a tool
    assert store.insert_turn(record, provisional=True, db_path=str(db), ledger_fence=fence) is False
    assert _turn_row(db, turn_id, "turn_id") is None
    # The next run aggregates the grown ledger.
    record, fence = orphans.repair_record(str(db), turn_id)
    assert (record.api_calls, record.input_tokens, fence) == (2, 30, (102.0, 2))
    assert store.insert_turn(record, provisional=True, db_path=str(db), ledger_fence=fence) is True
    assert _orphans(db) == []


def test_failed_ledger_read_never_becomes_a_zero_usage_repair(db, monkeypatch):
    """Prism P1 on #1504: ledger_turn_usage() -> None on a DB error must not
    be read as 'no main-lane calls'."""
    turn_id = "s:t:locked"
    _priced_call(turn_id, 1, inp=10, out=5)
    monkeypatch.setattr(store, "ledger_turn_usage", lambda *_a, **_k: None)
    assert orphans.repair_record(str(db), turn_id) == (None, None)
    assert orphans.repair({str(db): _orphans(db)}) == {str(db): [(turn_id, False)]}
    assert _turn_row(db, turn_id, "turn_id") is None


def test_losing_provisional_insert_leaves_the_real_rows_tool_calls_alone(db):
    """Prism P1 on #1504: the DO NOTHING loser must not run insert_turn's
    side effects (turn_tool_calls wipe, route/served-subs re-stamp)."""
    from plugins.blackbox.record import TurnRecord

    turn_id = "s:t:with-tools"
    _priced_call(turn_id, 1, inp=10, out=5)
    real = TurnRecord(
        turn_id=turn_id, profile="default", provider="claude-bpr", model="claude-opus-4-8",
        platform="cli", chat_id="c1", api_calls=1, input_tokens=10, output_tokens=5,
        tools=["terminal", "read_file"],
        tool_calls=[{"name": "terminal", "args_preview": "ls", "result_preview": "ok"},
                    {"name": "read_file", "args_preview": "x", "result_preview": "y"}],
    )
    assert store.insert_turn(real) is True
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    before = conn.execute(
        "SELECT lane_family, vendor, served_provider, served_subs_json FROM turns WHERE turn_id=?",
        (turn_id,)).fetchone()
    conn.close()

    record, fence = orphans.repair_record(str(db), turn_id)
    assert store.insert_turn(record, provisional=True, db_path=str(db),
                             ledger_fence=fence) is False

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    assert conn.execute("SELECT name FROM turn_tool_calls WHERE turn_id=? ORDER BY seq",
                        (turn_id,)).fetchall() == [("terminal",), ("read_file",)]
    assert conn.execute(
        "SELECT lane_family, vendor, served_provider, served_subs_json FROM turns WHERE turn_id=?",
        (turn_id,)).fetchone() == before
    assert conn.execute("SELECT terminal_error, tools FROM turns WHERE turn_id=?",
                        (turn_id,)).fetchone() == (None, '["terminal", "read_file"]')
    conn.close()


def test_repair_marks_the_row_even_when_only_aux_calls_exist(db):
    turn_id = "s:t:auxonly"
    blackbox.record_api_call(
        turn_id=turn_id, seq=1, ts=100.0, provider="claude-bpr", model="claude-haiku-4-5",
        usage=CanonicalUsage(input_tokens=5, output_tokens=1), api_mode="anthropic_messages",
        sub_key=None, attribution="aux:title", http_status=200, relay_synthetic=False,
        route_id=None,
    )
    assert [t for t, _ts, _e in _orphans(db)] == [turn_id]
    written = orphans.repair({str(db): _orphans(db)})
    assert written == {str(db): [(turn_id, True)]}
    assert _orphans(db) == []
    assert _turn_row(db, turn_id, "terminal_error, model, api_calls") == (
        orphans.REPAIR_MARKER, "claude-haiku-4-5", 0,
    )


def test_repair_skips_a_store_whose_profile_drops_subagent_turns(tmp_path, db, capsys):
    """Prism P1 on #1504: with record_subagents=false, orphans may be deliberate
    subagent drops the ledger cannot tell apart; the store is reported, not repaired.
    The config is the TARGET store's profile config, not the invoking process's."""
    (tmp_path / "config.yaml").write_text("blackbox:\n  enabled: true\n  record_subagents: false\n")
    assert orphans.store_config(str(db))["record_subagents"] is False
    turn_id = "s:t:maybe-subagent"
    _priced_call(turn_id, 1, inp=10, out=5)
    assert orphans.repair({str(db): _orphans(db)}) == {str(db): [(turn_id, False)]}
    assert orphans.main([str(db), "--since", "0", "--settle-s", "0", "--repair"]) == 1
    assert "skipped 1 orphan turn(s)" in capsys.readouterr().out
    assert [t for t, _ts, _e in _orphans(db)] == [turn_id]
    # An unreadable config fails closed too (Prism P1: unknown != permission).
    (tmp_path / "config.yaml").write_text("blackbox: [unterminated\n")
    assert orphans.store_config(str(db)) is None
    assert orphans.repairable(str(db))[0] is False
    assert orphans.repair({str(db): _orphans(db)}) == {str(db): [(turn_id, False)]}
    (tmp_path / "config.yaml").write_text("blackbox:\n  enabled: true\n")
    assert orphans.repair({str(db): _orphans(db)}) == {str(db): [(turn_id, True)]}
    assert _orphans(db) == []


def test_store_config_applies_the_managed_overlay(tmp_path, db, monkeypatch):
    """The eligibility check sees what the live recorder sees: the
    administrator's managed-scope overlay, not only the user file."""
    from hermes_cli import managed_scope

    (tmp_path / "config.yaml").write_text("blackbox:\n  enabled: true\n")
    monkeypatch.setattr(managed_scope, "apply_managed_overlay",
                        lambda cfg: {**cfg, "blackbox": {**cfg.get("blackbox", {}), "record_subagents": False}})
    assert orphans.store_config(str(db))["record_subagents"] is False
    assert orphans.repairable(str(db))[0] is False


def test_profile_for_path():
    assert orphans.profile_for_path("/h/.hermes/profiles/daedalus/blackbox/turns.db") == "daedalus"
    assert orphans.profile_for_path("/h/.hermes/blackbox/turns.db") == "default"


def test_interrupted_session_end_prices_from_ledger_and_keeps_marker(db):
    """The signal path emits no turn_usage; the row is built from the ledger."""
    turn_id = "s:t:killed"
    _priced_call(turn_id, 1, inp=400, out=20)
    blackbox._on_session_end(
        session_id="s", turn_id=turn_id, completed=False, interrupted=True,
        model="claude-opus-4-8", provider="claude-bpr", platform="cli",
        reason="signal_15", terminal_error="signal_15",
    )
    assert _orphans(db) == []
    assert _turn_row(
        db, turn_id, "interrupted, terminal_error, api_calls, input_tokens, output_tokens",
    ) == (1, "signal_15", 1, 400, 20)
