"""Kanban/boomerang wakes into a busy session coalesce per card; a refusal at the cap is filed once.

t_4a891379: on 2026-11-18 ~33 kanban and boomerang wake turns filled one long-running session's
pending queue (cap 32) while single turns ran up to 3,037 s; from then on every other message to
that chat, the review-deadman failsafe wakes included, was dropped. These tests drive the real
adapter admission (``deliver_wake`` -> ``handle_message`` -> runner busy handler -> FIFO).
"""
import asyncio
import json
from pathlib import Path

import pytest

from evals.heartbeat_idle_wire import WireAdapter
from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from gateway.wake import WakeNotAccepted, deliver_wake
from gateway.wake_kind import KANBAN, WATCHER, wake_meta

CARDS = [f"t_{i:08x}" for i in (0xa1, 0xb2, 0xc3, 0xd4, 0xe5)]


def _route():
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner.adapters = {adapter.platform: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._busy_input_mode = "queue"
    source = SessionSource(platform=adapter.platform, chat_id="42", user_id="42", chat_type="dm")
    return runner, adapter, source, build_session_key(source)


def _queued(runner, adapter, key):
    slot = adapter._pending_messages.get(key)
    return ([slot] if slot is not None else []) + list(runner._overflow_queue(key) or [])


async def _busy(runner, adapter, source):
    """Start one turn that never finishes inside the test: the session stays busy."""
    release, started = asyncio.Event(), asyncio.Event()

    async def handler(event):
        started.set()
        await release.wait()

    adapter.set_message_handler(handler)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    await adapter.connect()
    await deliver_wake(adapter, text="long turn", source=source, wake_kind=KANBAN)
    await asyncio.wait_for(started.wait(), 2)
    return release


def _user(runner, key, source, text):
    """A user follow-up through the runner FIFO (the busy path's queue step for a human message)."""
    runner._queue_or_replace_pending_event(key, MessageEvent(text=text, source=source))


async def _card_wake(adapter, source, card, n):
    await deliver_wake(adapter, text=f"[kanban] {card} notice {n}", source=source, wake_kind=KANBAN,
                       metadata=wake_meta(sub="handback", card_id=card))


@pytest.mark.asyncio
async def test_forty_wakes_for_five_cards_leave_five_queued_and_keep_the_user_in_place():
    runner, adapter, source, key = _route()
    release = await _busy(runner, adapter, source)
    try:
        sent = 0
        for n in range(8):
            for card in CARDS:
                await _card_wake(adapter, source, card, n)   # never raises WakeNotAccepted
                sent += 1
                if sent == 12:
                    _user(runner, key, source, "human between wakes")
        assert sent == 40
        queued = _queued(runner, adapter, key)
        texts = [e.text for e in queued]
        assert len(queued) == 6, texts           # one slot per card + the user message
        assert [(e.metadata or {}).get("card_id") for e in queued[:5]] == CARDS
        for card, event in zip(CARDS, queued[:5]):
            assert event.text.startswith(f"[kanban] {card} notice 7"), event.text   # newest text
            assert "7 earlier notices for this card" in event.text
        # The user message keeps its place behind the cards that were queued before it.
        assert queued[5].text == "human between wakes" and not queued[5].internal
    finally:
        release.set()
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_user_messages_are_never_coalesced():
    runner, adapter, source, key = _route()
    release = await _busy(runner, adapter, source)
    try:
        for _ in range(3):
            _user(runner, key, source, "same words")
        assert [e.text for e in _queued(runner, adapter, key)] == ["same words"] * 3
    finally:
        release.set()
        await adapter.disconnect()


def _fake_fleet_root(tmp_path: Path) -> Path:
    """A fleet root whose scripts/lib/agent_inbox.py records every call (the real library lives in
    hermes-home; the gateway loads it from the fleet root the same way the cron #alerts gate does)."""
    lib = tmp_path / "fleet" / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "agent_inbox.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "def inbox_dir(root):\n    return Path(root) / 'state' / 'agent-inbox'\n"
        "def stamp_key(k):\n    return 'k-' + ''.join(c if c.isalnum() else '_' for c in k)\n"
        "class Inbox:\n"
        "    def __init__(self, d):\n        self.d = Path(d)\n"
        "    def record(self, producer, sev, message):\n"
        "        self.d.mkdir(parents=True, exist_ok=True)\n"
        "        with open(self.d / 'calls.jsonl', 'a') as f:\n"
        "            f.write(json.dumps([producer, sev, message]) + '\\n')\n"
        "        return {'id': 'abcdef012345'}\n")
    home = tmp_path / "fleet" / "profiles" / "default"
    home.mkdir(parents=True)
    return home


