"""lcm.db must survive a kernel panic / power cut, and say so when it did not (t_6314cc2f).

2026-11-11 the Mac Studio panicked at 17:10:57 PST. lcm.db (34.5 GB, WAL,
synchronous=FULL, checkpoint_fullfsync=0) came back with ~58 torn pages at the
END of the file, the pages the last checkpoints wrote. On macOS fsync(2) does
not flush the drive cache, so a checkpoint's main-file writes were lost after
the WAL holding them had been reset. Nothing noticed for six hours.

* every LCM connection must read back checkpoint_fullfsync=1 (production path);
* after an unclean gateway exit the first open runs a bounded tail check and,
  when the tail is garbage, records the store as unhealthy for the engine and
  for monitors instead of continuing silently.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from plugins.context_engine.lcm import tail_check as T
from plugins.context_engine.lcm.dag import SummaryDAG
from plugins.context_engine.lcm.lifecycle_state import LifecycleStateStore
from plugins.context_engine.lcm.store import MessageStore


@pytest.fixture(autouse=True)
def _no_process_sentinel(monkeypatch):
    """Keep the real gateway process sentinel out of these tests."""
    monkeypatch.setattr(T, "_process_sentinel_path", lambda: None)


def _pragmas(conn) -> dict:
    return {
        name: conn.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("checkpoint_fullfsync", "fullfsync", "synchronous", "journal_mode")
    }


def test_every_lcm_writer_connection_reads_back_checkpoint_fullfsync(tmp_path):
    db = tmp_path / "lcm.db"
    store = MessageStore(db_path=str(db))
    dag = SummaryDAG(str(db))
    life = LifecycleStateStore(str(db))
    try:
        for owner in (store, dag, life):
            got = _pragmas(owner._conn)
            assert got["checkpoint_fullfsync"] == 1, (type(owner).__name__, got)
            # Per-commit F_FULLFSYNC is deliberately NOT enabled (cost; see PR body).
            assert got["fullfsync"] == 0, (type(owner).__name__, got)
            assert got["synchronous"] == 2, (type(owner).__name__, got)  # FULL
            assert str(got["journal_mode"]).lower() == "wal"
    finally:
        for owner in (store, dag, life):
            owner.close()


def _fill(db: Path, n: int = 4000) -> None:
    st = MessageStore(db_path=str(db))
    try:
        st.append_batch("s1", [{"role": "user", "content": f"message {i} " + "x" * 300} for i in range(n)])
        st._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        st.close()


def _mark_unclean(home: Path, pid: int = 4242) -> None:
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "gateway.lifecycle.json").write_text(json.dumps({
        "phase": "running", "pid": pid, "start_time": 1794460000.0 + pid, "prior_unclean_exit": True,
    }))


def _tear_tail(db: Path, pages: int) -> None:
    with sqlite3.connect(str(db)) as c:
        page_size = c.execute("PRAGMA page_size").fetchone()[0]
    size = db.stat().st_size
    with open(db, "r+b") as fh:
        fh.seek(size - pages * page_size)
        fh.write(os.urandom(pages * page_size))


def _open(db: Path):
    try:
        return MessageStore(db_path=str(db)), None
    except sqlite3.DatabaseError as exc:  # construction may also trip on the damage
        return None, exc


@pytest.mark.parametrize("use_dbpage", [True, False])
def test_unclean_exit_with_garbage_tail_marks_store_unhealthy(tmp_path, monkeypatch, use_dbpage):
    db = tmp_path / "lcm.db"
    _fill(db)
    _tear_tail(db, pages=8)
    _mark_unclean(tmp_path)
    if not use_dbpage:
        monkeypatch.setattr(T, "_has_dbpage", lambda conn: False)

    store, exc = _open(db)
    marker = T.read_store_health(tmp_path, db)
    assert marker is not None, "no health marker written"
    assert marker["status"] == "unhealthy", marker
    assert marker["bad_count"] >= 1 and marker["trigger"] == "unclean_exit"
    if store is not None:
        try:
            assert store.tail_health["status"] == "unhealthy"
        finally:
            store.close()


def test_unclean_exit_with_sound_tail_is_ok_and_runs_once_per_life(tmp_path, monkeypatch):
    db = tmp_path / "lcm.db"
    _fill(db)
    _mark_unclean(tmp_path)
    store = MessageStore(db_path=str(db))
    try:
        assert store.tail_health["status"] == "ok", store.tail_health
        assert store.tail_health["btrees_walked"] >= 3
    finally:
        store.close()

    calls = []
    real = T.check_tail
    monkeypatch.setattr(T, "check_tail", lambda conn, **kw: calls.append(1) or real(conn, **kw))
    MessageStore(db_path=str(db)).close()
    assert calls == [], "a second open in the same gateway life re-ran the check"
    _mark_unclean(tmp_path, pid=4343)  # next unclean life: runs again
    MessageStore(db_path=str(db)).close()
    assert calls == [1]


def test_clean_boot_skips_the_check(tmp_path, monkeypatch):
    db = tmp_path / "lcm.db"
    _fill(db)
    monkeypatch.setattr(T, "check_tail", lambda *a, **k: pytest.fail("check ran on a clean boot"))
    store = MessageStore(db_path=str(db))
    try:
        assert store.tail_health is None
    finally:
        store.close()
    assert not (tmp_path / "state" / "lcm-store-health.json").exists()


def test_failed_store_rechecks_on_every_open_and_clears_after_repair(tmp_path):
    db = tmp_path / "lcm.db"
    _fill(db)
    _mark_unclean(tmp_path)
    good = db.read_bytes()
    _tear_tail(db, pages=8)
    _open(db)
    assert T.read_store_health(tmp_path, db)["status"] == "unhealthy"

    db.write_bytes(good)  # the operator swaps in a repaired file, same gateway life
    store = MessageStore(db_path=str(db))
    try:
        assert store.tail_health["status"] == "ok"
    finally:
        store.close()
    assert T.read_store_health(tmp_path, db)["status"] == "ok"


def test_check_is_bounded(tmp_path):
    db = tmp_path / "lcm.db"
    _fill(db)
    conn = sqlite3.connect(str(db))
    try:
        out = T.check_tail(conn, vm_step_budget=1)
    finally:
        conn.close()
    assert out["status"] == "incomplete" and out["reason"] == "budget_exhausted", out


def test_zero_filled_overflow_page_is_not_corruption(tmp_path):
    """Prism 3fdba662487f: a zeroblob's last overflow page is all zeros and valid."""
    db = tmp_path / "z.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA page_size=4096")
        conn.execute("CREATE TABLE t(x BLOB)")
        conn.execute("INSERT INTO t VALUES(zeroblob(10000))")
        conn.commit()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        out = T.check_tail(conn)
    finally:
        conn.close()
    assert out["status"] == "ok", out


