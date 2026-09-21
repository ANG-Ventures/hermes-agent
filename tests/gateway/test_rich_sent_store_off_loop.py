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
import json
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
    stored = store_home / "state" / "rich_sent_index.json"
    for _ in range(200):
        if stored.exists():
            break
        await asyncio.sleep(0.01)
    assert stored.exists()
    assert rich_sent_store.lookup("12345", "678") == "morning briefing"


@pytest.mark.asyncio
async def test_record_async_ignores_empty_inputs(store_home):
    """Same no-op contract as the sync ``record``."""
    await rich_sent_store.record_async("12345", "678", None)
    await rich_sent_store.record_async("12345", None, "x")
    await rich_sent_store.record_async(None, "678", "x")
    assert rich_sent_store.lookup("12345", "678") is None


@pytest.mark.asyncio
async def test_record_async_does_not_wait_for_a_stalled_write(store_home, monkeypatch):
    """Adapter progress must not wait for best-effort index persistence."""
    real_replace = rich_sent_store.os.replace
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    loop = asyncio.get_running_loop()

    def stalled_replace(src, dst):
        loop.call_soon_threadsafe(write_started.set)
        asyncio.run_coroutine_threadsafe(release_write.wait(), loop).result()
        return real_replace(src, dst)

    monkeypatch.setattr(rich_sent_store.os, "replace", stalled_replace)

    loop.call_later(0.2, release_write.set)
    started = time.monotonic()
    await rich_sent_store.record_async("12345", "678", "slow write")
    elapsed = time.monotonic() - started
    assert elapsed < 0.1, f"adapter waited {elapsed:.3f}s for best-effort persistence"
    await asyncio.wait_for(write_started.wait(), timeout=1)
    stored = store_home / "state" / "rich_sent_index.json"
    for _ in range(100):
        if stored.exists():
            break
        await asyncio.sleep(0.01)
    assert stored.exists()


@pytest.mark.asyncio
async def test_record_async_ignores_default_executor_saturation(store_home, monkeypatch):
    """Unrelated default-executor work cannot hold adapter progress."""
    loop = asyncio.get_running_loop()
    release = loop.create_future()

    def saturated_run_in_executor(executor, func, *args):
        return release

    monkeypatch.setattr(loop, "run_in_executor", saturated_run_in_executor)
    started = time.monotonic()
    try:
        await asyncio.wait_for(
            rich_sent_store.record_async("12345", "saturated", "still returns"),
            timeout=0.1,
        )
    finally:
        if not release.done():
            release.cancel()
    elapsed = time.monotonic() - started
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_record_async_honors_the_profile_override(tmp_path, monkeypatch):
    """The caller resolves the active profile before handing work to the thread.

    In the single-process multi-profile runtime the profile boundary is the
    ``_HERMES_HOME_OVERRIDE`` ContextVar. The dedicated worker does not inherit
    each enqueueing caller's context, so ``record_async`` must capture the path
    while the caller's override is active.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.delenv("HERMES_HOME", raising=False)
    prof_b = tmp_path / "profB"
    (prof_b / "state").mkdir(parents=True)

    token = set_hermes_home_override(str(prof_b))
    try:
        await rich_sent_store.record_async("12345", "678", "profile B text")
        stored = prof_b / "state" / "rich_sent_index.json"
        for _ in range(200):
            if stored.exists():
                break
            await asyncio.sleep(0.01)
    finally:
        reset_hermes_home_override(token)

    assert stored.exists(), (
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
    path = store_home / "state" / "rich_sent_index.json"
    data = {}
    for _ in range(300):
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if all(data.get(f"12345:{i}", {}).get("t") == f"msg {i}" for i in range(25)):
            break
        await asyncio.sleep(0.01)
    assert all(data.get(f"12345:{i}", {}).get("t") == f"msg {i}" for i in range(25))
