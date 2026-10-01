"""Regression tests for the Discord split-delivery cap.

History:

* #86581 — a degenerate turn delivered 60,698 chars as 31 back-to-back
  messages.  A cap of ``MAX_SPLIT_MESSAGES`` was added.
* t_784a01bd (2026-09-29) — the #86581 cap was HEAD-preserving: it kept
  ``chunks[:N-1]`` and dropped the rest.  A 12-chunk reply delivered its
  opening narration and threw away the end, where a real reply keeps its
  conclusion.  Interim commentary (between-tool-call narration) went through
  the same path, so one narration block alone could hit the 8-message cap.

Invariants pinned here:

(a) a capped final reply always ENDS with its real last chunk;
(b) the elision notice is present and states how many messages were elided;
(c) never more than ``MAX_SPLIT_MESSAGES`` messages per logical response;
(d) interim commentary takes the same split + tail-preserving cap path as a
    final reply (t_9b9322a1 dropped the one-message collapse);
(e) chunk indicators on the delivered set are consistent (no ``(1/12)``
    next to ``(8/12)`` when only 7 content chunks were delivered).
"""

from __future__ import annotations

import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from gateway.platforms.base import mark_commentary_send  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


MAX = DiscordAdapter.MAX_MESSAGE_LENGTH
CAP = DiscordAdapter.MAX_SPLIT_MESSAGES
_TAG = re.compile(r" \((\d+)/(\d+)\)$")
NOTICE = "Response truncated"


def _make_adapter():
    return DiscordAdapter(PlatformConfig(enabled=True, token="***"))


