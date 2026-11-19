"""Watch-pattern notifications vs a busy session's pending queue (t_469a7ef8).

2026-11-18: a busy Discord session sat at its 32-item queue cap for hours. Its one refused
watch event was requeued and re-injected every ~2 s watcher tick (524 "injecting" lines, each
followed by a drop), and kanban failsafe wakes were dropped at the cap with WakeNotAccepted.
"""

import queue
import re
from types import SimpleNamespace

import pytest

import gateway.run_notifications as rn
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.run_busy import WATCH_NOTIFICATION_META
from gateway.session import SessionSource, build_session_key

SOURCE = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="123", chat_type="dm")
KEY = build_session_key(SOURCE)


def _runner(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text("display:\n  background_process_notifications: all\n")
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner = GatewayRunner(GatewayConfig())
    runner.session_store._entries[KEY] = SimpleNamespace(origin=SOURCE)
    injected = []

    async def handle_message(event):  # the session is mid-turn: every event takes the busy queue
        injected.append(event)
        runner._queue_or_replace_pending_event(KEY, event)

    adapter = SimpleNamespace(_pending_messages={}, handle_message=handle_message, send=None)
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._delivery_adapter_for = lambda source: adapter
    return runner, adapter, injected


def _queued(runner, adapter):
    return [e for e in [adapter._pending_messages.get(KEY), *(runner._overflow_queue(KEY) or ())] if e is not None]


def _watch(proc="proc_a", n=0):
    return {"type": "watch_match", "session_id": proc, "session_key": KEY, "pattern": "MATCH",
            "command": "chatty", "output": f"MATCH {n}\n", "suppressed": 0}


def _event(text, *, internal=True, watch=False):
    meta = {WATCH_NOTIFICATION_META: True} if watch else {}
    return MessageEvent(text=text, source=SOURCE, internal=internal, metadata=meta)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.mark.asyncio
async def test_process_emitting_500_matches_in_10s_queues_a_bounded_number(monkeypatch, tmp_path):
    """Real producer (ProcessRegistry._check_watch_patterns) + real gateway drain, 2 s ticks."""
    import tools.process_registry as pr
    registry = pr.ProcessRegistry()
    session = pr.ProcessSession(id="proc_chatty", command="chatty", session_key=KEY, watch_patterns=["MATCH"])
    clock = _Clock()
    monkeypatch.setattr(pr.time, "time", clock)
    monkeypatch.setattr(rn.time, "monotonic", clock)
    runner, adapter, injected = _runner(monkeypatch, tmp_path)
    for i in range(500):  # 500 matching lines over 10 s, drained every 2 s like the watcher loop
        registry._check_watch_patterns(session, f"MATCH line {i}\n")
        clock.now += 0.02
        if i % 100 == 99:
            await runner._drain_watch_notifications(registry.completion_queue)
    watch_queued = [e for e in _queued(runner, adapter) if e.metadata.get(WATCH_NOTIFICATION_META)]
    assert 1 <= len(watch_queued) <= runner._BUSY_QUEUE_MAX_WATCH
    assert len(injected) <= runner._WATCH_ROUTE_MAX_PER_WINDOW


@pytest.mark.asyncio
async def test_watch_flood_from_many_processes_is_rate_limited_and_folded(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(rn.time, "monotonic", clock)
    runner, adapter, injected = _runner(monkeypatch, tmp_path)
    q = queue.Queue()
    for n in range(500):
        q.put(_watch(f"proc_{n % 50}", n))
    for _ in range(5):  # 10 s of 2 s ticks
        await runner._drain_watch_notifications(q)
        clock.now += 2
    assert len(injected) == runner._WATCH_ROUTE_MAX_PER_WINDOW
    assert q.qsize() == 50 - runner._WATCH_ROUTE_MAX_PER_WINDOW  # one folded event per held process
    held = [q.get_nowait() for _ in range(q.qsize())]
    injected_folds = sum(int(m) for e in injected for m in re.findall(r"\((\d+) earlier matches", e.text))
    assert sum(e["suppressed"] for e in held) + len(held) + injected_folds + len(injected) == 500


@pytest.mark.asyncio
async def test_refused_watch_event_is_not_retried_every_tick(monkeypatch, tmp_path):
    """The 2026-11-18 shape: queue full of other wakes, the same watch event re-injected each tick."""
    clock = _Clock()
    monkeypatch.setattr(rn.time, "monotonic", clock)
    runner, adapter, injected = _runner(monkeypatch, tmp_path)
    for n in range(runner._BUSY_QUEUE_MAX_PENDING):
        runner._queue_or_replace_pending_event(KEY, _event(f"[kanban wake] t_{n}"))
    q = queue.Queue()
    q.put(_watch())
    for _ in range(30):  # 60 s of 2 s ticks
        await runner._drain_watch_notifications(q)
        clock.now += 2
    assert len(injected) <= 3  # t=0, 30, 60 at most -- not 30
    assert q.qsize() == 1  # still held for when the queue drains, not lost


def test_watch_notifications_never_take_the_reserved_slots(monkeypatch, tmp_path):
    runner, adapter, _ = _runner(monkeypatch, tmp_path)
    for n in range(40):
        runner._queue_or_replace_pending_event(KEY, _event(f"watch {n}", watch=True))
    assert len(_queued(runner, adapter)) == runner._BUSY_QUEUE_MAX_WATCH
    for n in range(runner._BUSY_QUEUE_MAX_PENDING - runner._BUSY_QUEUE_MAX_WATCH):
        event = _event(f"[kanban wake] t_{n}")
        runner._queue_or_replace_pending_event(KEY, event)
        assert event._gateway_accepted is True
    assert len(_queued(runner, adapter)) == runner._BUSY_QUEUE_MAX_PENDING
    late_watch = _event("watch late", watch=True)
    runner._queue_or_replace_pending_event(KEY, late_watch)
    assert getattr(late_watch, "_gateway_accepted", False) is False


@pytest.mark.parametrize("internal", [True, False], ids=["kanban-wake", "user-message"])
def test_full_queue_of_watch_notifications_still_admits_a_wake_or_user_message(monkeypatch, tmp_path, internal):
    runner, adapter, _ = _runner(monkeypatch, tmp_path)
    runner._BUSY_QUEUE_MAX_WATCH = runner._BUSY_QUEUE_MAX_PENDING  # a queue filled by watch notices
    runner._BUSY_QUEUE_RESERVED_SLOTS = 0
    for n in range(runner._BUSY_QUEUE_MAX_PENDING):
        runner._queue_or_replace_pending_event(KEY, _event(f"watch {n}", watch=True))
    assert len(_queued(runner, adapter)) == runner._BUSY_QUEUE_MAX_PENDING
    event = _event("[kanban wake] failsafe t_430e157f" if internal else "hello", internal=internal)
    runner._queue_or_replace_pending_event(KEY, event)
    assert event._gateway_accepted is True
    queued = _queued(runner, adapter)
    assert len(queued) == runner._BUSY_QUEUE_MAX_PENDING and queued[-1] is event
    # The oldest OVERFLOW watch was evicted; the head slot is never evicted.
    assert [e.text for e in queued[:2]] == ["watch 0", "watch 2"]


def test_human_input_merged_into_a_head_watch_notice_is_never_evicted(monkeypatch, tmp_path):
    """Prism cbbd6b0a5f80: busy text debounce can merge a same-sender human follow-up into the head
    slot's watch notice. A wake at the cap must not evict that slot and drop the human's text."""
    runner, adapter, _ = _runner(monkeypatch, tmp_path)
    runner._queue_or_replace_pending_event(KEY, _event("watch 0", watch=True))
    head = adapter._pending_messages[KEY]
    head.text += "\n\nplease also check the logs"   # what merge_pending_message_event(merge_text=True) does
    for n in range(runner._BUSY_QUEUE_MAX_PENDING - 1):
        runner._queue_or_replace_pending_event(KEY, _event(f"[kanban wake] t_{n}"))
    late = _event("another user message", internal=False)
    runner._queue_or_replace_pending_event(KEY, late)
    assert getattr(late, "_gateway_accepted", False) is False   # refused, not admitted by evicting the head
    assert adapter._pending_messages[KEY] is head and "please also check the logs" in head.text
    assert len(_queued(runner, adapter)) == runner._BUSY_QUEUE_MAX_PENDING


def test_fold_keeps_the_newest_match_when_an_older_retry_sits_behind_it():
    """Prism e0a50dc0c587: a retried match is requeued at the tail, behind a newer match from the
    same process. Folding must keep the newer output, not the retried older one."""
    first = rn._fold_watch_matches([_watch(n=1)])            # drained once: MATCH 1 is stamped
    retried = first[0]                                       # its injection failed: requeued at the tail
    folded = rn._fold_watch_matches([_watch(n=2), retried])
    assert len(folded) == 1
    assert folded[0]["output"] == "MATCH 2\n" and folded[0]["suppressed"] == 1


def test_user_message_at_cap_with_no_watch_notification_is_still_dropped(monkeypatch, tmp_path):
    runner, adapter, _ = _runner(monkeypatch, tmp_path)
    for n in range(runner._BUSY_QUEUE_MAX_PENDING):
        runner._queue_or_replace_pending_event(KEY, _event(f"[kanban wake] t_{n}"))
    event = _event("hello", internal=False)
    runner._queue_or_replace_pending_event(KEY, event)
    assert getattr(event, "_gateway_accepted", False) is False
    assert len(_queued(runner, adapter)) == runner._BUSY_QUEUE_MAX_PENDING


@pytest.mark.asyncio
async def test_injected_watch_event_carries_the_watch_flag_and_a_completion_does_not(monkeypatch, tmp_path):
    runner, adapter, injected = _runner(monkeypatch, tmp_path)
    await runner._inject_watch_notification("[IMPORTANT: matched]", _watch())
    await runner._inject_watch_notification("[done]", {"type": "completion", "session_id": "p", "session_key": KEY})
    assert [bool(e.metadata.get(WATCH_NOTIFICATION_META)) for e in injected] == [True, False]


@pytest.mark.asyncio
async def test_kanban_failsafe_wake_is_admitted_through_deliver_wake_while_watches_hold_slots(monkeypatch, tmp_path):
    """Replay of the review-deadman casualty: ``deliver_wake`` raised WakeNotAccepted at the cap."""
    from gateway.wake import deliver_wake
    runner, adapter, _ = _runner(monkeypatch, tmp_path)
    for n in range(10):
        runner._queue_or_replace_pending_event(KEY, _event(f"watch {n}", watch=True))
    for n in range(runner._BUSY_QUEUE_MAX_PENDING - runner._BUSY_QUEUE_MAX_WATCH):
        runner._queue_or_replace_pending_event(KEY, _event(f"[kanban wake] t_{n}"))
    assert len(_queued(runner, adapter)) == runner._BUSY_QUEUE_MAX_PENDING
    for card in ("t_430e157f", "t_718e6208"):  # both at the cap: each evicts one watch notice
        await deliver_wake(adapter, text=f"FAILSAFE wake {card}", source=SOURCE, wake_kind="kanban")
    queued = _queued(runner, adapter)
    assert len(queued) == runner._BUSY_QUEUE_MAX_PENDING
    assert [e.text for e in queued[-2:]] == ["FAILSAFE wake t_430e157f", "FAILSAFE wake t_718e6208"]