def _inbox_calls(home: Path):
    p = home.parents[1] / "state" / "agent-inbox" / "calls.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


@pytest.mark.asyncio
async def test_cap_refusal_files_one_inbox_item_per_episode(tmp_path, monkeypatch):
    home = _fake_fleet_root(tmp_path)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)
    runner, adapter, source, key = _route()
    release = await _busy(runner, adapter, source)
    try:
        runner._BUSY_QUEUE_MAX_PENDING = 3
        for i in range(3):
            _user(runner, key, source, f"human {i}")
        for _ in range(12):   # the deadman failsafe's control-socket wake, 12 ticks
            with pytest.raises(WakeNotAccepted):
                await deliver_wake(adapter, text="[kanban review deadman] review deadman FAILSAFE (tier 2)",
                                   source=source, wake_kind=WATCHER,
                                   metadata=wake_meta(name="kanban review deadman"))
        calls = _inbox_calls(home)
        assert len(calls) == 1, calls
        producer, sev, message = calls[0]
        assert producer == "gateway-busy-queue" and sev == "warn"
        assert key in message.splitlines()[0] and "(3/3)" in message.splitlines()[0]
        assert "kanban review deadman" in message
        # User messages kept their place; nothing was evicted for the refused wakes.
        assert [e.text for e in _queued(runner, adapter, key)] == ["human 0", "human 1", "human 2"]
        # The queue drains below the cap: the episode ends; the next fill + refusal is a new episode.
        runner._promote_queued_event(key, adapter, adapter._pending_messages.pop(key))
        _user(runner, key, source, "human 3")
        with pytest.raises(WakeNotAccepted):
            await deliver_wake(adapter, text="[kanban review deadman] again", source=source, wake_kind=WATCHER)
        assert len(_inbox_calls(home)) == 2
    finally:
        release.set()
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_card_wake_at_cap_evicts_a_watch_notice_instead_of_being_refused(tmp_path, monkeypatch):
    """#434 coalescing + t_469a7ef8 watch eviction together: a queue at its cap holding kanban wakes
    for distinct cards plus watch notices admits a wake for a NEW card by evicting the oldest watch
    notice from the overflow (the head slot is never evicted); no WakeNotAccepted and no inbox item."""
    from gateway.run_busy import WATCH_NOTIFICATION_META
    home = _fake_fleet_root(tmp_path)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)
    runner, adapter, source, key = _route()
    release = await _busy(runner, adapter, source)
    try:
        cap = runner._BUSY_QUEUE_MAX_PENDING
        for i in range(runner._BUSY_QUEUE_MAX_WATCH):
            await deliver_wake(adapter, text=f"[watch] match {i}", source=source, wake_kind=WATCHER,
                               metadata={**wake_meta(name="watch"), WATCH_NOTIFICATION_META: True})
        cards = [f"t_{i:08x}" for i in range(cap)]
        for card in cards[:cap - runner._BUSY_QUEUE_MAX_WATCH]:
            await _card_wake(adapter, source, card, 0)
        queued = _queued(runner, adapter, key)
        assert len(queued) == cap
        assert sum(1 for e in queued if (e.metadata or {}).get(WATCH_NOTIFICATION_META)) == runner._BUSY_QUEUE_MAX_WATCH
        await _card_wake(adapter, source, cards[-1], 0)   # new card at the cap: must not raise
        queued = _queued(runner, adapter, key)
        assert len(queued) == cap
        assert [e.text for e in queued if (e.metadata or {}).get(WATCH_NOTIFICATION_META)] == [
            f"[watch] match {i}" for i in range(runner._BUSY_QUEUE_MAX_WATCH) if i != 1]
        assert queued[-1].text == f"[kanban] {cards[-1]} notice 0"
        assert _inbox_calls(home) == []
        assert key not in runner.__dict__.get("_wake_refusal_episodes", set())
    finally:
        release.set()
        await adapter.disconnect()
