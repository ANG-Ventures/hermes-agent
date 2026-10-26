"""The WAL lock guard locks private descriptors; it never enumerates the fd table (t_2aad1d42).

2026-10-24, Mac Studio gateway: ``hold()`` scanned ``/dev/fd`` + one ``fstat`` per descriptor
INSIDE ``SessionDB._lock`` on every 50th write; under 12+ GIL-busy threads a 0.3 ms scan took
16-28 s and every Discord reply queued behind it (p50 103 s, max 963 s). Spec
``2026-10-24-state-db-write-lock-fd-scan-stall-spec.md`` §5.1-5.4: each SessionDB guards its own
private ``O_RDONLY`` fds (I1 preserved, I2 no enumeration, I3 O(1)), re-arms a replaced ``-shm`` on
one ``stat`` (§5.2), and a long ``_lock`` hold is logged (§5.4).
"""

from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hermes_state
import hermes_state_lockguard as lg
from hermes_state import SessionDB
from tests.hermes_state._wal_generation_harness import make_db, pin_wal, require_wal

pytestmark = pytest.mark.platforms("posix")

_REPO = Path(hermes_state.__file__).resolve().parent


def _foreign_exclusive(path: str, start: int, length: int) -> bool:
    """Another process tries the EXCLUSIVE a close-time WAL reset needs; True = nothing guards
    (a sidecar already unlinked by a true last close counts as free)."""
    if not os.path.exists(path):
        return True
    code = (
        "import fcntl, os, sys\n"
        f"sys.path.insert(0, {str(_REPO)!r})\n"
        "import hermes_state_lockguard as lg\n"
        f"fd = os.open({path!r}, os.O_RDWR)\n"
        f"print('ACQUIRED' if lg._ofd_lock(fd, fcntl.F_WRLCK, {start}, {length}) else 'REFUSED')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    return "ACQUIRED" in out


def _db_range_free(db_path) -> bool:
    return _foreign_exclusive(os.fspath(db_path), lg._SHARED_FIRST, lg._SHARED_SIZE)


def _shm_dms_free(db_path) -> bool:
    return _foreign_exclusive(os.fspath(db_path) + "-shm", lg._SHM_DMS_BYTE, 1)


@pytest.fixture
def wal_db(tmp_path, monkeypatch):
    if not lg.supported():
        pytest.skip("no OFD locks on this runtime")
    pin_wal(monkeypatch)
    db = make_db(tmp_path / "state.db", "s", "seed")
    require_wal(db)
    assert os.path.exists(f"{db.db_path}-shm")
    yield db
    db.close()


# R-1 (I1): the guard still refuses a foreign EXCLUSIVE on both ranges; close() lifts it.
def test_foreign_exclusive_refused_on_both_ranges_until_close(wal_db):
    db = wal_db
    assert not _db_range_free(db.db_path), "db shared range unguarded with a live writer"
    assert not _shm_dms_free(db.db_path), "-shm DMS byte unguarded with a live writer"
    db_ident, shm_ident = lg._identity(os.fspath(db.db_path)), lg._identity(f"{db.db_path}-shm")
    db_fd, shm_fd = lg._FDS[db_ident][0], lg._FDS[shm_ident][0]
    assert os.fstat(db_fd) and os.fstat(shm_fd), "guard is not on private descriptors"
    db.close()
    assert not db._wal_lock_guard and not lg._HANDLES
    assert not lg._FDS, "a private fd parked past count zero makes this process a phantom holder"
    for fd in (db_fd, shm_fd):
        with pytest.raises(OSError):
            os.fstat(fd)
    assert _db_range_free(db.db_path) and _shm_dms_free(db.db_path)
    SessionDB(db_path=db.db_path).close()  # a parked fd on a deleted -shm would read as a deleted-generation holder


def test_guard_survives_stray_close_and_a_second_handle_closing(wal_db):
    """The §2.2 case the module exists for, plus the sibling-handle case: another SessionDB on the
    same file closing (its own private fds) must not lift THIS handle's guard."""
    db = wal_db
    for name in ("state.db", "state.db-shm"):
        os.close(os.open(os.path.join(os.path.dirname(db.db_path), name), os.O_RDONLY))
    sibling = SessionDB(db_path=db.db_path)
    sibling.append_message("s", role="user", content="tab")
    sibling.close()
    assert not _db_range_free(db.db_path) and not _shm_dms_free(db.db_path)


def test_sibling_close_keeps_a_peer_transactions_wal_write_lock(wal_db):
    """Prism 975630a37100 / 4334da12494a on the first cut (one private fd PER INSTANCE): closing
    B's private -shm fd cancelled every POSIX lock this process held on that inode, including A's
    WAL write lock at byte 120 mid-transaction, and a foreign BEGIN IMMEDIATE then succeeded against
    A's open transaction (reproduced: ACQUIRED after B.close(), REFUSED before). One fd per inode,
    closed by the LAST handle only, keeps A's lock intact."""
    a = wal_db
    b = SessionDB(db_path=a.db_path)
    a._conn.execute("BEGIN IMMEDIATE")
    a._conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s','user','x',1.0)")
    try:
        assert not _foreign_exclusive(f"{a.db_path}-shm", 120, 1), "test premise: A holds the WAL write lock"
        b.close()
        assert not _foreign_exclusive(f"{a.db_path}-shm", 120, 1), "B.close() cancelled A's WAL write lock"
        assert not _foreign_begin_immediate(a.db_path), "a foreign writer got in under A's open transaction"
    finally:
        a._conn.rollback()


def test_handle_opened_during_a_siblings_close_keeps_its_wal_write_lock(wal_db, monkeypatch):
    """Prism a8c10e5114f6 on the counted cut: A's close() released the guard (count 0) and a
    deferred os.close of the retired fd ran AFTER B had opened on the same inode and begun a
    write, cancelling B's WAL write lock. close_fds() re-checks the count: B's count keeps the fd."""
    a = wal_db
    opened: dict = {}
    real_close = a._close_connection_quietly

    def _b_opens_and_writes_in_the_gap(conn):
        b = SessionDB(db_path=a.db_path)
        b._conn.execute("BEGIN IMMEDIATE")
        b._conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s','user','y',2.0)")
        opened["b"] = b
        real_close(conn)

    monkeypatch.setattr(a, "_close_connection_quietly", _b_opens_and_writes_in_the_gap)
    a.close()
    b = opened["b"]
    try:
        assert not _foreign_exclusive(f"{a.db_path}-shm", 120, 1), "A's close cancelled B's WAL write lock"
        assert not _foreign_begin_immediate(a.db_path)
    finally:
        b._conn.rollback()
        b.close()


def test_shm_fd_closes_with_the_last_counted_handle_so_a_foreign_last_close_never_strands_openers(
        tmp_path, monkeypatch):
    """R-6 (Apollo ruling, round 3; Prism 975630a37100 / 4334da12494a on the parked cut): A and B on
    one db; a foreign process ends the generation (new -shm inode); A checkpoints (arms the new
    inode) and closes (old inode count 1: B still holds it, not closed); B closes (count 0: closed);
    a third process then opens cleanly. A parked fd on the unlinked old -shm read as a
    deleted-generation holder to refuse_deleted_wal_generation and stranded every later opener."""
    if not lg.supported():
        pytest.skip("no OFD locks on this runtime")
    pin_wal(monkeypatch)
    a = make_db(tmp_path / "state.db", "s", "seed")
    require_wal(a)
    shm = f"{a.db_path}-shm"
    old_ident = lg._identity(shm)
    b = SessionDB(db_path=a.db_path)
    assert lg._HANDLES[old_ident] == 2
    # A foreign process ends the generation: a fresh -shm inode at the path.
    os.rename(shm, shm + ".retired")
    with open(shm, "wb") as handle:
        handle.write(b"\0" * 32768)
    new_ident = lg._identity(shm)
    a._try_wal_checkpoint()
    assert new_ident in a._wal_lock_guard and new_ident not in b._wal_lock_guard
    os.replace(shm + ".retired", shm)  # the real generation back: both close cleanly
    old_fd = lg._FDS[old_ident][0]
    a.close()
    assert lg._HANDLES[old_ident] == 1 and os.fstat(old_fd), "B still counts the old inode: its fd must stay open"
    assert new_ident not in lg._FDS, "A was the only counter of the new inode: closed at zero"
    b.close()
    assert old_ident not in lg._FDS and not lg._HANDLES
    with pytest.raises(OSError):
        os.fstat(old_fd)
    # The sidecars are gone (true last close) and nothing in this process holds a deleted inode.
    assert not os.path.exists(shm)
    assert "DeletedWalGenerationError" not in _third_process_open(a.db_path)
    SessionDB(db_path=a.db_path).close()  # and this process can open again too


def _third_process_open(db_path) -> str:
    code = (f"import sys\nsys.path.insert(0, {str(_REPO)!r})\n"
            "import hermes_state_wal\n"
            "hermes_state_wal.is_sqlite_wal_reset_vulnerable = lambda version_info=None: False\n"
            "hermes_state_wal.resolve_journal_mode = lambda: 'wal'\n"
            "from pathlib import Path\nfrom hermes_state import SessionDB\n"
            f"db = SessionDB(db_path=Path({os.fspath(db_path)!r})); db.append_message('s', role='user', content='third'); db.close()\n"
            "print('OPENED')\n")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env={**os.environ, "HERMES_STATE_DB_GUARD_BYPASS": "1"})
    assert "OPENED" in proc.stdout, proc.stderr[-2000:]
    return proc.stderr


