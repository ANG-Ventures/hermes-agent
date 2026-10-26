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
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, encoding="utf-8",
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
    rec = json.loads((_ledger(db.db_path) / f"{child.pid}.json").read_text(encoding="utf-8-sig"))
    assert rec["state"] == "released" and rec["op"] == "fixture_hold" and rec["held_s"] >= 2.0, rec
    assert f"PID {child.pid} " in detail and "fixture_hold" in detail and "released" in detail, detail
    assert "held the write lock for 3s" in detail or "held the write lock for 2s" in detail, detail
    assert any(f"PID {child.pid} " in r.getMessage() for r in caplog.records), caplog.text


def test_live_long_hold_is_marked_holding_while_it_is_open(db, monkeypatch):
    monkeypatch.setattr(lockowners, "state_db_write_lock_holders", lambda _p: [])
    child = _hold_in_child(db.db_path, 30.0)  # far past the poll: the SIGKILL below always lands mid-hold
    try:
        assert child.stdout.readline().strip() == "held"
        deadline = time.monotonic() + 15.0
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
    rec = json.loads((_ledger(db.db_path) / f"{os.getpid()}.json").read_text(encoding="utf-8-sig"))
    assert rec["op"] == "vacuum" and rec["state"] == "released", rec


def test_ledger_names_the_public_method_that_held_the_lock(db, monkeypatch):
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 0.0)
    db.create_session("s1", "cli")
    db.prune_sessions(older_than_days=90)
    rec = json.loads((_ledger(db.db_path) / f"{os.getpid()}.json").read_text(encoding="utf-8-sig"))
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
    out = AIAgent._format_turn_completion_explanation("session_persistence_failed", "locked", db_path=db.db_path)
    assert "busy" in out.lower() and "PID 4242 (hermes gateway run)" in out, out


def test_stop_message_names_only_the_failing_stores_holder(tmp_path, monkeypatch):
    """Two profiles' stores in one process: B's later timeout must not reach A's chat."""
    from run_agent import AIAgent

    a, b = tmp_path / "a" / "state.db", tmp_path / "b" / "state.db"
    monkeypatch.setattr(lockowners, "state_db_write_lock_holders", lambda _p: ["PID 1111 (holder-of-A)"])
    lockowners.log_write_lock_holders(a, 180.0)
    monkeypatch.setattr(lockowners, "state_db_write_lock_holders", lambda _p: ["PID 2222 (holder-of-B)"])
    lockowners.log_write_lock_holders(b, 180.0)
    out_a = AIAgent._format_turn_completion_explanation("session_persistence_failed", "locked", db_path=a)
    assert "holder-of-A" in out_a and "holder-of-B" not in out_a, out_a
    out_c = AIAgent._format_turn_completion_explanation(
        "session_persistence_failed", "locked", db_path=tmp_path / "c" / "state.db")
    assert "holder-of" not in out_c, out_c


class _InlineThread:
    """The tui_gateway test shape: ``threading.Thread`` patched to run its target inside start()."""

    def __init__(self, *, target, **_kwargs):
        self._target = target

    def start(self):
        self._target()


def test_writes_survive_an_inline_thread_patch(tmp_path, monkeypatch):
    """#2072 off-box reds: 4 tui_gateway tests patch threading.Thread; the watchdog then ran inside
    hold_started() under _holds_lock and deadlocked the first write."""
    import threading as _threading

    db = SessionDB(db_path=tmp_path / "state.db")  # not the fixture: a deadlocked db cannot close()
    monkeypatch.setattr(_threading, "Thread", _InlineThread)
    monkeypatch.setattr(lockowners, "_watchdog_running", False)
    done = []
    runner = lockowners._Thread(target=lambda: (db.create_session("s1", "cli"), done.append(1)), daemon=True)
    runner.start()
    runner.join(10.0)
    if done != [1]:
        # Unwedge the stuck writer so teardown/atexit can close the db: empty the registry, then
        # release the lock hold_started() is sitting on so the inline watchdog sees no holds and returns.
        lockowners._active_holds.clear()
        try:
            lockowners._holds_lock.release()
        except RuntimeError:
            pass
        runner.join(10.0)
        pytest.fail("write deadlocked under an inline threading.Thread patch")
    db.close()


