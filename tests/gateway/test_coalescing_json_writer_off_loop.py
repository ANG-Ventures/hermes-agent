# fork-only: behavioural tests for the 2026-09-24 Discord loop-block fix.
"""``CoalescingJsonWriter`` + the Discord state trackers built on it.

Card t_eb49443b.  ``_DiscordRestartRecoveryState.mark_channel_active`` ran on
the outbound ``send`` coroutine and paid ``atomic_json_write`` (fsync +
``os.replace``) inline; under SSD contention that blocked the gateway loop up
to 20 s (46 ``event_loop_blocked`` in 40 min).  These tests pin:

* N marks -> 1 write (coalescing), with the LATEST state on disk;
* shutdown flush writes immediately regardless of the window;
* the mark never touches the disk on the calling thread, and a stalled rename
  does not stop the event loop (ordering witness, no stopwatch) -- with a
  gate-proof that the same barrier DOES stall the loop in the pre-fix shape.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading

import pytest

import utils
from gateway.platforms.helpers import CoalescingJsonWriter


class _CountingWrite:
    """Wrap ``atomic_json_write`` as seen by the helpers module; record thread."""

    def __init__(self, monkeypatch):
        import gateway.platforms.helpers as helpers

        self.calls = []
        real = utils.atomic_json_write

        def _w(path, data, **kw):
            self.calls.append((threading.get_ident(), json.loads(json.dumps(data))))
            return real(path, data, **kw)

        monkeypatch.setattr(helpers, "atomic_json_write", _w)


@pytest.fixture()
def counting(monkeypatch):
    return _CountingWrite(monkeypatch)


def _wait_writes(writer, n, timeout=5.0):
    ev = threading.Event()
    deadline = timeout
    while writer.writes < n and deadline > 0:
        ev.wait(0.01)
        deadline -= 0.01
    return writer.writes >= n


def test_n_schedules_inside_one_window_produce_one_write(tmp_path, counting):
    path = tmp_path / "s.json"
    state = {"n": 0}
    lock = threading.Lock()

    def snap():
        with lock:
            return dict(state)

    w = CoalescingJsonWriter(lambda: path, snap, min_interval_s=3600.0)
    # First schedule is the leading edge -> one write.
    with lock:
        state["n"] = 1
    w.schedule()
    assert w.wait_idle()
    assert w.writes == 1

    # 50 more marks inside the window: none of them hit disk.
    for i in range(2, 52):
        with lock:
            state["n"] = i
        w.schedule()
    # Give a wrongly-eager writer every chance to write.
    w.wait_idle(timeout=0.2)
    assert w.writes == 1
    assert json.loads(path.read_text()) == {"n": 1}

    # Shutdown flush: exactly one more write, carrying the LATEST state.
    w.close(flush=True)
    assert w.writes == 2
    assert json.loads(path.read_text()) == {"n": 51}
    assert len(counting.calls) == 2


def test_trailing_edge_writes_latest_state_after_interval(tmp_path):
    path = tmp_path / "s.json"
    state = {"v": "a"}
    w = CoalescingJsonWriter(lambda: path, lambda: dict(state), min_interval_s=0.2)
    w.schedule()
    assert w.wait_idle()
    for v in ("b", "c", "d"):
        state["v"] = v
        w.schedule()
    # Coalesced trailing write lands once the interval elapses.
    assert w.wait_idle(timeout=5.0)
    assert w.writes == 2
    assert json.loads(path.read_text()) == {"v": "d"}
    w.close(flush=False)


def test_mark_channel_active_never_writes_on_the_calling_thread(tmp_path, monkeypatch, counting):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.platforms.discord.adapter import _DiscordRestartRecoveryState

    s = _DiscordRestartRecoveryState(persist_interval_s=3600.0)
    caller = threading.get_ident()
    for i in range(100):
        s.mark_channel_active(f"c{i % 7}", now=1000.0 + i)
    assert _wait_writes(s._writer, 1)
    s._writer.wait_idle(timeout=0.2)  # a wrongly-eager writer would write again
    assert counting.calls, "leading-edge background write never happened"
    assert all(tid != caller for tid, _ in counting.calls), (
        "mark_channel_active wrote on the caller's thread (the event loop)"
    )
    assert len(counting.calls) == 1  # 100 marks -> 1 write

    # Graceful shutdown: flush stamps the anchor and lands the latest map.
    s.flush(shutdown_ts=2000.0)
    data = json.loads(
        (tmp_path / "gateway" / "discord_restart_recovery.json").read_text()
    )
    assert data["shutdown_ts"] == 2000.0
    assert set(data["active_channels"]) == {f"c{i}" for i in range(7)}
    assert data["active_channels"]["c1"] == 1000.0 + 99


def test_nonconversational_mark_many_coalesces_and_flushes(tmp_path, monkeypatch, counting):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.platforms.discord.adapter import _DiscordNonConversationalMessageTracker

    t = _DiscordNonConversationalMessageTracker(persist_interval_s=3600.0)
    for i in range(30):
        t.mark_many([str(i)])
        assert str(i) in t  # read-after-mark is synchronous
    assert _wait_writes(t._writer, 1)
    t._writer.wait_idle(timeout=0.2)
    assert len(counting.calls) == 1
    t.flush()
    assert len(counting.calls) == 2
    assert _DiscordNonConversationalMessageTracker()._ids.keys() == {str(i) for i in range(30)}


class _HeldReplace:
    def __init__(self, monkeypatch):
        self.gate = threading.Event()
        self.entered = threading.Event()
        real = os.replace

        def _blocking(src, dst, *a, **kw):
            self.entered.set()
            self.gate.wait(timeout=10.0)
            return real(src, dst, *a, **kw)

        monkeypatch.setattr(os, "replace", _blocking)


async def _loop_ticks_while_rename_is_held(held, do_mark):
    released = threading.Event()
    order = {}

    async def sibling():
        await asyncio.sleep(0)
        order["ticked_before_release"] = not released.is_set()

    def _release_later():
        held.entered.wait(5.0)
        # Give the loop a real chance to run the sibling before releasing.
        threading.Event().wait(0.3)
        released.set()
        held.gate.set()

    threading.Thread(target=_release_later, daemon=True).start()
    task = asyncio.create_task(sibling())
    do_mark()
    await task
    return order["ticked_before_release"]


def test_stalled_rename_does_not_stall_the_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.platforms.discord.adapter import _DiscordRestartRecoveryState

    s = _DiscordRestartRecoveryState(persist_interval_s=0.0)
    held = _HeldReplace(monkeypatch)
    assert asyncio.run(
        _loop_ticks_while_rename_is_held(held, lambda: s.mark_channel_active("x"))
    ), "the loop could not run another task while the rename was held"
    held.gate.set()
    assert s._writer.wait_idle()


def test_gate_proof_inline_write_does_stall_the_loop(tmp_path, monkeypatch):
    """Same barrier, pre-fix shape (inline atomic_json_write): loop is stuck."""
    held = _HeldReplace(monkeypatch)
    path = tmp_path / "x.json"
    assert not asyncio.run(
        _loop_ticks_while_rename_is_held(
            held, lambda: utils.atomic_json_write(path, {"a": 1})
        )
    )
