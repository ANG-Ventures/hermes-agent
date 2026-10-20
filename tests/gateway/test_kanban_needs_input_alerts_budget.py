"""Needs-input pager #alerts budget (t_081bb603): a card homed in #alerts (or
with no home) pages #alerts once per (card, reason); the 2 h "still waiting"
re-pages go to #logs. 15 of the 20 #alerts needs-ruling lines on 2026-10-20
were those re-pages."""
from __future__ import annotations

import pytest

from gateway import kanban_watchers as kw

ALERTS = kw._NEEDS_INPUT_ALERTS_CHANNEL
LOGS = kw._NEEDS_INPUT_LOGS_CHANNEL
OTHER = "1553876718639390760"


@pytest.fixture
def calls(monkeypatch, tmp_path):
    monkeypatch.delenv("DISCORD_ALERTS_CHANNEL", raising=False)
    monkeypatch.delenv("DISCORD_LOGS_CHANNEL", raising=False)
    out: list = []
    monkeypatch.setattr(kw, "_alert_notify_script", lambda: tmp_path / "notify.py")
    monkeypatch.setattr(kw, "_notify_send", lambda script, msg, extra: out.append(extra) or True)
    return out


def _item(channel, priority=0, alerts=False):
    return {"task_id": "t_1", "title": "p", "priority": priority, "reason": "x",
            "channel": channel, "alerts": alerts}


@pytest.mark.parametrize("alerts", [False, True])
def test_alerts_homed_card_pages_once_then_repages_to_logs(calls, alerts):
    assert kw._send_needs_input_page("default", _item(ALERTS, 250 if alerts else 0, alerts))
    assert calls == [["--target", ALERTS]]  # no self-mirror at p>=200
    calls.clear()
    assert kw._send_needs_input_page("default", _item(ALERTS, alerts=alerts), True)
    assert calls == [["--target", LOGS]]


def test_unhomed_card_pages_alerts_once_then_repages_to_logs(calls):
    assert kw._send_needs_input_page("default", _item(None, 150))
    assert calls == [["--sev", "error"]]
    calls.clear()
    assert kw._send_needs_input_page("default", _item(None, 150), True)
    assert calls == [["--target", LOGS]]


def test_other_origin_channel_unchanged(calls):
    assert kw._send_needs_input_page("default", _item(OTHER), True)
    assert calls == [["--target", OTHER]]


def test_channel_ids_follow_notify_env(calls, monkeypatch):
    monkeypatch.setenv("DISCORD_ALERTS_CHANNEL", "111")
    monkeypatch.setenv("DISCORD_LOGS_CHANNEL", "222")
    assert kw._send_needs_input_page("default", _item("111"), True)
    assert calls == [["--target", "222"]]