def test_marker_updates_from_concurrent_writers_are_not_lost(tmp_path, monkeypatch):
    """Prism 08cd32efb0c8: read-modify-replace is serialized across writers."""
    import threading

    real_read = T._read_json
    barrier = threading.Barrier(2, timeout=5)

    def slow_read(path):
        data = real_read(path)
        if path.name == "lcm-store-health.json":
            try:
                barrier.wait(timeout=0.5)  # without the lock both readers meet here
            except threading.BrokenBarrierError:
                pass
        return data

    monkeypatch.setattr(T, "_read_json", slow_read)
    threads = [
        threading.Thread(target=T._write_store_health, args=(tmp_path, tmp_path / f"{n}.db", {"status": st}))
        for n, st in (("a", "unhealthy"), ("b", "ok"))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert T.read_store_health(tmp_path, tmp_path / "a.db")["status"] == "unhealthy"
    assert T.read_store_health(tmp_path, tmp_path / "b.db")["status"] == "ok"


def test_secondary_profile_store_uses_the_process_sentinel(tmp_path, monkeypatch):
    """Prism b7f6231a4f6f: the unclean-exit trigger is the gateway PROCESS sentinel."""
    process_home, profile_home = tmp_path / "proc", tmp_path / "profiles" / "b"
    _mark_unclean(process_home)
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(T, "_process_sentinel_path",
                        lambda: process_home / "state" / "gateway.lifecycle.json")
    db = profile_home / "lcm.db"
    _fill(db)
    store = MessageStore(db_path=str(db), hermes_home=str(profile_home))
    try:
        assert store.tail_health is not None and store.tail_health["status"] == "ok"
    finally:
        store.close()
    # verdict is scoped to the store's own profile home
    assert T.read_store_health(profile_home, db)["status"] == "ok"
    assert T.read_store_health(process_home, db) is None