def test_our_close_while_a_foreign_reader_is_open_leaves_no_shm_fd(wal_db):
    """The original 975630a37100 repro: P closes while Q (foreign) still has the db open, Q then
    does the true last close and unlinks -shm; P must not be left holding the unlinked inode."""
    db = wal_db
    shm_ident = lg._identity(f"{db.db_path}-shm")
    q = subprocess.Popen([sys.executable, "-c",
                          f"import sqlite3, sys\nc = sqlite3.connect({os.fspath(db.db_path)!r})\n"
                          "c.execute('select count(*) from messages').fetchone()\n"
                          "sys.stdout.write('open\\n'); sys.stdout.flush(); sys.stdin.readline(); c.close()\n"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert q.stdout.readline().strip() == "open"
        db.close()
        assert shm_ident not in lg._FDS and not lg._HANDLES
    finally:
        q.stdin.write("x\n")
        q.stdin.flush()
        q.wait(timeout=20)
    assert not os.path.exists(f"{db.db_path}-shm"), "test premise: Q's last close unlinked -shm"
    SessionDB(db_path=db.db_path).close()  # would raise DeletedWalGenerationError with a parked fd


def test_lockguard_closes_descriptors_only_at_count_zero_after_the_connection():
    """Structural half: a close of a descriptor on state.db or -shm cancels the process's POSIX locks
    on it, so ONE function closes fds: _close_all, called only for an inode no handle counts, from
    close_fds() (after the releasing connection closed) and the retired sweep (path no longer names
    the inode). Every other os.close this module ever had was a P1 (975630a37100, 30c639c0af16,
    a8c10e5114f6), including _arm closing a duplicate fd (it drops SQLite's own locks)."""
    tree = ast.parse((_REPO / "hermes_state_lockguard.py").read_text(encoding="utf-8"))
    closers = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
               for n in ast.walk(fn)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "close"}
    assert closers == {"_close_all"}, closers


def _foreign_begin_immediate(db_path) -> bool:
    code = (f"import sqlite3\nc = sqlite3.connect({os.fspath(db_path)!r}, timeout=0)\n"
            "try:\n    c.execute('BEGIN IMMEDIATE'); print('ACQUIRED')\n"
            "except sqlite3.OperationalError:\n    print('REFUSED')\n")
    return "ACQUIRED" in subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout


# R-2 (I2): no fd-table enumeration anywhere in the guard, and none reachable from the write path.
_ENUMERATION = ("_own_fds_for", "/dev/fd", "/proc/self/fd", "proc_pidinfo", "open_files", "listdir")


def test_lockguard_module_has_no_fd_enumeration():
    source = (_REPO / "hermes_state_lockguard.py").read_text(encoding="utf-8")
    for needle in _ENUMERATION:
        assert needle not in source, f"{needle!r} is back in hermes_state_lockguard.py"
    assert not hasattr(lg, "_own_fds_for")
    assert source.count("\n") < 300


def test_write_path_reaches_no_fd_enumeration():
    """Every name called from _execute_write/_try_wal_checkpoint (transitively, within
    hermes_state.py + the guard) is free of os.listdir / fd-table reads."""
    funcs = {}
    for module in ("hermes_state.py", "hermes_state_lockguard.py"):
        tree = ast.parse((_REPO / module).read_text(encoding="utf-8"))
        funcs.update({n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))})
    seen, todo = set(), ["_execute_write", "_try_wal_checkpoint", "_lock_hold_gauge"]
    while todo:
        name = todo.pop()
        if name in seen or name not in funcs:
            continue
        seen.add(name)
        for node in ast.walk(funcs[name]):
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("listdir", "scandir", "open_files", "proc_pidinfo"), f"{name} -> {node.attr}"
                todo.append(node.attr)
            elif isinstance(node, ast.Name):
                todo.append(node.id)
    assert {"_execute_write", "_try_wal_checkpoint", "hold"} <= seen


