"""A shutdown mark pass writes sessions.json ONCE, not once per running session (t_edfc146c).

Each per-session ``_save()`` rewrites the whole routing index; on the Studio (1.5 MB, 10 running
sessions) the two passes cost ~17 s of the 50 s stop budget and the shutdown watchdog fired.
"""
import asyncio
import logging

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource, SessionStore
from tests.gateway.restart_test_helpers import make_restart_runner

N_SESSIONS = 12


def _runner_with_sessions(tmp_path, monkeypatch, n=N_SESSIONS):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner, _adapter = make_restart_runner()
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    runner.session_store = store
    keys = []
    for i in range(n):
        entry = store.get_or_create_session(
            SessionSource(platform=Platform.TELEGRAM, chat_id=str(100 + i), user_id="u1"))
        keys.append(entry.session_key)
        runner._running_agents[entry.session_key] = object()
    saves = []
    real_save = store._save
    monkeypatch.setattr(store, "_save", lambda *a, **k: (saves.append(1), real_save(*a, **k))[1])
    return runner, store, keys, saves


@pytest.mark.parametrize("interrupted", [False, True], ids=["pre_drain", "post_drain"])
def test_one_save_per_pass(tmp_path, monkeypatch, caplog, interrupted):
    from gateway.run import GatewayRunner

    runner, store, keys, saves = _runner_with_sessions(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        marked = asyncio.run(GatewayRunner._mark_running_sessions_resume_pending(
            runner, "pre-drain mark_resume_pending", interrupted=interrupted))

    assert sorted(marked) == sorted(keys)
    assert len(saves) == 1, f"expected ONE sessions.json save for the pass, got {len(saves)}"
    # Every mark is in memory AND durable: a fresh store reading the same dir sees all of them.
    reloaded = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    reloaded._ensure_loaded()
    assert all(reloaded._entries[k].resume_pending for k in keys)
    # Per-session PHASE lines are kept; the pass logs its elapsed time.
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("PHASE=shutdown_mark" in m for m in msgs) == N_SESSIONS
    assert any(m.startswith("Shutdown phase: pre-drain mark_resume_pending marked 12 session(s) in ")
               for m in msgs), msgs


def test_failed_batched_save_does_not_abort_the_pass(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    runner, store, keys, _saves = _runner_with_sessions(tmp_path, monkeypatch, n=3)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_save", _boom)
    marked = asyncio.run(GatewayRunner._mark_running_sessions_resume_pending(runner, "mark"))
    assert sorted(marked) == sorted(keys)
    assert all(store._entries[k].resume_pending for k in keys)
    assert getattr(store, "_resume_mark_batch_depth", 0) == 0


def test_mark_outside_a_batch_still_saves_immediately(tmp_path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    entry = store.get_or_create_session(
        SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="u1"))
    calls = []
    real = store._save
    store._save = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    assert store.mark_resume_pending(entry.session_key, "restart_timeout") is True
    assert len(calls) == 1


def test_cancel_while_opening_the_batch_leaves_no_open_batch(tmp_path, monkeypatch):
    """Prism 55d3f612994e: a cancel while ``begin`` waits on the store lock must not leave the
    batch depth raised (that would silently defer every later resume-mark save)."""
    import threading
    from gateway.run import GatewayRunner

    runner, store, keys, _saves = _runner_with_sessions(tmp_path, monkeypatch, n=2)
    entered = threading.Event()
    real_begin = store.begin_resume_mark_batch

    def _slow_begin():
        entered.set()
        real_begin()

    monkeypatch.setattr(store, "begin_resume_mark_batch", _slow_begin)

    async def _scenario():
        store._lock.acquire()
        try:
            task = asyncio.create_task(GatewayRunner._mark_running_sessions_resume_pending(runner, "mark"))
            await asyncio.to_thread(entered.wait, 5)
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            store._lock.release()
        for _ in range(100):  # the begin thread finishes after the lock is released
            await asyncio.sleep(0.01)
            if not getattr(store, "_resume_mark_batch_depth", 0):
                break

    asyncio.run(_scenario())
    assert getattr(store, "_resume_mark_batch_depth", 0) == 0
    calls = []
    real = store._save
    store._save = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    assert store.mark_resume_pending(keys[0], "restart_timeout") is True
    assert len(calls) == 1, "a mark after the cancelled pass must still save immediately"


def test_stalled_replay_alert_does_not_delay_the_durable_save(tmp_path, monkeypatch):
    """Prism 353a5b148665: the replay alert is sent AFTER the batched save, so a notification
    that stalls until the stop deadline cannot keep the marks out of sessions.json."""
    from gateway.run import GatewayRunner

    runner, store, keys, _saves = _runner_with_sessions(tmp_path, monkeypatch, n=3)

    def _mark(sk, interrupted=False):
        store.mark_resume_pending(sk, "restart_timeout")
        return True, "restart_timeout", True

    durable_at_alert = []

    async def _notify(sk):
        reloaded = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
        reloaded._ensure_loaded()
        durable_at_alert.append(all(reloaded._entries[k].resume_pending for k in keys))
        await asyncio.sleep(3600)  # stalls until the process is killed

    runner._mark_resume_pending_for_shutdown = _mark
    runner._notify_restart_loop_suspended = _notify

    async def _scenario():
        task = asyncio.create_task(GatewayRunner._mark_running_sessions_resume_pending(runner, "mark"))
        for _ in range(500):
            await asyncio.sleep(0.01)
            if durable_at_alert:
                break
        task.cancel()  # the stop deadline
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_scenario())
    assert durable_at_alert == [True], durable_at_alert


def test_cancel_while_the_closing_save_is_queued_still_closes_the_batch(tmp_path, monkeypatch):
    """Prism 149729fd80f3: the closing ``end_resume_mark_batch`` is queued behind a busy executor
    when the stop task is cancelled. The cancel must not drop the queued close: the batch depth
    returns to 0 and the deferred marks reach sessions.json once the executor frees up."""
    import concurrent.futures
    import threading
    from gateway.run import GatewayRunner

    runner, store, keys, _saves = _runner_with_sessions(tmp_path, monkeypatch, n=3)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    blocker_started = threading.Event()
    release = threading.Event()

    def _blocker():
        blocker_started.set()
        release.wait(10)

    def _mark(sk, interrupted=False):
        store.mark_resume_pending(sk, "restart_timeout")
        if sk == keys[-1]:
            pool.submit(_blocker)  # saturates the only worker; the closing end queues behind it
        return True, "restart_timeout", False

    runner._mark_resume_pending_for_shutdown = _mark

    async def _scenario():
        asyncio.get_running_loop().set_default_executor(pool)
        task = asyncio.create_task(GatewayRunner._mark_running_sessions_resume_pending(runner, "mark"))
        for _ in range(500):
            await asyncio.sleep(0.01)
            if blocker_started.is_set():
                break
        await asyncio.sleep(0.05)  # the finally has queued end_resume_mark_batch behind the blocker
        assert getattr(store, "_resume_mark_batch_depth", 0) == 1
        task.cancel()  # the stop deadline
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        for _ in range(500):
            await asyncio.sleep(0.01)
            if not getattr(store, "_resume_mark_batch_depth", 0):
                break

    try:
        asyncio.run(_scenario())
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert getattr(store, "_resume_mark_batch_depth", 0) == 0
    reloaded = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    reloaded._ensure_loaded()
    assert all(reloaded._entries[k].resume_pending for k in keys)
