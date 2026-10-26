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
    fds = [fd for fd, _ in db._wal_lock_guard.values()]
    assert len(fds) == 2 and all(os.fstat(fd) for fd in fds), "guard is not on private descriptors"
    db.close()
    assert not db._wal_lock_guard
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)  # the private fds are closed with the handle
    assert _db_range_free(db.db_path) and _shm_dms_free(db.db_path)


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


# R-2 (I2): no fd-table enumeration anywhere in the guard, and none reachable from the write path.
_ENUMERATION = ("_own_fds_for", "/dev/fd", "/proc/self/fd", "proc_pidinfo", "open_files", "listdir")


def test_lockguard_module_has_no_fd_enumeration():
    source = (_REPO / "hermes_state_lockguard.py").read_text(encoding="utf-8")
    for needle in _ENUMERATION:
        assert needle not in source, f"{needle!r} is back in hermes_state_lockguard.py"
    assert not hasattr(lg, "_own_fds_for") and not hasattr(lg, "_HANDLES")
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
    lg.hold(db.db_path, db._wal_lock_guard)
    monkeypatch.undo()
    assert opens == [] and listed == [], f"hold() opened {opens} / listed {listed} on an unchanged -shm"
    assert len(stats) == 2 and stats[1] == f"{db.db_path}-shm"
    assert db._wal_lock_guard == before


def test_replaced_shm_is_rearmed_once(wal_db, monkeypatch):
    db = wal_db
    shm = f"{db.db_path}-shm"
    old_ident = next(i for i, (_, r) in db._wal_lock_guard.items() if r == (lg._SHM_DMS_BYTE, 1))
    old_fd = db._wal_lock_guard[old_ident][0]
    # A foreign checkpoint ended the generation and a new opener minted a fresh -shm inode.
    os.rename(shm, shm + ".retired")
    with open(shm, "wb") as handle:
        handle.write(b"\0" * 32768)
    new_ident = lg._identity(shm)
    assert new_ident != old_ident
    opens = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda *a, **k: (opens.append(a[0]), real_open(*a, **k))[1])
    lg.hold(db.db_path, db._wal_lock_guard)
    lg.hold(db.db_path, db._wal_lock_guard)
    monkeypatch.undo()
    assert opens == [shm], f"expected exactly one open of the new -shm, got {opens}"
    assert new_ident in db._wal_lock_guard and old_ident not in db._wal_lock_guard
    with pytest.raises(OSError):
        os.fstat(old_fd)  # the stale private fd was closed
    assert not _shm_dms_free(db.db_path)
    os.replace(shm + ".retired", shm)  # let close() checkpoint the real generation cleanly


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
    monkeypatch.setattr(lg, "hold", lambda *a, **k: time.sleep(0.4))
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        db._try_wal_checkpoint()
    msgs = [r.getMessage() for r in caplog.records if "SessionDB._lock held" in r.getMessage()]
    assert len(msgs) == 1 and "_try_wal_checkpoint" in msgs[0]
