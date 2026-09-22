"""The rich-sent index write must not block the event loop.

``rich_sent_store`` records ``(chat_id, message_id) -> text`` / ``-> media`` so
a reply to something we sent can be resolved on inbound. Every write is a
read-modify-write ending in ``atomic_json_write`` -> ``os.replace``, executed
inline on whatever thread calls it.

Every caller is on the send or inbound path inside an ``async def``
(``whatsapp_cloud.send``, ``_send_media_from_path_or_link``,
``_build_message_event_from_cloud``; telegram ``_try_send_rich``,
``_try_edit_rich``), so on the running loop that filesystem write stalls the
ENTIRE gateway -- every other adapter's polling, every in-flight turn, every
heartbeat -- for something nothing is waiting on. The index is best-effort by
construction (every operation already swallows errors and degrades to a no-op),
so the write belongs off the loop.

These tests are about OBSERVABLE BEHAVIOUR -- can the loop keep running while
a write is stalled, and does the adapter still make progress -- not about which
API was called.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading

import pytest

from gateway import rich_sent_store


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Point the store at a private path and reset its module state."""
    path = str(tmp_path / "state" / "rich_sent_index.json")
    monkeypatch.setattr(rich_sent_store, "_store_path", lambda: path)
    with rich_sent_store._PENDING_CONDITION:
        rich_sent_store._PENDING_WRITES.clear()
        rich_sent_store._RECENT_WRITES.clear()
    yield path


async def _drain(path: str, key: str, *, timeout: float = 10.0) -> dict:
    """Wait for the writer thread to land ``key`` durably, then return it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if key in data:
                return data[key]
        except (FileNotFoundError, ValueError):
            pass
        await asyncio.sleep(0.01)
    raise AssertionError(f"{key} never reached {path}")


@pytest.mark.asyncio
async def test_the_loop_keeps_running_while_an_index_write_is_stalled(
    _isolated_store, monkeypatch
):
    """The loop must make progress while a write sits in ``os.replace``.

    No wall-clock threshold: the write is held on a ``threading.Event`` until
    the loop has demonstrably ticked, and the assertion is on the tick COUNT.
    On the blocking shape the ticker cannot run at all, so the count is 0 --
    which is why the release comes from a watchdog thread, not from the loop.
    """
    entered = threading.Event()
    release = threading.Event()
    real_replace = os.replace

    def _stalling_replace(src, dst, *a, **k):
        if str(dst).endswith("rich_sent_index.json"):
            entered.set()
            release.wait(timeout=10)
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", _stalling_replace)

    ticks = 0
    stop = False

    async def _ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.005)

    def _watchdog() -> None:
        entered.wait(timeout=5)
        for _ in range(500):
            if ticks >= 5:
                break
            threading.Event().wait(0.01)
        release.set()

    ticker = asyncio.ensure_future(_ticker())
    threading.Thread(target=_watchdog, daemon=True).start()

    await rich_sent_store.record_async("chat", "m1", "hello")
    # The caller returns immediately; the write lands on the writer thread.
    await _drain(_isolated_store, "chat:m1")

    stop = True
    ticker.cancel()

    assert ticks >= 5, (
        f"the event loop ticked {ticks} times while a rich-sent index write "
        "was stalled in os.replace; the write is still on the loop"
    )


@pytest.mark.asyncio
async def test_a_stalled_write_does_not_hold_the_caller(_isolated_store, monkeypatch):
    """A blocked ``os.replace`` must not hold the awaiting adapter."""
    release = threading.Event()
    real_replace = os.replace

    def _stalling_replace(src, dst, *a, **k):
        if str(dst).endswith("rich_sent_index.json"):
            release.wait(timeout=10)
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", _stalling_replace)

    # Completes while the write is still blocked -- no timeout needed as an
    # oracle: if the caller were held, this await would not return at all.
    await asyncio.wait_for(
        rich_sent_store.record_async("chat", "m2", "text"), timeout=5
    )

    # The value is readable before it is durable, so an inbound reply that
    # references a message we only just sent still resolves.
    assert rich_sent_store.lookup("chat", "m2") == "text"

    release.set()
    await _drain(_isolated_store, "chat:m2")


@pytest.mark.asyncio
async def test_the_queued_write_is_durable_and_merges_like_the_sync_path(
    _isolated_store,
):
    """Off-loop writes must keep ``_update``'s merge semantics.

    ``record`` and ``record_media`` merge into one entry; a queued text write
    followed by a queued media write for the same message must not drop either.
    """
    await rich_sent_store.record_async("chat", "m3", "caption")
    await rich_sent_store.record_media_async("chat", "m3", [("/tmp/a.png", "image/png")])

    entry = await _drain(_isolated_store, "chat:m3")
    assert entry.get("t") == "caption", entry
    assert entry.get("m") == [["/tmp/a.png", "image/png"]], entry
    assert "ts" in entry


@pytest.mark.asyncio
async def test_repeated_writes_for_one_message_coalesce(_isolated_store):
    """Bursts for the same message must not queue unbounded work."""
    for i in range(50):
        await rich_sent_store.record_async("chat", "m4", f"v{i}")

    entry = await _drain(_isolated_store, "chat:m4")
    # Last write wins; the intermediate ones coalesced rather than each
    # costing a separate read-modify-write.
    assert entry.get("t") == "v49", entry
    assert rich_sent_store.lookup("chat", "m4") == "v49"


@pytest.mark.asyncio
async def test_a_failing_write_cannot_break_the_caller(_isolated_store, monkeypatch):
    """The index is best-effort: a write failure must not surface to the adapter."""
    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(rich_sent_store, "atomic_json_write", _boom)

    # Must not raise.
    await rich_sent_store.record_async("chat", "m5", "text")
    await rich_sent_store.record_media_async("chat", "m5", [("/tmp/b.png", "image/png")])


def test_the_sync_api_still_works(_isolated_store):
    """The synchronous entry points keep their exact contract.

    Non-vacuity floor: the off-loop variants are additive, so a change that
    broke the sync path would otherwise go unnoticed by the tests above.
    """
    rich_sent_store.record("chat", "m6", "sync text")
    rich_sent_store.record_media("chat", "m6", [("/tmp/c.png", "image/png")])

    assert rich_sent_store.lookup("chat", "m6") == "sync text"
    with open(_isolated_store, "r", encoding="utf-8") as fh:
        entry = json.load(fh)["chat:m6"]
    assert entry["t"] == "sync text"
    assert entry["m"] == [["/tmp/c.png", "image/png"]]
