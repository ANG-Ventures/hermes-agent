"""Needs-input pager #alerts budget (t_081bb603): a card homed in #alerts (or
with no home) pages #alerts once per (card, reason); the 2 h "still waiting"
re-pages go to #logs. 15 of the 20 #alerts needs-ruling lines on 2026-10-20
were those re-pages.

t_d36da044 (13 lines on 2026-10-29 vs a budget of 3): the p>=200 #alerts mirror
re-fired on every 2 h re-page, and cards homed in #alerts below the pager's own
priority floor (100) paged #alerts only because #alerts was their home.

t_c5800c91 (55 lines on 2026-11-04 vs a budget of 3): 42 came from cards whose
``origin:`` line names no channel but whose home session is a Discord chat, so
they fell back to #alerts; the p>=200 mirror paged a second time for a card
that already paged its home; and one card re-blocked with 8 new reasons paged
#alerts 8 times. A card's home now includes its session's chat, there is no
mirror, and #alerts takes one line per card per 24 h.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from gateway import kanban_watchers as kw
from hermes_cli import kanban_db as kb

ALERTS = kw._NEEDS_INPUT_ALERTS_CHANNEL
LOGS = kw._NEEDS_INPUT_LOGS_CHANNEL
OTHER = "1553876718639390760"


@pytest.fixture
def calls(monkeypatch, tmp_path):
    monkeypatch.delenv("DISCORD_ALERTS_CHANNEL", raising=False)
    monkeypatch.delenv("DISCORD_LOGS_CHANNEL", raising=False)
    out: list = []
    monkeypatch.setattr(kw, "_alert_notify_script", lambda: tmp_path / "notify.py")
    monkeypatch.setattr(kw, "_notify_send", lambda script, msg, extra, **kwargs: out.append(extra) or True)
    return out


def _item(channel, priority=0, reason="x", task_id="t_1", session_id=None):
    return {"task_id": task_id, "title": "p", "priority": priority, "reason": reason,
            "channel": channel, "session_id": session_id}


@pytest.mark.parametrize("priority", [100, 250])
def test_alerts_homed_card_pages_once_then_repages_to_logs(calls, priority):
    assert kw._send_needs_input_page("default", _item(ALERTS, priority))
    assert calls == [["--target", ALERTS]]  # no self-mirror at p>=200
    calls.clear()
    assert kw._send_needs_input_page("default", _item(ALERTS, priority), True)
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


def test_other_origin_p200_pages_only_its_home(calls):
    assert kw._send_needs_input_page("default", _item(OTHER, 200))
    assert kw._send_needs_input_page("default", _item(OTHER, 300), True)
    assert calls == [["--target", OTHER], ["--target", OTHER]]  # no #alerts mirror


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
                "channel": channel}
        assert kw._send_needs_input_page("default", item, still)
    to_alerts = [c for c in calls if c == ["--sev", "error"] or c == ["--target", ALERTS]]
    assert len(to_alerts) == 0  # was 12, then 2 (p>=200 mirrors); budget <= 3/day
    assert calls.count(["--target", LOGS]) == 7


_REPAGE = kw._NEEDS_INPUT_REPAGE_SECONDS
_WINDOW = kw._NEEDS_INPUT_ALERTS_CARD_WINDOW_S


# r2 (Prism 7dfa6f11a80d): a card homed in #alerts has its own "alerts-first"
# ledger entry, so one raised over the floor after paging #logs below it still
# gets its first #alerts page, exactly once.
def _alerts_homed_ticks(monkeypatch, tmp_path, priorities, alerts_results=None):
    monkeypatch.delenv("DISCORD_ALERTS_CHANNEL", raising=False)
    monkeypatch.delenv("DISCORD_LOGS_CHANNEL", raising=False)
    monkeypatch.setattr(kw, "_alert_notify_script", lambda: tmp_path / "notify.py")
    results = iter(alerts_results or [])
    out: list = []

    def fake_send(script, msg, extra, **kwargs):
        out.append(extra)
        return next(results, True) if extra == ["--target", ALERTS] else True

    monkeypatch.setattr(kw, "_notify_send", fake_send)
    per_tick = []
    for n, priority in enumerate(priorities):
        out.clear()
        cards = [("default", _item(ALERTS, priority))]
        pager = kw._NeedsInputPager(tmp_path / "ledger.json")
        pager.observe(cards, kw._send_needs_input_page, {"default"},
                      now=1000.0 + n * (_REPAGE + 1))
        per_tick.append(list(out))
    return per_tick


def test_alerts_homed_card_raised_over_floor_gets_first_alerts_page_once(monkeypatch, tmp_path):
    per_tick = _alerts_homed_ticks(monkeypatch, tmp_path, [50, 200, 200, 200])
    assert per_tick == [
        [["--target", LOGS]],    # p50 at t=1000: below the floor
        [["--target", ALERTS]],  # raised to p200: first #alerts page
        [["--target", LOGS]],    # later pages follow the re-page rule
        [["--target", LOGS]],
    ]


def test_failed_first_alerts_page_is_retried(monkeypatch, tmp_path):
    per_tick = _alerts_homed_ticks(monkeypatch, tmp_path, [50, 200, 200, 200],
                                   alerts_results=[False, True])
    # The failed send stays due, so the next tick (same window) retries it.
    assert per_tick[0] == [["--target", LOGS]]
    assert per_tick[1] == [["--target", ALERTS]]
    assert per_tick[2] == [["--target", ALERTS]]
    assert per_tick[3] == [["--target", LOGS]]


# t_c5800c91 ------------------------------------------------------------------

SESSION_CHAT = "1566803268141588561"


def _state_db(path, sessions):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, origin_json TEXT)")
    con.executemany("INSERT INTO sessions VALUES (?, ?)",
                    [(sid, json.dumps(origin)) for sid, origin in sessions.items()])
    con.commit()
    con.close()


@pytest.fixture
def root(monkeypatch, tmp_path):
    home = tmp_path / "root"
    _state_db(home / "state.db", {
        "s_discord": {"platform": "discord", "chat_id": SESSION_CHAT},
        "s_thread": {"platform": "discord", "chat_id": OTHER, "thread_id": "1567000000000000001"},
        "s_alerts": {"platform": "discord", "chat_id": ALERTS},
        "s_cli": {"platform": "cli", "chat_id": None},
    })
    # A profile store without a sessions table is skipped, not fatal (seen live).
    (home / "profiles" / "broken").mkdir(parents=True)
    sqlite3.connect(home / "profiles" / "broken" / "state.db").close()
    _state_db(home / "profiles" / "coder" / "state.db",
              {"s_profile": {"platform": "discord", "chat_id": OTHER}})
    monkeypatch.setattr(kb, "kanban_home", lambda: home)
    return home


@pytest.mark.parametrize("session_id, expected", [
    ("s_discord", ["--target", SESSION_CHAT]),
    ("s_thread", ["--target", "1567000000000000001"]),  # a thread is its own channel
    ("s_profile", ["--target", OTHER]),                  # found in a profile's store
    ("s_alerts", ["--target", ALERTS]),                  # homed in #alerts: first page there
    ("s_cli", ["--sev", "error"]),                       # no chat: #alerts fallback, as before
    ("s_missing", ["--sev", "error"]),
    (None, ["--sev", "error"]),
])
def test_unhomed_origin_line_pages_the_session_chat(calls, root, session_id, expected):
    assert kw._send_needs_input_page("default", _item(None, 150, session_id=session_id))
    assert calls == [expected]


def test_origin_line_channel_wins_over_the_session_chat(calls, root):
    assert kw._send_needs_input_page("default", _item(OTHER, 150, session_id="s_discord"))
    assert calls == [["--target", OTHER]]


def test_foreign_session_uses_its_transport_profile(monkeypatch, tmp_path, root):
    con = sqlite3.connect(root / "profiles" / "coder" / "state.db")
    con.execute("ALTER TABLE sessions ADD COLUMN transport_profile TEXT")
    con.execute("UPDATE sessions SET transport_profile = 'receiving_bot'")
    con.commit()
    con.close()
    sent = []
    monkeypatch.setattr(kw, "_alert_notify_script", lambda: tmp_path / "notify.py")
    monkeypatch.setattr(kw.subprocess, "run", lambda argv, **kwargs:
                        sent.append(argv) or type("Result", (), {"returncode": 0})())
    assert kw._send_needs_input_page("default", _item(None, 150, session_id="s_profile"))
    assert len(sent) == 1
    assert sent[0][sent[0].index("--profile") + 1] == "receiving_bot"
    assert sent[0][-2:] == ["--target", OTHER]


def test_failed_session_home_falls_back_once_per_card(monkeypatch, tmp_path, root):
    sent = []
    monkeypatch.setattr(kw, "_alert_notify_script", lambda: tmp_path / "notify.py")

    def run(argv, **kwargs):
        sent.append(argv)
        failed = argv[-2:] == ["--target", OTHER]
        return type("Result", (), {"returncode": 1 if failed else 0})()

    monkeypatch.setattr(kw.subprocess, "run", run)
    for now, reason in [(1000, "first"), (1001, "second"), (9000, "second")]:
        pager = kw._NeedsInputPager(tmp_path / "ledger.json")
        assert pager.observe([("default", _item(None, 150, reason, session_id="s_profile"))],
                             kw._send_needs_input_page, {"default"}, now=now) == 1
    alerts = [argv for argv in sent if argv[-2:] == ["--sev", "error"]]
    assert len(alerts) == 1
    assert "t_1" in alerts[0][3] and OTHER in alerts[0][3]
    assert alerts[0][alerts[0].index("--profile") + 1] == "default"
    assert sum(argv[-2:] == ["--target", LOGS] for argv in sent) == 2
    assert sent[0][sent[0].index("--profile") + 1] == "coder"


def test_candidates_carry_the_session_id(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="p", body="origin: cli", priority=150, assignee=None,
                             session_id="s_discord")
        assert kb.block_task(conn, tid, reason="which?", kind="needs_input")
        (item,) = kb.needs_input_page_candidates(conn)
    assert (item["task_id"], item["channel"], item["session_id"]) == (tid, None, "s_discord")


def _pager_run(monkeypatch, tmp_path, steps, results=None):
    """``steps``: (now, item, still_waiting) through the real pager and sender."""
    monkeypatch.delenv("DISCORD_ALERTS_CHANNEL", raising=False)
    monkeypatch.delenv("DISCORD_LOGS_CHANNEL", raising=False)
    monkeypatch.setattr(kw, "_alert_notify_script", lambda: tmp_path / "notify.py")
    results = iter(results or [])
    out: list = []

    def fake_send(script, msg, extra, **kwargs):
        out.append(extra)
        return next(results, True)

    monkeypatch.setattr(kw, "_notify_send", fake_send)
    per_step = []
    for now, item, _still in steps:
        out.clear()
        # A fresh pager per step re-reads the ledger, as after a gateway restart.
        pager = kw._NeedsInputPager(tmp_path / "ledger.json")
        pager.observe([("default", item)], kw._send_needs_input_page, {"default"}, now=now)
        per_step.append(list(out))
    return per_step


@pytest.mark.parametrize("channel, first", [(ALERTS, ["--target", ALERTS]),
                                            (None, ["--sev", "error"])])
def test_new_reasons_page_alerts_once_per_card_per_window(monkeypatch, tmp_path, channel, first):
    t0 = 1000.0
    steps = [(t0, _item(channel, 310, "r1"), False),
             (t0 + 3600, _item(channel, 310, "r2"), False),
             (t0 + 3 * 3600, _item(channel, 310, "r3"), False),
             (t0 + _WINDOW + 1, _item(channel, 310, "r4"), False)]
    assert _pager_run(monkeypatch, tmp_path, steps) == [
        [first], [["--target", LOGS]], [["--target", LOGS]], [first]]


def test_withheld_reason_never_reaches_alerts_on_its_repages(monkeypatch, tmp_path):
    t0 = 1000.0
    steps = [(t0, _item(ALERTS, 310, "r1"), False),
             (t0 + 3600, _item(ALERTS, 310, "r2"), False),          # withheld -> #logs
             (t0 + _WINDOW + 3600, _item(ALERTS, 310, "r2"), True)]  # window over, still #logs
    assert _pager_run(monkeypatch, tmp_path, steps) == [
        [["--target", ALERTS]], [["--target", LOGS]], [["--target", LOGS]]]


def test_failed_alerts_page_does_not_open_the_window(monkeypatch, tmp_path):
    steps = [(1000.0, _item(None, 310, "r1"), False),
             (1005.0, _item(None, 310, "r1"), False),
             (1000.0 + 3600, _item(None, 310, "r2"), False)]
    assert _pager_run(monkeypatch, tmp_path, steps, results=[False, True]) == [
        [["--sev", "error"]], [["--sev", "error"]], [["--target", LOGS]]]


def test_other_cards_keep_their_own_window(monkeypatch, tmp_path):
    steps = [(1000.0, _item(ALERTS, 310, "r1", task_id="t_a"), False),
             (1060.0, _item(ALERTS, 310, "r1", task_id="t_b"), False)]
    assert _pager_run(monkeypatch, tmp_path, steps) == [
        [["--target", ALERTS]], [["--target", ALERTS]]]


# The 55 legacy:kanban-needs-ruling lines in #alerts for the 24 h ending
# 2026-11-04 07:4xZ (channel-noise-census.py --hours 24 --limit 5000), oldest
# first: (ts, card, priority, home, still_waiting, reason prefix). ``home`` as
# read from the board: ALERTS / ORIGIN = the origin line's channel (#alerts / a
# chat), SESSION = no channel in the origin line but the home session is a
# Discord chat, None = neither.
ORIGIN, SESSION = "ORIGIN", "SESSION"
_CENSUS_2026_11_04 = [
    ('2026-11-03T10:35:45', 't_c09d9dcb', 315, SESSION, False, 'Step 2 needs the operato'),
    ('2026-11-03T10:48:49', 't_906bd62e', 310, SESSION, False, 'Gate 2b FAIL: home topol'),
    ('2026-11-03T11:23:28', 't_868e850a', 315, SESSION, False, 'Two PRs need review and'),
    ('2026-11-03T14:03:40', 't_868e850a', 315, SESSION, False, 'Two PRs are ready to lan'),
    ('2026-11-03T14:34:01', 't_ddac87fd', 310, ALERTS, False, 'Wiring ruling needed bef'),
    ('2026-11-03T14:43:07', 't_ddac87fd', 310, ALERTS, False, "Ruling (B) can't be buil"),
    ('2026-11-03T15:39:19', 't_fef79316', 310, SESSION, False, 'id-ebay-nick is sealed,'),
    ('2026-11-03T16:26:41', 't_ddac87fd', 310, ALERTS, False, 'Core PR ready to land: A'),
    ('2026-11-03T16:35:23', 't_c09d9dcb', 315, SESSION, False, 'Step 2 done (135/469 her'),
    ('2026-11-03T17:16:12', 't_ddac87fd', 310, ALERTS, False, 'Both #8726 review findin'),
    ('2026-11-03T18:53:17', 't_9db3644a', 100, ALERTS, False, 'Seam-drift review + land'),
    ('2026-11-03T19:02:12', 't_ddac87fd', 310, ALERTS, False, 'Migration PR ready: herm'),
    ('2026-11-03T19:02:12', 't_70d288e8', 100, SESSION, False, 'Done except the live p4_'),
    ('2026-11-03T19:16:03', 't_ddac87fd', 310, ALERTS, False, 'Ready to land; the flip'),
    ('2026-11-03T19:16:04', 't_e59a6627', 200, SESSION, False, 'hue-ace#53 (head caf7596'),
    ('2026-11-03T19:20:07', 't_c09d9dcb', 315, SESSION, False, 'Steps 1-5 done, PR herme'),
    ('2026-11-03T19:38:44', 't_c09d9dcb', 315, SESSION, False, "Step 6 still needs Ace's"),
    ('2026-11-03T19:42:23', 't_daa333c1', 150, SESSION, False, '#8440 head a209bc1888ff8'),
    ('2026-11-03T20:08:35', 't_ddac87fd', 310, ALERTS, False, 'hermes-home#8759 has the'),
    ('2026-11-03T20:08:36', 't_ea40ff83', 150, SESSION, False, 'Part 1 shipped in hermes'),
    ('2026-11-03T20:18:24', 't_2160633c', 200, SESSION, False, 'Round-4 rework (A–E) is'),
    ('2026-11-03T20:20:52', 't_e59a6627', 200, SESSION, False, 'hue-ace#53 is rebased on'),
    ('2026-11-03T20:31:47', 't_9db3644a', 100, ALERTS, False, 'Pre-land items done for'),
    ('2026-11-03T20:38:05', 't_a16226c5', 100, SESSION, False, 'Hostinger and Google are'),
    ('2026-11-03T21:26:34', 't_ddac87fd', 310, ALERTS, False, 'Operator land needed: AN'),
    ('2026-11-03T21:45:26', 't_e59a6627', 200, SESSION, False, "I've split the card. Par"),
    ('2026-11-03T21:45:27', 't_a3d2b6b2', 300, ORIGIN, False, 'Premise not reproducible'),
    ('2026-11-03T22:03:17', 't_9db3644a', 100, ALERTS, False, 'Land ANG-Ventures/claude'),
    ('2026-11-03T22:12:42', 't_557cbc8f', 300, SESSION, False, 'pin host:ace-ai lacks br'),
    ('2026-11-03T23:21:05', 't_e59a6627', 200, SESSION, False, '#57 head 5062a50: the 13'),
    ('2026-11-03T23:23:49', 't_1362d969', 315, SESSION, False, 'Retire gate (a) FAILED:'),
    ('2026-11-03T23:35:31', 't_2160633c', 200, SESSION, False, 'Part A is handed back: h'),
    ('2026-11-03T23:57:34', 't_118ced88', 330, SESSION, False, 'The requested 28d00f3e0b'),
    ('2026-11-04T00:12:01', 't_e59a6627', 200, SESSION, False, '#57 is at head f7073c0.'),
    ('2026-11-04T00:45:54', 't_f57bfd8f', 310, SESSION, False, 'Operator-only now (Apoll'),
    ('2026-11-04T00:48:33', 't_a002a0ab', 200, SESSION, False, 'Redispatch is done and t'),
    ('2026-11-04T01:17:21', 't_becd75ae', 315, SESSION, False, 'BROWSER_RAIL_ABSENT cap='),
    ('2026-11-04T01:23:06', 't_c8420f77', 100, SESSION, False, 'Task and latest Apollo r'),
    ('2026-11-04T01:35:09', 't_a002a0ab', 200, SESSION, False, 'The single authorized re'),
    ('2026-11-04T01:43:58', 't_8886e495', 300, SESSION, False, 'Apollo attended handback'),
    ('2026-11-04T01:55:59', 't_e59a6627', 200, SESSION, False, '#57 at 2d3f64d has three'),
    ('2026-11-04T02:30:42', 't_e59a6627', 200, SESSION, False, '#57 pushed 640c3fb; new'),
    ('2026-11-04T02:34:28', 't_4b735391', 328, SESSION, False, 'PR #8846 head 7e1808a00a'),
    ('2026-11-04T02:44:41', 't_557cbc8f', 300, SESSION, False, 'BROWSER_RAIL_ABSENT cap='),
    ('2026-11-04T02:48:31', 't_557cbc8f', 300, SESSION, False, 'BROWSER_RAIL_ABSENT cap='),
    ('2026-11-04T02:52:21', 't_1fccef3a', 100, None, False, 'Confirmed active supply'),
    ('2026-11-04T02:53:59', 't_2160633c', 200, SESSION, False, '#50 fecb515 verified: ar'),
    ('2026-11-04T03:12:16', 't_d3e792dd', 150, SESSION, False, 'Partial fixes pushed: hu'),
    ('2026-11-04T03:51:49', 't_70d288e8', 100, SESSION, False, 'Explicit ROUND CAP forbi'),
    ('2026-11-04T03:59:43', 't_e59a6627', 200, SESSION, False, '#57 pushed2337e77: exact'),
    ('2026-11-04T04:21:00', 't_e9b20915', 200, SESSION, False, '#54 integration/fixes pu'),
    ('2026-11-04T04:29:01', 't_d3e792dd', 150, SESSION, False, '#647 pushed 19739879: 13'),
    ('2026-11-04T04:30:32', 't_a9544cd6', 120, SESSION, False, 'Draft #8851 @2cf958853bc'),
    ('2026-11-04T04:43:13', 't_a9544cd6', 120, SESSION, False, 'Draft #8851 @611ed97f4ff'),
    ('2026-11-04T07:06:45', 't_46ce1477', 150, SESSION, False, '#8443 head 195bc4bd3ac38'),
]


def test_census_2026_11_04_replay_fits_the_family_budget(monkeypatch, tmp_path, root):
    _state_db(root / "profiles" / "replay" / "state.db",
              {"s_replay": {"platform": "discord", "chat_id": SESSION_CHAT}})
    channel = {ALERTS: ALERTS, ORIGIN: OTHER, SESSION: None, None: None}
    steps = []
    for ts, tid, priority, home, still, reason in _CENSUS_2026_11_04:
        # The live reasons differ per line; the 24-char prefix alone can collide.
        item = _item(channel[home], priority, f"{ts} {reason}", task_id=tid,
                     session_id="s_replay" if home == SESSION else None)
        steps.append((datetime.fromisoformat(ts + "+00:00").timestamp(), item, still))
    per_step = _pager_run(monkeypatch, tmp_path, steps)
    to_alerts = [(steps[i][1]["task_id"]) for i, sent in enumerate(per_step)
                 for c in sent if c in (["--sev", "error"], ["--target", ALERTS])]
    assert len(steps) == 55
    assert to_alerts == ["t_ddac87fd", "t_9db3644a", "t_1fccef3a"]  # was 55; budget <= 3/day
    assert all(sent for sent in per_step)  # every block still pages somewhere


# t_98cdaa01 (13 #alerts needs-ruling lines on 2026-11-12, 5 of them ONE tick
# paging "pin host lacks studio-state" for 5 cards): a tick sends one line per
# (reason, home) that lists the card ids; distinct reasons stay distinct lines.

@pytest.fixture
def lines(monkeypatch, tmp_path):
    monkeypatch.delenv("DISCORD_ALERTS_CHANNEL", raising=False)
    monkeypatch.delenv("DISCORD_LOGS_CHANNEL", raising=False)
    out: list = []
    monkeypatch.setattr(kw, "_alert_notify_script", lambda: tmp_path / "notify.py")
    monkeypatch.setattr(kw, "_notify_send",
                        lambda script, msg, extra, **kwargs: out.append((msg, extra)) or True)
    return out


def _tick_items(pager, items, now=1000.0):
    cards = [("default", it) for it in items]
    return pager.observe(cards, kw._send_needs_input_page, {"default"}, now=now)


def test_five_same_reason_asks_in_one_tick_are_one_line(lines, tmp_path):
    pager = kw._NeedsInputPager(tmp_path / "pages.json")
    reason = "pin host lacks studio-state"
    items = [_item(None, 150, reason, f"t_{i}") for i in range(5)]
    assert _tick_items(pager, items) == 5
    assert len(lines) == 1
    msg, extra = lines[0]
    assert extra == ["--sev", "error"]
    assert "5 cards need a ruling: pin host lacks studio-state" in msg
    for i in range(5):
        assert f"`t_{i}`" in msg
    # The ledger is per card: the next tick inside the window sends nothing.
    lines.clear()
    assert _tick_items(pager, items, now=1100.0) == 0
    assert lines == []
    # The 2 h re-page is one #logs line for the group, not five.
    assert _tick_items(pager, items, now=1000.0 + 2 * 3600 + 1) == 5
    assert len(lines) == 1 and lines[0][1] == ["--target", LOGS]
    assert lines[0][0].startswith("⚠️ still waiting: 5 cards")


def test_two_different_reasons_in_one_tick_are_two_lines(lines, tmp_path):
    pager = kw._NeedsInputPager(tmp_path / "pages.json")
    items = [_item(None, 150, "reason A", "t_a1"), _item(None, 150, "reason A", "t_a2"),
             _item(None, 150, "reason B", "t_b1")]
    assert _tick_items(pager, items) == 3
    assert len(lines) == 2
    msgs = sorted(m for m, _ in lines)
    assert "2 cards need a ruling: reason A" in msgs[0]
    assert "`t_b1` needs a ruling: reason B" in msgs[1]


def test_same_reason_different_homes_are_separate_lines(lines, tmp_path):
    pager = kw._NeedsInputPager(tmp_path / "pages.json")
    items = [_item(OTHER, 150, "r", "t_1"), _item(OTHER, 150, "r", "t_2"),
             _item(None, 150, "r", "t_3")]
    _tick_items(pager, items)
    targets = sorted(str(extra) for _, extra in lines)
    assert targets == [str(["--sev", "error"]), str(["--target", OTHER])]


def test_group_line_caps_the_id_list(lines, tmp_path):
    pager = kw._NeedsInputPager(tmp_path / "pages.json")
    n = kw._NEEDS_INPUT_GROUP_LIST_CAP + 3
    _tick_items(pager, [_item(None, 150, "r", f"t_{i:02d}") for i in range(n)])
    assert len(lines) == 1
    msg = lines[0][0]
    assert f"{n} cards need a ruling" in msg and "+3 more" in msg
    assert msg.count("`t_") == kw._NEEDS_INPUT_GROUP_LIST_CAP