def _huge_content(chars: int = 60_000) -> str:
    # Distinct filler — this test is about SIZE, not repetition.  The last
    # token is a sentinel standing in for the reply's conclusion.
    body = " ".join(f"word-{i}-" + "x" * 12 for i in range(chars // 20))
    return body + " FINAL-CONCLUSION-SENTINEL"


def _assert_tags_consistent(messages):
    """Every tagged message carries (i/total) with one shared total that
    equals the number of tagged messages, numbered 1..total in order."""
    tags = [_TAG.search(m) for m in messages]
    tagged = [(int(t.group(1)), int(t.group(2))) for t in tags if t]
    if not tagged:
        return
    totals = {total for _, total in tagged}
    assert totals == {len(tagged)}, f"inconsistent chunk totals: {tagged}"
    assert [i for i, _ in tagged] == list(range(1, len(tagged) + 1)), tagged


class TestCapSplitChunks:
    def test_below_cap_unchanged(self):
        adapter = _make_adapter()
        chunks = ["a", "b", "c"]
        assert adapter._cap_split_chunks(chunks) == chunks

    def test_at_cap_unchanged(self):
        adapter = _make_adapter()
        chunks = [f"c{i}" for i in range(CAP)]
        assert adapter._cap_split_chunks(chunks) == chunks

    @pytest.mark.parametrize("n", [CAP + 1, 12, 40])
    def test_over_cap_keeps_head_and_tail(self, n):
        adapter = _make_adapter()
        chunks = [f"chunk-{i}-" + "z" * 100 for i in range(n)]
        capped = adapter._cap_split_chunks(chunks)
        # (c) never more than the cap
        assert len(capped) <= CAP
        # (a) the reply ends with its real last chunk
        assert capped[-1] == chunks[-1]
        # context: the opening chunk is kept
        assert capped[0] == chunks[0]
        # (b) exactly one notice, stating the elided message count
        notices = [c for c in capped if NOTICE in c]
        assert len(notices) == 1
        elided = n - (CAP - 1)
        assert f"{elided} messages" in notices[0]
        assert len(notices[0]) <= MAX
        # the notice sits between the head and the tail, never at the end
        assert capped.index(notices[0]) == 1
        # the delivered content is chunks[0] + the last CAP-2 chunks
        content = [c for c in capped if NOTICE not in c]
        assert content == [chunks[0], *chunks[-(CAP - 2):]]

    def test_indicator_tags_are_renumbered(self):
        adapter = _make_adapter()
        content = _huge_content(24_000)
        chunks = adapter.truncate_message(content, MAX)
        assert len(chunks) > CAP, "fixture must exceed the cap"
        capped = adapter._cap_split_chunks(chunks)
        assert len(capped) == CAP
        _assert_tags_consistent(capped)
        # last delivered message carries the real end of the reply
        assert "FINAL-CONCLUSION-SENTINEL" in capped[-1]
        assert all(len(c) <= MAX for c in capped)


class TestSendCap:
    @staticmethod
    def _wire(adapter):
        sends = []

        async def fake_send(*, content, reference=None):
            sends.append(content)
            return SimpleNamespace(id=9000 + len(sends))

        channel = SimpleNamespace(id=555, send=AsyncMock(side_effect=fake_send))
        adapter._client = SimpleNamespace(
            get_channel=lambda _cid: channel,
            fetch_channel=AsyncMock(),
        )
        return sends

    @pytest.mark.asyncio
    async def test_send_caps_split_flood_and_keeps_conclusion(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        sends = self._wire(adapter)

        result = await adapter.send("555", _huge_content())

        assert result.success is True
        assert len(sends) == CAP
        assert "FINAL-CONCLUSION-SENTINEL" in sends[-1]
        assert NOTICE not in sends[-1]
        assert sum(NOTICE in s for s in sends) == 1
        _assert_tags_consistent(sends)

    @pytest.mark.asyncio
    async def test_interim_commentary_uses_normal_split_no_marker(self, monkeypatch, tmp_path):
        """(d) Commentary is split like any reply (t_9b9322a1): a 3-chunk
        narration arrives as 3 messages, nothing elided, no footer."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        sends = self._wire(adapter)
        content = _huge_content(5_000)
        expected = adapter.truncate_message(content, MAX)
        assert len(expected) == 3

        result = await adapter.send(
            "555", content, metadata=mark_commentary_send(None)
        )

        assert result.success is True
        assert sends == expected
        assert not any("continued in session log" in s for s in sends)
        assert "FINAL-CONCLUSION-SENTINEL" in sends[-1]

    @pytest.mark.asyncio
    async def test_long_interim_commentary_gets_the_tail_preserving_cap(self, monkeypatch, tmp_path):
        """A 12-chunk narration is capped like a final reply: head + notice
        + tail, never more than ``MAX_SPLIT_MESSAGES``."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        sends = self._wire(adapter)
        content = _huge_content(22_000)
        assert len(adapter.truncate_message(content, MAX)) >= 12

        await adapter.send("555", content, metadata=mark_commentary_send(None))

        assert len(sends) == CAP
        assert "FINAL-CONCLUSION-SENTINEL" in sends[-1]
        assert sum(NOTICE in s for s in sends) == 1
        assert not any("continued in session log" in s for s in sends)
        _assert_tags_consistent(sends)

    @pytest.mark.asyncio
    async def test_short_commentary_is_untouched(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        sends = self._wire(adapter)

        await adapter.send("555", "checking the log", metadata=mark_commentary_send(None))

        assert sends == ["checking the log"]

    @pytest.mark.asyncio
    async def test_other_interim_sends_are_not_collapsed(self, monkeypatch, tmp_path):
        """Heartbeats / approval fallbacks (``_interim_send``) keep their
        tail under the cap — a plain-text approval prompt must never lose it."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        sends = self._wire(adapter)

        await adapter.send("555", _huge_content(), metadata={"_interim_send": True})

        assert len(sends) == CAP
        assert "FINAL-CONCLUSION-SENTINEL" in sends[-1]


class TestForumCap:
    @pytest.mark.asyncio
    async def test_send_to_forum_caps_and_keeps_conclusion(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        thread_sends = []

        async def fake_thread_send(*, content):
            thread_sends.append(content)
            return SimpleNamespace(id=8000 + len(thread_sends))

        thread_channel = SimpleNamespace(
            id=777, send=AsyncMock(side_effect=fake_thread_send)
        )
        forum_channel = SimpleNamespace(
            id=666,
            type=SimpleNamespace(value=15),
            create_thread=AsyncMock(return_value=SimpleNamespace(
                id=777,
                thread=thread_channel,
                message=SimpleNamespace(id=8000),
            )),
        )

        result = await adapter._send_to_forum(forum_channel, _huge_content())

        assert result.success is True
        # 1 starter message + at most (CAP - 1) follow-up chunks.
        assert len(thread_sends) <= CAP - 1
        assert "FINAL-CONCLUSION-SENTINEL" in thread_sends[-1]
        assert sum(NOTICE in s for s in thread_sends) == 1


class TestEditOverflowCap:
    @pytest.mark.asyncio
    async def test_edit_overflow_split_capped_and_keeps_conclusion(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        edits = []
        sends = []

        async def fake_edit(*, content):
            edits.append(content)

        async def fake_send(*, content, reference=None):
            sends.append(content)
            return SimpleNamespace(id=9000 + len(sends))

        msg = SimpleNamespace(id=42, edit=AsyncMock(side_effect=fake_edit))
        channel = SimpleNamespace(id=555, send=AsyncMock(side_effect=fake_send))

        result = await adapter._edit_overflow_split(channel, msg, "42", _huge_content())

        assert result.success is True
        # 1 in-place edit + at most (CAP - 1) continuation sends.
        assert len(edits) == 1
        assert len(sends) <= CAP - 1
        assert "FINAL-CONCLUSION-SENTINEL" in sends[-1]
        assert sum(NOTICE in s for s in sends) == 1
        # the consumer keeps editing the LAST visible message: the real tail
        assert result.message_id == str(9000 + len(sends))
        _assert_tags_consistent([*edits, *sends])
