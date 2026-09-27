"""t_cc8533d1: ``SessionStore._lock`` never covers SQLite or fsync.

On 2026-09-24 06:57-06:59 the gateway event loop waited ~100 s in
``_ensure_loaded`` for ``SessionStore._lock``.  A worker thread held it across
back-to-back routing saves, each spending SQLite's 20 s busy_timeout on a
contended ``state.db`` (errors.log: "state.db routing save failed: database is
locked" at 06:57:51, 06:58:12, 06:58:34, 06:59:00).  ``_save()`` ran inside
``with self._lock`` in ~25 methods.

These tests drive the real store against a real ``SessionDB`` and record, at
every state.db call and every ``os.fsync``, whether the calling thread holds
the store lock.  The second test reproduces the incident shape: a worker whose
routing write is stuck in SQLite must not stall another thread's
``_ensure_loaded``.
"""
from __future__ import annotations

import os
import threading
import time
import traceback

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore, _StoreLock


class _RecordingDB:
    """Proxy for SessionDB that records lock state at every method call."""

    def __init__(self, real, store_ref, violations, *, gate=None, gated=()):
        self._real = real
        self._store_ref = store_ref
        self._violations = violations
        self._gate = gate
        self._gated = set(gated)

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def _call(*args, **kwargs):
            store = self._store_ref()
            if store is not None and store._lock.held_by_current_thread():
                self._violations.append(
                    f"state.db.{name} via " + " <- ".join(
                        f"{f.name}:{f.lineno}"
                        for f in reversed(traceback.extract_stack()[-9:-1])
                    )
                )
            if self._gate is not None and name in self._gated:
                self._gate.wait(timeout=10)
            return attr(*args, **kwargs)

        return _call


def _source(chat_id: str = "12345") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="dm",
        user_id=chat_id,
    )


def _make_store(tmp_path, violations, **proxy_kwargs):
    from hermes_state import SessionDB

    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    if store._db is not None:
        store._db.close()
    real = SessionDB(db_path=tmp_path / "state.db")
    holder = {"store": store}
    store._db = _RecordingDB(real, lambda: holder.get("store"), violations, **proxy_kwargs)
    return store, real


@pytest.fixture
def fsync_violations(monkeypatch):
    violations: list[str] = []
    stores: list[SessionStore] = []
    real_fsync = os.fsync

    def _fsync(fd):
        for store in stores:
            if store._lock.held_by_current_thread():
                violations.append("os.fsync")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _fsync)
    return violations, stores


def test_store_lock_is_a_store_lock(tmp_path):
    """Guard the choke point itself: a plain Lock would re-open the class."""
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    assert isinstance(store._lock, _StoreLock)


def test_no_sqlite_or_fsync_under_store_lock(tmp_path, fsync_violations):
    violations: list[str] = []
    store, real = _make_store(tmp_path, violations)
    fs_violations, stores = fsync_violations
    stores.append(store)
    try:
        entry = store.get_or_create_session(_source())
        key = entry.session_key
        store.update_session(key, last_prompt_tokens=42)
        token = store.mark_turn_active(key)
        assert token
        assert store.clear_turn_active(key, token)
        store.mark_resume_pending(key, reason="restart_timeout")
        store.clear_resume_pending(key)
        store.suspend_session(key)
        store.reset_session(key)
        other = store.get_or_create_session(_source("67890"))
        store.update_session(other.session_key, last_prompt_tokens=7)

        # A fresh store over the same files: the cold load (state.db
        # gateway_routing + sessions.json) must also read outside the lock.
        store2, real2 = _make_store(tmp_path / "second", violations)
        stores.append(store2)
        store2._ensure_loaded()
        real2.close()
    finally:
        real.close()

    assert violations == [], violations
    assert fs_violations == [], fs_violations


def test_stuck_routing_write_does_not_stall_other_threads(tmp_path):
    """The 06:57 incident shape: SQLite stuck in a worker, loop needs the lock."""
    violations: list[str] = []
    gate = threading.Event()
    store, real = _make_store(
        tmp_path,
        violations,
        gate=gate,
        gated=("replace_gateway_routing_entries", "save_gateway_routing_entry"),
    )
    worker = None
    try:
        gate.set()
        entry = store.get_or_create_session(_source())
        key = entry.session_key
        gate.clear()

        worker = threading.Thread(
            target=store.mark_resume_pending, args=(key,), daemon=True,
        )
        worker.start()
        # Let the worker reach the gated SQLite write.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not store._entries[key].resume_pending:
            time.sleep(0.01)

        started = time.monotonic()
        acquired = store._lock.acquire(timeout=2)
        waited = time.monotonic() - started
        if acquired:
            store._lock.release()
        assert acquired and waited < 1.0, (
            f"store lock unavailable for {waited:.2f}s while a routing write "
            "sat in SQLite — _lock is spanning state.db I/O again"
        )
        assert worker.is_alive(), "worker was expected to still be inside SQLite"
    finally:
        gate.set()
        if worker is not None:
            worker.join(timeout=10)
        real.close()
    assert violations == []