# R-4 (§5.2): refresh is one stat when -shm is unchanged; a replaced -shm is re-armed exactly once.
def test_refresh_costs_one_stat_and_zero_opens_when_shm_unchanged(wal_db, monkeypatch):
    db = wal_db
    before = dict(db._wal_lock_guard)
    opens, stats, listed = [], [], []
    real_open, real_stat, real_listdir = os.open, os.stat, os.listdir
    monkeypatch.setattr(os, "open", lambda *a, **k: (opens.append(a[0]), real_open(*a, **k))[1])
    monkeypatch.setattr(os, "stat", lambda *a, **k: (stats.append(a[0]), real_stat(*a, **k))[1])
    monkeypatch.setattr(os, "listdir", lambda *a, **k: (listed.append(a), real_listdir(*a, **k))[1])
    lg.refresh_shm(db.db_path, db._wal_lock_guard)
    monkeypatch.undo()
    assert opens == [] and listed == [], f"refresh opened {opens} / listed {listed} on an unchanged -shm"
    assert stats == [f"{db.db_path}-shm"], stats
    assert db._wal_lock_guard == before and lg._HANDLES == {i: 1 for i in before}


def test_replaced_shm_is_rearmed_once(wal_db, monkeypatch):
    db = wal_db
    shm = f"{db.db_path}-shm"
    old_ident = next(i for i, r in db._wal_lock_guard.items() if r == (lg._SHM_DMS_BYTE, 1))
    old_fd = lg._FDS[old_ident][0]
    # A foreign checkpoint ended the generation and a new opener minted a fresh -shm inode.
    os.rename(shm, shm + ".retired")
    with open(shm, "wb") as handle:
        handle.write(b"\0" * 32768)
    new_ident = lg._identity(shm)
    assert new_ident != old_ident
    opens = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda *a, **k: (opens.append(a[0]), real_open(*a, **k))[1])
    lg.refresh_shm(db.db_path, db._wal_lock_guard)
    lg.refresh_shm(db.db_path, db._wal_lock_guard)
    monkeypatch.undo()
    assert opens == [shm], f"expected exactly one open of the new -shm, got {opens}"
    assert new_ident in db._wal_lock_guard and old_ident in db._wal_lock_guard  # the old stays counted until release
    assert os.fstat(old_fd), "the stale fd must not be closed while its connection is open (it holds SQLite's POSIX locks)"
    assert not _shm_dms_free(db.db_path)
    os.replace(shm + ".retired", shm)  # let close() checkpoint the real generation cleanly
    new_fd = lg._FDS[new_ident][0]
    db.close()
    assert not lg._HANDLES
    assert new_ident not in lg._FDS and old_ident not in lg._FDS, "-shm fds at count zero were not closed"
    for fd in (old_fd, new_fd):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_refused_lock_keeps_the_fd_until_its_inode_is_retired(wal_db, monkeypatch):
    """Prism 30c639c0af16 + 5fa2be0eea10: a refused arm must not close the fd it just opened (the
    refusal may be this process's own POSIX lock, and closing drops SQLite's locks), and must not
    park it forever either. It stays uncounted; the next refresh re-uses it, or, once the path names
    another inode, the sweep closes it."""
    db = wal_db
    shm = f"{db.db_path}-shm"
    os.rename(shm, shm + ".retired")
    with open(shm, "wb") as handle:
        handle.write(b"\0" * 32768)
    monkeypatch.setattr(lg, "_ofd_lock", lambda *a, **k: False)
    lg.refresh_shm(db.db_path, db._wal_lock_guard)
    monkeypatch.undo()
    refused = lg._identity(shm)
    assert refused not in db._wal_lock_guard and refused in lg._FDS, "kept, not closed, on refusal"
    os.remove(shm)  # the foreign reset unlinks the inode we were refused on
    with open(shm, "wb") as handle:
        handle.write(b"\0" * 32768)
    lg.refresh_shm(db.db_path, db._wal_lock_guard)  # next checkpoint: arms the new one, sweeps the old
    assert refused not in lg._FDS, "a retired, uncounted fd is closed by the next refresh"
    assert lg._identity(shm) in db._wal_lock_guard
    os.replace(shm + ".retired", shm)