def test_released_record_is_never_republished_as_holding(db, monkeypatch):
    """Prism 147f02dd5e23 barrier: the watchdog selects a due hold, the hold completes and publishes
    "released", then the watchdog resumes publication. The record must stay "released"."""
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 0.0)
    monkeypatch.setattr(lockowners, "_watchdog_running", True)  # no real watchdog: we play it by hand
    monkeypatch.setattr(lockowners, "_watchdog_pid", os.getpid())
    token = lockowners.hold_started(db.db_path, "raced_op")
    due = lockowners._select_due_holds()  # watchdog: selected, about to publish
    assert [h["op"] for h in due] == ["raced_op"]
    lockowners.hold_ended(token)  # completion wins the race
    for hold in due:
        lockowners._publish_holding(hold)  # watchdog resumes
    rec = json.loads((_ledger(db.db_path) / f"{os.getpid()}.json").read_text(encoding="utf-8-sig"))
    assert rec["state"] == "released" and rec["op"] == "raced_op", rec


def test_ledger_failure_never_blocks_the_write(db, monkeypatch):
    """Prism 840e2f7857ad: thread exhaustion at load must not leave BEGIN IMMEDIATE open."""
    def _no_threads(*_a, **_k):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(lockowners, "_Thread", _no_threads)
    monkeypatch.setattr(lockowners, "_watchdog_running", False)
    db.create_session("s1", "cli")
    db.append_message(session_id="s1", role="user", content="still written")
    assert not db._conn.in_transaction
    assert [m["content"] for m in db.get_messages("s1")] == ["still written"]


def test_pid_probe_never_signals_on_windows(monkeypatch):
    """Prism 765905ac86f6: os.kill(pid, 0) is CTRL_C_EVENT on Windows; the probe must not signal."""
    import hermes_cli._subprocess_compat as compat

    def _no_signal(*_a):
        raise AssertionError("os.kill called")

    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    monkeypatch.setattr(compat, "win32_pid_exists", lambda pid: pid == 4242)
    monkeypatch.setattr(os, "kill", _no_signal)
    assert lockowners._pid_alive(4242) is True
    assert lockowners._pid_alive(4243) is False


@pytest.mark.platforms("posix")
def test_fork_child_does_not_inherit_a_held_registry_lock(tmp_path):
    """Prism c0c7a990a4d6: fork while another thread holds _holds_lock; the child's write must not hang."""
    db_path = tmp_path / "state.db"
    SessionDB(db_path=db_path).close()
    with lockowners._holds_lock:  # the parent's watchdog, mid-tick
        pid = os.fork()  # windows-footgun: ok — platforms("posix") test
        if pid == 0:  # child
            import signal

            signal.alarm(20)
            try:
                child = SessionDB(db_path=db_path)
                child.create_session("forked", "cli")
                child.close()
                os._exit(0)
            except BaseException:
                os._exit(3)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, status


@pytest.mark.platforms("posix")
def test_symlinked_ledger_is_skipped_and_its_target_never_pruned(db, tmp_path, monkeypatch):
    """Prism "unsafe cleanup": a planted ``<db>.lockholders`` symlink must not steer the write or the
    24 h prune into its target. The ledger is skipped; the old file in the target survives."""
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 0.0)
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.chmod(target, 0o700)  # passes the owner/mode check: only the no-follow open can refuse it
    victim = target / "123.json"  # a name the prune would otherwise match
    victim.write_text("{}", encoding="utf-8")
    old = time.time() - 3 * 86400
    os.utime(victim, (old, old))
    _ledger(db.db_path).symlink_to(target, target_is_directory=True)
    db.create_session("s1", "cli")
    assert victim.exists()
    assert sorted(p.name for p in target.iterdir()) == ["123.json"]  # nothing written through the link
    assert lockowners.recent_ledger_holders(db.db_path, 60.0) == []


@pytest.mark.platforms("posix")
def test_group_writable_ledger_dir_is_skipped(db, monkeypatch):
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 0.0)
    ledger = _ledger(db.db_path)
    ledger.mkdir(mode=0o700)
    os.chmod(ledger, 0o777)
    db.create_session("s1", "cli")
    assert list(ledger.iterdir()) == []


def test_prune_removes_only_old_ledger_records(db, monkeypatch):
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 0.0)
    ledger = _ledger(db.db_path)
    ledger.mkdir(mode=0o700)
    old = time.time() - 3 * 86400
    for name in ("99999.json", "notes.txt"):
        (ledger / name).write_text("{}", encoding="utf-8")
        os.utime(ledger / name, (old, old))
    db.create_session("s1", "cli")
    names = sorted(p.name for p in ledger.iterdir())
    assert "99999.json" not in names and "notes.txt" in names and f"{os.getpid()}.json" in names, names


