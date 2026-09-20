import asyncio
import time

import pytest

from hermes_cli.pty_session import RingBuffer


def test_ringbuffer_keeps_everything_under_capacity():
    rb = RingBuffer(10)
    rb.append(b"abc")
    rb.append(b"def")
    assert rb.snapshot() == b"abcdef"
    assert rb.truncated is False


def test_ringbuffer_drops_oldest_over_capacity():
    rb = RingBuffer(4)
    rb.append(b"abcdef")          # 6 bytes into a 4-byte buffer
    assert rb.snapshot() == b"cdef"
    assert rb.truncated is True




async def wait_until(predicate, *, timeout=10.0, what="condition"):
    """Await ``predicate()`` becoming true instead of sleeping a fixed span.

    The drain task runs on an executor thread, so a loaded machine can miss a
    fixed ``asyncio.sleep(0.05)`` budget and fail a test whose behaviour is
    correct. Polling the exact fact the assertion checks makes load cost time,
    not correctness; ``timeout`` is only a hang guard.
    """
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.005)


class FakeBridge:
    """Implements the bridge contract PtySession depends on."""

    def __init__(self, chunks):
        self._chunks = list(chunks)   # bytes; b"" = idle tick; None = EOF
        self.written = bytearray()
        self.closed = False
        self.resized = None

    def read(self, timeout):
        if not self._chunks:
            return b""                # idle
        return self._chunks.pop(0)

    def write(self, data):
        self.written.extend(data)

    def resize(self, cols, rows):
        self.resized = (cols, rows)

    def close(self):
        self.closed = True


class FakeWS:
    def __init__(self):
        self.sent = []               # list of ("bytes"|"text", payload)
        self.close_code = None

    async def send_bytes(self, data):
        self.sent.append(("bytes", bytes(data)))

    async def send_text(self, text):
        self.sent.append(("text", text))

    async def close(self, code=1000, reason=""):
        self.close_code = code


@pytest.mark.asyncio
async def test_attach_replays_buffer_then_streams_live():
    from hermes_cli.pty_session import PtySession
    bridge = FakeBridge([b"hello ", b"world", None])
    s = PtySession("k", bridge, buffer_cap=1024, read_timeout=0.01)
    await s.start()
    await wait_until(lambda: s.buffer.snapshot() == b"hello world",
                     what="the drain task to buffer 'hello world'")
    ws = FakeWS()
    await s.attach(ws)
    replay = b"".join(p for kind, p in ws.sent if kind == "bytes")
    assert replay == b"hello world"
    await s.close()


@pytest.mark.asyncio
async def test_reattach_can_force_complete_tui_redraw_after_replay():
    """A fresh terminal cannot reconstruct a differential ANSI tail alone."""
    from hermes_cli.pty_session import PtySession

    bridge = FakeBridge([b"partial differential frame", b""])
    s = PtySession("k", bridge, buffer_cap=1024, read_timeout=0.01)
    await s.start()
    await wait_until(lambda: s.buffer.snapshot() == b"partial differential frame",
                     what="the drain task to buffer the differential frame")

    ws = FakeWS()
    await s.attach(ws, force_redraw=True)

    replay = b"".join(p for kind, p in ws.sent if kind == "bytes")
    assert replay == b"partial differential frame"
    assert bytes(bridge.written) == b"\x0c"
    await s.close()


@pytest.mark.asyncio
async def test_detach_keeps_draining_into_buffer():
    from hermes_cli.pty_session import PtySession
    bridge = FakeBridge([b"one", b"", b"two"])
    s = PtySession("k", bridge, buffer_cap=1024, read_timeout=0.01)
    await s.start()
    ws = FakeWS()
    await s.attach(ws)
    s.detach(ws)
    assert s.attached is False
    assert s.last_detached_at is not None
    await wait_until(lambda: s.buffer.snapshot() == b"onetwo",
                     what="'two' to drain into the buffer while detached")
    ws2 = FakeWS()
    await s.attach(ws2)
    replay = b"".join(p for kind, p in ws2.sent if kind == "bytes")
    assert replay == b"onetwo"
    await s.close()


@pytest.mark.asyncio
async def test_eof_marks_dead_and_closes_socket_4410():
    from hermes_cli.pty_session import PtySession
    bridge = FakeBridge([b"bye", None])
    s = PtySession("k", bridge, buffer_cap=1024, read_timeout=0.01)
    await s.start()
    ws = FakeWS()
    await s.attach(ws)
    await wait_until(lambda: ws.close_code is not None,
                     what="the drain task to observe EOF and close the socket")
    assert s.alive is False
    assert ws.close_code == 4410
    await s.close()


from hermes_cli.pty_session import PtySessionRegistry, RegistryFull


def make_registry(ttl=1800.0, max_sessions=16):
    return PtySessionRegistry(ttl=ttl, max_sessions=max_sessions,
                              buffer_cap=1024, read_timeout=0.01)


@pytest.mark.asyncio
async def test_same_key_reattaches_same_session():
    reg = make_registry()
    b1 = FakeBridge([b"", b"", b""])
    s1, created1 = await reg.attach_or_spawn("tok", spawn=lambda: b1)
    s2, created2 = await reg.attach_or_spawn("tok", spawn=lambda: FakeBridge([]))
    assert created1 is True and created2 is False
    assert s1 is s2
    assert s2.bridge is b1                     # second spawn callable was NOT used
    await reg.close_all()




@pytest.mark.asyncio
async def test_new_key_at_capacity_raises_when_none_reapable():
    reg = make_registry(max_sessions=1)
    b = FakeBridge([b"", b""])
    s, _ = await reg.attach_or_spawn("a", spawn=lambda: b)
    await s.attach(FakeWS())                    # attached → not reapable
    with pytest.raises(RegistryFull):
        await reg.attach_or_spawn("b", spawn=lambda: FakeBridge([]))
    await reg.close_all()


@pytest.mark.asyncio
async def test_reaper_loop_invokes_reap(monkeypatch):
    """The reaper keeps iterating: it calls reap_idle more than once.

    Witness is the iteration COUNT reached, not elapsed wall-clock time —
    ``reaped_twice`` fires the instant the second call lands, so a loaded
    runner only makes this test slower, never red. The 10s ``wait_for`` is a
    hang guard, orders of magnitude above the ~20ms the loop really needs.
    """
    from hermes_cli.pty_session import run_reaper
    reg = make_registry()
    calls = {"n": 0}
    reaped_twice = asyncio.Event()

    async def fake_reap(now=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            reaped_twice.set()

    monkeypatch.setattr(reg, "reap_idle", fake_reap)
    task = asyncio.create_task(run_reaper(reg, interval=0.01))
    try:
        await asyncio.wait_for(reaped_twice.wait(), timeout=10)
    except asyncio.TimeoutError:
        pass                                        # fall through to the named assert
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert calls["n"] >= 2, f"reaper loop only invoked reap_idle {calls['n']} time(s)"
