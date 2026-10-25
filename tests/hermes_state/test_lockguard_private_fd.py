"""Private-descriptor WAL lock guard (t_aeae3fe7).

``hold()`` used to find SQLite's own descriptors with ``listdir('/dev/fd')`` + one ``fstat`` per fd
under ``SessionDB._lock``; under GIL load that held the write lock 16-28 s and delayed Discord
replies by minutes. The guard now OFD-locks descriptors it opens itself. OFD locks conflict by inode
range, so the foreign-EXCLUSIVE refusal (the reason the guard exists) must be unchanged, including
after a stray in-process open()/close() (howtocorrupt §2.2). Linux-only: OFD locks + ``/proc``.
"""

import importlib
import logging
import os
import subprocess
import sys
import time

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
