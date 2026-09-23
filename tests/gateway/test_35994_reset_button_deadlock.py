"""Regression test for #35994: Telegram /new confirm-button deadlock.

The /new confirmation button callback runs the slash-confirm handler on the
asyncio event loop (see GatewayRunner._request_slash_confirm). That handler
calls _handle_reset_command, which used to invoke the SYNCHRONOUS, potentially
long-blocking _cleanup_agent_resources (agent.close() tears down terminal
sandboxes / browser daemons / background processes; shutdown_memory_provider()
may make a network call) inline on the loop. A slow teardown wedged the entire
event loop, so the bot went silent until a manual restart.

The fix offloads _cleanup_agent_resources to a worker thread with a bounded
timeout, so the loop is never blocked and a stuck teardown degrades gracefully.
"""
import asyncio
import logging
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner_with_cached_agent(close_fn):
    """Build a bare GatewayRunner with a cached agent whose close() runs
    ``close_fn`` (used to simulate slow / blocking teardown)."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()

    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key, session_id="sess-old",
        created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.TELEGRAM, chat_type="dm",
    )
    new_entry = SessionEntry(
        session_key=session_key, session_id="sess-new",
        created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.TELEGRAM, chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.reset_session.return_value = new_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    # Enable the cache-lock path (this is what the button callback exercises)
    runner._agent_cache_lock = threading.RLock()
    agent = MagicMock()
    agent.close = close_fn
    agent.shutdown_memory_provider = MagicMock()
    runner._agent_cache = {session_key: agent}
    return runner


# Deadlock backstops, NOT assertions. Nothing below asserts on elapsed time or
# on a per-second rate: the test's verdict is the ORDERING witness
# ``dispatched_while_close_blocking``. These bounds only stop a regression from
# hanging the suite forever, so they are set far above any plausible scheduling
# latency on a loaded CI runner.
_WORKER_BLOCK_BACKSTOP_S = 30.0
_LOOP_DISPATCH_BACKSTOP_S = 20.0


@pytest.mark.asyncio
async def test_reset_does_not_block_event_loop_during_cleanup():
    """#35994: a slow agent.close() must NOT block the event loop.

    Ordering witness (deterministic; no tick counting, no stopwatch): close()
    schedules a callback onto the loop via ``call_soon_threadsafe`` and then
    blocks. The callback records whether close() had ALREADY RETURNED by the
    time the loop got around to dispatching it.

      * offloaded to a worker thread (the fix) -> the loop is free, the
        callback runs while close() is still blocking -> witness True.
      * run inline on the loop (the pre-fix bug) -> the loop cannot dispatch
        anything until close() returns, so the callback necessarily observes
        ``close_returned`` already set -> witness False.

    A loaded runner can delay the dispatch arbitrarily without changing that
    ordering, which is what makes this immune to the CI-load flake the tick
    count had.
    """
    loop = asyncio.get_running_loop()

    close_started = threading.Event()
    close_returned = threading.Event()
    loop_dispatched = asyncio.Event()
    release = threading.Event()
    witness: dict[str, bool | None] = {"dispatched_while_close_blocking": None}

    def _on_loop() -> None:
        # Runs ON the event loop. `close_returned` is the ordering fact.
        witness["dispatched_while_close_blocking"] = not close_returned.is_set()
        loop_dispatched.set()

    def slow_close():
        try:
            close_started.set()
            loop.call_soon_threadsafe(_on_loop)
            # Block until the test releases us. Bounded only so a regression
            # cannot wedge the suite forever.
            release.wait(timeout=_WORKER_BLOCK_BACKSTOP_S)
        finally:
            close_returned.set()

    runner = _make_runner_with_cached_agent(slow_close)

    reset_task = asyncio.create_task(
        runner._handle_reset_command(_make_event("/new"))
    )

    try:
        await asyncio.wait_for(
            loop_dispatched.wait(), timeout=_LOOP_DISPATCH_BACKSTOP_S
        )
    except asyncio.TimeoutError:
        release.set()
        await asyncio.gather(reset_task, return_exceptions=True)
        pytest.fail(
            "event loop was blocked during agent cleanup (#35994): a callback "
            "scheduled from inside close() was never dispatched"
        )

    assert close_started.is_set(), "close() never ran"

    release.set()
    await reset_task

    # The ORDERING witness is the verdict for #35994; assert it FIRST so a
    # regression reds on this line by name rather than on an adjacent
    # invariant (an inline cleanup also skips the housekeeping pool below).
    assert witness["dispatched_while_close_blocking"] is True, (
        "event loop was blocked during agent cleanup (#35994): the loop only "
        "dispatched the callback scheduled inside close() AFTER close() had "
        "already returned, i.e. cleanup ran inline on the loop"
    )

    # This path abandons the worker on timeout, so it must use the isolated
    # housekeeping pool. Running it on the turn pool reproduces the 2026-09-20
    # starvation: N wedged /new cleanups retire N turn slots indefinitely.
    assert getattr(runner, "_executor", None) is None
    housekeeping_pool = getattr(runner, "_housekeeping_executor", None)
    assert housekeeping_pool is not None
    assert len(housekeeping_pool._threads) == 1

    runner.session_store.reset_session.assert_called_once()


@pytest.mark.asyncio
async def test_reset_completes_when_cleanup_raises(caplog):
    """#35994: if the offloaded cleanup itself raises, the handler swallows it
    (logs a warning) and still rotates the session — it must not abort /new.

    Note: _cleanup_agent_resources swallows its own internal errors, so to
    exercise the handler's `except Exception` branch we make the cleanup call
    itself raise (patched on the instance), then assert the warning fired —
    proving the branch executed rather than the success path.
    """
    runner = _make_runner_with_cached_agent(lambda: None)

    def boom_cleanup(_agent):
        raise RuntimeError("cleanup blew up")

    runner._cleanup_agent_resources = boom_cleanup

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        result = await asyncio.wait_for(
            runner._handle_reset_command(_make_event("/new")), timeout=3
        )

    assert any(
        "failed during /new reset" in r.message and "#35994" in r.message
        for r in caplog.records
    ), "expected the cleanup-failure warning to be logged (except branch not hit)"
    runner.session_store.reset_session.assert_called_once()
    assert result is not None


@pytest.mark.asyncio
async def test_reset_completes_when_cleanup_times_out(caplog):
    """#35994: if cleanup exceeds the bounded timeout, the reset still completes
    (graceful degradation) and the timeout warning fires."""
    import gateway.slash_commands as _sc

    # Force the wait_for to time out immediately, closing the offloaded awaitable
    # so no worker thread dangles past the test.
    async def _instant_timeout(aw, timeout=None):
        if asyncio.iscoroutine(aw):
            aw.close()
        raise asyncio.TimeoutError

    runner = _make_runner_with_cached_agent(lambda: None)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        with patch.object(_sc.asyncio, "wait_for", _instant_timeout):
            result = await runner._handle_reset_command(_make_event("/new"))

    assert any(
        "exceeded" in r.message and "#35994" in r.message for r in caplog.records
    ), "expected the timeout warning to be logged"
    runner.session_store.reset_session.assert_called_once()
    assert result is not None
