"""A busy steer the closed parent refuses is not reported as "steered" because a child took it (t_3ae2c201).

``_steer_running_agent`` fans a steer out to the parent's ``_active_children``. Since #575 the parent's
``steer()`` returns False once its turn has made its last drain. On fork main the fan-out still ran
and the call returned True when any child accepted: the gateway reported "steered", did not queue the
message, and the parent's next turn never saw it. A child still attached at that point belongs to a
delegation the finished turn abandoned (an interrupted ``delegate_task``), so the text went nowhere
the user could get an answer from.

Fix: fan out only when the parent accepted. A refused steer returns False and the gateway queues it
as the next turn, as it does for a parent with no children. The parent here goes through the real
``finalize_turn``; the child is open and would accept.
"""
from __future__ import annotations

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tests.agent.test_steer_turn_closed import _SteerableStub, finished_agent


def _event(text):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c", user_id="u", chat_type="dm")
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id="m")


def _closed_parent_with_open_child():
    import threading
    parent = finished_agent()          # real finalize_turn: steering closed
    child = _SteerableStub()           # a live child: steering open
    assert child.steer("probe") is True
    child._pending_steer = None
    parent._active_children = [child]
    parent._active_children_lock = threading.Lock()
    return parent, child


def test_closed_parent_with_a_live_child_is_not_steered(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    parent, child = _closed_parent_with_open_child()
    accepted = runner._steer_running_agent(parent, "use branch B", event=_event("use branch B"))
    assert accepted is False, "reported steered although the parent refused; only an orphaned child took it"
    assert child._pending_steer is None, "the text also went to the child; queuing it too would duplicate it"
    assert parent._pending_steer is None


@pytest.mark.asyncio
async def test_busy_steer_into_a_closed_parent_with_a_live_child_is_queued(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    parent, child = _closed_parent_with_open_child()
    runner._session_state("key").turn.agent = parent
    outcome = await runner._resolve_busy_steer_or_redirect(_event("use branch B"), "key", "steer", parent)
    assert outcome.steered is False
    assert child._pending_steer is None


@pytest.mark.asyncio
async def test_busy_message_into_a_closed_parent_with_a_live_child_reaches_the_queue():
    """End to end through the busy handler (Prism f37acc69cb88): the refused message is stored as the
    next turn, the ack says queued, and the orphaned child does not also get it."""
    from gateway.session import build_session_key
    from tests.gateway.test_busy_session_ack import _make_adapter, _make_event, _make_runner
    runner, _ = _make_runner()
    runner._busy_input_mode = "steer"
    adapter = _make_adapter()
    event = _make_event(text="use branch B")
    sk = build_session_key(event.source)
    runner.adapters[event.source.platform] = adapter
    parent, child = _closed_parent_with_open_child()
    runner._running_agents[sk] = parent

    await runner._handle_active_session_busy_message(event, sk)

    assert adapter._pending_messages.get(sk) is event, "refused steer was not queued as the next turn"
    assert child._pending_steer is None
    content = adapter._send_with_retry.call_args.kwargs.get("content", "")
    assert "Queued for the next turn" in content
    assert "Steered" not in content


def test_open_parent_still_fans_out_to_its_children(tmp_path, monkeypatch):
    """Control: mid-turn, the parent and every live child get the text (#112095), as before."""
    import threading
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    parent, child = _SteerableStub(), _SteerableStub()
    parent._active_children = [child]
    parent._active_children_lock = threading.Lock()
    assert runner._steer_running_agent(parent, "use branch B", event=_event("use branch B")) is True
    assert parent._pending_steer and parent._pending_steer.endswith("use branch B")
    assert child._pending_steer == parent._pending_steer
