"""The macOS system-proxy probe (``scutil --proxy``, a subprocess with a 3 s timeout) must never run
on the event loop: adapters reach it from ``connect()`` via ``resolve_proxy_url``, and a blocked loop
starves every other adapter (Discord's heartbeat ACK in particular)."""

import asyncio
import threading
import time

import pytest

from gateway.platforms import base

SCUTIL_OUT = "<dictionary> {\n  HTTPSEnable : 1\n  HTTPSProxy : 10.0.0.1\n  HTTPSPort : 3128\n}\n"


@pytest.fixture
def probe(monkeypatch):
    """A slow, counted fake ``scutil``; records the thread each probe ran on.

    The probe only runs on a real macOS host (``sys.platform == "darwin"``), so the tests that
    expect it to run carry ``platforms("macos")`` instead of faking the host OS.
    """
    base.reset_macos_proxy_cache()
    calls = []

    def fake_check_output(*_a, **_k):
        calls.append(threading.current_thread().name)
        time.sleep(0.3)
        return SCUTIL_OUT

    monkeypatch.setattr(base.subprocess, "check_output", fake_check_output)
    yield calls
    base.reset_macos_proxy_cache()


def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.platforms("macos")
def test_cold_memo_on_the_loop_returns_immediately_and_warms_off_thread(probe):
    async def scenario():
        loop_thread = threading.current_thread().name
        t0 = time.monotonic()
        value = base._detect_macos_system_proxy()
        return value, time.monotonic() - t0, loop_thread

    value, elapsed, loop_thread = asyncio.run(scenario())
    assert value is None  # cold: nothing to serve yet, and no inline probe
    assert elapsed < 0.2, f"loop caller waited {elapsed:.2f}s on the scutil probe"
    assert _wait_for(lambda: base._macos_proxy_cache is not None)
    assert probe and all(name != loop_thread for name in probe)
    assert base._detect_macos_system_proxy() == "http://10.0.0.1:3128"


@pytest.mark.platforms("macos")
def test_stale_memo_on_the_loop_is_served_while_revalidating(probe, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(base.time, "monotonic", lambda: clock["t"])
    assert base._detect_macos_system_proxy() == "http://10.0.0.1:3128"  # off-loop: synchronous probe
    assert len(probe) == 1
    clock["t"] += base._MACOS_PROXY_TTL_SECONDS + 1

    async def scenario():
        t0 = time.perf_counter()  # monotonic is frozen above
        return base._detect_macos_system_proxy(), time.perf_counter() - t0

    value, elapsed = asyncio.run(scenario())
    assert value == "http://10.0.0.1:3128"  # the stale value ...
    assert elapsed < 0.2, f"loop caller waited {elapsed:.2f}s revalidating inline"  # ... without waiting
    assert _wait_for(lambda: len(probe) == 2)


@pytest.mark.platforms("macos")
def test_off_loop_callers_keep_the_synchronous_probe(probe):
    assert base._detect_macos_system_proxy() == "http://10.0.0.1:3128"
    assert probe == [threading.current_thread().name]


@pytest.mark.platforms("macos")
def test_prime_then_on_loop_read_is_a_hit_with_no_probe(probe):
    assert base.prime_macos_proxy_cache() == "http://10.0.0.1:3128"

    async def scenario():
        return base._detect_macos_system_proxy()

    assert asyncio.run(scenario()) == "http://10.0.0.1:3128"
    assert len(probe) == 1


@pytest.mark.platforms("linux", "windows")
def test_non_darwin_never_probes(probe):
    assert base.prime_macos_proxy_cache() is None
    assert base._detect_macos_system_proxy() is None
    assert probe == []
