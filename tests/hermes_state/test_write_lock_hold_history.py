"""Write-lock hold history, callback CPU vs wall, and waiter-side attribution (t_7fe48f0b).

2026-11-09: the per-pid ledger kept only each process's LATEST hold, so the holder of a 06:05-06:07
stall had been overwritten before anyone read it, and a 172 s hold of a one-row SELECT+UPDATE could
not say whether the thread was running. These tests pin:

- every recorded hold is appended to ``<ledger>/history.jsonl`` (op, pid, who, since, held_s,
  commit_s, callback_s, callback_cpu_s) and a past window is rebuilt by ``ledger_history``;
- the history is bounded (one rotation, one backup);
- ``callback_cpu_s`` is thread CPU: a sleeping callback reads wall >> cpu, a spinning one cpu ~ wall;
- a writer that waits WAITER_ATTRIBUTION_S logs the holder on its own side, for the in-process
  writer lock and for the SQLite lock, once per write;
- all of it fails open: a broken history or attribution never fails the write.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import hermes_state_lockowners as lockowners
import hermes_state as sessiondb_mod
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def _record_everything(monkeypatch):
    monkeypatch.setattr(lockowners, "_last_timeout", {})
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 0.0)
    monkeypatch.setattr(lockowners, "state_db_write_lock_holders", lambda _p: [])  # ledger-only, any OS


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    d.close()


def _history(db_path):
    path = Path(lockowners._ledger_dir(db_path)) / lockowners._HISTORY_NAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _sleeping_write(db, seconds):
    def fixture_sleep_hold():  # the ledger names the method that opened the transaction
        db._execute_write(lambda conn: time.sleep(seconds))
    fixture_sleep_hold()


def test_every_recorded_hold_lands_in_history_and_a_window_is_rebuilt(db):
    t_start = time.time()
    _sleeping_write(db, 0.3)
    _sleeping_write(db, 0.1)  # overwrites <pid>.json; the history keeps both
    t_end = time.time()
    rows = [r for r in _history(db.db_path) if r["op"] == "fixture_sleep_hold"]
    assert len(rows) == 2, rows
    first = rows[0]
    for key in ("op", "pid", "who", "since", "held_s", "commit_s", "callback_s", "callback_cpu_s", "released_at"):
        assert key in first, (key, first)
    assert first["pid"] == os.getpid() and first["state"] == "released"
    assert first["callback_s"] >= 0.2 and first["held_s"] >= first["callback_s"], first
    latest = json.loads((Path(lockowners._ledger_dir(db.db_path)) / f"{os.getpid()}.json").read_text())
    assert latest["seq"] == rows[1]["seq"], "per-pid file holds only the latest hold"
    assert latest["seq"] != rows[0]["seq"], "per-pid file must not retain the first hold"
    window = [r for r in lockowners.ledger_history(db.db_path, t_start, t_end) if r["op"] == "fixture_sleep_hold"]
    assert [r["seq"] for r in window] == [r["seq"] for r in rows]
    assert lockowners.ledger_history(db.db_path, t_end + 60, t_end + 120) == []
    print("history line:", json.dumps(first, sort_keys=True))


def test_callback_cpu_separates_a_parked_thread_from_a_running_one(db):
    _sleeping_write(db, 0.4)  # holds the lock without running: wall >> cpu

    def fixture_spin_hold():
        def _spin(conn):
            end = time.thread_time() + 0.3
            while time.thread_time() < end:
                pass
        db._execute_write(_spin)
    fixture_spin_hold()
    rows = {r["op"]: r for r in _history(db.db_path)}
    parked, running = rows["fixture_sleep_hold"], rows["fixture_spin_hold"]
    assert parked["callback_s"] >= 0.3 and parked["callback_cpu_s"] < 0.1, parked
    assert running["callback_cpu_s"] >= 0.25, running


def test_rolled_back_hold_counts_the_whole_hold_as_callback(db):
    def fixture_raising_hold():
        def _boom(conn):
            time.sleep(0.2)
            raise ValueError("fixture")
        db._execute_write(_boom)
    with pytest.raises(ValueError):
        fixture_raising_hold()
    row = [r for r in _history(db.db_path) if r["op"] == "fixture_raising_hold"][-1]
    assert "commit_s" not in row and row["callback_s"] == row["held_s"] and row["callback_cpu_s"] is not None, row


@pytest.mark.parametrize("portable", [False, True])
def test_history_is_bounded_to_one_rotation(db, monkeypatch, portable):
    monkeypatch.setattr(lockowners, "_DIR_FD_OK", not portable)
    monkeypatch.setattr(lockowners, "_HISTORY_MAX_BYTES", 600)
    for _ in range(12):
        _sleeping_write(db, 0.0)
    ledger = Path(lockowners._ledger_dir(db.db_path))
    current, backup = ledger / "history.jsonl", ledger / "history.jsonl.1"
    assert backup.exists() and current.stat().st_size <= 600 and backup.stat().st_size <= 600
    assert sorted(p.name for p in ledger.iterdir() if p.name.startswith("history.jsonl")) == [
        "history.jsonl", "history.jsonl.1"]
    assert lockowners.ledger_history(db.db_path, 0, time.time() + 1), "both generations are readable"


def test_broken_history_never_fails_the_write(db, monkeypatch, tmp_path):
    ledger = Path(lockowners._ledger_dir(db.db_path))
    _sleeping_write(db, 0.0)  # creates the ledger dir
    (ledger / "blocked-history").mkdir()
    monkeypatch.setattr(lockowners, "_HISTORY_NAME", "blocked-history")  # open() -> EISDIR
    db.set_meta("k", "v")
    assert db.get_meta("k") == "v"


def _fake_hold(ledger, seq, op):
    return {"ledger": str(ledger), "pid": 4242, "seq": seq, "who": "PID 4242 (peer)", "op": op, "since": time.time()}


def test_concurrent_rotation_never_clobbers_the_archived_generation(tmp_path, monkeypatch):
    """Prism 8e1b5c4107a7: writers A and B both find history.jsonl full. Without a cross-process lock,
    B rotates the full file to .1 and appends, and then A rotates B's new file over .1, which destroys the
    archived generation. flock belongs to the open file description, so two threads with separate opens
    conflict exactly like two processes."""
    ledger = tmp_path / "state.db.lockholders"
    ledger.mkdir(mode=0o700)
    # Cap sized so the pre-filled file is over it but A's and B's records fit together after ONE
    # rotation: any second rotation is then the race, not the cap.
    monkeypatch.setattr(lockowners, "_HISTORY_MAX_BYTES", 600)
    full = b"".join(b'{"since": 1.0, "op": "archived", "n": %02d}\n' % i for i in range(16))
    assert len(full) > 600
    (ledger / "history.jsonl").write_bytes(full)

    real_replace = os.replace
    a_rotating = threading.Event()
    a_ident = []

    def _slow_first_rotation(*args, **kwargs):  # A has stat'ed the full file; park it before it rotates
        if threading.get_ident() in a_ident and not a_rotating.is_set():
            a_rotating.set()
            time.sleep(0.4)
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(lockowners.os, "replace", _slow_first_rotation)

    def _writer_a():
        a_ident.append(threading.get_ident())
        lockowners._append_history(_fake_hold(ledger, 1, "writer_a"), {"held_s": 3.0, "state": "released"})

    a = threading.Thread(target=_writer_a)
    a.start()
    assert a_rotating.wait(5.0)
    lockowners._append_history(_fake_hold(ledger, 2, "writer_b"), {"held_s": 3.0, "state": "released"})
    a.join(timeout=10.0)

    assert (ledger / "history.jsonl.1").read_bytes() == full, "the archived generation was overwritten"
    current = (ledger / "history.jsonl").read_text(encoding="utf-8")
    assert '"writer_a"' in current and '"writer_b"' not in current, current


def test_busy_history_lock_drops_without_rotating(tmp_path, monkeypatch):
    """A busy history lock drops the diagnostic without rotating or growing the file."""
    import fcntl

    ledger = tmp_path / "state.db.lockholders"
    ledger.mkdir(mode=0o700)
    monkeypatch.setattr(lockowners, "_HISTORY_MAX_BYTES", 100)
    full = b"x" * 150 + b"\n"
    (ledger / "history.jsonl").write_bytes(full)
    peer = os.open(str(ledger / "history.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(peer, fcntl.LOCK_EX)
        real_flock = fcntl.flock
        attempts = []

        def nonblocking_flock(fd, operation):
            attempts.append(operation)
            assert operation == fcntl.LOCK_EX | fcntl.LOCK_NB, "history lock acquisition must not block"
            return real_flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", nonblocking_flock)
        lockowners._append_history(_fake_hold(ledger, 1, "contended"), {"held_s": 3.0, "state": "released"})
        assert attempts == [fcntl.LOCK_EX | fcntl.LOCK_NB], "busy history lock gets one nonblocking attempt"
    finally:
        os.close(peer)
    assert not (ledger / "history.jsonl.1").exists()
    data = (ledger / "history.jsonl").read_bytes()
    assert data == full


def test_unusable_history_lock_drops_and_never_rotates(tmp_path, monkeypatch):
    """An unusable history lock drops diagnostics; it must not permit unbounded appends."""
    ledger = tmp_path / "state.db.lockholders"
    ledger.mkdir(mode=0o700)
    (ledger / "history.lock").mkdir()
    monkeypatch.setattr(lockowners, "_HISTORY_MAX_BYTES", 100)
    full = b"y" * 150 + b"\n"
    (ledger / "history.jsonl").write_bytes(full)
    lockowners._append_history(_fake_hold(ledger, 1, "release"), {"held_s": 3.0, "state": "released"})
    data = (ledger / "history.jsonl").read_bytes()
    assert data == full
    assert not (ledger / "history.jsonl.1").exists()


def _waiter_lines(caplog):
    return [r.getMessage() for r in caplog.records if "state.db write waiting" in r.getMessage()]


def test_writer_lock_waiter_names_the_in_process_holder(db, monkeypatch, caplog):
    """The gateway shape: two threads of one process share one SessionDB writer; the second queues on
    the in-process lock, never sees SQLITE_BUSY, and used to log nothing."""
    monkeypatch.setattr(sessiondb_mod, "WAITER_ATTRIBUTION_S", 0.9)
    started = threading.Event()

    def fixture_long_holder():
        def _hold(conn):
            started.set()
            time.sleep(2.0)
        db._execute_write(_hold)

    holder = threading.Thread(target=fixture_long_holder)
    holder.start()
    try:
        assert started.wait(5.0)
        with caplog.at_level(logging.WARNING, logger=lockowners.logger.name):
            db.set_meta("k", "after the holder")
    finally:
        holder.join(timeout=10.0)
    assert db.get_meta("k") == "after the holder"
    lines = _waiter_lines(caplog)
    assert len(lines) == 1, caplog.text
    assert "writer lock" in lines[0] and "fixture_long_holder" in lines[0] and "still holding" in lines[0], lines
    print("waiter line:", lines[0])


def test_sqlite_lock_waiter_logs_once_and_the_write_lands(db, monkeypatch, caplog):
    monkeypatch.setattr(sessiondb_mod, "WAITER_ATTRIBUTION_S", 0.4)
    db._conn.execute("PRAGMA busy_timeout=100")
    started = threading.Event()

    def _foreign_holder():
        conn = sqlite3.connect(str(db.db_path), timeout=1.0, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            started.set()
            time.sleep(1.5)
            conn.execute("COMMIT")
        finally:
            conn.close()

    holder = threading.Thread(target=_foreign_holder)
    holder.start()
    try:
        assert started.wait(5.0)
        with caplog.at_level(logging.WARNING, logger=lockowners.logger.name):
            db.set_meta("k2", "v2")
    finally:
        holder.join(timeout=10.0)
    assert db.get_meta("k2") == "v2"
    lines = _waiter_lines(caplog)
    assert len(lines) == 1 and "sqlite lock" in lines[0], caplog.text


def test_writer_wait_timer_keeps_context_protocol_and_cancels(db, monkeypatch):
    lock = db._lock
    events = []

    class ObservedLock:
        def locked(self):
            return True  # force the contended telemetry path without a wall-clock wait

        def __enter__(self):
            events.append("enter")
            return lock.__enter__()

        def __exit__(self, *args):
            events.append("exit")
            return lock.__exit__(*args)

    class Timer:
        def __init__(self, interval, callback):
            assert interval == sessiondb_mod.WAITER_ATTRIBUTION_S

        def start(self):
            events.append("start")

        def cancel(self):
            events.append("cancel")

    monkeypatch.setattr(db, "_lock", ObservedLock())
    monkeypatch.setattr(sessiondb_mod.threading, "Timer", Timer)
    db._execute_write(lambda conn: events.append("write"))
    assert events == ["start", "enter", "cancel", "write", "exit"]


def test_writer_wait_timer_start_failure_does_not_fail_write(db, monkeypatch):
    lock = db._lock

    class ObservedLock:
        def locked(self):
            return True

        def __enter__(self):
            return lock.__enter__()

        def __exit__(self, *args):
            return lock.__exit__(*args)

    def broken_timer(*args, **kwargs):
        raise RuntimeError("cannot start diagnostic thread")

    monkeypatch.setattr(db, "_lock", ObservedLock())
    monkeypatch.setattr(sessiondb_mod.threading, "Timer", broken_timer)
    db.set_meta("timer-failure", "write survived")
    assert db.get_meta("timer-failure") == "write survived"


@pytest.mark.parametrize("portable", [False, True])
def test_history_contention_is_one_try_no_sleep_and_bounded(tmp_path, monkeypatch, portable):
    ledger = tmp_path / "ledger"
    ledger.mkdir(mode=0o700)
    monkeypatch.setattr(lockowners, "_DIR_FD_OK", not portable)
    monkeypatch.setattr(lockowners, "_HISTORY_MAX_BYTES", 600)
    calls = []
    monkeypatch.setattr(lockowners, "_try_lock_fd", lambda fd: calls.append(fd) or False)
    def no_sleep(*args):
        pytest.fail("history telemetry slept under the writer mutex")
    monkeypatch.setattr(lockowners.time, "sleep", no_sleep)
    for i in range(20):
        lockowners._append_history(_fake_hold(ledger, i, "busy"), {"held_s": 1})
    assert len(calls) == 20
    assert not (ledger / "history.jsonl").exists()


@pytest.mark.parametrize("portable", [False, True])
def test_unusable_history_lock_drops_instead_of_unbounded_append(tmp_path, monkeypatch, portable):
    ledger = tmp_path / "ledger"
    ledger.mkdir(mode=0o700)
    (ledger / "history.lock").mkdir()
    monkeypatch.setattr(lockowners, "_DIR_FD_OK", not portable)
    monkeypatch.setattr(lockowners, "_HISTORY_MAX_BYTES", 600)
    for i in range(20):
        lockowners._append_history(_fake_hold(ledger, i, "unusable"), {"held_s": 1})
    assert not (ledger / "history.jsonl").exists()


def test_hold_end_clock_failure_does_not_fail_write(db, monkeypatch):
    real = lockowners.time.monotonic
    def callback(conn):
        monkeypatch.setattr(lockowners.time, "monotonic", lambda: (_ for _ in ()).throw(RuntimeError("clock")))
    try:
        db._execute_write(callback)
    finally:
        monkeypatch.setattr(lockowners.time, "monotonic", real)


def test_history_skips_non_object_json(tmp_path):
    ledger = Path(lockowners._ledger_dir(tmp_path / "db"))
    ledger.mkdir(mode=0o700)
    (ledger / "history.jsonl").write_text('[]\nnull\n1\n{"since": 1, "held_s": 2}\n')
    assert lockowners.ledger_history(tmp_path / "db", 0, 10) == [{"since": 1, "held_s": 2}]


@pytest.mark.parametrize("phase", ["start", "commit", "waiter"])
@pytest.mark.parametrize("callback_raises", [False, True])
def test_clock_and_logging_failures_never_change_write_result(db, monkeypatch, phase, callback_raises):
    from types import SimpleNamespace

    class RaisingHandler(logging.Handler):
        def emit(self, record):
            raise RuntimeError("logging handler failed")

    writer_tid = threading.get_ident()

    def writer_clock(real):
        def read():
            if threading.get_ident() == writer_tid:
                raise RuntimeError("diagnostic clock failed")
            return real()
        return read

    clock = SimpleNamespace(**{name: writer_clock(getattr(time, name))
                               for name in ("monotonic", "time", "thread_time")})
    original = ValueError("original callback error")
    handler = RaisingHandler()
    monkeypatch.setattr(lockowners.logger, "level", logging.DEBUG)
    lockowners.logger.addHandler(handler)
    try:
        if phase == "start":
            monkeypatch.setattr(lockowners, "time", clock)

        def callback(conn):
            conn.execute("INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
                         ("fault-injection", "committed"))
            if phase in ("commit", "waiter"):
                monkeypatch.setattr(lockowners, "time", clock)
            if phase == "waiter":
                assert lockowners.log_write_lock_waiter(db.db_path, 12.0, lock="sqlite") == ""
            if callback_raises:
                raise original
            return "callback result"

        if callback_raises:
            with pytest.raises(ValueError) as caught:
                db._execute_write(callback)
            assert caught.value is original
        else:
            assert db._execute_write(callback) == "callback result"
        assert db.get_meta("fault-injection") == (None if callback_raises else "committed")
    finally:
        lockowners.logger.removeHandler(handler)


def test_history_skips_invalid_utf8_in_both_generations(tmp_path):
    db_path = tmp_path / "db"
    ledger = Path(lockowners._ledger_dir(db_path))
    ledger.mkdir(mode=0o700)
    for name, since in (("history.jsonl.1", 1), ("history.jsonl", 4)):
        (ledger / name).write_bytes(
            b'\xff\n' + json.dumps({"since": since, "held_s": 2}).encode() + b'\n{"since": "\xff"}\n'
        )
    assert lockowners.ledger_history(db_path, 0, 10) == [
        {"since": 1, "held_s": 2}, {"since": 4, "held_s": 2}]


def test_waiter_attribution_fails_open(db, monkeypatch):
    def _broken(*_a, **_k):
        raise RuntimeError("ledger unreadable")
    monkeypatch.setattr(lockowners, "describe_write_lock_holders", _broken)
    assert lockowners.log_write_lock_waiter(db.db_path, 12.0, lock="writer") == ""
