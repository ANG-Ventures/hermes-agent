"""t_b2e9bb23 C: a route-change notice the adapter failed to send is queued
and redelivered on the next turn in that chat (dedupe by transition)."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway import route_notice_outbox as rno
from gateway.config import Platform
from gateway.run import TurnRunner
from gateway.turn_context import TurnContext

ROUTE_MESSAGE = (
    "🔄 Model fallback (connection dropped): "
    "openrouter/moonshotai/kimi-k3 → claude-bpr/claude-opus-5-5"
)
CHAT = "1536471262346477598"


class _FlakyAdapter:
    """Fails the first ``fail`` sends (host network down), then succeeds."""

    def __init__(self, fail=1, *, raise_instead=False):
        self.fail = fail
        self.raise_instead = raise_instead
        self.attempts = []
        self.delivered = []

    async def send(self, chat_id, content, metadata=None):
        self.attempts.append(content)
        if self.fail > 0:
            self.fail -= 1
            if self.raise_instead:
                raise ConnectionError("Cannot connect to host discord.com:443")
            return SimpleNamespace(success=False, message_id=None,
                                   error="Cannot connect to host discord.com:443")
        self.delivered.append((chat_id, content, metadata))
        return SimpleNamespace(success=True, message_id=str(len(self.delivered)), error=None)


@pytest.fixture(autouse=True)
def _outbox_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _runner(adapter, loop):
    source = SimpleNamespace(platform=Platform.DISCORD, chat_id=CHAT)
    ctx = TurnContext(
        source=source,
        _run_still_current=lambda: True,
        _loop_for_step=loop,
        _status_adapter=adapter,
        _current_status_adapter=lambda: adapter,
        _status_chat_id=CHAT,
        _status_thread_metadata={"thread_id": "t1"},
    )
    return TurnRunner(SimpleNamespace(), ctx)  # type: ignore[arg-type]


async def _until(pred, n=200):
    for _ in range(n):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def _key():
    return rno.chat_key(Platform.DISCORD, CHAT, {"thread_id": "t1"})


@pytest.mark.parametrize("raise_instead", [False, True])
def test_failed_announce_is_queued_then_redelivered_next_turn(raise_instead):
    adapter = _FlakyAdapter(fail=1, raise_instead=raise_instead)

    async def scenario():
        loop = asyncio.get_running_loop()
        turn1 = _runner(adapter, loop)
        await asyncio.to_thread(turn1._status_callback_sync, "lifecycle", ROUTE_MESSAGE)
        await _until(lambda: rno.default_outbox().pending(_key()))
        assert adapter.delivered == []

        # Next turn in the same chat (fresh TurnRunner, as after a restart).
        turn2 = _runner(adapter, loop)
        assert await asyncio.to_thread(turn2._flush_route_notice_outbox) == 1
        await _until(lambda: bool(adapter.delivered))

    asyncio.run(scenario())

    [(chat, text, meta)] = adapter.delivered
    assert chat == CHAT and meta == {"thread_id": "t1"}
    assert text.startswith(ROUTE_MESSAGE) and "delayed: undelivered at" in text
    assert rno.default_outbox().pending(_key()) == []


def test_outbox_survives_restart_via_file(_outbox_home):
    """The queue is the file: a fresh outbox object (new process) sees it."""
    rno.RouteNoticeOutbox().enqueue(_key(), ROUTE_MESSAGE, {"thread_id": "t1"})
    assert (_outbox_home / rno.FILE_NAME).exists()
    [entry] = rno.RouteNoticeOutbox().take(_key())
    assert entry["message"] == ROUTE_MESSAGE
    assert rno.RouteNoticeOutbox().pending(_key()) == []


def test_same_transition_is_deduped_per_chat():
    box = rno.default_outbox()
    assert box.enqueue(_key(), ROUTE_MESSAGE) is True
    assert box.enqueue(_key(), ROUTE_MESSAGE) is False
    assert box.enqueue(rno.chat_key(Platform.DISCORD, "other"), ROUTE_MESSAGE) is True
    assert len(box.pending(_key())) == 1


def test_failed_redelivery_goes_back_on_queue():
    adapter = _FlakyAdapter(fail=2)

    async def scenario():
        loop = asyncio.get_running_loop()
        rno.default_outbox().enqueue(_key(), ROUTE_MESSAGE, {"thread_id": "t1"})
        turn = _runner(adapter, loop)
        assert await asyncio.to_thread(turn._flush_route_notice_outbox) == 1
        await _until(lambda: len(adapter.attempts) == 1
                     and bool(rno.default_outbox().pending(_key())))
        # Platform back: the next flush delivers it once.
        adapter.fail = 0
        assert await asyncio.to_thread(turn._flush_route_notice_outbox) == 1
        await _until(lambda: bool(adapter.delivered))

    asyncio.run(scenario())
    assert len(adapter.delivered) == 1
    assert rno.default_outbox().pending(_key()) == []


def test_successful_announce_flushes_older_queued_notice():
    adapter = _FlakyAdapter(fail=0)
    older = "🔄 Model fallback (connection dropped): a/x → b/y"

    async def scenario():
        loop = asyncio.get_running_loop()
        rno.default_outbox().enqueue(_key(), older, {"thread_id": "t1"})
        turn = _runner(adapter, loop)
        await asyncio.to_thread(turn._status_callback_sync, "lifecycle", ROUTE_MESSAGE)
        await _until(lambda: len(adapter.delivered) == 2)

    asyncio.run(scenario())
    texts = [d[1] for d in adapter.delivered]
    assert ROUTE_MESSAGE in texts
    assert any(t.startswith(older) and "delayed" in t for t in texts)


def test_nothing_queued_is_a_noop():
    adapter = _FlakyAdapter(fail=0)
    turn = _runner(adapter, None)
    assert turn._flush_route_notice_outbox() == 0
    assert adapter.attempts == []
