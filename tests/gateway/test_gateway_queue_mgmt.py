"""Gateway /queue management: verbs, held store, /stop suffix + knob, resets, cap (spec 2026-10-12).

Store-level tests drive ``gateway/queue_mgmt.py`` against a real ``GatewayRunner`` shell (real
``SessionState``) and a real ``BasePlatformAdapter`` subclass (real ``_pending_messages``); the
entry-point tests drive ``_busy_queue_command`` / ``_hm_cmd_queue`` themselves.
"""

import importlib
from types import SimpleNamespace

import pytest

from gateway import queue_mgmt as qm
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource

KEY = "agent:main:telegram:dm:7"


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="m1")

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="7", chat_type="dm")


def _ev(text, **kw):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_source(), **kw)


def _cmd(text, **kw):
    return MessageEvent(text=text, message_type=kw.pop("message_type", MessageType.TEXT), source=_source(), **kw)


@pytest.fixture
def rig():
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    adapter = _Adapter()
    runner.adapters = {adapter.platform: adapter}
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.config = SimpleNamespace(thread_sessions_per_user=False, group_sessions_per_user=False)
    runner._delivery_adapter_for = lambda source: adapter
    return runner, adapter


def _seed(runner, adapter, *texts, internal_at=None):
    """Seed the ACTIVE store: slot = item 1, overflow = the rest."""
    events = [_ev(t, internal=(i == internal_at)) for i, t in enumerate(texts)]
    assert qm.rewrite_active(runner, KEY, adapter, events)
    return events


def _active(runner, adapter):
    return [e.text for e in qm.active_items(runner, KEY, adapter)]


def _held(runner):
    return [e.text for e in runner._session_state(KEY).conversation.held_queue]


# --------------------------------------------------------------------------- active-store verbs

def test_list_empty_and_numbered(rig):
    runner, adapter = rig
    assert qm.apply_management(runner, qm.QueueOp("list"), KEY, adapter) == "Queue is empty."
    _seed(runner, adapter, "a", "b")
    out = qm.apply_management(runner, qm.QueueOp("list"), KEY, adapter)
    assert out.splitlines()[:3] == ["Queue (2 pending):", "1. a", "2. b"]


