"""The macOS system-proxy probe (``scutil --proxy``) must never run synchronously on
the asyncio event loop.

Measured 2026-09-20 on a busy gateway: a Telegram reconnect built its transport inside
``connect()``, which called ``resolve_proxy_url()`` -> ``_detect_macos_system_proxy()``
-> ``subprocess.check_output([\"scutil\", \"--proxy\"])`` ON THE LOOP. discord.py's
keep-alive thread logged ``heartbeat blocked for more than 10 seconds`` with that exact
traceback, the heartbeat ACK was never processed, and the Discord adapter force-reconnected
(``latency_exceeded``). One platform's reconnect took another platform down.

Contract: the probe result is cached (TTL); off-loop callers may probe synchronously;
an on-loop caller NEVER waits for the subprocess — it gets the cached value (stale or
None) and a background thread refreshes the cache.
"""
import asyncio
import threading
import time

import pytest

import gateway.platforms.base as base

_SCUTIL_OUT = "HTTPEnable : 1\nHTTPProxy : proxy.example\nHTTPPort : 8080\n"


@pytest.fixture(autouse=True)
def _darwin_and_clean_cache(monkeypatch):
    monkeypatch.setattr(base.sys, "platform", "darwin")
    base._reset_system_proxy_cache()
    yield
    base._reset_system_proxy_cache()


def _fake_scutil(calls, out=_SCUTIL_OUT, delay=0.0):
    def fake(cmd, **kw):
        assert cmd[:2] == ["scutil", "--proxy"]
        calls.append(threading.current_thread())
        if delay:
            time.sleep(delay)
        return out
    return fake


def test_probe_runs_once_and_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(base.subprocess, "check_output", _fake_scutil(calls))
    assert base._detect_macos_system_proxy() == "http://proxy.example:8080"
    assert base._detect_macos_system_proxy() == "http://proxy.example:8080"
    assert len(calls) == 1


def test_negative_result_is_cached_too(monkeypatch):
    calls = []
    monkeypatch.setattr(base.subprocess, "check_output", _fake_scutil(calls, out="HTTPEnable : 0\n"))
    assert base._detect_macos_system_proxy() is None
    assert base._detect_macos_system_proxy() is None
    assert len(calls) == 1


def test_cache_expires_after_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(base.subprocess, "check_output", _fake_scutil(calls))
    now = [1000.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: now[0])
    base._detect_macos_system_proxy()
    now[0] += base._SYSTEM_PROXY_CACHE_TTL_S + 1
    base._detect_macos_system_proxy()
    assert len(calls) == 2


def test_on_running_loop_cold_cache_never_waits_and_warms_in_background(monkeypatch):
    calls = []
    monkeypatch.setattr(base.subprocess, "check_output", _fake_scutil(calls, delay=0.3))

    async def main():
        loop_thread = threading.current_thread()
        t0 = time.monotonic()
        value = base._detect_macos_system_proxy()
        return value, time.monotonic() - t0, loop_thread

    value, elapsed, loop_thread = asyncio.run(main())
    assert value is None                      # cold cache: no answer yet...
    assert elapsed < 0.2                       # ...and the loop did not wait for scutil
    # The background refresh fills the cache. Read the CACHE, not the function —
    # calling it again here would be an off-loop call that legitimately probes
    # synchronously on this thread and would pollute `calls`.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and base._system_proxy_cache is None:
        time.sleep(0.02)
    assert base._system_proxy_cache is not None
    assert base._system_proxy_cache[1] == "http://proxy.example:8080"
    assert calls and all(th is not loop_thread for th in calls)


def test_on_running_loop_stale_cache_serves_stale_value_without_waiting(monkeypatch):
    calls = []
    monkeypatch.setattr(base.subprocess, "check_output", _fake_scutil(calls, delay=0.3))
    now = [1000.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: now[0])
    assert base._detect_macos_system_proxy() == "http://proxy.example:8080"   # off-loop: sync probe
    now[0] += base._SYSTEM_PROXY_CACHE_TTL_S + 1                               # expire it

    async def main():
        t0 = time.perf_counter()
        v = base._detect_macos_system_proxy()
        return v, time.perf_counter() - t0

    value, elapsed = asyncio.run(main())
    assert value == "http://proxy.example:8080"   # stale-while-revalidate
    assert elapsed < 0.2
    deadline = time.perf_counter() + 3.0
    while time.perf_counter() < deadline and len(calls) < 2:
        time.sleep(0.02)
    assert len(calls) == 2


def test_prime_before_loop_makes_first_on_loop_call_a_hit(monkeypatch):
    calls = []
    monkeypatch.setattr(base.subprocess, "check_output", _fake_scutil(calls))
    base.prime_system_proxy_cache()

    async def main():
        return base._detect_macos_system_proxy()

    assert asyncio.run(main()) == "http://proxy.example:8080"
    assert len(calls) == 1


def test_non_darwin_never_probes(monkeypatch):
    monkeypatch.setattr(base.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(base.subprocess, "check_output", _fake_scutil(calls))
    assert base._detect_macos_system_proxy() is None
    assert base.prime_system_proxy_cache() is None
    assert calls == []
