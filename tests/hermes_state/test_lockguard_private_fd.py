"""Private-descriptor WAL lock guard (t_aeae3fe7).

``hold()`` used to find SQLite's own descriptors with ``listdir('/dev/fd')`` + one ``fstat`` per fd
under ``SessionDB._lock``; under GIL load that held the write lock 16-28 s and delayed Discord
replies by minutes. The guard now OFD-locks descriptors it opens itself. OFD locks conflict by inode
range, so the foreign-EXCLUSIVE refusal (the reason the guard exists) must be unchanged, including
after a stray in-process open()/close() (howtocorrupt §2.2). Linux-only: OFD locks + ``/proc``.
"""

import contextlib
import importlib
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hermes_state
import hermes_state_lockguard as lg
import hermes_state_lockowners as lockowners
from tests.hermes_state._wal_generation_harness import make_db, pin_wal, require_wal

pytestmark = pytest.mark.platforms("linux")

_SHARED = (0x40000002, 510)
_DMS = (128, 1)


def _foreign_exclusive(path: str, start: int, length: int) -> str:
    """A CHILD process tries F_OFD_SETLK F_WRLCK on (start, length): 'ACQUIRED' or 'REFUSED'."""
    code = (
        "import fcntl, os, struct\n"
        f"fd = os.open({path!r}, os.O_RDWR)\n"
        f"lk = struct.pack('@hhqqi', fcntl.F_WRLCK, 0, {start}, {length}, 0)\n"
        "try:\n    fcntl.fcntl(fd, 37, lk); print('ACQUIRED')\n"
        "except BlockingIOError:\n    print('REFUSED')\n"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()


def _sqlite_close(*paths) -> None:
    """Stand-in for SQLite's own close after release(): closing its descriptor drops this process's
    POSIX locks on the inode, including the one release() swapped in for the OFD lock."""
    for path in paths:
        os.close(os.open(path, os.O_RDONLY))


def test_private_hold_refuses_foreign_exclusive_until_released(tmp_path):
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    held = lg.hold(db)
    closing = []
    try:
        assert set(held) == {(os.stat(db).st_dev, os.stat(db).st_ino), (os.stat(shm).st_dev, os.stat(shm).st_ino)}
        assert _foreign_exclusive(str(db), *_SHARED) == "REFUSED"
        assert _foreign_exclusive(str(shm), *_DMS) == "REFUSED"
        # howtocorrupt §2.2: a stray open()/close() of both files in THIS process.
        for path in (db, shm):
            os.close(os.open(path, os.O_RDONLY))
        assert _foreign_exclusive(str(db), *_SHARED) == "REFUSED"
        assert _foreign_exclusive(str(shm), *_DMS) == "REFUSED"
    finally:
        closing = lg.release(held)
    _sqlite_close(db, shm)
    lg.retire(closing)
    assert _foreign_exclusive(str(db), *_SHARED) == "ACQUIRED"
    assert _foreign_exclusive(str(shm), *_DMS) == "ACQUIRED"
    # state.db stays parked for reuse; the -shm descriptor is never parked (Prism 9016456d284b).
    db_ident = (os.stat(db).st_dev, os.stat(db).st_ino)
    shm_ident = (os.stat(shm).st_dev, os.stat(shm).st_ino)
    assert db_ident in lg._FDS and shm_ident not in lg._FDS


def test_missing_shm_is_tolerated(tmp_path):
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    held = lg.hold(db)  # never raises; arms the main file only
    assert list(held) == [(os.stat(db).st_dev, os.stat(db).st_ino)]
    lg.retire(lg.release(held))
    assert lg.hold(tmp_path / "absent.db") == {}


def test_late_shm_is_armed_by_checkpoint_with_one_stat_and_no_listdir(tmp_path, monkeypatch):
    """-shm absent at open, created later: the next _try_wal_checkpoint arms it with exactly one
    os.stat (the -shm) and zero os.listdir calls."""
    pin_wal(monkeypatch)
    sdb = make_db(tmp_path / "state.db", "s", "seed")
    require_wal(sdb)
    try:
        shm = f"{sdb.db_path}-shm"
        shm_ident = (os.stat(shm).st_dev, os.stat(shm).st_ino)
        # Simulate "-shm minted after open": drop it from this handle's guard record.
        lg.retire(lg.release({shm_ident: sdb._wal_lock_guard.pop(shm_ident)}))
        assert shm_ident not in sdb._wal_lock_guard
        calls = {"stat": 0, "listdir": 0}
        real_stat, real_listdir = os.stat, os.listdir

        def _stat(path, *a, **k):
            if os.fspath(path) == shm:
                calls["stat"] += 1
            return real_stat(path, *a, **k)

        def _listdir(*a, **k):
            calls["listdir"] += 1
            return real_listdir(*a, **k)

        monkeypatch.setattr(lg.os, "stat", _stat)
        monkeypatch.setattr(lg.os, "listdir", _listdir)
        sdb._try_wal_checkpoint()
        monkeypatch.setattr(lg.os, "stat", real_stat)
        monkeypatch.setattr(lg.os, "listdir", real_listdir)
        assert shm_ident in sdb._wal_lock_guard, "checkpoint did not arm the new -shm"
        assert calls == {"stat": 1, "listdir": 0}, calls
        # Already guarded: the next checkpoint is one stat and nothing else.
        calls.update(stat=0, listdir=0)
        monkeypatch.setattr(lg.os, "stat", _stat)
        monkeypatch.setattr(lg.os, "listdir", _listdir)
        sdb._try_wal_checkpoint()
        assert calls == {"stat": 1, "listdir": 0}, calls
    finally:
        monkeypatch.undo()
        sdb.close()


def test_write_path_never_reaches_the_fd_table_scan(tmp_path, monkeypatch):
    """I2: open, write, checkpoint, reopen and close never call _own_fds_for in private mode."""
    pin_wal(monkeypatch)
    real_scan = lg._own_fds_for
    holders = []
    forbidden = {"_execute_write", "_try_wal_checkpoint", "_open_writer", "_reopen_after_close_locked", "hold",
                 "refresh_shm"}

    def _recording_scan(identities):
        frames, f = set(), sys._getframe(1)
        while f is not None:
            frames.add(f.f_code.co_name)
            f = f.f_back
        holders.append((frames & forbidden, [db._lock.locked() for db in dbs]))
        return real_scan(identities)

    dbs = []
    monkeypatch.setattr(lg, "_own_fds_for", _recording_scan)
    sdb = make_db(tmp_path / "state.db", "s", "seed")
    dbs.append(sdb)
    require_wal(sdb)
    sdb._try_wal_checkpoint()
    assert holders == [], "fd-table scan on the open/write/checkpoint path"
    sdb.close()
    sdb.append_message("s", role="user", content="after close")  # reopen path
    assert sdb._wal_lock_guard
    shm = (os.stat(f"{sdb.db_path}-shm").st_dev, os.stat(f"{sdb.db_path}-shm").st_ino)
    main = (os.stat(sdb.db_path).st_dev, os.stat(sdb.db_path).st_ino)
    sdb.close()
    assert holders == [], holders
    assert not lg._HANDLES
    assert shm not in lg._FDS, "the -shm descriptor outlived the last close (pins a deleted generation)"
    assert main in lg._FDS, "state.db's private fd is parked for the next hold(), never closed"


class _SlowWrite:
    def __init__(self, seconds):
        self.seconds = seconds

    def __call__(self, conn):
        time.sleep(self.seconds)
        return None


@pytest.mark.parametrize("seconds,expect", [(1.2, True), (0.5, False)])
def test_lock_hold_gauge(tmp_path, monkeypatch, caplog, seconds, expect):
    pin_wal(monkeypatch)
    notes = []
    monkeypatch.setattr(lockowners, "note_lock_hold", lambda db, op, held: notes.append((op, held)))
    sdb = make_db(tmp_path / "state.db", "s", "seed")
    try:
        with caplog.at_level(logging.WARNING, logger="hermes_state"):
            caplog.clear()
            sdb._execute_write(_SlowWrite(seconds))
        warned = [r for r in caplog.records if "SessionDB._lock held" in r.getMessage()]
        assert bool(warned) is expect and bool(notes) is expect
        if expect:
            assert notes[0][0].startswith("_lock:") and notes[0][1] >= 1.2
            assert hermes_state._LOCK_HOLD_WARN_S == 1.0
    finally:
        sdb.close()


def test_lock_hold_note_writes_a_released_ledger_row(tmp_path):
    db = tmp_path / "state.db"
    db.write_bytes(b"")
    lockowners.note_lock_hold(db, "_lock:append_messages_batch", 1.7)
    import json
    rows = [json.loads(p.read_text()) for p in (tmp_path / "state.db.lockholders").glob("*.json")]
    assert rows and rows[0]["op"] == "_lock:append_messages_batch"
    assert rows[0]["state"] == "released" and rows[0]["held_s"] == 1.7


@pytest.mark.parametrize("value,mode,armed", [
    (None, "private", True), ("scan", "scan", True), ("off", "off", False), ("bogus", "private", True)])
def test_mode_kill_switch_is_read_at_import(monkeypatch, tmp_path, value, mode, armed):
    """D5: HERMES_STATE_LOCKGUARD_MODE=private|scan|off, read once at import; unknown -> private."""
    if value is None:
        monkeypatch.delenv("HERMES_STATE_LOCKGUARD_MODE", raising=False)
    else:
        monkeypatch.setenv("HERMES_STATE_LOCKGUARD_MODE", value)
    monkeypatch.delitem(sys.modules, "hermes_state_lockguard", raising=False)
    try:
        guard = importlib.import_module("hermes_state_lockguard")
        assert guard._MODE == mode and guard.supported() is armed
        db = tmp_path / "state.db"
        db.write_bytes(b"\0" * 4096)
        fd = os.open(db, os.O_RDONLY)  # scan mode guards descriptors the process already has
        try:
            held = guard.hold(db)
            assert bool(held) is armed
            if armed:
                assert _foreign_exclusive(str(db), *_SHARED) == "REFUSED"
            closing = guard.release(held)
        finally:
            os.close(fd)
        guard.retire(closing)
        assert set(guard._FDS) <= {(os.stat(db).st_dev, os.stat(db).st_ino)}  # private: parked, reused
    finally:
        monkeypatch.delitem(sys.modules, "hermes_state_lockguard", raising=False)
        sys.modules["hermes_state_lockguard"] = lg


@pytest.mark.parametrize("failure", ["refused", "oserror"])
def test_refused_lock_descriptor_is_counted_parked_and_reused(tmp_path, monkeypatch, failure):
    """Prism 78f32301dabd + e63d481511da: a hold() whose OFD lock is refused (or fails) still COUNTS
    its private fds, so no retire() closes them under the live connection; once released, the
    state.db fd is parked and the next hold() reuses it: one fd per inode, no leak."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    ident = (os.stat(db).st_dev, os.stat(db).st_ino)
    shm_ident = _ident(shm)

    def _fail(*a, **k):
        if failure == "oserror":
            raise OSError(22, "EINVAL")
        return False

    monkeypatch.setattr(lg, "_ofd_lock", _fail)
    first = lg.hold(db)
    monkeypatch.undo()
    assert set(first) == {ident, shm_ident}
    with lg.registered_reader(db):  # a reader's exit runs retire(): must not close A's -shm fd
        pass
    lg.retire()
    assert shm_ident in lg._FDS, "an uncounted -shm fd was closed under a live writer"
    lg.retire(lg.release(first))
    fd = lg._FDS[ident]
    held = lg.hold(db)
    try:
        assert ident in held and lg._FDS[ident] == fd
    finally:
        lg.retire(lg.release(held))


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_fork_child_closes_inherited_private_fds_and_parent_stays_guarded(tmp_path):
    """Prism 377384e39385: the child closes (never unlocks) its inherited private fds, so it pins no
    -shm, and the parent's OFD lock survives because the parent still holds the description."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    held = lg.hold(db)
    try:
        inodes = {(os.stat(p).st_dev, os.stat(p).st_ino) for p in (db, shm)}
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # child: after_in_child already ran
            try:
                left = 0
                for name in os.listdir("/proc/self/fd"):
                    try:
                        st = os.stat(f"/proc/self/fd/{name}")
                    except OSError:
                        continue
                    if (st.st_dev, st.st_ino) in inodes:
                        left += 1
                os.write(w, f"{len(lg._FDS)} {left}".encode())
            finally:
                os._exit(0)
        os.close(w)
        out = os.read(r, 64).decode()
        os.close(r)
        os.waitpid(pid, 0)
        assert out == "0 0", f"child kept inherited descriptors (bookkeeping, fds on inode): {out}"
        assert _foreign_exclusive(str(db), *_SHARED) == "REFUSED"
        assert _foreign_exclusive(str(shm), *_DMS) == "REFUSED"
    finally:
        lg.retire(lg.release(held))


def _foreign_posix_exclusive(path: str, start: int, length: int) -> str:
    """A CHILD process tries a process-owned POSIX F_SETLK F_WRLCK: 'ACQUIRED' or 'REFUSED'."""
    code = (
        "import fcntl, os, struct\n"
        f"fd = os.open({path!r}, os.O_RDWR)\n"
        f"lk = struct.pack('@hhqqi', fcntl.F_WRLCK, 0, {start}, {length}, 0)\n"
        "try:\n    fcntl.fcntl(fd, fcntl.F_SETLK, lk); print('ACQUIRED')\n"
        "except (BlockingIOError, PermissionError):\n    print('REFUSED')\n"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()


def test_retire_never_strips_a_reader_that_opened_after_the_snapshot(tmp_path, monkeypatch):
    """Prism 7072ffc85b72: a raw in-process sqlite3 reader opens on the inode between hold/release
    and retire(). Any close() of a descriptor on that inode cancels the reader's POSIX SHARED lock,
    so retire() must not close it. The fd-table snapshot is made blind to the reader (it opened
    after the scan): private mode must not depend on that scan at all."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    import sqlite3
    db = tmp_path / "state.db"
    setup = sqlite3.connect(db)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE t(x)")
    setup.execute("INSERT INTO t VALUES (1)")
    setup.commit()
    setup.close()
    lg.retire(lg.release(lg.hold(db)))
    reader = sqlite3.connect(db)
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT x FROM t").fetchall() == [(1,)]
        assert _foreign_posix_exclusive(str(db), *_SHARED) == "REFUSED", "reader holds no SHARED lock?"
        real_scan = lg._own_fds_for
        mine = set(lg._FDS.values())

        def _stale_snapshot(identities):  # the scan ran before the reader opened
            return ((fd, ident) for fd, ident in real_scan(identities) if fd in mine)

        monkeypatch.setattr(lg, "_own_fds_for", _stale_snapshot)
        lg.retire()
        assert _foreign_posix_exclusive(str(db), *_SHARED) == "REFUSED", "retire() stripped the reader's lock"
    finally:
        reader.rollback()
        reader.close()


def test_hold_retire_hold_reuses_one_parked_fd(tmp_path, monkeypatch):
    """hold -> release/retire -> hold on the same state.db opens exactly one private descriptor
    (the main file only: a -shm descriptor is closed at retire and reopened by the next hold)."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    opens = []
    real_open = os.open

    def _open(path, *a, **k):
        opens.append(os.fspath(path))
        return real_open(path, *a, **k)

    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    monkeypatch.setattr(lg.os, "open", _open)
    held = lg.hold(db)
    lg.retire(lg.release(held))
    held = lg.hold(db)
    closing = []
    try:
        assert opens.count(str(db)) == 1, opens
        assert opens.count(str(shm)) == 2, opens
        assert _foreign_exclusive(str(db), *_SHARED) == "REFUSED"
    finally:
        closing = lg.release(held)
        _sqlite_close(db, shm)
        lg.retire(closing)
    assert _foreign_exclusive(str(db), *_SHARED) == "ACQUIRED", "a parked fd must carry no OFD lock"


def test_fd_table_scan_never_runs_in_private_mode(tmp_path, monkeypatch):
    """I2 with no exceptions: open, write, checkpoint, close, reopen, retire, close -> 0 scans."""
    pin_wal(monkeypatch)
    calls = []
    monkeypatch.setattr(lg, "_own_fds_for", lambda identities: calls.append(identities) or iter(()))
    sdb = make_db(tmp_path / "state.db", "s", "seed")
    require_wal(sdb)
    sdb._try_wal_checkpoint()
    sdb.close()
    sdb.append_message("s", role="user", content="after close")  # reopen path
    assert sdb._wal_lock_guard
    sdb.close()
    lg.retire()
    assert calls == []
    assert not lg._HANDLES


def test_pool_gauge_warns_once_past_the_bound(tmp_path, monkeypatch, caplog):
    """More parked fds than _POOL_WARN_FDS logs one WARNING, not one per hold()."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    monkeypatch.setattr(lg, "_POOL_WARN_FDS", len(lg._FDS) + 1)
    monkeypatch.setattr(lg, "_POOL_WARNED", False)
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        for i in range(4):
            db = tmp_path / f"s{i}.db"
            db.write_bytes(b"\0" * 4096)
            lg.retire(lg.release(lg.hold(db)))
    assert len([r for r in caplog.records if "private descriptors parked" in r.getMessage()]) == 1


def test_sessiondb_close_leaves_no_lock_on_parked_fds(tmp_path, monkeypatch):
    """After a guarded SessionDB's close, the parked state.db fd carries no lock of any kind (SQLite's
    close dropped the swapped-in POSIX lock), so a sibling's true last close is not refused forever;
    the unlinked -shm generation's fd is closed by retire()."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    pin_wal(monkeypatch)
    sdb = make_db(tmp_path / "state.db", "s", "seed")
    require_wal(sdb)
    shm = f"{sdb.db_path}-shm"
    shm_ident = (os.stat(shm).st_dev, os.stat(shm).st_ino)
    assert _foreign_exclusive(str(sdb.db_path), *_SHARED) == "REFUSED"
    sdb.close()
    assert _foreign_exclusive(str(sdb.db_path), *_SHARED) == "ACQUIRED"
    assert _foreign_posix_exclusive(str(sdb.db_path), *_SHARED) == "ACQUIRED"
    if os.path.exists(shm):
        assert _foreign_exclusive(shm, *_DMS) == "ACQUIRED"
    assert shm_ident not in lg._FDS, "the -shm descriptor is never parked"


_FOREIGN_CHILD = """
import os, sys
sys.path.insert(0, {repo!r})
import hermes_state_wal
hermes_state_wal.is_sqlite_wal_reset_vulnerable = lambda version_info=None: False
hermes_state_wal.resolve_journal_mode = lambda: "wal"
from pathlib import Path
from hermes_state import SessionDB
b = SessionDB(db_path=Path({db!r}))
b.append_message("s", role="user", content="b")
print("B-open", flush=True)
sys.stdin.readline()
b.close()
print("B-closed", os.path.exists({db!r} + "-shm"), flush=True)
"""


def test_foreign_last_close_does_not_strand_openers(tmp_path, monkeypatch):
    """Prism 9016456d284b: process A closes while B is open, B's last close unlinks -shm, then a third
    opener must SUCCEED. A parked -shm fd in A would pin the deleted generation and the open-path
    probe would refuse it with DeletedWalGenerationError (RED on 14bbc5f7)."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    pin_wal(monkeypatch)
    import hermes_state_dbfile as dbf
    db = tmp_path / "state.db"
    a = make_db(db, "s", "a")
    require_wal(a)
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    child = subprocess.Popen([sys.executable, "-c", _FOREIGN_CHILD.format(repo=repo, db=str(db))],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "B-open"
        shm_ident = (os.stat(f"{db}-shm").st_dev, os.stat(f"{db}-shm").st_ino)
        a.close()
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.stdout.readline().startswith("B-closed")
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()
    c = hermes_state.SessionDB(db_path=db)  # third opener: must not raise DeletedWalGenerationError
    c.close()
    assert dbf.iter_deleted_sqlite_sidecar_holders(db) == []
    assert shm_ident not in lg._FDS, "A parked its -shm descriptor"


def test_relative_path_survives_chdir(tmp_path, monkeypatch):
    """Prism 5214a704d594: a relative db_path is made absolute at hold() time, so retire() after a
    chdir does not take a live state.db for a gone one and close its parked descriptor."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "state.db").write_bytes(b"\0" * 4096)
    ident = (os.stat(a / "state.db").st_dev, os.stat(a / "state.db").st_ino)
    monkeypatch.chdir(a)
    lg.retire(lg.release(lg.hold("state.db")))
    fd = lg._FDS[ident]
    assert lg._PATHS[ident] == str(a / "state.db")
    monkeypatch.chdir(b)
    lg.retire()
    assert lg._FDS.get(ident) == fd, "retire() after a chdir closed a live parked descriptor"


def test_retire_closes_only_on_enoent_or_identity_change(tmp_path, monkeypatch, caplog):
    """Prism a97a215db7d8: an inconclusive stat (EACCES/EIO) keeps the parked descriptor and logs
    once; ENOENT or a different inode at the path closes it."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    import errno as _errno
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    ident = (os.stat(db).st_dev, os.stat(db).st_ino)
    lg.retire(lg.release(lg.hold(db)))
    real_stat = os.stat

    def _eacces(path, *a, **k):
        if os.fspath(path) == str(db):
            raise PermissionError(_errno.EACCES, "denied", str(db))
        return real_stat(path, *a, **k)

    monkeypatch.setattr(lg.os, "stat", _eacces)
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        lg.retire()
        lg.retire()
    monkeypatch.setattr(lg.os, "stat", real_stat)
    assert ident in lg._FDS, "an inconclusive stat closed a possibly live descriptor"
    assert len([r for r in caplog.records if "cannot stat" in r.getMessage()]) == 1
    db.unlink()
    db.write_bytes(b"\0" * 4096)  # a different inode at the same path
    lg.retire()
    assert ident not in lg._FDS
    ident2 = (os.stat(db).st_dev, os.stat(db).st_ino)
    lg.retire(lg.release(lg.hold(db)))
    db.unlink()  # ENOENT
    lg.retire()
    assert ident2 not in lg._FDS


def test_lock_gauge_does_not_overwrite_a_recorded_transaction_hold(tmp_path, monkeypatch):
    """The ledger keeps one record per pid. A write transaction long enough for hold_ended() to
    record it already names the holder; the _lock: row must not replace it."""
    pin_wal(monkeypatch)
    notes = []
    monkeypatch.setattr(lockowners, "note_lock_hold", lambda db, op, held: notes.append(op))
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 1.0)
    sdb = make_db(tmp_path / "state.db", "s", "seed")
    try:
        sdb._execute_write(_SlowWrite(1.2))
        assert notes == []
    finally:
        sdb.close()


def _ident(path) -> tuple:
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def test_registered_reader_keeps_the_shm_fd_until_it_exits(tmp_path, monkeypatch):
    """Prism f73fc9c5f6cc: a registered reader open on the inode -> a SessionDB close does NOT close
    the private -shm descriptor (closing it would cancel the reader's POSIX read-mark on -shm); once
    the reader exits, the descriptor is closed."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    import sqlite3
    pin_wal(monkeypatch)
    sdb = make_db(tmp_path / "state.db", "s", "seed")
    require_wal(sdb)
    shm = f"{sdb.db_path}-shm"
    shm_ident = _ident(shm)
    assert shm_ident in lg._FDS
    with lg.registered_reader(sdb.db_path):
        reader = sqlite3.connect(f"file:{sdb.db_path}?mode=ro", uri=True)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM messages").fetchone()
            sdb.close()
            assert shm_ident in lg._FDS, "SessionDB close closed the -shm fd under a registered reader"
            fd = lg._FDS[shm_ident]
            os.fstat(fd)  # still open
        finally:
            reader.rollback()
            reader.close()
    assert shm_ident not in lg._FDS, "the -shm fd outlived the last registered reader"


def test_closing_window_keeps_the_shm_fd_until_retire(tmp_path, monkeypatch):
    """Prism 5ac4f2a27277: between a writer's release() and its retire() its SQLite connection is still
    open. A reader arriving and leaving in that window runs retire() too; it must not close the -shm
    descriptor (that would cancel the writer's swapped-in POSIX DMS lock). The writer's own
    retire(token) after its close does."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    import sqlite3
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    shm = f"{db}-shm"
    shm_ident = _ident(shm)
    held = lg.hold(db)
    assert shm_ident in held
    closing = lg.release(held)  # the writer's connection is still open
    with lg.registered_reader(db):  # a reader arrives in the window ...
        pass  # ... and leaves: unregister_reader() runs retire()
    lg.retire()  # and any sibling handle's close
    try:
        assert shm_ident in lg._FDS, "a retire() inside the release->retire window closed the -shm fd"
        assert _foreign_posix_exclusive(shm, *_DMS) == "REFUSED", "the writer lost its DMS lock in the window"
    finally:
        conn.close()
        lg.retire(closing)
    assert shm_ident not in lg._FDS


_FORK_DEADLINE_S = 0.5


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_fork_mid_hold_child_owns_no_private_fds(tmp_path, monkeypatch):
    """Prism 11061d53f43a: a fork that lands between a hold()'s os.open() and its _park() would hand the
    child a private descriptor missing from its _FDS, which _reset_after_fork therefore never closes
    (it would pin a -shm the parent's last close unlinks). The at-fork hook takes _LOCK, so the fork
    waits for the park and the child closes every inherited private descriptor."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    import threading
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    inode = _ident(db)
    opened, go = threading.Event(), threading.Event()
    real_open = os.open

    def _open(path, *a, **k):
        fd = real_open(path, *a, **k)
        if os.fspath(path) == str(db):
            opened.set()
            go.wait(10)  # hold() is now between os.open() and _park()
        return fd

    monkeypatch.setattr(lg.os, "open", _open)
    held = {}
    t = threading.Thread(target=lambda: held.update(lg.hold(db)))
    t.start()
    try:
        assert opened.wait(10)
        threading.Timer(_FORK_DEADLINE_S, go.set).start()  # lets the hold finish while fork() waits
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                left = 0
                for name in os.listdir("/proc/self/fd"):
                    try:
                        if _ident(f"/proc/self/fd/{name}") == inode:
                            left += 1
                    except OSError:
                        continue
                os.write(w, str(left).encode())
            finally:
                os._exit(0)
        os.close(w)
        out = os.read(r, 16).decode()
        os.close(r)
        os.waitpid(pid, 0)
        assert out == "0", f"fork child kept {out} private fd(s) on the inode"
    finally:
        go.set()
        t.join(10)
        monkeypatch.undo()
        lg.retire(lg.release(held))


_ATEXIT_CHILD = """
import atexit, os, sys
sys.path.insert(0, {repo!r})
handlers = []
_register = atexit.register
def _capture(fn, *a, **k):
    handlers.append((fn, a, k))
    return _register(fn, *a, **k)
atexit.register = _capture
import hermes_state_wal
hermes_state_wal.is_sqlite_wal_reset_vulnerable = lambda version_info=None: False
hermes_state_wal.resolve_journal_mode = lambda: "wal"
from pathlib import Path
import hermes_state_lockguard as lg
from hermes_state import SessionDB
sdb = SessionDB(db_path=Path({db!r}))
sdb.create_session("s", "cli")
sdb.append_message("s", role="user", content="before")
mine = [h for h in handlers if getattr(h[0], "__module__", "") == "hermes_state_lockguard"]
for fn, a, k in mine:
    fn(*a, **k)  # the interpreter-exit path, run while the connection is live
print("RAN", len(mine), len(lg._FDS), flush=True)
sys.stdin.readline()
sdb.append_message("s", role="user", content="after")
print("ROWS", len(sdb.get_messages("s")), flush=True)
sdb.close()
"""


def test_exit_path_leaves_a_live_connection_usable_and_guarded(tmp_path):
    """Prism c00385eecb2c / ea4900b85b87: whatever the lock guard runs at interpreter exit, a SessionDB
    connection still live at that point stays usable AND guarded (a close of the private descriptors
    would cancel its POSIX locks). The kernel releases descriptors and OFD locks at process exit."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    child = subprocess.Popen([sys.executable, "-c", _ATEXIT_CHILD.format(repo=repo, db=str(db))],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        ran = child.stdout.readline().split()
        assert ran[:1] == ["RAN"], ran
        assert int(ran[2]) >= 1, f"exit path closed the private descriptors: {ran}"
        assert _foreign_exclusive(str(db), *_SHARED) == "REFUSED", "live writer unguarded after the exit path"
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.stdout.readline().split() == ["ROWS", "2"]
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()


def test_reader_registered_before_a_path_replacement_still_pins_the_new_files_shm(tmp_path, monkeypatch):
    """Prism e0cbbe030829: a reader registers on inode A, the path is replaced by WAL database B, the
    reader connects to B. A guarded writer's close on B must not close B's private -shm fd under it."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    import sqlite3
    pin_wal(monkeypatch)
    db = tmp_path / "state.db"
    db.write_bytes(b"")  # inode A
    with lg.registered_reader(db):
        os.unlink(db)
        sdb = make_db(db, "s", "seed")  # inode B at the same path
        require_wal(sdb)
        shm_ident = _ident(f"{db}-shm")
        reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM messages").fetchone()
            sdb.close()
            assert shm_ident in lg._FDS, "B's -shm fd was closed under a reader registered before the replacement"
        finally:
            reader.rollback()
            reader.close()
    assert shm_ident not in lg._FDS


def test_initializing_writer_is_counted_before_its_hold(tmp_path, monkeypatch):
    """Prism c4ca84c945ca / 8f1afbe91566: a second SessionDB between its connect (SQLite's POSIX locks on
    -shm from here, the WAL write lock in _init_schema) and its hold() is counted, so a sibling's close
    in that gap does not close the private -shm fd under it."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    pin_wal(monkeypatch)
    db = tmp_path / "state.db"
    first = make_db(db, "s", "seed")
    require_wal(first)
    shm_ident = _ident(f"{db}-shm")
    seen = {}
    real_init = hermes_state.SessionDB._init_schema

    def _init_schema(self):
        if not seen:  # inside the second opener's init: its connection is open, hold() not yet run
            first.close()
            seen["kept"] = shm_ident in lg._FDS
        return real_init(self)

    monkeypatch.setattr(hermes_state.SessionDB, "_init_schema", _init_schema)
    second = hermes_state.SessionDB(db_path=db)
    try:
        assert seen == {"kept": True}, "a sibling's close retired the -shm fd under an initializing writer"
        assert shm_ident in second._wal_lock_guard
        # the opener's own registration was dropped once hold() counted it (other tests' leaked read
        # connections may still be registered on THEIR paths: scope the check to this database)
        assert ("path", str(db)) not in lg._READERS, lg._READERS
    finally:
        second.close()


def test_clean_close_never_reads_its_own_unretired_shm_fd_as_a_lost_generation(tmp_path, monkeypatch):
    """Prism 9d02254cf252: between SQLite's clean last close (it unlinks -shm) and retire() the guard's
    private -shm fd points at a deleted inode. A deleted-generation probe in that window (an independent
    opener's refuse_deleted_wal_generation) must wait for retire() instead of refusing the open."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    import threading
    import hermes_state_dbfile as dbf
    pin_wal(monkeypatch)
    db = tmp_path / "state.db"
    sdb = make_db(db, "s", "seed")
    require_wal(sdb)
    in_window, go = threading.Event(), threading.Event()
    real_retire = lg.retire

    def _slow_retire(closing=None):
        if closing and not in_window.is_set():  # the writer's own retire, after SQLite's close
            in_window.set()
            go.wait(10)
        return real_retire(closing)

    monkeypatch.setattr(lg, "retire", _slow_retire)
    t = threading.Thread(target=sdb.close)
    t.start()
    try:
        assert in_window.wait(10)
        assert not os.path.exists(f"{db}-shm"), "the clean last close did not unlink -shm"
        result = {}

        def _open():
            try:
                hermes_state.SessionDB(db_path=db).close()
                result["ok"] = True
            except Exception as exc:  # DeletedWalGenerationError without the gate
                result["err"] = repr(exc)

        opener = threading.Thread(target=_open)
        opener.start()
        opener.join(0.5)
        go.set()
        opener.join(30)
        assert result == {"ok": True}, result
        assert dbf.iter_deleted_sqlite_sidecar_holders(db) == []
    finally:
        go.set()
        t.join(30)


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_inherited_reader_token_never_settles_a_child_registration(tmp_path):
    """Prism 1e8ea9086d86: a read-only handle opened before fork() and closed in the child must not
    decrement a reader the CHILD registered on the same db, or the child's next writer close retires
    the private -shm fd under that live reader."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    inherited = lg.register_reader(db)
    try:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                assert not lg._READERS  # the child starts with no counts
                mine = lg.register_reader(db)
                lg.unregister_reader(inherited)  # the inherited handle closes in the child
                ok = lg._READERS.get(("path", str(db)), 0) == 1
                lg.unregister_reader(mine)
                ok = ok and not lg._READERS
                os.write(w, b"ok" if ok else repr(dict(lg._READERS)).encode()[:200])
            finally:
                os._exit(0)
        os.close(w)
        out = os.read(r, 256).decode()
        os.close(r)
        os.waitpid(pid, 0)
        assert out == "ok", f"inherited token settled the child's reader count: {out}"
    finally:
        lg.unregister_reader(inherited)
    assert ("path", str(db)) not in lg._READERS


@pytest.mark.parametrize("helper", ["stats", "structural", "repair_conn", "change_watcher"])
def test_state_module_readers_connect_while_registered(tmp_path, monkeypatch, helper):
    """Prism 230be3dd449d: collect_state_db_stats, state_db_has_structural_damage, the repair
    connections and the desktop change watcher open state.db beside live SessionDB handles. Each must
    be a registered reader for the connection's whole life (registered at connect, dropped after close)."""
    import sqlite3
    db = tmp_path / "state.db"
    sqlite3.connect(db).close()
    key = ("path", str(db))
    seen = []
    real_connect = sqlite3.connect

    def _connect(target, *a, **k):
        if "state.db" in str(target):
            seen.append(lg._READERS.get(key, 0))
        return real_connect(target, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", _connect)
    if helper == "stats":
        import hermes_state_dbfile as dbf
        dbf.collect_state_db_stats(db)
    elif helper == "structural":
        import hermes_state_repair as rep
        rep.state_db_has_structural_damage(db)
    elif helper == "repair_conn":
        import hermes_state_repair as rep
        with rep._repair_conn(db):
            assert lg._READERS.get(key, 0) == 1
    else:
        from tui_gateway import change_watcher as cw
        cw._session_db_content_sig(db)
    assert seen and all(n >= 1 for n in seen), seen
    assert key not in lg._READERS, "registration outlived the connection"


def test_repair_connection_double_close_settles_its_token_once(tmp_path):
    """Prism 4eff69c393c6: close() twice on a tracked repair connection must not consume ANOTHER
    reader's registration (SQLite allows repeated close())."""
    import sqlite3
    import hermes_state_repair as rep
    db = tmp_path / "state.db"
    sqlite3.connect(db).close()
    key = ("path", str(db))
    with lg.registered_reader(db):
        conn = rep._connect_repair_durable(db)
        assert lg._READERS.get(key) == 2
        conn.close()
        conn.close()
        assert lg._READERS.get(key) == 1, "the second close() removed the live reader's count"
    assert key not in lg._READERS


def test_reader_last_close_never_strands_an_opener_before_its_retire(tmp_path, monkeypatch):
    """Prism e5b80b447fa9: a registered reader outlives the last SessionDB writer; its own last SQLite
    close unlinks -shm while the guard's private -shm fd is still open (its retire() runs at block
    exit). A SessionDB opened in that window must succeed, not raise DeletedWalGenerationError."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    import sqlite3
    pin_wal(monkeypatch)
    db = tmp_path / "state.db"
    writer = make_db(db, "s", "seed")
    require_wal(writer)
    shm_ident = _ident(f"{db}-shm")
    with lg.registered_reader(db):
        raw = sqlite3.connect(db)  # an allowlisted read-write reader (e.g. PRAGMA quick_check)
        raw.execute("SELECT count(*) FROM messages").fetchone()
        writer.close()
        assert shm_ident in lg._FDS  # pinned by the reader
        raw.close()  # the last in-process SQLite close: checkpoints and unlinks -wal/-shm
        assert not os.path.exists(f"{db}-shm")
        third = hermes_state.SessionDB(db_path=db)  # must not raise DeletedWalGenerationError
        third.close()
    assert shm_ident not in lg._FDS


def test_failed_private_open_still_counts_the_writer(tmp_path, monkeypatch):
    """Prism 2814ba2fca00: A's private -shm open raises (EMFILE). A must still be counted, so when a
    sibling B later opens the private -shm fd and closes, retire() keeps that fd (closing it would
    cancel A's POSIX locks on -shm)."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    import errno as _errno
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    shm_ident = _ident(shm)
    real_open = os.open

    def _emfile(path, *a, **k):
        if os.fspath(path).endswith("-shm"):
            raise OSError(_errno.EMFILE, "Too many open files")
        return real_open(path, *a, **k)

    monkeypatch.setattr(lg.os, "open", _emfile)
    a = lg.hold(db)
    monkeypatch.undo()
    assert shm_ident in a, "the writer whose private open failed is not counted"
    b = lg.hold(db)
    assert shm_ident in lg._FDS
    lg.retire(lg.release(b))
    try:
        assert shm_ident in lg._FDS, "B's close retired the -shm fd under the still-open writer A"
    finally:
        lg.retire(lg.release(a))
    assert shm_ident not in lg._FDS


def test_surplus_descriptor_on_a_replaced_path_is_tracked_and_closed(tmp_path):
    """Prism c8e1b4b3568d: T1 stats -shm inode A, the path is replaced by B and T2 parks B, then T1
    opens B. The second descriptor on B must be tracked and closed with B's fd, never leaked (a leaked
    -shm fd outlives its generation and every opener's deleted-generation probe refuses)."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    stale = _ident(shm)
    os.rename(shm, tmp_path / "keep-A")  # A stays allocated, so B cannot reuse its inode number
    shm.write_bytes(b"\0" * 32768)  # inode B at the path
    b_ident = _ident(shm)
    assert b_ident != stale
    with lg._LOCK:
        first = lg._private_fd(str(shm), b_ident)  # T2 parks B
        assert lg._private_fd(str(shm), stale) is None  # T1, stat said A, opens B
        assert len(lg._SURPLUS.get(b_ident, [])) == 1, lg._SURPLUS
        surplus = lg._SURPLUS[b_ident][0]
    os.unlink(shm)
    lg.retire()  # B is gone and unused: main fd AND the surplus close
    assert b_ident not in lg._FDS and b_ident not in lg._SURPLUS
    for fd in (first, surplus):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_failed_pool_close_keeps_the_reader_token(tmp_path, monkeypatch):
    """Prism 6ed61d49912e: a read-pool/read-only connection whose close() raised may still be open; its
    reader registration must stay (pinned), never be dropped as if it had closed."""
    pin_wal(monkeypatch)
    sdb = make_db(tmp_path / "state.db", "s", "seed")
    try:
        key = ("path", os.path.abspath(sdb.db_path))

        class _Failing:
            def close(self):
                raise RuntimeError("cross-thread close")

        conn = _Failing()
        token = lg.register_reader(sdb.db_path)
        sdb.__dict__.setdefault("_reader_tokens", {})[id(conn)] = token
        before = lg._READERS.get(key, 0)
        sdb._close_conn_logged(conn, "test conn")
        assert lg._READERS.get(key, 0) == before, "a failed close dropped the reader token"
        lg.unregister_reader(token)
    finally:
        sdb.close()


def test_writer_held_before_shm_existed_still_pins_the_new_shm(tmp_path):
    """Prism 1de1d88b5bec: A holds the db before -shm exists (counted on state.db only); -shm appears;
    B holds both and closes. B's retire() must keep the -shm fd while A is still open."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    a = lg.hold(db)
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    shm_ident = _ident(shm)
    assert shm_ident not in a
    b = lg.hold(db)
    assert shm_ident in b
    lg.retire(lg.release(b))
    try:
        assert shm_ident in lg._FDS, "B's close retired the -shm fd under writer A"
    finally:
        lg.retire(lg.release(a))
    assert shm_ident not in lg._FDS


def test_externally_unlinked_shm_with_a_live_connection_is_not_retired(tmp_path):
    """Prism 24d6965d4428: -shm unlinked from outside while a connection here still uses the inode is a
    LIVE deleted generation: retire_completed_shm() must keep the private fd (and the probe still sees
    the deleted holder). After that connection closes, it is retired."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    shm_ident = _ident(shm)
    with lg.registered_reader(db):
        held = lg.hold(db)
        lg.retire(lg.release(held))  # the -shm fd stays: the registered reader pins it
    live = os.open(shm, os.O_RDONLY)  # a live in-process user of the inode (stands in for SQLite's fd)
    try:
        held = lg.hold(db)
        closing = lg.release(held)
        os.unlink(shm)  # external removal
        with lg.registered_reader(db):  # keep the private fd from the normal retire path
            lg.retire(closing)
            lg.retire_completed_shm()
            assert shm_ident in lg._FDS, "a live deleted generation's private fd was closed"
    finally:
        os.close(live)
    lg.retire_completed_shm()
    assert shm_ident not in lg._FDS


def test_backup_source_is_a_registered_reader(tmp_path, monkeypatch):
    """Prism 95ebc4a81ebe: _safe_copy_db's source connection holds a WAL read transaction for the whole
    copy; it must be a registered reader from before its connect until after its close."""
    import sqlite3
    from hermes_cli import backup_sqlite as bs
    src = tmp_path / "state.db"
    c = sqlite3.connect(src)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t(x)")
    c.commit()
    c.close()
    key = ("path", str(src))
    seen = []
    real = sqlite3.connect

    def _connect(target, *a, **k):
        if "state.db" in str(target) and "dst" not in str(target):
            seen.append(lg._READERS.get(key, 0))
        return real(target, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", _connect)
    assert bs._safe_copy_db(src, tmp_path / "dst.db")
    assert seen and all(n >= 1 for n in seen), seen
    assert key not in lg._READERS


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_inherited_hold_record_never_releases_a_child_claim(tmp_path):
    """Prism bf7314233ba1: a hold() record taken before fork() and released in the child must not
    decrement the child's own fresh claim on the same inodes."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    parent = lg.hold(db)
    try:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                child = lg.hold(db)
                lg.release(parent)  # an inherited SessionDB closing in the child
                ok = all(lg._HANDLES.get(i) == 1 for i in child)
                lg.retire()
                ok = ok and all(i in lg._FDS for i in child)
                lg.retire(lg.release(child))
                os.write(w, b"ok" if ok else repr(dict(lg._HANDLES)).encode()[:200])
            finally:
                os._exit(0)
        os.close(w)
        out = os.read(r, 256).decode()
        os.close(r)
        os.waitpid(pid, 0)
        assert out == "ok", f"inherited hold record released the child's claim: {out}"
    finally:
        lg.retire(lg.release(parent))


# ── round 15 (Apollo 10:3x PT, 2026-10-25) ───────────────────────────────────────────────────


def _ident(path) -> tuple:
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def test_reader_whose_close_raises_keeps_its_token(tmp_path, monkeypatch):
    """Rule A (Prism 2d8ea17ee6c0 / c3cd126492f6 / 5160bb865254 / 1b2a27c4097a): the token is dropped
    only by the code that observed the close succeed. A read-only handle whose close raises keeps its
    reader token, so the writer's retire() does not close the private -shm fd under the still-open
    connection; after a successful close it does."""
    pin_wal(monkeypatch)
    path = tmp_path / "state.db"
    writer = make_db(path, "s", "seed")
    require_wal(writer)
    ro = hermes_state.SessionDB(db_path=path, read_only=True)
    real = ro._conn
    fail = {"on": True}

    class _Flaky:
        def __init__(self, inner):
            self._inner = inner

        def close(self):
            if fail["on"]:
                raise sqlite3.ProgrammingError("synthetic close failure")
            self._inner.close()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    proxy = _Flaky(real)
    tokens = ro.__dict__["_reader_tokens"]
    token = tokens.pop(id(real))
    tokens[id(proxy)] = token
    ro._conn = proxy
    key = ("path", lg._canonical(path))
    try:
        before = lg._READERS.get(key, 0)
        ro.close()
        assert lg._READERS.get(key, 0) == before, "a failed close dropped the reader token"
        shm = _ident(f"{path}-shm")
        writer.close()  # release() + retire(): the -shm fd must survive (a reader still counts it)
        assert shm in lg._FDS, "retire() closed the private -shm fd under a connection whose close failed"
        fail["on"] = False
        ro._close_conn_logged(proxy, "retry")  # the successful close drops the token and retires
        assert lg._READERS.get(key, 0) == before - 1
        assert shm not in lg._FDS, "the successful close did not let retire() close the -shm fd"
    finally:
        fail["on"] = False
        with contextlib.suppress(Exception):
            real.close()
        lg.unregister_reader(token)
        writer.close()


def test_failed_open_keeps_the_opening_registration_until_closed(tmp_path, monkeypatch):
    """Rule A, failed-init path: an open that fails AND whose connection close raises keeps the
    opener's registration (the connection may still hold -shm locks)."""
    pin_wal(monkeypatch)
    path = tmp_path / "state.db"
    seed = make_db(path, "s", "seed")
    seed.close()
    key = ("path", lg._canonical(path))
    saved = dict(lg._READERS)
    monkeypatch.setattr(hermes_state.SessionDB, "_init_schema",
                        lambda self: (_ for _ in ()).throw(RuntimeError("synthetic init failure")))
    monkeypatch.setattr(hermes_state.SessionDB, "_close_connection_quietly", staticmethod(lambda conn: False))
    try:
        with pytest.raises(RuntimeError):
            hermes_state.SessionDB(db_path=path)
        assert lg._READERS.get(key, 0) == saved.get(key, 0) + 1, (
            "a failed open whose close failed dropped its registration")
    finally:
        monkeypatch.undo()
        lg._READERS.clear()
        lg._READERS.update(saved)  # the leaked (pinned) registration is this test's synthetic one


def test_unlistable_fd_table_keeps_the_shm_fd(tmp_path, monkeypatch):
    """Rule B (Prism 18a64534907c / 85e4628aedef): inconclusive is not proof. When neither fd
    directory lists, retire_completed_shm() keeps the private -shm fd of a gone path."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    token = lg.register_reader(db)  # keeps the -shm fd parked past the writer's retire()
    try:
        lg.retire(lg.release(lg.hold(db)))
        ident = _ident(shm)
        assert ident in lg._FDS
        os.unlink(shm)  # the generation's path is gone: a retire_completed_shm() candidate
        real_listdir = os.listdir

        def _no_listing(p):
            if str(p) in ("/proc/self/fd", "/dev/fd"):
                raise PermissionError(13, "synthetic", p)
            return real_listdir(p)

        monkeypatch.setattr(lg.os, "listdir", _no_listing)
        lg.retire_completed_shm()
        monkeypatch.setattr(lg.os, "listdir", real_listdir)
        assert ident in lg._FDS, "an unlistable fd table was read as 'no other descriptor'"
        lg.retire_completed_shm()  # conclusive listing, no other fd: now it closes
        assert ident not in lg._FDS
    finally:
        lg.unregister_reader(token)


def test_reader_registered_through_a_symlinked_home_matches_the_writer(tmp_path):
    """Prism 45bacae04f3f: a reader registers through an alias before state.db exists (so there is
    no inode yet); the writer then opens the target path. Both key the same canonical path, so the
    writer's retire() keeps the -shm fd while the reader is registered."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    token = lg.register_reader(alias / "state.db")  # file absent: path key only
    try:
        (real / "state.db").write_bytes(b"\0" * 4096)
        (real / "state.db-shm").write_bytes(b"\0" * 32768)
        shm = _ident(real / "state.db-shm")
        lg.retire(lg.release(lg.hold(real / "state.db")))
        assert shm in lg._FDS, "alias registration did not match the writer's target path"
    finally:
        lg.unregister_reader(token)
    assert shm not in lg._FDS


def test_writer_whose_journal_probe_returns_none_stays_counted(tmp_path, monkeypatch):
    """Prism ca0b4382bc99: the on-disk journal_mode probe returns None, so _wal_active is False and
    hold() never counts the writer. Its opener registration must stay (bound to the connection) so a
    sibling handle's close cannot retire the private -shm fd under it."""
    pin_wal(monkeypatch)
    path = tmp_path / "state.db"
    sibling = make_db(path, "s", "seed")
    require_wal(sibling)
    real_probe = hermes_state._on_disk_journal_mode
    monkeypatch.setattr(hermes_state, "_on_disk_journal_mode", lambda conn: None)
    blind = hermes_state.SessionDB(db_path=path)
    monkeypatch.setattr(hermes_state, "_on_disk_journal_mode", real_probe)
    try:
        assert not blind._wal_active and not blind._wal_lock_guard
        assert id(blind._conn) in blind.__dict__.get("_reader_tokens", {}), "opener token dropped uncounted"
        shm = _ident(f"{path}-shm")
        sibling.close()
        assert shm in lg._FDS, "a sibling's retire() closed the -shm fd under an uncounted writer"
    finally:
        blind.close()
    assert shm not in lg._FDS


def test_legacy_room_import_reads_state_db_as_a_registered_reader(tmp_path, monkeypatch):
    """Prism c349a9fa2a66: gateway/hosted_rooms_legacy_import._copy_rows streams rows from the live
    state.db; its connection must be a registered reader for the whole copy, so a same-process
    SessionDB close cannot close the private -shm fd (and cancel the read-mark) under it."""
    import sqlite3 as _sq
    from gateway import hosted_rooms_legacy_import as legacy

    source = tmp_path / "state.db"
    with contextlib.closing(_sq.connect(source)) as c:
        c.execute("CREATE TABLE hosted_rooms (room_id TEXT PRIMARY KEY)")
        c.commit()
    target = _sq.connect(":memory:")
    target.execute("CREATE TABLE hosted_rooms (room_id TEXT PRIMARY KEY)")
    key = ("path", lg._canonical(source))
    seen = []
    real = legacy._select_expressions

    def _spy(*a, **k):
        seen.append(lg._READERS.get(key, 0))
        return real(*a, **k)

    monkeypatch.setattr(legacy, "_select_expressions", _spy)
    legacy._copy_rows(target, source)
    assert seen and all(n >= 1 for n in seen), f"legacy import read state.db unregistered: {seen}"
    assert key not in lg._READERS


# ── round 16 ─────────────────────────────────────────────────────────────────────────────────


def test_failed_private_open_is_retried_by_refresh_shm(tmp_path, monkeypatch):
    """Prism 049e55321e1b: an EMFILE at the first hold() counted the inodes unarmed, and refresh_shm()
    skipped them for good. The next refresh must arm them (foreign EXCLUSIVE refused)."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    real_open = lg.os.open

    def _emfile(*a, **k):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(lg.os, "open", _emfile)
    held = lg.hold(db)
    monkeypatch.setattr(lg.os, "open", real_open)
    try:
        assert set(held) == {_ident(db), _ident(shm)}  # counted
        assert _foreign_exclusive(str(db), *_SHARED) == "ACQUIRED"  # but not armed
        lg.refresh_shm(db, held)
        assert _foreign_exclusive(str(db), *_SHARED) == "REFUSED", "refresh_shm never retried the main-file guard"
        assert _foreign_exclusive(str(shm), *_DMS) == "REFUSED", "refresh_shm never retried the -shm guard"
        assert not held.unarmed
        assert lg._HANDLES.get(_ident(db)) == 1, "a retry must not count the inode twice"
    finally:
        closing = lg.release(held)
    _sqlite_close(db, shm)
    lg.retire(closing)


def test_unstattable_descriptor_makes_the_scan_inconclusive(tmp_path, monkeypatch):
    """Prism 5c0c99319f38: a listed descriptor whose stat fails (EACCES/EIO) may be a live connection;
    retire_completed_shm() must keep the private -shm fd, not read the failure as 'no other fd'."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    token = lg.register_reader(db)
    try:
        lg.retire(lg.release(lg.hold(db)))
        ident = _ident(shm)
        assert ident in lg._FDS
        os.unlink(shm)
        mine = lg._FDS[ident]
        real_fstat = lg.os.fstat

        def _eio(fd, *a, **k):
            if fd != mine:
                raise OSError(5, "synthetic EIO")
            return real_fstat(fd, *a, **k)

        monkeypatch.setattr(lg.os, "fstat", _eio)
        lg.retire_completed_shm()
        monkeypatch.setattr(lg.os, "fstat", real_fstat)
        assert ident in lg._FDS, "an uninspectable descriptor was read as 'no other descriptor'"
        lg.retire_completed_shm()
        assert ident not in lg._FDS
    finally:
        lg.unregister_reader(token)


def test_foreign_scan_skips_a_guard_only_deleted_shm(tmp_path):
    """Prism ba6fa9c4a5a7: between SQLite's clean last close (which unlinks -shm) and retire(), the
    guard's private O_RDONLY -shm fd is still open. Another process's open-time scan must not read it
    as a live deleted generation; a process holding the deleted -shm O_RDWR (SQLite's) still counts."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    import hermes_state_dbfile as dbf

    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    code = (
        "import os, sys, time\n"
        f"fd = os.open({str(shm)!r}, {{'guard': os.O_RDONLY | os.O_NONBLOCK, 'ro': os.O_RDONLY, 'rw': os.O_RDWR}}[sys.argv[1]])\n"
        "print('open', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    # Prism fd548cf605f5: a plain O_RDONLY -shm is a real read-only SQLite reader and still counts;
    # only the guard's marked descriptor (O_RDONLY|O_NONBLOCK) is skipped.
    for mode, expect_holder in (("guard", False), ("ro", True), ("rw", True)):
        if not shm.exists():
            shm.write_bytes(b"\0" * 32768)
        child = subprocess.Popen([sys.executable, "-c", code, mode], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "open"
            os.unlink(shm)  # the clean last close's unlink, before the guard's retire()
            holders = [pid for pid, _t in dbf.iter_deleted_sqlite_sidecar_holders(db)]
            assert (child.pid in holders) is expect_holder, (mode, holders)
        finally:
            child.communicate("\n", timeout=10)


def test_failed_fstat_after_private_open_never_closes_the_descriptor(tmp_path, monkeypatch):
    """Prism 7e0266f152b2: an fstat failure right after the private open closed that descriptor,
    which cancels every POSIX lock this process holds on the inode (SQLite's own SHARED lock of a live
    connection included), so a foreign EXCLUSIVE then succeeded. The descriptor is quarantined."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    sqlite_fd = os.open(str(db), os.O_RDONLY)  # stands in for SQLite's descriptor + POSIX SHARED lock
    assert lg._ofd_lock(sqlite_fd, lg._F_RDLCK, *_SHARED, cmd=lg._F_SETLK)
    ident = (os.stat(db).st_dev, os.stat(db).st_ino)
    real_fstat, calls = os.fstat, []

    def failing_fstat(fd):
        calls.append(fd)
        raise OSError(5, "Input/output error")

    held = None
    try:
        monkeypatch.setattr(lg.os, "fstat", failing_fstat)
        held = lg.hold(db)
        monkeypatch.setattr(lg.os, "fstat", real_fstat)
        assert calls, "the private open's fstat was not reached"
        assert _foreign_exclusive(str(db), *_SHARED) == "REFUSED", "our POSIX lock was cancelled"
        assert lg._SURPLUS.get(ident), "the descriptor is quarantined, not closed"
    finally:
        monkeypatch.setattr(lg.os, "fstat", real_fstat)
        if held is not None:
            lg.release(held)
        with lg._LOCK:
            lg._close_private(ident)
        os.close(sqlite_fd)


def test_generation_ended_during_an_open_is_retired_once_the_opener_is_counted(tmp_path, monkeypatch):
    """Prism 201b553b8a1e: opener B passes the gated preflight, then sibling A's last close unlinks the
    -shm while B's opening registration pins A's private -shm fd. B connects to a NEW generation and
    drops its token, but nothing retired the completed old generation, so a foreign opener's
    deleted-generation scan saw this process holding a deleted -shm. B's open now retires it."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    pin_wal(monkeypatch)
    import hermes_state_dbfile as dbf
    db = tmp_path / "state.db"
    a = make_db(db, "s", "a")
    require_wal(a)
    old_shm = (os.stat(f"{db}-shm").st_dev, os.stat(f"{db}-shm").st_ino)
    real_conn = hermes_state.SessionDB._open_writer_conn
    fired = []

    def open_after_sibling_close(self):
        if not fired:
            fired.append(1)
            a.close()  # A's last close lands between B's preflight and B's connect
            assert old_shm in lg._FDS, "B's opening registration pins A's -shm fd (the race window)"
        return real_conn(self)

    monkeypatch.setattr(hermes_state.SessionDB, "_open_writer_conn", open_after_sibling_close)
    b = hermes_state.SessionDB(db_path=db)
    try:
        assert fired
        assert old_shm not in lg._FDS, "the completed generation's private -shm fd stays pinned"
        assert not any(str(h).endswith("-shm") for h in dbf.iter_deleted_sqlite_sidecar_holders(db))
    finally:
        b.close()



_GEN_CHILD = (
    "import os, sys\n"
    "shm = os.open(sys.argv[1] + '-shm', os.O_RDONLY | os.O_NONBLOCK)\n"  # the guard's private fd on the OLD -shm
    "wal = os.open(sys.argv[1] + '-wal', os.O_RDWR)\n"
    "print('open', flush=True)\n"
    "sys.stdin.readline()\n"  # parent retires the old generation
    "if sys.argv[2] == 'new':\n"
    "    os.close(wal)\n"
    "    wal = os.open(sys.argv[1] + '-wal', os.O_RDWR | os.O_CREAT)\n"  # a NEW generation's -wal
    "print('ready', flush=True)\n"
    "sys.stdin.readline()\n"
)


@pytest.mark.parametrize("wal_generation,expect_holder", [("new", False), ("old", True)])
def test_guard_only_shm_beside_a_current_generation_wal(tmp_path, wal_generation, expect_holder):
    """Prism 17cb68d1ff79: the Linux scan counted ANY -wal descriptor of the pid, so a process that had
    opened the NEW generation (live -wal) made its completed old guard -shm read as a live deleted
    generation, and a foreign opener was refused. Only a RETIRED -wal keeps a read-only -shm a holder:
    that is a writer stranded on the old generation."""
    import hermes_state_dbfile as dbf
    if not sys.platform.startswith("linux"):
        pytest.skip("/proc scan")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    Path(f"{db}-shm").write_bytes(b"\0" * 32768)
    Path(f"{db}-wal").write_bytes(b"\0" * 32)
    child = subprocess.Popen([sys.executable, "-c", _GEN_CHILD, str(db), wal_generation],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "open"
        os.unlink(f"{db}-wal")  # the old generation ends: SQLite unlinks -wal, then -shm
        os.unlink(f"{db}-shm")
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "ready"
        holders = [pid for pid, _t in dbf.iter_deleted_sqlite_sidecar_holders(db)]
        assert (child.pid in holders) is expect_holder, (wal_generation, holders)
    finally:
        child.communicate("\n", timeout=10)


@pytest.mark.parametrize("shm_flags,with_retired_wal,expect", [
    (0x5, False, []),               # guard: FREAD|FNONBLOCK -> skipped
    (0x1, False, ["-shm"]),         # read-only SQLite reader (no mark) -> holder (Prism fd548cf605f5)
    (0x3, False, ["-shm"]),         # SQLite's read-write -shm -> holder
    (0x5, True, ["-wal"]),          # the -wal of a stranded writer always counts
])
def test_darwin_scan_skips_a_completed_guard_only_shm(tmp_path, monkeypatch, shm_flags, with_retired_wal, expect):
    """Prism 87071f0f59e3: the macOS leg reported every retired sidecar descriptor, so the guard's
    private O_RDONLY -shm between a clean last close and retire() refused a concurrent opener. The
    darwin leg now applies the same guard-only rule as Linux, from libproc's fi_openflags."""
    import hermes_state_dbfile as dbf
    db = tmp_path / "state.db"
    base = os.path.realpath(str(db))
    fds = [(4242, 7, base + "-shm", (1, 100), shm_flags)]
    if with_retired_wal:
        fds.append((4242, 8, base + "-wal", (1, 101), 0x3))
    monkeypatch.setattr(dbf, "_iter_darwin_fd_targets_with_flags", lambda: iter(fds))
    monkeypatch.setattr(dbf, "_identity_is_truly_unlinked", lambda ident, path: True)
    got = sorted(t[-4:] for _p, t in dbf._iter_darwin_sidecar_holders(db))
    assert got == expect, got


def test_private_descriptor_carries_the_guard_mark(tmp_path):
    """The scan's positive evidence (Prism fd548cf605f5): every private descriptor is O_RDONLY with
    GUARD_FD_MARK, which SQLite never sets on a sidecar."""
    if not lg.supported():
        pytest.skip("no OFD locks")
    import fcntl
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    held = lg.hold(db)
    try:
        ident = (os.stat(db).st_dev, os.stat(db).st_ino)
        flags = fcntl.fcntl(lg._FDS[ident], fcntl.F_GETFL)
        assert flags & os.O_ACCMODE == os.O_RDONLY and flags & lg.GUARD_FD_MARK
    finally:
        lg.release(held)
