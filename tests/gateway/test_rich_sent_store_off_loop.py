# fork-only: upstream AGENTS.md forbids source-reading tests; do not port.
"""``rich_sent_store.record_async`` must not block the event loop.

The rich-sent index is written on the SEND path (telegram ``_try_send_rich`` /
``_try_edit_rich``, whatsapp ``send``) and on the INBOUND path (whatsapp
``_build_message_event_from_cloud``).  The write is a read-modify-write of one
JSON file finished by ``os.replace`` -- the exact shape that stalled Apollo's
event loop for 30s on 2026-09-20 from ``sessions.json``.  Four of those sites
were frozen in ``REACHABLE_BASELINE``; this module is the behavioural half of
their fix.

These are BEHAVIOURAL tests: they make the write genuinely slow and assert the
loop kept running, rather than asserting on the shape of the source.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from gateway import rich_sent_store


@pytest.fixture
def store_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_record_async_persists_and_is_readable(store_home):
    """The off-loop path must still actually write the entry."""
    await rich_sent_store.record_async("12345", "678", "morning briefing")
    assert rich_sent_store.lookup("12345", "678") == "morning briefing"


@pytest.mark.asyncio
async def test_record_async_ignores_empty_inputs(store_home):
    """Same no-op contract as the sync ``record``."""
    await rich_sent_store.record_async("12345", "678", None)
    await rich_sent_store.record_async("12345", None, "x")
    await rich_sent_store.record_async(None, "678", "x")
    assert rich_sent_store.lookup("12345", "678") is None


@pytest.mark.asyncio
async def test_record_async_keeps_the_loop_responsive(store_home, monkeypatch):
    """A SLOW write must not stall a concurrent coroutine.

    This is the assertion that fails without the fix: with a blocking
    ``record`` the heartbeat cannot tick while ``os.replace`` sleeps, so it
    records one tick.  Off-loop it keeps ticking throughout.
    """
    real_replace = rich_sent_store.os.replace

    def slow_replace(src, dst):
        time.sleep(0.5)
        return real_replace(src, dst)

    monkeypatch.setattr(rich_sent_store.os, "replace", slow_replace)

    ticks = 0
    stop = False

    async def heartbeat():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.01)

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)  # let the heartbeat get going
    before = ticks
    await rich_sent_store.record_async("12345", "678", "slow write")
    after = ticks
    stop = True
    await hb

    # 0.5s of blocking write / 0.01s tick interval: an off-loop write leaves
    # room for dozens of ticks, a blocking one allows at most a couple.
    assert after - before > 10, (
        f"loop stalled during the write: only {after - before} heartbeat ticks "
        "elapsed across a 0.5s record_async"
    )
    assert rich_sent_store.lookup("12345", "678") == "slow write"


@pytest.mark.asyncio
async def test_record_async_honors_the_profile_override(tmp_path, monkeypatch):
    """Moving the write off-loop resolves the store path in a WORKER THREAD.

    In the single-process multi-profile runtime the profile boundary is the
    ``_HERMES_HOME_OVERRIDE`` ContextVar, so a write that resolves its path on
    another thread could land in the launch profile's tree -- one profile's
    message text leaking into another's index.  ``asyncio.to_thread`` copies
    the context, which is exactly what makes this move safe; pin it.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.delenv("HERMES_HOME", raising=False)
    prof_b = tmp_path / "profB"
    (prof_b / "state").mkdir(parents=True)

    token = set_hermes_home_override(str(prof_b))
    try:
        await rich_sent_store.record_async("12345", "678", "profile B text")
    finally:
        reset_hermes_home_override(token)

    assert (prof_b / "state" / "rich_sent_index.json").exists(), (
        "record_async wrote outside the active profile's tree"
    )


@pytest.mark.asyncio
async def test_concurrent_record_async_does_not_lose_entries(store_home):
    """Running off-loop makes writes genuinely concurrent -- the lock holds.

    On the event loop the read-modify-write was implicitly serialized.  In the
    executor two writes can interleave and the second can clobber the first's
    entry; ``_WRITE_LOCK`` is what prevents it.
    """
    await asyncio.gather(*[
        rich_sent_store.record_async("12345", str(i), f"msg {i}")
        for i in range(25)
    ])
    for i in range(25):
        assert rich_sent_store.lookup("12345", str(i)) == f"msg {i}"