def test_indices_span_slot_and_overflow_and_slot_stays_item_one(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a", "b", "c")
    assert adapter._pending_messages[KEY].text == "a"
    reply = qm.apply_management(runner, qm.QueueOp("rm", index=1), KEY, adapter)
    assert reply == "Removed queue item 1: a"
    assert adapter._pending_messages[KEY].text == "b"          # I2: slot is item 1 again
    assert _active(runner, adapter) == ["b", "c"]
    qm.apply_management(runner, qm.QueueOp("move", index=2, to=1), KEY, adapter)
    assert _active(runner, adapter) == ["c", "b"] and adapter._pending_messages[KEY].text == "c"
    qm.apply_management(runner, qm.QueueOp("edit", index=2, text="B!"), KEY, adapter)
    assert _active(runner, adapter) == ["c", "B!"]


def test_rewrite_keeps_overflow_list_identity(rig):
    runner, adapter = rig
    overflow = runner._session_state(KEY).conversation.queued_events
    _seed(runner, adapter, "a", "b", "c")
    qm.apply_management(runner, qm.QueueOp("rm", index=2), KEY, adapter)
    assert runner._session_state(KEY).conversation.queued_events is overflow   # I5b: in place


def test_rewrite_refuses_without_slot_storage(rig):
    runner, _adapter = rig
    bare = SimpleNamespace()   # an adapter with no _pending_messages dict
    runner._session_state(KEY).conversation.queued_events.extend([_ev("x")])
    assert qm.rewrite_active(runner, KEY, bare, []) is False
    assert [e.text for e in runner._session_state(KEY).conversation.queued_events] == ["x"]


def test_out_of_range_and_usage_are_loud(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a")
    assert qm.apply_management(runner, qm.QueueOp("rm", index=5), KEY, adapter) == \
        "Queue item 5 not found. Current size: 1"
    assert "Usage: /queue edit" in qm.apply_management(runner, qm.QueueOp("edit", index=1, text=""), KEY, adapter)
    assert "Usage: /queue move" in qm.apply_management(runner, qm.QueueOp("move", index=1, to=None), KEY, adapter)


def test_system_events_are_listed_refused_and_survive_clear(rig):
    runner, adapter = rig
    _seed(runner, adapter, "wake", "a", internal_at=0)
    assert "1. [system] wake" in qm.apply_management(runner, qm.QueueOp("list"), KEY, adapter)
    assert "system event" in qm.apply_management(runner, qm.QueueOp("rm", index=1), KEY, adapter)
    assert "system event" in qm.apply_management(runner, qm.QueueOp("edit", index=1, text="x"), KEY, adapter)
    assert qm.apply_management(runner, qm.QueueOp("clear"), KEY, adapter) == "Cleared 1 queued prompt."
    assert _active(runner, adapter) == ["wake"]


def test_edit_keeps_attachment(rig):
    runner, adapter = rig
    ev = _ev("look", media_urls=["/tmp/p.jpg"], media_types=["image/jpeg"])
    qm.rewrite_active(runner, KEY, adapter, [ev])
    reply = qm.apply_management(runner, qm.QueueOp("edit", index=1, text="look closer"), KEY, adapter)
    assert reply.endswith("(attachment kept)")
    assert adapter._pending_messages[KEY].media_urls == ["/tmp/p.jpg"]


# --------------------------------------------------------------------------- held store (D14/D20)

def test_hold_moves_user_items_in_order_and_leaves_system_events(rig):
    runner, adapter = rig
    _seed(runner, adapter, "wake", "a", "b", internal_at=0)
    assert qm.hold_user_items(runner, KEY, adapter) == 2
    assert _held(runner) == ["a", "b"]
    assert _active(runner, adapter) == ["wake"]


def test_hold_rolls_back_when_active_strip_fails(rig, monkeypatch):
    """Never in both stores, never in neither: a failed strip leaves the queue exactly as it was."""
    runner, adapter = rig
    _seed(runner, adapter, "a", "b")
    monkeypatch.setattr(qm, "rewrite_active", lambda *a, **k: False)
    qm.hold_user_items(runner, KEY, adapter)
    assert _held(runner) == [] and _active(runner, adapter) == ["a", "b"]


def test_hold_and_stop_suffix_never_raise(rig, monkeypatch):
    runner, adapter = rig
    _seed(runner, adapter, "a")
    monkeypatch.setattr(qm, "active_items", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert qm.hold_user_items(runner, KEY, adapter) == 0
    monkeypatch.setattr(qm, "held_list", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert qm.stop_reply_suffix(runner, KEY, adapter) == ""


def test_mutate_held_keeps_rest_held_and_never_arms_the_slot(rig):
    """Momus p2 B3: rm on the held store must not route survivors through the slot."""
    runner, adapter = rig
    _seed(runner, adapter, "a", "b", "c")
    qm.hold_user_items(runner, KEY, adapter)
    assert KEY not in adapter._pending_messages
    out = qm.apply_management(runner, qm.QueueOp("list"), KEY, adapter)
    assert out.splitlines()[0].startswith("Queue (3 held after /stop")
    assert qm.apply_management(runner, qm.QueueOp("rm", index=1), KEY, adapter) == "Removed queue item 1: a"
    assert _held(runner) == ["b", "c"]
    assert KEY not in adapter._pending_messages          # nothing armed to run
    assert _active(runner, adapter) == []


def test_new_queue_item_joins_held(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a")
    qm.hold_user_items(runner, KEY, adapter)
    reply = qm.append_if_held(runner, KEY, _ev("z"))
    assert "2 held" in reply and _held(runner) == ["a", "z"]


def test_restage_moves_held_into_slot_then_overflow(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a", "b", "c")
    qm.hold_user_items(runner, KEY, adapter)
    assert qm.restage_held(runner, KEY, adapter) == 3
    assert adapter._pending_messages[KEY].text == "a"
    assert _active(runner, adapter) == ["a", "b", "c"] and _held(runner) == []


@pytest.mark.asyncio
async def test_queue_run_idle_releases_and_reports(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a", "b")
    qm.hold_user_items(runner, KEY, adapter)
    assert await qm.release(runner, KEY, _source(), adapter, busy=False) == \
        "Released 2 queued prompt(s); they run next, in order."
    assert adapter._pending_messages[KEY].text == "a"
    assert await qm.release(runner, KEY, _source(), adapter, busy=False) == \
        "Released 2 queued prompt(s); they run next, in order."    # already active: idempotent


@pytest.mark.asyncio
async def test_queue_run_empty(rig):
    runner, adapter = rig
    assert await qm.release(runner, KEY, _source(), adapter, busy=False) == "Queue is empty."


# --------------------------------------------------------------------------- /stop suffix + knob (D12/D17)

def test_stop_suffix_reminds_held_count(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a", "b", "c")
    qm.hold_user_items(runner, KEY, adapter)
    suffix = qm.stop_reply_suffix(runner, KEY, adapter)
    assert suffix.startswith("\n3 queued prompt(s) held.")
    assert _held(runner) == ["a", "b", "c"] and KEY not in adapter._pending_messages


def test_stop_suffix_empty_when_nothing_held(rig):
    runner, adapter = rig
    assert qm.stop_reply_suffix(runner, KEY, adapter) == ""


@pytest.mark.parametrize("value, releases", [(True, True), (False, False), ("true", False), (None, False)])
def test_stop_releases_queue_knob_is_true_only(rig, value, releases):
    runner, adapter = rig
    runner._stop_releases_queue = value
    _seed(runner, adapter, "a", "b")
    qm.hold_user_items(runner, KEY, adapter)
    suffix = qm.stop_reply_suffix(runner, KEY, adapter)
    if releases:
        assert "Releasing 2 queued" in suffix and adapter._pending_messages[KEY].text == "a"
    else:
        assert "2 queued prompt(s) held" in suffix and KEY not in adapter._pending_messages


@pytest.mark.parametrize("raw, expected", [(None, False), ("", False), ("false", False), (False, False),
                                            (True, True), ("true", True)])
def test_knob_loader_defaults_to_hold(monkeypatch, raw, expected):
    gateway_run = importlib.import_module("gateway.run")
    cfg = {} if raw is None else {"display": {"stop_releases_queue": raw}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda *a, **k: cfg)
    assert gateway_run.GatewayRunner._load_stop_releases_queue() is expected


# --------------------------------------------------------------------------- resets (D21/I8)

def test_auto_reset_carries_held_items(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a", "b")
    qm.hold_user_items(runner, KEY, adapter)
    runner._clear_conversation_scope(KEY, reason="auto_reset")
    assert _held(runner) == ["a", "b"]


@pytest.mark.parametrize("reason", ["session_reset", "resume"])
def test_user_reset_drops_and_reports(rig, reason):
    runner, adapter = rig
    _seed(runner, adapter, "a", "b")
    qm.hold_user_items(runner, KEY, adapter)
    runner._session_state(KEY).conversation.queued_events.append(_ev("c"))
    runner._clear_conversation_scope(KEY, reason=reason)
    assert _held(runner) == []
    assert qm.take_reset_drop_suffix(runner, KEY) == "\nDropped 3 queued prompt(s) with the old conversation."
    assert qm.take_reset_drop_suffix(runner, KEY) == ""                # consumed once


# --------------------------------------------------------------------------- refusals (I6/I6b/I8)

def test_cap_counts_held_and_active(rig):
    runner, adapter = rig
    runner._BUSY_QUEUE_MAX_PENDING = 3
    _seed(runner, adapter, "a", "b")
    qm.hold_user_items(runner, KEY, adapter)
    _seed(runner, adapter, "c")
    assert "queue is full (3 items)" in qm.cap_reply(runner, KEY, adapter)


def test_refuse_command_prompt():
    assert "/model is a command" in qm.refuse_command_prompt("/model opus")
    assert qm.refuse_command_prompt("/notarealcommandxyz hi") is None
    assert qm.refuse_command_prompt("model opus") is None


def test_media_only_queue_still_enqueues():
    ev = _cmd("/queue", media_urls=["/tmp/p.jpg"], media_types=["image/jpeg"], message_type=MessageType.PHOTO)
    assert qm.classify(ev).kind == "add"
    assert qm.classify(_cmd("/queue")).kind == "list"


# --------------------------------------------------------------------------- entry points

@pytest.mark.asyncio
async def test_busy_queue_verb_not_enqueued(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a")
    reply = await runner._busy_queue_command(_cmd("/queue list"), KEY, _source())
    assert reply.startswith("Queue (1 pending):")
    assert _active(runner, adapter) == ["a"]                            # nothing appended


@pytest.mark.asyncio
async def test_busy_queue_prompt_joins_held_when_held(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a")
    qm.hold_user_items(runner, KEY, adapter)
    reply = await runner._busy_queue_command(_cmd("/queue next thing"), KEY, _source())
    assert "2 held" in reply and _held(runner) == ["a", "next thing"]


@pytest.mark.asyncio
async def test_idle_queue_verb_not_sent_to_model(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a")
    handled, reply = await runner._hm_cmd_queue(_cmd("/queue list"), _source(), KEY)
    assert handled is True and reply.startswith("Queue (1 pending):")


@pytest.mark.asyncio
async def test_idle_queue_prompt_still_sends_as_turn(rig):
    runner, _adapter = rig
    ev = _cmd("/queue summarize this")
    handled, reply = await runner._hm_cmd_queue(ev, _source(), KEY)
    assert (handled, reply) == (False, None) and ev.text == "summarize this"


@pytest.mark.asyncio
async def test_idle_queue_add_escape_hatch(rig):
    runner, _adapter = rig
    ev = _cmd("/queue add clear the cache")
    handled, _ = await runner._hm_cmd_queue(ev, _source(), KEY)
    assert handled is False and ev.text == "clear the cache"


# --------------------------------------------------------------------------- B1: /stop's own tail runs nothing held

@pytest.mark.asyncio
async def test_stop_tail_drain_runs_nothing_held(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a", "b")
    qm.hold_user_items(runner, KEY, adapter)
    started = []
    adapter._start_session_processing = lambda event, key, **kw: started.append(event) or True
    import asyncio
    await adapter._drain_pending_after_session_command(KEY, asyncio.Event())
    assert started == [] and _held(runner) == ["a", "b"]


@pytest.mark.asyncio
async def test_released_queue_dispatches_exactly_one_head_through_the_tail(rig):
    """D16/I9 single door: after a release the adapter's tail starts item 1 only; b stays queued."""
    runner, adapter = rig
    _seed(runner, adapter, "a", "b")
    qm.hold_user_items(runner, KEY, adapter)
    runner._stop_releases_queue = True
    qm.stop_reply_suffix(runner, KEY, adapter)
    started = []
    adapter._start_session_processing = lambda event, key, **kw: started.append(event.text) or True
    import asyncio
    await adapter._drain_pending_after_session_command(KEY, asyncio.Event())
    assert started == ["a"]
    assert [e.text for e in runner._session_state(KEY).conversation.queued_events] == ["b"]


# --------------------------------------------------------------------------- Momus pass-3 fixes

def test_edit_refuses_a_slash_command_and_leaves_the_item(rig):
    """RC1: `/queue edit 1 /model x` must not reopen the silent dequeue-discard path."""
    runner, adapter = rig
    _seed(runner, adapter, "a")
    reply = qm.apply_management(runner, qm.QueueOp("edit", index=1, text="/model opus"), KEY, adapter)
    assert "/model is a command" in reply and _active(runner, adapter) == ["a"]


def test_auto_reset_carries_active_user_items_into_held(rig):
    """RC2b: an automatic reset has no reply target, so nothing may drop."""
    runner, adapter = rig
    runner._session_state(KEY).conversation.queued_events.extend([_ev("x"), _ev("wake", internal=True)])
    runner._clear_conversation_scope(KEY, reason="auto_reset")
    assert _held(runner) == ["x"]


def test_unclassified_reset_reason_carries_and_warns(rig, caplog):
    """RC2a: a boundary reason nobody classified carries the queue rather than dropping it."""
    runner, adapter = rig
    _seed(runner, adapter, "a")
    qm.hold_user_items(runner, KEY, adapter)
    runner._clear_conversation_scope(KEY, reason="some_future_boundary")
    assert _held(runner) == ["a"] and "unclassified" in caplog.text


def test_every_scope_clear_call_site_reason_is_classified():
    """RC2a: each literal reason passed to _clear_conversation_scope is a known auto or user reset."""
    import pathlib
    import re
    root = pathlib.Path(qm.__file__).resolve().parent
    reasons = set()
    for path in root.glob("*.py"):
        reasons |= set(re.findall(r'_clear_conversation_scope\([^)]*reason="([a-z_]+)"', path.read_text()))
    assert reasons, "no call sites found: the scan is broken"
    assert reasons <= (qm.AUTO_RESET_REASONS | qm.USER_RESET_REASONS), reasons


def test_list_shows_later_active_items_under_held_and_run_restages_oldest_first(rig):
    """RC4: a plain busy-mode follow-up after /stop stays active; list shows it, run keeps age order."""
    runner, adapter = rig
    _seed(runner, adapter, "a", "b")
    qm.hold_user_items(runner, KEY, adapter)
    _seed(runner, adapter, "later")                 # e.g. busy_input_mode: queue after /stop
    out = qm.apply_management(runner, qm.QueueOp("list"), KEY, adapter)
    assert "1. a" in out and "2. b" in out and "Also waiting (1)" in out and "· later" in out
    qm.restage_held(runner, KEY, adapter)
    assert _active(runner, adapter) == ["a", "b", "later"]


# --------------------------------------------------------------------------- Momus pass-4 RC-B

def _seed_orphans(runner, adapter, *texts):
    """Idle orphans: user items in overflow with an EMPTY slot (the eviction/rescue state)."""
    assert qm.rewrite_active(runner, KEY, adapter, [_ev(t) for t in texts], arm=False)


@pytest.mark.parametrize("op", [qm.QueueOp("rm", index=3), qm.QueueOp("edit", index=1, text="A"),
                                qm.QueueOp("move", index=2, to=1)])
def test_idle_management_verbs_never_arm_the_slot(rig, op):
    """Editing an idle queue must not start a turn: the slot (a run-next trigger) stays empty."""
    runner, adapter = rig
    _seed_orphans(runner, adapter, "a", "b", "c")
    qm.apply_management(runner, op, KEY, adapter)
    assert KEY not in adapter._pending_messages


def test_busy_management_verbs_keep_the_slot_armed(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a", "b", "c")                    # slot armed (a turn will drain it)
    qm.apply_management(runner, qm.QueueOp("rm", index=1), KEY, adapter)
    assert adapter._pending_messages[KEY].text == "b"


@pytest.mark.asyncio
async def test_queue_run_arms_idle_orphans_so_released_is_true(rig):
    runner, adapter = rig
    _seed_orphans(runner, adapter, "a", "b")
    reply = await qm.release(runner, KEY, _source(), adapter, busy=False)
    assert reply.startswith("Released 2") and adapter._pending_messages[KEY].text == "a"


# --------------------------------------------------------------------------- follow-up: release mode

@pytest.mark.parametrize("raw, expected", [(None, "per_item"), ("", "per_item"), ("per_item", "per_item"),
                                            ("merged", "merged"), ("MERGED", "merged"), ("bogus", "per_item")])
def test_release_mode_loader(monkeypatch, caplog, raw, expected):
    gateway_run = importlib.import_module("gateway.run")
    cfg = {} if raw is None else {"display": {"queue_release_mode": raw}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda *a, **k: cfg)
    assert gateway_run.GatewayRunner._load_queue_release_mode() == expected
    if raw == "bogus":
        assert "queue_release_mode" in caplog.text


def test_merged_release_builds_one_event_in_order_and_marks_it(rig):
    runner, adapter = rig
    runner._queue_release_mode = "merged"
    _seed(runner, adapter, "a", "b", "c")
    qm.hold_user_items(runner, KEY, adapter)
    assert qm.restage_held(runner, KEY, adapter) == 3          # reply still says "Released 3"
    assert _active(runner, adapter) == ["a\n\nb\n\nc"]
    assert adapter._pending_messages[KEY]._queue_released is True
    assert _held(runner) == []


def test_per_item_release_marks_every_released_event(rig):
    runner, adapter = rig
    _seed(runner, adapter, "a", "b")
    qm.hold_user_items(runner, KEY, adapter)
    qm.restage_held(runner, KEY, adapter)
    assert [e._queue_released for e in qm.active_items(runner, KEY, adapter)] == [True, True]
