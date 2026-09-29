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
    # The profile's own config: --repair reads the TARGET store's config and
    # fails closed without one.
    (tmp_path / "config.yaml").write_text("blackbox:\n  enabled: true\n")
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
    """Prism P1 on #1504: a DB error inside ledger_turn_usage() must not be
    read as 'no main-lane calls' -- neither by --repair nor by the signal path."""
    turn_id = "s:t:locked"
    _priced_call(turn_id, 1, inp=10, out=5)
    real = store.ledger_turn_usage

    def broken(*a, **k):
        if k.get("raise_on_error"):
            raise sqlite3.OperationalError("database is locked")
        return None

    monkeypatch.setattr(store, "ledger_turn_usage", broken)
    assert orphans.repair_record(str(db), turn_id) == (None, None)
    assert orphans.repair({str(db): _orphans(db)}) == {str(db): [(turn_id, False)]}
    assert _turn_row(db, turn_id, "turn_id") is None
    # The signal path must still write (the process is exiting) but with
    # every bucket UNKNOWN, never measured zeros.
    blackbox._on_session_end(
        session_id="s", turn_id=turn_id, completed=False, interrupted=True,
        model="claude-opus-4-8", provider="claude-bpr", platform="cli",
        terminal_error="signal_15",
    )
    assert _turn_row(
        db, turn_id, "terminal_error, usage_unknown, input_tokens_unknown, "
        "output_tokens_unknown, cache_read_tokens_unknown, cache_write_tokens_unknown, api_calls",
    ) == ("signal_15", 1, 1, 1, 1, 1, 0)
    monkeypatch.setattr(store, "ledger_turn_usage", real)


def test_repair_leaves_a_turn_whose_ledger_moved_since_the_scan(db):
    """Prism P1 on #1504 (stale eligibility): a call that landed after the scan
    settled on the turn means the turn is live again -- not repaired this run."""
    turn_id = "s:t:woke-up"
    _priced_call(turn_id, 1, inp=10, out=5)
    scanned = _orphans(db)
    assert scanned == [(turn_id, 101.0, 0)]
    _priced_call(turn_id, 2, inp=10, out=5)  # lands between scan() and repair()
    assert orphans.repair({str(db): scanned}) == {str(db): [(turn_id, False)]}
    assert _turn_row(db, turn_id, "turn_id") is None
    assert orphans.repair({str(db): _orphans(db)}) == {str(db): [(turn_id, True)]}
    assert _turn_row(db, turn_id, "api_calls, input_tokens") == (2, 20)


def test_repair_prices_a_composite_moa_orphan_from_its_physical_children(db):
    """Prism P1 on #1504: the virtual 'moa' parent is unpriceable; its child
    rows carry the real routes and travel as pricing_calls."""
    turn_id = "s:t:moa"
    blackbox.record_api_call(
        turn_id=turn_id, seq=0, ts=100.0, provider="moa", model="default",
        usage=CanonicalUsage(request_count=0), api_mode="anthropic_messages",
        sub_key=None, attribution="wire", http_status=200, relay_synthetic=False,
        route_id=None,
    )
    store.insert_composite_calls(turn_id, 0, sub_harness="moa", calls=[
        {"seq": 1, "ts": 100.5, "provider": "claude-bpr", "model": "claude-opus-4-8",
         "usage": CanonicalUsage(input_tokens=1000, output_tokens=100)},
        {"seq": 2, "ts": 100.6, "provider": "claude-bpr", "model": "claude-opus-4-8",
         "usage": CanonicalUsage(input_tokens=2000, output_tokens=100)},
    ])
    usage = store.ledger_turn_usage(turn_id)
    assert usage["api_calls"] == 1 and len(usage["calls"][0]["pricing_calls"]) == 2
    assert orphans.repair({str(db): _orphans(db)}) == {str(db): [(turn_id, True)]}
    row = _turn_row(db, turn_id, "provider, api_calls, input_tokens, output_tokens, cost_status, cost_usd")
    assert row[:4] == ("moa", 1, 3000, 200)
    assert row[4] not in ("unknown", "partial") and row[5] and row[5] > 0


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
    # An unreadable, missing or empty config fails closed too (unknown != permission).
    for bad in ("blackbox: [unterminated\n", "", "- not a mapping\n", None):
        if bad is None:
            (tmp_path / "config.yaml").unlink()
        else:
            (tmp_path / "config.yaml").write_text(bad)
        assert orphans.store_config(str(db)) is None, repr(bad)
        assert orphans.repairable(str(db))[0] is False, repr(bad)
        assert orphans.repair({str(db): _orphans(db)}) == {str(db): [(turn_id, False)]}, repr(bad)
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


def test_finalizer_carries_the_signal_stamp_into_the_real_row(db):
    """The CLI signal handler stamps (turn_id, 'signal_15') before interrupting;
    when the loop's own finalizer then writes the row (it wins over the signal
    path), the marker travels with it. A stamp for another turn is ignored."""
    for turn_id, stamp, want in (
        ("sess:task:sig", ("sess:task:sig", "signal_15"), "signal_15"),
        ("sess:task:other", ("sess:task:sig", "signal_15"), None),
    ):
        _priced_call(turn_id, 1, inp=10, out=5)
        agent = SimpleNamespace(
            _current_turn_id=turn_id, _current_task_id="task", session_id="sess",
            model="claude-opus-4-8", provider="claude-bpr", platform="cli",
            _blackbox_turn_calls=(turn_id, []), _turn_original_user_message=(turn_id, "go"),
            _turn_terminal_error=stamp,
        )
        with patch("hermes_cli.lifecycle.invoke_hook", _route_session_end):
            assert emit_unfinalized_session_end(agent, turn_id, exc=KeyboardInterrupt())
        assert _turn_row(db, turn_id, "interrupted, terminal_error, api_calls") == (1, want, 1)


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
