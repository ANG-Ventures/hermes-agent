"""Background-review receipts stay out of act-now page channels (t_a823c573).

"💾 Self-improvement review: Skill '...' patched" was posted 20x/day into fleet #alerts because
the alerts-handling session runs IN #alerts and every review receipt went to the turn's chat.
A receipt is not a page; the review still runs, only the chat post is dropped.
"""

import types

import pytest

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner

ALERTS = "1480528231286181948"
MSG = "💾 Self-improvement review: Skill 'handle-alerts-and-logs' patched"


def _runner(chat_id, platform=Platform.DISCORD, user_config=None):
    sent = []
    ctx = types.SimpleNamespace(
        source=types.SimpleNamespace(chat_id=chat_id, platform=platform),
        user_config=user_config or {},
        _status_thread_metadata=None,
    )
    tr = TurnRunner(runner=types.SimpleNamespace(), ctx=ctx)
    tr._status_live = lambda: True
    tr._send_status_text = lambda text, metadata, log_message: sent.append(text)
    return tr, sent


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("DISCORD_ALERTS_CHANNEL", raising=False)


def _fire(tr):
    send, release = tr._make_bg_review_callbacks()
    send(MSG)
    release()
    send(MSG)


def test_alerts_channel_drops_review_receipt():
    tr, sent = _runner(ALERTS)
    _fire(tr)
    assert sent == []


def test_other_channel_still_gets_review_receipt():
    tr, sent = _runner("1111")
    _fire(tr)
    assert sent == [MSG, MSG]


def test_non_discord_platform_with_same_id_not_muted_by_default():
    tr, sent = _runner(ALERTS, platform=Platform.TELEGRAM)
    _fire(tr)
    assert sent == [MSG, MSG]


def test_env_alerts_channel_is_honoured(monkeypatch):
    monkeypatch.setenv("DISCORD_ALERTS_CHANNEL", "2222")
    tr, sent = _runner("2222")
    _fire(tr)
    assert sent == []


def test_config_list_overrides_default():
    cfg = {"display": {"memory_notifications_muted_chats": ["3333"]}}
    tr, sent = _runner("3333", user_config=cfg)
    _fire(tr)
    assert sent == []
    tr, sent = _runner(ALERTS, user_config=cfg)
    _fire(tr)
    assert sent == [MSG, MSG]


def test_config_empty_list_disables_mute():
    tr, sent = _runner(ALERTS, user_config={"display": {"memory_notifications_muted_chats": []}})
    _fire(tr)
    assert sent == [MSG, MSG]
