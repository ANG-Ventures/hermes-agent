"""The periodic checkpoint re-arms the WAL lock guard only when it can have changed (t_0d85826a).

2026-10-26, live gateway 3 min after boot: 13.8% of write-path samples were
``hermes_state_lockguard._own_fds_for`` (``os.listdir('/dev/fd')`` + one fstat per fd, ~500 fds) inside
``_try_wal_checkpoint``'s two ``hold()`` calls under ``SessionDB._lock``; writers parked behind it. The
guard only needs a refresh on the checkpoint connection's first use or when ``-shm`` has a new inode.
Linux-only: OFD locks + ``/proc``.
"""

import os
import shutil
import time

import pytest

import hermes_state_lockguard as lg
from tests.hermes_state._wal_generation_harness import make_db, pin_wal, require_wal

pytestmark = pytest.mark.platforms("linux")


class _TimedLock:
    """Wraps SessionDB._lock and records how long each ``with`` holds it."""

    def __init__(self, inner):
        self._inner, self.held_s = inner, []

    def __enter__(self):
        self._inner.acquire()
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.held_s.append(time.perf_counter() - self._t0)
        self._inner.release()
        return False

    def acquire(self, *a, **kw):
        return self._inner.acquire(*a, **kw)

    def release(self):
        self._inner.release()


@pytest.fixture()
def scans(monkeypatch):
    calls = []
    real = lg._own_fds_for

    def _spy(identities):
        calls.append(set(identities))
        return real(identities)

    monkeypatch.setattr(lg, "_own_fds_for", _spy)
    return calls


@pytest.fixture()
def db(tmp_path, monkeypatch):
    if not lg.supported():
        pytest.skip("no OFD locks on this runtime")
    pin_wal(monkeypatch)
    d = make_db(tmp_path / "state.db", "s", "seed")
    require_wal(d)
    assert d._wal_lock_guard, "writer opened without a WAL lock guard"
    yield d
    d.close()


def test_unchanged_shm_checkpoint_skips_the_fd_scan(db, scans):
    db._try_wal_checkpoint()  # first use: the checkpoint conn's fds must be covered
    assert len(scans) == 1
    assert db._ckpt_conn is not None
    scans.clear()
    fds = [os.open(os.devnull, os.O_RDONLY) for _ in range(500)]
    try:
        db._lock = timed = _TimedLock(db._lock)
        for _ in range(5):
            db._try_wal_checkpoint()
    finally:
        db._lock = timed._inner
        for fd in fds:
            os.close(fd)
    assert scans == [], f"unchanged -shm still scanned the fd table: {len(scans)} scans"
    assert timed.held_s and max(timed.held_s) < 0.005, f"_lock held {max(timed.held_s):.4f}s"


def test_new_shm_inode_rearms_the_guard_exactly_once(db, scans):
    db._try_wal_checkpoint()
    scans.clear()
    shm = f"{db.db_path}-shm"
    old = os.stat(shm).st_ino
    shutil.copyfile(shm, shm + ".new")
    os.replace(shm + ".new", shm)  # same path, new inode
    new_ident = (os.stat(shm).st_dev, os.stat(shm).st_ino)
    assert new_ident[1] != old
    fd = os.open(shm, os.O_RDWR)  # an in-process descriptor on the new inode for hold() to lock
    try:
        db._try_wal_checkpoint()
        assert len(scans) == 1, f"new -shm inode: expected one re-arm, got {len(scans)}"
        assert new_ident in db._wal_lock_guard
        db._try_wal_checkpoint()
        assert len(scans) == 1, "guard re-armed again with the -shm inode unchanged"
    finally:
        os.close(fd)