def test_older_hold_end_never_overwrites_a_newer_holding_record(db, monkeypatch):
    """Prism "reordered publication": writer A starts and commits, writer B starts and is published as
    holding, then A's descheduled hold_ended() runs. The record must still be B's."""
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 0.0)
    monkeypatch.setattr(lockowners, "_watchdog_running", True)  # no real watchdog: we play it by hand
    monkeypatch.setattr(lockowners, "_watchdog_pid", os.getpid())
    token_a = lockowners.hold_started(db.db_path, "writer_a")
    token_b = lockowners.hold_started(db.db_path, "writer_b")
    due = lockowners._select_due_holds()
    for hold in due:
        if hold["op"] == "writer_b":
            lockowners._publish_holding(hold)
    lockowners.hold_ended(token_a)  # A's end lands late
    rec = json.loads((_ledger(db.db_path) / f"{os.getpid()}.json").read_text(encoding="utf-8-sig"))
    assert rec["op"] == "writer_b" and rec["state"] == "holding", rec
    lockowners.hold_ended(token_b)
    rec = json.loads((_ledger(db.db_path) / f"{os.getpid()}.json").read_text(encoding="utf-8-sig"))
    assert rec["op"] == "writer_b" and rec["state"] == "released", rec


class _SlowCommitConn:
    """The writer connection with a slow ``commit()``: the 2026-10-26 00:25-00:29 shape, where a
    one-row ``_write_model_config_patch`` was recorded as a 212 s hold (t_d9caaea3). Everything but
    commit is the real connection."""

    def __init__(self, conn, commit_s):
        self._real, self._commit_s = conn, commit_s

    def commit(self):
        time.sleep(self._commit_s)
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_long_hold_names_the_commit_phase_and_the_patch_body_is_short(db, monkeypatch):
    """t_d9caaea3: a long hold must say whether the time went to the caller's statements or to
    SQLite's commit. The model-config patch body (SELECT + UPDATE of one row) holds the lock for well
    under a second; the slow step here is commit(), and both the live and the released ledger lines
    attribute the time to it."""
    monkeypatch.setattr(lockowners, "state_db_write_lock_holders", lambda _p: [])
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 0.2)
    db.create_session("s1", "cli")
    real = db._conn
    db._conn = _SlowCommitConn(real, 2.0)
    try:
        writer = threading.Thread(target=db.patch_session_model_config, args=("s1", {"k": "v"}))
        writer.start()
        live = []
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not live:
            live = [line for line in lockowners.recent_ledger_holders(db.db_path, 60.0)
                    if "still holding" in line and "in commit" in line]
            time.sleep(0.05)
        writer.join(timeout=15)
    finally:
        db._conn = real
    assert live and "_write_model_config_patch" in live[0], live
    rec = json.loads((_ledger(db.db_path) / f"{os.getpid()}.json").read_text(encoding="utf-8-sig"))
    assert rec["state"] == "released" and rec["op"] == "_write_model_config_patch", rec
    assert rec["commit_s"] >= 1.9, rec
    assert rec["held_s"] - rec["commit_s"] < 1.0, rec  # the patch body itself: well under 1 s
    detail = lockowners.describe_write_lock_holders(db.db_path, 60.0)
    assert "of it in commit" in detail and "_write_model_config_patch" in detail, detail
    assert db.get_session_model_config_value("s1", "k") == "v"


def test_hold_marked_before_commit_republishes_the_commit_phase(db, monkeypatch):
    """A hold the watchdog already published as "holding" (slow callback) is republished when commit
    starts, so the live line does not keep saying nothing about the phase."""
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 0.0)
    monkeypatch.setattr(lockowners, "_watchdog_running", True)  # no real watchdog: we play it by hand
    monkeypatch.setattr(lockowners, "_watchdog_pid", os.getpid())
    token = lockowners.hold_started(db.db_path, "slow_body")
    for hold in lockowners._select_due_holds():
        lockowners._publish_holding(hold)
    path = _ledger(db.db_path) / f"{os.getpid()}.json"
    assert "commit_since" not in json.loads(path.read_text(encoding="utf-8-sig"))
    lockowners.hold_committing(token)
    rec = json.loads(path.read_text(encoding="utf-8-sig"))
    assert rec["state"] == "holding" and "commit_since" in rec, rec
    lockowners.hold_ended(token)
    assert "commit_s" in json.loads(path.read_text(encoding="utf-8-sig"))


def test_records_without_a_commit_phase_still_render(db, monkeypatch):
    """Ledger files written by an older process carry no phase: the line is the old one."""
    ledger = _ledger(db.db_path)
    ledger.mkdir(mode=0o700, exist_ok=True)
    now = time.time()
    (ledger / "424242.json").write_text(json.dumps({
        "pid": 424242, "seq": 1, "who": "PID 424242 (old)", "op": "append_messages_batch",
        "since": now - 10, "state": "released", "held_s": 9.0, "released_at": now - 1,
    }), encoding="utf-8")
    lines = lockowners.recent_ledger_holders(db.db_path, 60.0)
    assert lines == ["PID 424242 (old) held the write lock for 9s (append_messages_batch), released 1s ago"], lines
