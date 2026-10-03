"""A wake delivered to an IDLE session during a restart drain must not be lost.

t_cff27691: the kanban notifier keeps ticking while the gateway drains, so it
can call ``deliver_wake`` (internal MessageEvent -> ``adapter.handle_message``)
into an idle session. ``_handle_message`` refused it at the generic
``if self._draining`` gate ("not accepting new work") without spooling, after
the notifier had already advanced its cursor, so the wake was gone.

Busy sessions already park such events (``_handle_active_session_busy_message``
-> pending slot -> restart spool); this covers the idle path. Oracle: the
on-disk restart spool (``take_followups``), independent of the run.py code.
"""

import asyncio

import pytest

from gateway.fork_ext import restart_followups as rf
from gateway.platforms.base import MessageEvent, MessageType
from gateway.wake import deliver_wake
from tests.gateway.restart_test_helpers import (
    RestartTestAdapter,
    make_restart_runner,
    make_restart_source,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    (tmp_path / "logs").mkdir()
    return tmp_path


def _draining_runner(*, restart_requested: bool, mode: str = "queue"):
    adapter = RestartTestAdapter()
    runner, adapter = make_restart_runner(adapter)
    adapter.set_message_handler(runner._handle_message)
    runner._busy_input_mode = mode
    runner._draining = True
    runner._restart_requested = restart_requested
    return runner, adapter


async def _settle(adapter):
    for _ in range(50):
        tasks = [t for t in getattr(adapter, "_background_tasks", set()) if not t.done()]
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_idle_session_wake_during_restart_drain_is_spooled(home):
    runner, adapter = _draining_runner(restart_requested=True)
    src = make_restart_source(chat_id="4242")

    await deliver_wake(adapter, text="kanban: t_x done", source=src)
    await _settle(adapter)

    records, _stale = rf.take_followups()
    assert [r["text"] for r in records] == ["kanban: t_x done"], records
    assert records[0]["source"]["chat_id"] == "4242"
    # Replays as the same internal event (no user-auth gate on boot).
    assert (records[0].get("event") or {}).get("internal") is True
    # The user-facing refusal is not posted for a system wake.
    assert not any("not accepting new work" in s for s in adapter.sent), adapter.sent


@pytest.mark.asyncio
async def test_idle_internal_event_direct_handle_message_returns_none_and_spools(home):
    runner, _adapter = _draining_runner(restart_requested=True, mode="steer")
    event = MessageEvent(
        text="wake", message_type=MessageType.TEXT,
        source=make_restart_source(chat_id="7"), internal=True,
    )
    assert await runner._handle_message(event) is None
    records, _ = rf.take_followups()
    assert [r["text"] for r in records] == ["wake"]


@pytest.mark.asyncio
async def test_user_message_to_idle_session_during_drain_still_refused(home):
    """Scope guard: only internal events are spooled on the idle path."""
    runner, _adapter = _draining_runner(restart_requested=True)
    event = MessageEvent(
        text="hello", message_type=MessageType.TEXT, source=make_restart_source(chat_id="8"),
    )
    reply = await runner._handle_message(event)
    assert "not accepting new work" in str(reply)
    assert rf.take_followups()[0] == []


@pytest.mark.asyncio
async def test_internal_event_not_spooled_when_stopping_not_restarting(home):
    """Plain stop (no restart) has no next boot to replay into: unchanged refusal."""
    runner, _adapter = _draining_runner(restart_requested=False)
    event = MessageEvent(
        text="wake", message_type=MessageType.TEXT,
        source=make_restart_source(chat_id="9"), internal=True,
    )
    reply = await runner._handle_message(event)
    assert "not accepting new work" in str(reply)
    assert rf.take_followups()[0] == []
