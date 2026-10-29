"""Needs-input pager #alerts budget (t_081bb603): a card homed in #alerts (or
with no home) pages #alerts once per (card, reason); the 2 h "still waiting"
re-pages go to #logs. 15 of the 20 #alerts needs-ruling lines on 2026-10-20
were those re-pages.

t_d36da044 (13 lines on 2026-10-29 vs a budget of 3): the p>=200 #alerts mirror
re-fired on every 2 h re-page, and cards homed in #alerts below the pager's own
priority floor (100) paged #alerts only because #alerts was their home.
"""
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


@pytest.mark.parametrize("priority", [100, 250])
def test_alerts_homed_card_pages_once_then_repages_to_logs(calls, priority):
    alerts = priority >= 200
    assert kw._send_needs_input_page("default", _item(ALERTS, priority, alerts))
    assert calls == [["--target", ALERTS]]  # no self-mirror at p>=200
    calls.clear()
    assert kw._send_needs_input_page("default", _item(ALERTS, priority, alerts), True)
    assert calls == [["--target", LOGS]]


@pytest.mark.parametrize("priority", [0, 45, 99, None, "junk"])
def test_alerts_homed_card_below_floor_posts_to_logs(calls, priority):
    assert kw._send_needs_input_page("default", _item(ALERTS, priority))
    assert calls == [["--target", LOGS]]
    calls.clear()
    assert kw._send_needs_input_page("default", _item(ALERTS, priority), True)
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


def test_other_origin_p200_mirrors_alerts_on_first_page_only(calls):
    assert kw._send_needs_input_page("default", _item(OTHER, 200, True))
    assert calls == [["--target", OTHER], ["--sev", "error"]]
    calls.clear()
    assert kw._send_needs_input_page("default", _item(OTHER, 200, True), True)
    assert calls == [["--target", OTHER]]


def test_channel_ids_follow_notify_env(calls, monkeypatch):
    monkeypatch.setenv("DISCORD_ALERTS_CHANNEL", "111")
    monkeypatch.setenv("DISCORD_LOGS_CHANNEL", "222")
    assert kw._send_needs_input_page("default", _item("111"), True)
    assert calls == [["--target", "222"]]


# The 12 needs-input pages in the 2026-10-29 06:30 census of #alerts (family
# legacy:kanban-needs-ruling; the 13th row was an unrelated chat line that the
# census's 15 s continuation rule attached to the 10:03:46Z re-page).
# (card, priority, origin channel, still_waiting) as read from the board.
_CENSUS_2026_10_29 = [
    ("t_b526ea53", 50, ALERTS, False),
    ("t_3f0a2ddd", 0, ALERTS, False),
    ("t_e779d28a", 3, ALERTS, False),
    ("t_91dc0628", 45, ALERTS, False),
    ("t_4c991c91", 200, "1563065398943940649", False),
    ("t_4c991c91", 200, "1563065398943940649", True),
    ("t_4c991c91", 200, "1563065398943940649", True),
    ("t_6a1013d1", 45, ALERTS, False),
    ("t_41aabdd2", 200, "1561924267669594182", False),
    ("t_ee514d57", 65, ALERTS, False),
    ("t_41aabdd2", 200, "1561924267669594182", True),
    ("t_0865fe02", 45, ALERTS, False),
]


def test_census_replay_fits_the_family_budget(calls):
    for tid, priority, channel, still in _CENSUS_2026_10_29:
        item = {"task_id": tid, "title": "t", "priority": priority, "reason": "r",
                "channel": channel, "alerts": priority >= 200}
        assert kw._send_needs_input_page("default", item, still)
    to_alerts = [c for c in calls if c == ["--sev", "error"] or c == ["--target", ALERTS]]
    assert len(to_alerts) == 2  # was 12; budget <= 3/day
    assert calls.count(["--target", LOGS]) == 7


# r1 (Prism 560c78346c1a): mirror delivery has its own ledger entry, so a p>=200
# mirror lost on the first page is retried on the next page, exactly once.
_REPAGE = kw._NEEDS_INPUT_REPAGE_SECONDS


def _pager_ticks(monkeypatch, tmp_path, mirror_results):
    monkeypatch.delenv("DISCORD_ALERTS_CHANNEL", raising=False)
    monkeypatch.delenv("DISCORD_LOGS_CHANNEL", raising=False)
    monkeypatch.setattr(kw, "_alert_notify_script", lambda: tmp_path / "notify.py")
    results = iter(mirror_results)
    out: list = []

    def fake_send(script, msg, extra):
        out.append(extra)
        return next(results) if extra == ["--sev", "error"] else True

    monkeypatch.setattr(kw, "_notify_send", fake_send)
    cards = [("default", _item(OTHER, 200, True))]
    per_tick = []
    for n in range(3):
        out.clear()
        # A fresh pager each tick re-reads the ledger, as after a gateway restart.
        pager = kw._NeedsInputPager(tmp_path / "ledger.json")
        assert pager.observe(cards, kw._send_needs_input_page, {"default"},
                             now=1000.0 + n * (_REPAGE + 1)) == 1
        per_tick.append([c for c in out if c == ["--sev", "error"]])
    return per_tick


def test_failed_first_mirror_is_retried_once_on_repage(monkeypatch, tmp_path):
    per_tick = _pager_ticks(monkeypatch, tmp_path, [False, True])
    assert per_tick == [[["--sev", "error"]], [["--sev", "error"]], []]


def test_delivered_mirror_is_never_repeated(monkeypatch, tmp_path):
    per_tick = _pager_ticks(monkeypatch, tmp_path, [True])
    assert per_tick == [[["--sev", "error"]], [], []]
