import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg_adapter  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


@pytest.mark.asyncio
async def test_await_with_thread_deadline_abandons_and_runs_cleanup_on_timeout():
    """A wedged awaitable must raise TimeoutError promptly AND trigger the
    best-effort on_abandon cleanup (the httpx-pool-leak guard).

    This exercises the REAL _await_with_thread_deadline (not a monkeypatched
    stub), covering the abandonment + cleanup mechanism directly.

    DETERMINISTIC ORDERING WITNESS — replaces ``assert elapsed < 0.8``.
    Measured idle: 0.324 / 0.347 / 0.304s against a 0.8 bound — only ~2.3x of
    headroom over a 0.2s timeout, i.e. the ceiling was mostly measuring loop
    scheduling and would flip on a loaded runner with nothing wrong.

    The fact the stopwatch stood in for is ordering: the helper must return
    control while the cancellation-swallowing coroutine is STILL running.
    ``wedged_returned`` is set on every exit path of that coroutine and is
    asserted UNSET at the instant TimeoutError surfaces.
    """
    import asyncio as _asyncio

    cleanup_ran = _asyncio.Event()
    wedged_returned = _asyncio.Event()  # set on EVERY exit path of the wedged coro

    async def _wedged():
        # Swallows cancellation for a bounded window — long enough that the
        # helper must return control BEFORE this finishes (proving it doesn't
        # await cancellation, the #58236 shielded-scope behavior), but bounded
        # so the abandoned task can't outlive the test and wedge teardown.
        try:
            for _ in range(20):
                try:
                    await _asyncio.sleep(0.05)
                except _asyncio.CancelledError:
                    # Keep going despite cancellation, like the shielded scope.
                    pass
        finally:
            wedged_returned.set()

    async def _cleanup():
        cleanup_ran.set()

    with pytest.raises(_asyncio.TimeoutError):
        await tg_adapter._await_with_thread_deadline(
            _wedged(), timeout=0.2, on_abandon=_cleanup
        )

    assert not wedged_returned.is_set(), (
        "TimeoutError surfaced only AFTER the wedged awaitable finished — the "
        "helper awaited cancellation instead of abandoning the task"
    )
    # The detached cleanup was scheduled; give the loop a tick to run it.
    await _asyncio.wait_for(cleanup_ran.wait(), timeout=10.0)
    assert cleanup_ran.is_set()
    # Let the abandoned task finish so it can't outlive the test.
    await _asyncio.wait_for(wedged_returned.wait(), timeout=10.0)


@pytest.mark.asyncio
async def test_await_with_thread_deadline_cleanup_error_is_swallowed():
    """A cleanup that raises must not surface as an unhandled task error."""
    import asyncio as _asyncio

    async def _wedged():
        for _ in range(20):
            try:
                await _asyncio.sleep(0.05)
            except _asyncio.CancelledError:
                pass

    def _boom():
        raise RuntimeError("cleanup blew up")

    # Must still raise TimeoutError (not the cleanup error) and not crash.
    with pytest.raises(_asyncio.TimeoutError):
        await tg_adapter._await_with_thread_deadline(
            _wedged(), timeout=0.2, on_abandon=_boom
        )
    # Let the detached cleanup task run and be observed (no unraised error).
    await _asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_blocked_loop_after_expiry_dumps_diagnostics(monkeypatch):
    """#63309: when the loop thread is stuck in a synchronous call, the expiry
    callback never runs and every asyncio timeout goes silent. The off-loop
    watchdog must detect that state and emit diagnostics from its own thread."""
    import asyncio as _asyncio
    import time as _time

    from agent import deadline as _deadline

    dumps = []
    monkeypatch.setattr(
        _deadline,
        "_dump_blocked_loop_diagnostics",
        lambda label, timeout_s: dumps.append((label, timeout_s)),
    )
    monkeypatch.setattr(_deadline, "_LOOP_BLOCKED_DUMP_GRACE_S", 0.15)

    hung = _asyncio.get_running_loop().create_future()  # never completes
    task = _asyncio.ensure_future(
        tg_adapter._await_with_thread_deadline(hung, timeout=0.05)
    )
    # Let the helper start its deadline + watchdog timers…
    await _asyncio.sleep(0)
    # …then block the event loop straight through deadline (0.05s) AND the
    # watchdog grace (0.15s): call_soon_threadsafe stays queued, exactly like
    # a sync call pinning the loop during Application.initialize().
    # Margin matters: the watchdog thread only dumps if the loop is STILL
    # blocked when it wakes, and thread wakeup lags under parallel-suite load.
    # 0.2s (= deadline+grace exactly) flaked in a 40-worker full-suite run.
    _time.sleep(1.0)
    with pytest.raises(_asyncio.TimeoutError):
        await task

    assert dumps == [("telegram-init", 0.05)]
    hung.cancel()


