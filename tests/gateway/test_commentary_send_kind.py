"""Interim commentary is marked as COMMENTARY end-to-end (t_784a01bd).

Adapters that cap split deliveries (Discord) collapse commentary to a single
message, so the marker has to be set on BOTH commentary lanes:

* the stream consumer's ``_send_commentary`` (streaming turns), and
* ``gateway/run.py``'s ``_interim_assistant_cb`` direct send (non-streaming
  turns with ``display.interim_assistant_messages: true``).

The marker is gateway-internal: the relay adapter must strip it before the
wire, exactly as it strips ``_interim_send``.
"""

from __future__ import annotations

import ast
import inspect

import pytest

import gateway.run as gateway_run
from gateway.platforms.base import is_commentary_send, mark_commentary_send
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from tests.gateway.test_stream_final_contract import _make_draft_adapter


class TestMarkerHelpers:
    def test_mark_preserves_and_marks_interim(self):
        md = mark_commentary_send({"thread_id": "t1"})
        assert md["thread_id"] == "t1"
        assert md["_interim_send"] is True
        assert is_commentary_send(md) is True

    def test_mark_does_not_mutate_input(self):
        src = {"thread_id": "t1"}
        mark_commentary_send(src)
        assert src == {"thread_id": "t1"}

    @pytest.mark.parametrize("md", [None, {}, {"_interim_send": True}])
    def test_plain_and_other_interim_sends_are_not_commentary(self, md):
        assert is_commentary_send(md) is False


class TestConsumerLane:
    @pytest.mark.asyncio
    async def test_send_commentary_is_marked_commentary(self):
        adapter = _make_draft_adapter()
        cfg = StreamConsumerConfig(
            transport="auto", chat_type="dm",
            edit_interval=0.01, buffer_threshold=1, cursor="",
        )
        sc = GatewayStreamConsumer(adapter, "D1", cfg)
        assert await sc._send_commentary("let me check the log") is True
        md = adapter.send_calls[-1]["metadata"]
        assert md.get("_interim_send") is True
        assert is_commentary_send(md) is True


class TestRunCallbackLane:
    def test_interim_assistant_cb_marks_commentary(self):
        """The direct-send branch of ``_interim_assistant_cb`` must wrap its
        metadata with ``mark_commentary_send``."""
        src = inspect.getsource(gateway_run)
        tree = ast.parse(src)
        cbs = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_interim_assistant_cb"
        ]
        assert cbs, "_interim_assistant_cb not found"
        for cb in cbs:
            send_calls = [
                n for n in ast.walk(cb)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "send"
            ]
            assert send_calls, "expected a direct adapter.send in the callback"
            for call in send_calls:
                md = next((k.value for k in call.keywords if k.arg == "metadata"), None)
                assert (
                    isinstance(md, ast.Call)
                    and isinstance(md.func, ast.Name)
                    and md.func.id == "mark_commentary_send"
                ), "interim commentary send is not marked as commentary"


class TestRelayStripsMarker:
    @pytest.mark.asyncio
    async def test_relay_send_strips_commentary_marker(self):
        from tests.gateway.relay.test_relay_live_cards import _connected_adapter

        adapter, _ = _connected_adapter(supported_ops=("send", "edit", "typing", "draft"))

        class _T:
            def __init__(self):
                self.ops = []

            async def send_outbound(self, payload, platform=None):
                self.ops.append(dict(payload))
                return {"success": True, "message_id": "111.222"}

        t = _T()
        adapter._transport = t
        res = await adapter.send(
            "C1", "narration", metadata=mark_commentary_send({"thread_ts": "1.2"})
        )
        assert res.success
        sent = [o for o in t.ops if o["op"] == "send"]
        assert len(sent) == 1
        wire_md = sent[0].get("metadata") or {}
        assert "_interim_send" not in wire_md
        assert "_interim_kind" not in wire_md

    @pytest.mark.asyncio
    async def test_relay_send_for_platform_strips_commentary_marker(self):
        from tests.gateway.relay.test_relay_live_cards import _connected_adapter

        adapter, _ = _connected_adapter(supported_ops=("send", "edit", "typing", "draft"))

        class _T:
            def __init__(self):
                self.ops = []
                self._identities = [("slack", "U-bot")]

            async def send_outbound(self, payload, platform=None):
                self.ops.append(dict(payload))
                return {"success": True, "message_id": "111.333"}

        t = _T()
        adapter._transport = t
        res = await adapter.send_for_platform(
            "slack", "C2", "narration", metadata=mark_commentary_send({"thread_ts": "1.3"})
        )
        assert res.success
        sent = [o for o in t.ops if o["op"] == "send"]
        assert len(sent) == 1
        wire_md = sent[0].get("metadata") or {}
        assert "_interim_send" not in wire_md
        assert "_interim_kind" not in wire_md
