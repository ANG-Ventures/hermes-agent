"""state.db write-lock stall attribution and transcript patience (t_c54736c9).

2026-10-18 04:17 PT: three surfaces failed ``database is locked (held >60 s)`` and every deadline scan
said "no write-class lock held at the deadline (the holder released just before it)". These tests pin:

- a holder that RELEASES before the waiter's deadline is still named, by pid, argv, op and duration,
  from the holder-side ledger (no /proc/locks involved, so the same path works on macOS);
- a transcript append rides out a lock held longer than the old 60 s budget (budget 180 s);
- when the budget does run out, the turn's stop message names the holder.
"""

import json
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import hermes_state_lockowners as lockowners
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def _forget_timeouts(monkeypatch):
    # The last deadline's holder is process memory the stop message reads; never leak it across tests.
    monkeypatch.setattr(lockowners, "_last_timeout", {})


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    d.close()


def _ledger(db_path):
    return Path(lockowners._ledger_dir(db_path))


def _hold_in_child(db_path, hold_s, op="fixture_hold"):
    """A second PROCESS that takes the write lock through the real SessionDB path and holds it."""
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
        from pathlib import Path
        from hermes_state import SessionDB
        db = SessionDB(db_path=Path({str(db_path)!r}))
        def _hold(conn):
            print("held", flush=True)
            time.sleep({hold_s})
        def {op}():  # the ledger names the method that opened the transaction
            db._execute_write(_hold)
        {op}()
        db.close()
        print("released", flush=True)
    """)
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "HERMES_HOME": str(Path(db_path).parent)})


def test_short_holds_write_nothing(db):
    db.create_session("s1", "cli")
    db.append_message(session_id="s1", role="user", content="fast")
    assert not _ledger(db.db_path).exists()


def test_holder_that_released_before_the_deadline_is_named(db, monkeypatch, caplog):
    """The 04:17 shape: the holder lets go just before the waiter gives up. /proc/locks sees nothing;
    the ledger still names it."""
    monkeypatch.setattr(lockowners, "state_db_write_lock_holders", lambda _p: [])  # macOS / released
    db.create_session("s1", "cli")
    child = _hold_in_child(db.db_path, 2.6)
    try:
        assert child.stdout.readline().strip() == "held"
        db._conn.execute("PRAGMA busy_timeout=100")
        monkeypatch.setattr(SessionDB, "_WRITE_PATIENCE_S", 1.0)
        with caplog.at_level(logging.WARNING, logger="hermes_state_lockowners"):
            with pytest.raises(sqlite3.OperationalError, match="another Hermes process"):
                db.set_meta("k", "v")  # gives up while the child still holds
            assert child.stdout.readline().strip() == "released"
            child.wait(timeout=10)
            detail = lockowners.log_write_lock_holders(db.db_path, 30.0)  # deadline AFTER the release
    finally:
        child.kill()
        child.wait()
    rec = json.loads((_ledger(db.db_path) / f"{child.pid}.json").read_text())
    assert rec["state"] == "released" and rec["op"] == "fixture_hold" and rec["held_s"] >= 2.0, rec
    assert f"PID {child.pid} " in detail and "fixture_hold" in detail and "released" in detail, detail
    assert "held the write lock for 3s" in detail or "held the write lock for 2s" in detail, detail
    assert any(f"PID {child.pid} " in r.getMessage() for r in caplog.records), caplog.text


def test_live_long_hold_is_marked_holding_while_it_is_open(db, monkeypatch):
    monkeypatch.setattr(lockowners, "state_db_write_lock_holders", lambda _p: [])
    child = _hold_in_child(db.db_path, 4.0)
    try:
        assert child.stdout.readline().strip() == "held"
        deadline = time.monotonic() + 5.0
        lines = []
        while time.monotonic() < deadline:
            lines = lockowners.recent_ledger_holders(db.db_path, 60.0)
            if lines:
                break
            time.sleep(0.1)
        assert any(f"PID {child.pid} " in line and "still holding" in line for line in lines), lines
    finally:
        child.kill()
        child.wait()
    # A SIGKILLed holder never wrote "released": a dead pid's stale "holding" record is not reported.
    assert lockowners.recent_ledger_holders(db.db_path, 60.0) == []


def test_vacuum_is_recorded_as_a_vacuum_hold(db, monkeypatch):
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 0.0)
    db.create_session("s1", "cli")
    db.vacuum()
    rec = json.loads((_ledger(db.db_path) / f"{os.getpid()}.json").read_text())
    assert rec["op"] == "vacuum" and rec["state"] == "released", rec


def test_ledger_names_the_public_method_that_held_the_lock(db, monkeypatch):
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 0.0)
    db.create_session("s1", "cli")
    db.prune_sessions(older_than_days=90)
    rec = json.loads((_ledger(db.db_path) / f"{os.getpid()}.json").read_text())
    assert rec["op"] == "prune_sessions", rec


def test_transcript_patience_is_180s_and_loud(db):
    assert db._TRANSCRIPT_WRITE_PATIENCE_S == 180.0
    assert db._LOCK_WAIT_WARN_S < db._TRANSCRIPT_WRITE_PATIENCE_S


def _hold_write_lock(db_path, hold_s, started):
    conn = sqlite3.connect(str(db_path), timeout=1.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        started.set()
        time.sleep(hold_s)
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_append_rides_out_a_hold_longer_than_the_old_budget_and_warns(db, monkeypatch, caplog):
    """Held-lock fixture at 1/30 scale: budget 6 s (=180 s), warn 2 s (=60 s), hold 3.5 s (>105 s, past
    the old 60 s budget at the same scale). The append must land, and warn once with the holder."""
    monkeypatch.setattr(SessionDB, "_TRANSCRIPT_WRITE_PATIENCE_S", 6.0)
    monkeypatch.setattr(SessionDB, "_LOCK_WAIT_WARN_S", 2.0)
    db._conn.execute("PRAGMA busy_timeout=100")
    db.create_session("s1", "cli")
    started = threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(db.db_path, 3.5, started))
    holder.start()
    try:
        assert started.wait(5.0)
        with caplog.at_level(logging.WARNING, logger="hermes_state"):
            msg_id = db.append_message(session_id="s1", role="user", content="survived a long hold")
    finally:
        holder.join(timeout=10.0)
    assert isinstance(msg_id, int)
    assert any(m["content"] == "survived a long hold" for m in db.get_messages("s1"))
    warns = [r.getMessage() for r in caplog.records if "still busy after" in r.getMessage()]
    assert len(warns) == 1, caplog.text


def test_locked_stop_message_names_the_holder(db, monkeypatch):
    from run_agent import AIAgent

    monkeypatch.setattr(lockowners, "state_db_write_lock_holders",
                        lambda _p: ["PID 4242 (hermes gateway run) holds WAL write lock on state.db-shm"])
    lockowners.log_write_lock_holders(db.db_path, 180.0)
    out = AIAgent._format_turn_completion_explanation("session_persistence_failed", "locked")
    assert "busy" in out.lower() and "PID 4242 (hermes gateway run)" in out, out
