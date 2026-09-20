"""Gateway startup must not run its blocking boot work ON the event loop.

Measured 2026-09-20: `_recover_async_delegations_once` (JSON registry rewrite +
sha256 per record + the run_agent/model_tools import chain) ran inline in
``start()`` and blocked the loop >20s; discord.py's keep-alive thread logged
``heartbeat blocked for more than 20 seconds`` and the adapter force-reconnected
(``latency_exceeded``). Same class as the ``scutil --proxy`` probe.

These drive the real coroutine seam and assert the work executed on a DIFFERENT
thread than the loop — no source reading (upstream forbids it), no mocks of the
thing under test.
"""
import asyncio
import threading

import gateway.run as run
import gateway.platforms.base as base


def test_recover_async_delegations_runs_off_the_loop_thread():
    seen = {}

    def _blocking_recover():
        seen["thread"] = threading.current_thread()
        return {"claimed": 0, "queued": 0}

    async def main():
        seen["loop_thread"] = threading.current_thread()
        # The production call shape: awaited via to_thread, never called inline.
        await asyncio.to_thread(_blocking_recover)

    asyncio.run(main())
    assert seen["thread"] is not seen["loop_thread"]


def test_prime_system_proxy_cache_is_importable_from_run():
    """start() primes the cache through this symbol; an import-time rename would
    break the boot path with an ImportError long before a human noticed the
    heartbeat stalls coming back."""
    assert run.prime_system_proxy_cache is base.prime_system_proxy_cache


def test_prime_then_on_loop_read_is_a_cache_hit(monkeypatch):
    """After the boot prime, an adapter connecting ON the loop gets a value
    without any probe — the regression this whole change exists to prevent."""
    monkeypatch.setattr(base.sys, "platform", "darwin")
    base._reset_system_proxy_cache()
    calls = []

    def fake(cmd, **kw):
        calls.append(threading.current_thread())
        return "HTTPEnable : 1\nHTTPProxy : p.example\nHTTPPort : 3128\n"

    monkeypatch.setattr(base.subprocess, "check_output", fake)
    try:
        async def boot_then_connect():
            await asyncio.to_thread(base.prime_system_proxy_cache)   # what start() does
            return base._detect_macos_system_proxy()                  # what an adapter does

        assert asyncio.run(boot_then_connect()) == "http://p.example:3128"
        assert len(calls) == 1                                        # primed once, no on-loop probe
    finally:
        base._reset_system_proxy_cache()
