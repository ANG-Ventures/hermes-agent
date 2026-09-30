"""Sibling-defect sweep for hermes-agent#1493 (t_11223645).

#1493 fixed Discord's head-preserving split cap: a long reply lost its END,
which is where the conclusion lives.  These tests cover every OTHER adapter's
split/cap path.  Defective paths (head-only slice of a single capped message)
are RED on the pre-fix tree; unbounded paths get a pin that the tail survives.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult, mark_commentary_send
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

TAIL = "CONCLUSIONXYZZY"


def _long(n: int, filler: str = "narration ") -> str:
    body = (filler * (n // len(filler) + 1))[:n]
    return body + "\n\n" + TAIL


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


class TestKeepHeadAndTailText:
    def test_within_limit_unchanged(self):
        from gateway.platforms.base import keep_head_and_tail_text

        assert keep_head_and_tail_text("short", 100) == "short"

    @pytest.mark.parametrize("n", [500, 5_000, 60_000])
    def test_over_limit_keeps_head_and_tail(self, n):
        from gateway.platforms.base import keep_head_and_tail_text

        text = "HEAD-START " + _long(n)
        out = keep_head_and_tail_text(text, 400)
        assert len(out) <= 400
        assert out.startswith("HEAD-START")
        assert out.endswith(TAIL)
        assert "characters omitted" in out

    def test_utf8_byte_budget(self):
        from gateway.platforms.base import keep_head_and_tail_text

        text = "开始" + "汉字" * 5_000 + TAIL
        out = keep_head_and_tail_text(text, 1_000, utf8_bytes=True)
        assert len(out.encode("utf-8")) <= 1_000
        assert out.startswith("开始")
        assert out.endswith(TAIL)


# ---------------------------------------------------------------------------
# Telegram — 4096-char splitter: unbounded (pin) + commentary one-message
# ---------------------------------------------------------------------------


def _telegram():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    adapter._rich_send_disabled = True
    return adapter


class TestTelegram:
    @pytest.mark.asyncio
    async def test_long_reply_delivers_every_chunk_including_the_end(self):
        adapter = _telegram()
        result = await adapter.send("123", _long(30_000))
        assert result.success is True
        texts = [c.kwargs["text"] for c in adapter._bot.send_message.await_args_list]
        assert len(texts) >= 8  # no cap: 30k chars -> 8+ messages
        assert TAIL in texts[-1]

    @pytest.mark.asyncio
    async def test_interim_commentary_is_one_message(self):
        adapter = _telegram()
        await adapter.send(
            "123", _long(12_000), metadata=mark_commentary_send(None)
        )
        texts = [c.kwargs["text"] for c in adapter._bot.send_message.await_args_list]
        assert len(texts) == 1
        assert "continued in session log" in texts[0]
        assert len(texts[0]) <= adapter.MAX_MESSAGE_LENGTH

    @pytest.mark.asyncio
    async def test_short_commentary_is_untouched(self):
        adapter = _telegram()
        await adapter.send("123", "checking the logs", metadata=mark_commentary_send(None))
        texts = [c.kwargs["text"] for c in adapter._bot.send_message.await_args_list]
        assert len(texts) == 1
        assert "continued in session log" not in texts[0]


# ---------------------------------------------------------------------------
# ntfy — single 4096-char body: was head-only
# ---------------------------------------------------------------------------

_ntfy = load_plugin_adapter("ntfy")


class TestNtfy:
    def _adapter(self):
        adapter = _ntfy.NtfyAdapter(PlatformConfig(enabled=True, extra={"topic": "t"}))
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"id": "x"}
        adapter._http_client = AsyncMock()
        adapter._http_client.post = AsyncMock(return_value=resp)
        return adapter

    @pytest.mark.asyncio
    async def test_send_over_limit_keeps_the_end(self):
        adapter = self._adapter()
        await adapter.send("t", "HEAD-START " + _long(20_000))
        raw = adapter._http_client.post.call_args.kwargs["content"]
        assert len(raw) <= _ntfy.MAX_MESSAGE_LENGTH  # ntfy's cap is BYTES
        body = raw.decode("utf-8")
        assert body.startswith("HEAD-START")
        assert body.endswith(TAIL)

    def test_standalone_body_keeps_the_end(self):
        raw = _ntfy._truncate_body("HEAD-START " + _long(20_000), context="t")
        assert len(raw) <= _ntfy.MAX_MESSAGE_LENGTH  # ntfy's cap is BYTES
        body = raw.decode("utf-8")
        assert body.startswith("HEAD-START")
        assert body.endswith(TAIL)

    def test_multibyte_body_within_byte_cap(self):
        raw = _ntfy._truncate_body("开始" + "汉字" * 5_000 + TAIL, context="t")
        assert len(raw) <= _ntfy.MAX_MESSAGE_LENGTH
        assert raw.decode("utf-8").endswith(TAIL)

    def test_standalone_short_body_unchanged(self):
        assert _ntfy._truncate_body("hi", context="t") == b"hi"


# ---------------------------------------------------------------------------
# WeCom — markdown 4000 chars / stream frame 20480 bytes: were head-only
# ---------------------------------------------------------------------------


def _wecom():
    from plugins.platforms.wecom.adapter import WeComAdapter

    return WeComAdapter(PlatformConfig(enabled=True))


class TestWeCom:
    @pytest.mark.asyncio
    async def test_reply_markdown_keeps_the_end(self):
        adapter = _wecom()
        adapter._send_reply_request = AsyncMock(return_value={"errcode": 0})
        await adapter._send_reply_markdown("req-1", "HEAD-START " + _long(9_000))
        content = adapter._send_reply_request.await_args.args[1]["markdown"]["content"]
        assert len(content) <= adapter.MAX_MESSAGE_LENGTH
        assert content.startswith("HEAD-START")
        assert content.endswith(TAIL)

    @pytest.mark.asyncio
    async def test_proactive_send_keeps_the_end(self):
        adapter = _wecom()
        adapter._send_request = AsyncMock(return_value={"errcode": 0, "headers": {}})
        await adapter._send_inner("chat-1", "HEAD-START " + _long(9_000))
        payload = adapter._send_request.await_args.args[1]
        content = payload["markdown"]["content"]
        assert len(content) <= adapter.MAX_MESSAGE_LENGTH
        assert content.endswith(TAIL)

    @pytest.mark.asyncio
    async def test_final_stream_frame_keeps_the_end(self):
        adapter = _wecom()
        adapter._send_reply_queued = AsyncMock(return_value={"errcode": 0})
        await adapter._send_stream_reply(
            "req-1", "s-1", "开始" + "汉字" * 12_000 + TAIL, finish=True,
        )
        content = adapter._send_reply_queued.await_args.args[1]["stream"]["content"]
        assert len(content.encode("utf-8")) <= adapter.MAX_STREAM_CONTENT_LENGTH
        assert content.startswith("开始")
        assert content.endswith(TAIL)


# ---------------------------------------------------------------------------
# Yuanbao — send_dm 10000-char pre-cut: was head-only
# ---------------------------------------------------------------------------


class TestYuanbao:
    @pytest.mark.asyncio
    async def test_send_dm_keeps_the_end(self):
        from gateway.platforms.yuanbao import YuanbaoAdapter

        adapter = YuanbaoAdapter.__new__(YuanbaoAdapter)
        adapter._access_policy = SimpleNamespace(is_dm_allowed=lambda _uid: True)
        adapter.send = AsyncMock(return_value=SendResult(success=True))
        await adapter.send_dm("u1", "HEAD-START " + _long(15_000))
        text = adapter.send.await_args.args[1]
        assert len(text) <= adapter.DM_MAX_CHARS
        assert text.startswith("HEAD-START")
        assert text.endswith(TAIL)


# ---------------------------------------------------------------------------
# WhatsApp Cloud — truncate_message, no cap (pin)
# ---------------------------------------------------------------------------


class TestWhatsAppCloudPin:
    @pytest.mark.asyncio
    async def test_long_reply_last_post_carries_the_end(self):
        from tests.gateway.test_whatsapp_cloud import _make_adapter, _mock_httpx_response

        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(
            return_value=_mock_httpx_response(200, {"messages": [{"id": "wamid.x"}]})
        )
        await adapter.send("15551234567", _long(30_000))
        bodies = [c.kwargs["json"]["text"]["body"] for c in adapter._http_client.post.call_args_list]
        assert len(bodies) >= 8
        assert TAIL in bodies[-1]


# ---------------------------------------------------------------------------
# LINE — fixed in #1493 (pin)
# ---------------------------------------------------------------------------


class TestLinePin:
    def test_split_for_line_keeps_the_end(self):
        line = load_plugin_adapter("line")
        chunks = line.split_for_line(_long(60_000))
        assert len(chunks) <= line.LINE_MAX_MESSAGES_PER_CALL
        assert chunks[-1].endswith(TAIL)
