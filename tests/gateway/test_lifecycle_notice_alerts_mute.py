"""Gateway lifecycle notices stay out of act-now page channels (t_75a91ccc).

The alerts-handling session runs in fleet #alerts, so its own lifecycle chrome posted there:
"⚠️ Hermes is shutting down", "🔄 Restarted at 06:36 by default (kickstart -k) — planned" and
"🗜️ Context compacted (crossed the compaction threshold, ...)" were 11 of the 15 lines/day of
legacy:gateway-progress in the 2026-10-29 noise census (budget 3). They are the session's own
machinery, not a page. Other chats still get every notice, an UNPLANNED restart still posts, and the
requester's own chat on an in-chat /restart still gets the shutdown notice.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.warning_notifications as wn
from gateway.config import Platform
from gateway.fork_ext.unclean_restart_notice import PriorLifeVerdict
from gateway.platforms.base import SendResult
from gateway.response_filters import is_compaction_announce, lifecycle_notice_muted
from gateway.run import TurnRunner
from gateway.session import build_session_key
from gateway.turn_context import TurnContext
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

ALERTS = "1480528231286181948"
OTHER = "1536471262346477598"
COMPACTED = ("🗜️ Context compacted (crossed the compaction threshold, ~650,000 tokens): 412→38 messages"
             " · ~651K→~41K tokens · claude-dtlr/claude-fable-5-1 · engine: lcm")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_ALERTS_CHANNEL", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


# ── pure helpers ────────────────────────────────────────────────────────────────────────────

def test_alerts_channel_is_muted_by_default():
    assert lifecycle_notice_muted(Platform.DISCORD, ALERTS, {}) is True
    assert lifecycle_notice_muted("discord", ALERTS, None) is True   # shutdown fan-out passes a str


def test_other_chat_and_other_platform_are_not_muted():
    assert lifecycle_notice_muted(Platform.DISCORD, OTHER, {}) is False
    assert lifecycle_notice_muted(Platform.TELEGRAM, ALERTS, {}) is False
    assert lifecycle_notice_muted(Platform.DISCORD, "", {}) is False


def test_config_list_replaces_default_and_empty_disables():
    cfg = {"display": {"lifecycle_notice_muted_chats": ["3333"]}}
    assert lifecycle_notice_muted(Platform.TELEGRAM, "3333", cfg) is True
    assert lifecycle_notice_muted(Platform.DISCORD, ALERTS, cfg) is False
    assert lifecycle_notice_muted(Platform.DISCORD, ALERTS, {"display": {"lifecycle_notice_muted_chats": []}}) is False


def test_compaction_classifier_matches_the_formatter_only():
    assert is_compaction_announce(COMPACTED)
    assert is_compaction_announce("🗜️ Context compacted after model fallback: 286→34 messages")
    assert is_compaction_announce("🗜️ lcm: nothing to compact this pass · 214 messages")
    # in-progress status, retries and route notices are not the announce
    assert not is_compaction_announce("🗜️ Compacting context — summarizing earlier conversation so I can continue...")
    assert not is_compaction_announce("🗜️ Compressed 400 → 120 messages, retrying...")
    assert not is_compaction_announce("🔄 Model fallback (relay busy): a → b")
    assert not is_compaction_announce(None)


def test_classifier_matches_real_formatter_output():
    from agent.fork_ext.compaction_ext import _format_compaction_announce
    line = _format_compaction_announce(
        engine_name="lcm", status="compacted", old_session_id="s1", new_session_id="s1",
        old_messages=412, new_messages=38, pre_tokens=651_000, post_tokens=41_000,
        model="claude-fable-5-1", provider="claude-dtlr", summary_snippet=None,
        trigger_reason="threshold", trigger_value=650_000)
    assert line and is_compaction_announce(line), line


# ── in-turn compaction announce through _status_callback_sync ───────────────────────────────

class _Adapter:
    def __init__(self):
        self.delivered = []

    async def send(self, chat_id, content, metadata=None):
        self.delivered.append((chat_id, content))
        return SimpleNamespace(success=True, message_id=str(len(self.delivered)), error=None)

    async def edit_message(self, *a, **k):
        return SimpleNamespace(success=True, message_id="e", error=None)


def _drive_status(chat_id, message):
    adapter = _Adapter()

    async def scenario():
        loop = asyncio.get_running_loop()
        source = SimpleNamespace(platform=Platform.DISCORD, chat_id=chat_id)
        ctx = TurnContext(source=source, user_config={}, _run_still_current=lambda: True,
                          _loop_for_step=loop, _status_adapter=adapter,
                          _current_status_adapter=lambda: adapter, _status_chat_id=chat_id,
                          _status_thread_metadata=None)
        turn = TurnRunner(SimpleNamespace(), ctx)  # type: ignore[arg-type]
        result = await asyncio.to_thread(turn._status_callback_sync, "lifecycle", message)
        for _ in range(50):
            if adapter.delivered:
                break
            await asyncio.sleep(0.01)
        return result

    return adapter, asyncio.run(scenario())


def test_in_turn_compaction_announce_in_alerts_is_not_posted_and_not_a_lost_send(caplog):
    with caplog.at_level(logging.INFO):
        adapter, result = _drive_status(ALERTS, COMPACTED)
    assert adapter.delivered == []
    # _emit_status reads an explicit False as a LOST send (STATUS_CALLBACK_FAILED + re-announce)
    assert result is not False
    assert any("COMPACTION_ANNOUNCE_MUTED" in r.getMessage() for r in caplog.records)


def test_in_turn_compaction_announce_in_other_chat_is_posted():
    adapter, _ = _drive_status(OTHER, COMPACTED)
    assert [c for c, _ in adapter.delivered] == [OTHER]


def test_other_status_lines_in_alerts_still_post():
    # (the in-progress "Compacting context" status is already filtered for every chat upstream)
    adapter, _ = _drive_status(ALERTS, "⚠️ Provider returned an empty response, retrying")
    assert [c for c, _ in adapter.delivered] == [ALERTS]


# ── session-hygiene compaction announce (raw adapter.send rail) ─────────────────────────────

async def _hygiene_announce(monkeypatch, chat):
    import gateway.run as gr
    monkeypatch.setattr(gr, "_load_gateway_config",
                        lambda: {"display": {"lifecycle_notice_muted_chats": ["page-1"]}})
    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m"))
    agent = SimpleNamespace(context_compressor=SimpleNamespace(name="lcm", _last_compression_status="compacted"))
    await runner._announce_hygiene_compaction(
        agent=agent, source=make_restart_source(chat_id=chat, chat_type="group"), meta=None,
        old_session_id="s1", new_session_id="s1", eligible_count=412, new_count=38,
        pre_tokens=651_000, post_tokens=41_000, model="claude-fable-5-1",
        runtime={"provider": "claude-dtlr"}, trigger_reason="threshold", trigger_value=650_000)
    return [c.args for c in adapter.send.await_args_list]


@pytest.mark.asyncio
async def test_hygiene_compaction_announce_skips_page_channel(monkeypatch):
    assert await _hygiene_announce(monkeypatch, "page-1") == []


@pytest.mark.asyncio
async def test_hygiene_compaction_announce_posts_in_other_chat(monkeypatch):
    sent = await _hygiene_announce(monkeypatch, "work-2")
    assert len(sent) == 1 and sent[0][0] == "work-2" and is_compaction_announce(sent[0][1]), sent


# ── shutdown notice ─────────────────────────────────────────────────────────────────────────

def _muting(monkeypatch, *chats):
    monkeypatch.setattr(wn, "effective_user_config",
                        lambda: {"display": {"lifecycle_notice_muted_chats": list(chats)}})


@pytest.mark.asyncio
async def test_shutdown_notice_skips_page_channel_but_posts_elsewhere(monkeypatch):
    _muting(monkeypatch, "page-1")
    runner, adapter = make_restart_runner()
    for chat in ("page-1", "work-2"):
        src = make_restart_source(chat_id=chat, chat_type="group")
        key = build_session_key(src)
        runner._running_agents[key] = object()
        runner.session_store._entries[key] = MagicMock(origin=src)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m"))

    await runner._notify_active_sessions_of_shutdown()

    assert [c.args[0] for c in adapter.send.await_args_list] == ["work-2"]


@pytest.mark.asyncio
async def test_shutdown_home_channel_in_page_channel_is_skipped(monkeypatch):
    from gateway.config import HomeChannel
    _muting(monkeypatch, "page-1")
    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id="page-1", name="alerts")
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m"))

    await runner._notify_active_sessions_of_shutdown()

    adapter.send.assert_not_awaited()


# ── boot restart notice ─────────────────────────────────────────────────────────────────────

def _verdict(planned):
    return PriorLifeVerdict(unclean=True, boot_id="boot-1", planned=planned, planned_by="default",
                            planned_detail="kickstart -k", ended_at="2026-10-29T08:42:00+00:00",
                            killer=None if planned else "SIGKILL")


async def _restart_notice(tmp_path, chat, planned):
    runner, adapter = make_restart_runner()
    runner._prior_life_verdict = lambda: _verdict(planned)
    runner._unclean_restart_home = tmp_path
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m"))
    src = make_restart_source(chat_id=chat, chat_type="group")
    await runner._maybe_notify_unclean_restart(adapter, SimpleNamespace(source=src),
                                               build_session_key(src), "restart_interrupted")
    return [c.args for c in adapter.send.await_args_list]


@pytest.mark.asyncio
async def test_planned_restart_notice_skips_page_channel(tmp_path, monkeypatch):
    _muting(monkeypatch, "page-1")
    assert await _restart_notice(tmp_path, "page-1", planned=True) == []


@pytest.mark.asyncio
async def test_planned_restart_notice_posts_in_other_chat(tmp_path, monkeypatch):
    _muting(monkeypatch, "page-1")
    sent = await _restart_notice(tmp_path, "work-2", planned=True)
    assert len(sent) == 1 and sent[0][0] == "work-2" and "planned" in sent[0][1]


@pytest.mark.asyncio
async def test_unplanned_restart_notice_still_posts_in_page_channel(tmp_path, monkeypatch):
    _muting(monkeypatch, "page-1")
    sent = await _restart_notice(tmp_path, "page-1", planned=False)
    assert len(sent) == 1 and "UNPLANNED" in sent[0][1]
