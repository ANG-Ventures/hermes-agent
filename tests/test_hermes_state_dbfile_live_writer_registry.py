"""Open-path deleted-WAL refusal decides from live writers' recorded sidecars (t_69e10010).

``refuse_deleted_wal_generation`` runs on every writable SessionDB open. It used to enumerate
the fd table (listdir + one stat per descriptor, each dropping the GIL), which stalled a live
gateway for 3-28 s under GIL contention. The default ``registry`` mode must keep the refusal
contract while touching neither ``os.listdir`` nor ``os.fstat``; the default ``registry+scan``
consults the registry first and keeps the cross-process scan after it.
"""

import os
from pathlib import Path

import pytest

import hermes_state_dbfile
from hermes_state import DeletedWalGenerationError, SessionDB
from hermes_state_dbfile import refuse_deleted_wal_generation
from tests.hermes_state._wal_generation_harness import (
    lose_sidecars, make_db, pin_wal, require_wal,
)


@pytest.fixture
def force_wal(monkeypatch):
    pin_wal(monkeypatch)
    monkeypatch.delenv(hermes_state_dbfile.DELETED_WAL_CHECK_ENV, raising=False)


@pytest.fixture
def registry_mode(monkeypatch):
    monkeypatch.setenv(hermes_state_dbfile.DELETED_WAL_CHECK_ENV, "registry")


@pytest.fixture
def fd_scan_counters(monkeypatch):
    """Count every os.listdir / os.fstat call (the fd-table scan's two primitives)."""
    counts = {"listdir": 0, "fstat": 0}
    real_listdir, real_fstat = os.listdir, os.fstat

    def listdir(*a, **k):
        counts["listdir"] += 1
        return real_listdir(*a, **k)

    def fstat(*a, **k):
        counts["fstat"] += 1
        return real_fstat(*a, **k)

    def arm():
        monkeypatch.setattr(os, "listdir", listdir)
        monkeypatch.setattr(os, "fstat", fstat)
        return counts

    return arm


def _recreate_sidecars(path: Path) -> None:
    """Unlink the live writer's -wal/-shm and put DIFFERENT inodes at the same paths."""
    base = os.fspath(path)
    before = {s: os.stat(base + s).st_ino for s in ("-wal", "-shm") if os.path.exists(base + s)}
    lose_sidecars(path, rename=False)
    for suffix in ("-wal", "-shm"):
        Path(base + suffix).write_bytes(b"")
    for suffix, ino in before.items():
        assert os.stat(base + suffix).st_ino != ino


def test_refuse_raises_for_in_process_writer_with_stale_sidecars_without_fd_scan(
        tmp_path, force_wal, registry_mode, fd_scan_counters):
    path = tmp_path / "state.db"
    writer = make_db(path, "s", "held")
    require_wal(writer)
    _recreate_sidecars(path)
    try:
        counts = fd_scan_counters()
        with pytest.raises(DeletedWalGenerationError, match="deleted state.db-wal"):
            refuse_deleted_wal_generation(path)
        assert counts == {"listdir": 0, "fstat": 0}
    finally:
        writer.close()


def test_refuse_passes_when_writer_sidecars_are_current_without_fd_scan(
        tmp_path, force_wal, registry_mode, fd_scan_counters):
    path = tmp_path / "state.db"
    writer = make_db(path, "s", "held")
    require_wal(writer)
    try:
        counts = fd_scan_counters()
        refuse_deleted_wal_generation(path)  # must not raise
        assert counts == {"listdir": 0, "fstat": 0}
    finally:
        writer.close()


def test_refuse_passes_after_the_stale_writer_closes(tmp_path, force_wal):
    path = tmp_path / "state.db"
    writer = make_db(path, "s", "held")
    require_wal(writer)
    writer.close()
    for suffix in ("-wal", "-shm"):  # whatever the close left, replace it with new inodes
        side = Path(os.fspath(path) + suffix)
        side.unlink(missing_ok=True)
        side.write_bytes(b"")
    refuse_deleted_wal_generation(path)  # closed cleanly: holds nothing


def test_second_sessiondb_open_refuses_through_registry(tmp_path, force_wal):
    """End to end: the open path itself refuses, and no new WAL is minted."""
    path = tmp_path / "state.db"
    writer = make_db(path, "s", "held")
    require_wal(writer)
    lose_sidecars(path, rename=False)
    try:
        with pytest.raises(DeletedWalGenerationError):
            SessionDB(db_path=path)
        assert not Path(os.fspath(path) + "-wal").exists()
    finally:
        writer.close()