def test_shm_minted_after_open_is_armed_by_the_checkpoint_refresh(tmp_path, monkeypatch):
    if not lg.supported():
        pytest.skip("no OFD locks on this runtime")
    pin_wal(monkeypatch)
    db = make_db(tmp_path / "state.db", "s", "seed")
    require_wal(db)
    shm = f"{db.db_path}-shm"
    shm_ident = lg._identity(shm)
    db._wal_lock_guard.pop(shm_ident)  # as if -shm did not exist at open
    db._try_wal_checkpoint()
    assert shm_ident in db._wal_lock_guard
    db.close()


# R-5 (§5.4): a long _lock hold is logged once, naming the caller.
def test_long_lock_hold_logs_one_warning_naming_the_caller(wal_db, caplog, monkeypatch):
    db = wal_db
    monkeypatch.setattr(hermes_state, "_LOCK_HOLD_WARN_S", 0.3)

    def slow_fn(conn):
        time.sleep(0.5)
        return conn.execute("SELECT 1").fetchone()

    def record_slowly():  # the SessionDB method name the gauge should attribute the hold to
        return db._execute_write(slow_fn)

    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        caplog.clear()
        assert tuple(record_slowly()) == (1,)
        db._execute_write(lambda conn: conn.execute("SELECT 1").fetchone())  # fast: no warning
    warnings = [r for r in caplog.records if "SessionDB._lock held" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert "record_slowly" in warnings[0].getMessage() and warnings[0].levelno == logging.WARNING


def test_checkpoint_lock_hold_is_gauged(wal_db, caplog, monkeypatch):
    db = wal_db
    monkeypatch.setattr(hermes_state, "_LOCK_HOLD_WARN_S", 0.2)
    monkeypatch.setattr(lg, "refresh_shm", lambda *a, **k: time.sleep(0.4))
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        db._try_wal_checkpoint()
    msgs = [r.getMessage() for r in caplog.records if "SessionDB._lock held" in r.getMessage()]
    assert len(msgs) == 1 and "_try_wal_checkpoint" in msgs[0]


# Prism 49110a024c0e: release() must find count-zero fds by COUNT, not by the path string the
# releasing handle used. A handle opened under another spelling of the same db (symlink, relative
# path) counts onto the same inode; when it is the last one out, the fd closes.
def test_last_handle_under_another_path_spelling_still_closes_the_fds(wal_db, tmp_path):
    db = wal_db
    link = tmp_path / "alias"
    link.symlink_to(tmp_path)
    alias = link / "state.db"
    other = SessionDB(db_path=alias)
    other._execute_write(lambda c: c.execute("select 1").fetchone())  # make it a live writer
    db_ident, shm_ident = lg._identity(os.fspath(db.db_path)), lg._identity(f"{db.db_path}-shm")
    assert lg._HANDLES.get(db_ident) == 2 and lg._HANDLES.get(shm_ident) == 2, "same inode, two counts"
    db.close()  # first out: counts drop to 1, fds stay (other still holds them)
    assert db_ident in lg._FDS and shm_ident in lg._FDS
    other.close()  # last out, under the alias spelling
    assert db_ident not in lg._FDS and shm_ident not in lg._FDS
    assert not lg._HANDLES.get(db_ident) and not lg._HANDLES.get(shm_ident)


# Prism 5fa2be0eea10: a refused -shm arm leaves an uncounted fd; it must be closed by the next
# release on ANY handle in the process, not only one that spelled the same path.
def test_refused_arm_leftover_is_closed_by_the_next_release_under_any_spelling(wal_db, tmp_path, monkeypatch):
    db = wal_db
    shm = f"{db.db_path}-shm"
    os.rename(shm, shm + ".retired")
    with open(shm, "wb") as handle:
        handle.write(b"\0" * 32768)
    monkeypatch.setattr(lg, "_ofd_lock", lambda *a, **k: False)
    lg.refresh_shm(db.db_path, db._wal_lock_guard)
    monkeypatch.undo()
    leftover = lg._identity(shm)
    assert leftover in lg._FDS and not lg._HANDLES.get(leftover)
    link = tmp_path / "alias2"
    link.symlink_to(tmp_path)
    os.replace(shm + ".retired", shm)  # the foreign reset finished: the inode we refused is gone
    SessionDB(db_path=link / "state.db").close()  # a release under another spelling sweeps it
    assert leftover not in lg._FDS, "the uncounted leftover is closed by whichever release comes next"


def _foreign_truncate_checkpoint(db_path) -> bool:
    """A foreign wal_checkpoint(TRUNCATE) needs every reader's read-mark; True when it fully reset
    the WAL (which it must NOT while an in-process reader is mid-transaction)."""
    code = (f"import sqlite3\nc = sqlite3.connect({os.fspath(db_path)!r}, timeout=0)\n"
            "r = c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()\nprint('RESET' if r[0] == 0 else 'BUSY')\n")
    return "RESET" in subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout


# Prism 12297a6faa04 / f4694ff962e4: the handle count never saw the read pool. A pooled reader
# mid-transaction outlives close(); release()+close_fds() at "count zero" must not close the
# private -shm fd under it (that cancels the reader's POSIX read-mark lock, and a foreign TRUNCATE
# then resets the WAL under a live read). The fd closes when the reader finally closes.
def test_close_with_a_live_pool_reader_keeps_the_fds_until_the_reader_closes(wal_db):
    db = wal_db
    for i in range(300):
        db.append_message("s", role="user", content=f"m{i}")
    shm_ident = lg._identity(f"{db.db_path}-shm")
    ctx = db._read_ctx()
    reader = ctx.__enter__()  # pooled, mode=ro, outside _lock
    assert reader is not db._conn, "test premise: a pooled reader, not the writer"
    reader.execute("BEGIN")
    reader.execute("select count(*) from messages").fetchone()  # read transaction open: holds a read-mark
    db.close()  # last counted handle; the reader is still live
    assert shm_ident in lg._FDS, "fd must stay while a tracked connection is live"
    assert not _foreign_truncate_checkpoint(db.db_path), "foreign TRUNCATE must be refused under the live read"
    reader.execute("COMMIT")
    ctx.__exit__(None, None, None)  # late reader closes itself (close() already drained the pool)
    assert shm_ident not in lg._FDS, "the late reader's close released what close() had to leave"
