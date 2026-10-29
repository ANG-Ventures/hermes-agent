"""Route-change notices stay out of act-now page channels (t_733b4104).

The alerts-handling session runs in fleet #alerts, so every fallback hop its turn rode out
("🔄 Model fallback (relay busy): claude-dtlr/... → claude-btpr/...") posted there: 30 lines/day in
the 2026-10-26 noise census (family ?:model-fallback-relay-busy, budget 3). A hop the agent already
survived is telemetry, not a page. Other chats still get the notice, and a muted notice is never
queued for redelivery.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from gateway import route_notice_outbox as rno
from gateway.config import Platform
from gateway.response_filters import route_notice_muted
from gateway.run import TurnRunner
from gateway.turn_context import TurnContext

ALERTS = "1480528231286181948"
OTHER = "1536471262346477598"
ROUTE_MESSAGE = (
    "🔄 Model fallback (relay busy): claude-dtlr/claude-fable-5-1 → claude-btpr/claude-fable-5-1"
    " — relay busy: all subs at capacity (at the relay), 21:08:57"
)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_ALERTS_CHANNEL", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _src(chat_id=ALERTS, platform=Platform.DISCORD):
    return SimpleNamespace(chat_id=chat_id, platform=platform)


# ── pure helper ─────────────────────────────────────────────────────────────────────────────

def test_alerts_channel_is_muted_by_default():
    assert route_notice_muted(_src(), {}) is True
    assert route_notice_muted(_src(), None) is True


def test_other_chat_is_not_muted():
    assert route_notice_muted(_src(OTHER), {}) is False


def test_non_discord_same_id_not_muted_by_default():
    assert route_notice_muted(_src(platform=Platform.TELEGRAM), {}) is False


def test_env_override_names_the_alerts_channel(monkeypatch):
    monkeypatch.setenv("DISCORD_ALERTS_CHANNEL", "2222")
    assert route_notice_muted(_src("2222"), {}) is True
    assert route_notice_muted(_src(), {}) is False


def test_config_list_replaces_default_and_empty_disables():
    cfg = {"display": {"route_notice_muted_chats": ["3333"]}}
    assert route_notice_muted(_src("3333", Platform.TELEGRAM), cfg) is True
    assert route_notice_muted(_src(), cfg) is False
    assert route_notice_muted(_src(), {"display": {"route_notice_muted_chats": []}}) is False


# ── through _status_callback_sync ───────────────────────────────────────────────────────────

class _Adapter:
    def __init__(self):
        self.delivered = []

    async def send(self, chat_id, content, metadata=None):
        self.delivered.append((chat_id, content))
        return SimpleNamespace(success=True, message_id=str(len(self.delivered)), error=None)


def _runner(adapter, loop, chat_id):
    source = SimpleNamespace(platform=Platform.DISCORD, chat_id=chat_id)
    ctx = TurnContext(
        source=source,
        user_config={},
        _run_still_current=lambda: True,
        _loop_for_step=loop,
        _status_adapter=adapter,
        _current_status_adapter=lambda: adapter,
        _status_chat_id=chat_id,
        _status_thread_metadata=None,
    )
    return TurnRunner(SimpleNamespace(), ctx)  # type: ignore[arg-type]


def _drive(chat_id):
    adapter = _Adapter()

    async def scenario():
        loop = asyncio.get_running_loop()
        turn = _runner(adapter, loop, chat_id)
        result = await asyncio.to_thread(turn._status_callback_sync, "lifecycle", ROUTE_MESSAGE)
        for _ in range(50):
            if adapter.delivered:
                break
            await asyncio.sleep(0.01)
        return result

    return adapter, asyncio.run(scenario())


def test_route_notice_in_alerts_is_not_posted_or_queued(caplog):
    with caplog.at_level(logging.INFO, logger="gateway.run_turn_runner"):
        adapter, result = _drive(ALERTS)
    assert adapter.delivered == []
    assert result is False
    assert rno.default_outbox().pending(rno.chat_key(Platform.DISCORD, ALERTS, None)) == []
    assert not [r for r in caplog.records if "route-change status dropped" in r.getMessage()]


def test_route_notice_in_other_chat_is_posted():
    adapter, result = _drive(OTHER)
    assert result is True
    assert [c for c, _ in adapter.delivered] == [OTHER]
    assert adapter.delivered[0][1].startswith("🔄 Model fallback (relay busy)")