def test_kill_switch_off_and_scan(tmp_path, force_wal, monkeypatch):
    path = tmp_path / "state.db"
    writer = make_db(path, "s", "held")
    require_wal(writer)
    _recreate_sidecars(path)
    try:
        monkeypatch.setenv(hermes_state_dbfile.DELETED_WAL_CHECK_ENV, "off")
        refuse_deleted_wal_generation(path)  # skipped entirely
        monkeypatch.setenv(hermes_state_dbfile.DELETED_WAL_CHECK_ENV, "scan")
        scanned = []
        monkeypatch.setattr(hermes_state_dbfile, "iter_deleted_sqlite_sidecar_holders",
                            lambda p: scanned.append(p) or [(os.getpid(), "x-wal")])
        with pytest.raises(DeletedWalGenerationError):
            refuse_deleted_wal_generation(path)
        assert scanned == [path]
        monkeypatch.setenv(hermes_state_dbfile.DELETED_WAL_CHECK_ENV, "bogus")
        with pytest.raises(DeletedWalGenerationError):  # unknown value -> registry+scan default
            refuse_deleted_wal_generation(path)
        # the in-process registry already found the stale writer: the scan was not needed
        assert scanned == [path]
    finally:
        writer.close()


def test_default_runs_cross_process_scan_when_registry_is_clean(tmp_path, force_wal, monkeypatch):
    """Default registry+scan: no in-process stale writer, so the cross-process scan decides
    (a foreign holder of a deleted generation is still refused at open)."""
    path = tmp_path / "state.db"
    writer = make_db(path, "s", "held")
    require_wal(writer)
    try:
        scanned = []
        monkeypatch.setattr(hermes_state_dbfile, "iter_deleted_sqlite_sidecar_holders",
                            lambda p: scanned.append(p) or [(os.getpid() + 1, "foreign-wal")])
        with pytest.raises(DeletedWalGenerationError):
            refuse_deleted_wal_generation(path)
        assert scanned == [path]
        monkeypatch.setattr(hermes_state_dbfile, "iter_deleted_sqlite_sidecar_holders",
                            lambda p: scanned.append(p) or [])
        refuse_deleted_wal_generation(path)  # clean everywhere: passes
        assert scanned == [path, path]
        monkeypatch.setenv(hermes_state_dbfile.DELETED_WAL_CHECK_ENV, "registry")
        refuse_deleted_wal_generation(path)  # opt-in in-process only: no scan
        assert scanned == [path, path]
    finally:
        writer.close()


def test_registry_snapshot_released_outside_lock(tmp_path, force_wal, registry_mode, monkeypatch):
    """An owner whose last strong reference is the check's snapshot finalizes after the lock is
    released, so its __del__ -> unregister_live_writer() cannot self-deadlock (Prism 89cdde946d88)."""
    import gc
    path = tmp_path / "state.db"
    events = []

    class Owner:
        db_path = path
        _db_sidecar_identity = {}

        def __del__(self):
            events.append(hermes_state_dbfile._LIVE_WRITERS_LOCK.locked())
            hermes_state_dbfile.unregister_live_writer(self)

    owner = Owner()
    hermes_state_dbfile.register_live_writer(owner)
    real = hermes_state_dbfile._live_writers

    calls = []

    def live_writers_dropping_last_ref():
        calls.append(1)
        if len(calls) > 1:  # the finalizer's unregister_live_writer() gets the real set
            return real()
        # Yields the owner into the snapshot list, THEN drops the only external reference
        # (thread B in the Prism repro): the snapshot is now the last strong reference.
        def gen():
            nonlocal owner
            for w in list(real()):
                yield w
            owner = None
        return gen()

    import threading
    monkeypatch.setattr(hermes_state_dbfile, "_live_writers", live_writers_dropping_last_ref)
    worker = threading.Thread(target=refuse_deleted_wal_generation, args=(path,), daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "refuse_deleted_wal_generation deadlocked on the registry lock"
    monkeypatch.setattr(hermes_state_dbfile, "_live_writers", real)
    gc.collect()
    assert events == [False]


def test_handle_retired_unclosed_keeps_its_generation_registered(tmp_path, monkeypatch):
    """close() that pins a lost-generation connection still holds its descriptors: the
    registry must keep refusing on its behalf after unregister."""
    monkeypatch.delenv(hermes_state_dbfile.DELETED_WAL_CHECK_ENV, raising=False)
    monkeypatch.setattr(hermes_state_dbfile, "_RETAINED_SIDECAR_GENERATIONS", [])
    path = tmp_path / "state.db"
    wal = Path(os.fspath(path) + "-wal")
    wal.write_bytes(b"old")
    st = wal.stat()

    class Owner:
        db_path = path
        _db_sidecar_identity = {"-wal": (st.st_dev, st.st_ino)}
        _connection_pinned = True

    owner = Owner()
    hermes_state_dbfile.register_live_writer(owner)
    hermes_state_dbfile.unregister_live_writer(owner)
    refuse_deleted_wal_generation(path)  # same inode still at the path
    with wal.open("rb"):  # the pinned connection's descriptor keeps the inode alive
        wal.unlink()
        wal.write_bytes(b"new")
        with pytest.raises(DeletedWalGenerationError):
            refuse_deleted_wal_generation(path)
