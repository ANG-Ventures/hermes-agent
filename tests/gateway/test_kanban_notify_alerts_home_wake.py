"""A card homed in a LIVE #alerts session wakes THAT session, not the operator sub (t_5763e008).

Ace 2026-11-15 09:19 (msg 1571575452424998953): a live session whose home chat is #alerts is woken
itself for events on its own cards. Incident: t_ca89a948 (home 20261111_224326_f1a10ee6, #alerts)
handed off for review at 11:10:16; the notifier woke the #apollo operator session (the card's
notify+wake sub) instead, and the home waited 17 min for the review deadman.

Driven through the REAL notifier tick against a REAL ``SessionStore``; the adapter records sends
(lines) and handled events (wakes).
"""
from __future__ import annotations

import asyncio

import pytest

from gateway import kanban_alerts_home as ah
from gateway import kanban_home_route as hr
from gateway import kanban_notice_inbox as ni
from gateway import kanban_owner_wake as ow
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB

ALERTS = "1480528231286181948"   # fleet #alerts
APOLLO = "1502228850338435153"   # operator chat holding the card's wake sub
OTHER = "1553876718639390760"    # an ordinary home chat
HUMAN = "117431298246705156"


class Adapter:
    def __init__(self) -> None:
        self.sent: list = []
        self.handled: list = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": str(chat_id), "text": text})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    for var in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_SESSION_ID",
                "DISCORD_ALERTS_CHANNEL", ah.KILL_SWITCH_ENV, ni.KILL_SWITCH_ENV):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = SessionDB(db_path=tmp_path / "state.db")

    def session(chat):
        return store.get_or_create_session(SessionSource(
            platform=Platform.DISCORD, chat_id=chat, chat_type="group", user_id=HUMAN))

    adapter = Adapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner.session_store = store
    runner._active_profile_name = lambda: "default"
    rows = {"20261115_gone_000000": {"source": "discord", "session_key": None}}
    monkeypatch.setattr(hr, "read_session_row", lambda sid: rows.get(sid))
    monkeypatch.setattr(ow, "default_pr_health", lambda: None)
    return {"runner": runner, "adapter": adapter, "session": session, "monkeypatch": monkeypatch,
            "apollo": session(APOLLO)}


@pytest.fixture
def inbox(monkeypatch):
    """Agent-inbox rows the notifier wrote: (task_id, board, kind, text, why)."""
    rows: list = []

    def fake_write(task_id, board, kind, text, why):
        rows.append((task_id, board, kind, text, why))
        return True

    monkeypatch.setattr(ni, "write_inbox", fake_write)
    return rows


def _tick(env):
    runner, mp = env["runner"], env["monkeypatch"]
    runner._running = True
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    mp.setattr(asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(runner._kanban_notifier_watcher(interval=1))
    finally:
        mp.setattr(asyncio, "sleep", real_sleep)


def _handed_off(home_sid: str) -> str:
    """A card homed at ``home_sid`` whose waker sub is the #apollo operator chat, handed to human:apollo."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, boomerang_wake=False, title="alerts-born card", assignee="worker",
                             session_id=home_sid)
        kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=APOLLO, chat_type="group",
                           user_id=HUMAN, delivery_mode="notify+wake")
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))  # a hand-off notice needs a review card
        kb._append_event(conn, tid, "review_requested", {
            "summary": "review hold: checks settled, handed to human:apollo",
            "implementer": "worker", "reviewer": "human:apollo"})
    return tid


def _chats(events) -> list:
    return [str(e.source.chat_id) for e in events]


def test_live_alerts_home_is_woken_itself_and_nothing_posts(env):
    home = env["session"](ALERTS)
    tid = _handed_off(home.session_id)
    _tick(env)
    adapter = env["adapter"]
    assert _chats(adapter.handled) == [ALERTS], adapter.handled
    wake = adapter.handled[0]
    assert wake.metadata["gateway_session_key"] == home.session_key
    assert wake.metadata["gateway_session_id"] == home.session_id
    assert f"[kanban] Task {tid} handed off for review" in wake.text
    assert wake.internal and not wake.allow_gateway_control
    # nothing addressed to #alerts, nor to the #apollo operator chat/session
    assert adapter.sent == []
    with kb.connect_closing() as conn:
        assert kbn.unseen_events_for_sub(conn, task_id=tid, platform="discord", chat_id=APOLLO,
                                         kinds=["review_requested"])[1] == []


def test_alerts_home_without_live_session_goes_to_agent_inbox(env, inbox):
    """t_10556779: the home is not live, so the line is an agent-inbox row, never #apollo's chat."""
    tid = _handed_off("20261115_gone_000000")   # state.db row, no live gateway session
    _tick(env)
    adapter = env["adapter"]
    assert _chats(adapter.handled) == [APOLLO]  # the wake is unchanged
    assert adapter.sent == []
    assert [(r[0], r[2], r[4]) for r in inbox] == [(tid, "review_requested", "home-not-live")]
    assert tid in inbox[0][3]


def test_card_homed_elsewhere_is_unchanged(env):
    home = env["session"](OTHER)
    _handed_off(home.session_id)
    _tick(env)
    adapter = env["adapter"]
    assert _chats(adapter.handled) == [APOLLO]
    assert [s["chat_id"] for s in adapter.sent] == [APOLLO]


def test_kill_switch_restores_todays_routing(env):
    env["monkeypatch"].setenv(ah.KILL_SWITCH_ENV, "reroute")
    home = env["session"](ALERTS)
    _handed_off(home.session_id)
    _tick(env)
    adapter = env["adapter"]
    assert _chats(adapter.handled) == [APOLLO]
    assert [s["chat_id"] for s in adapter.sent] == [APOLLO]


def test_home_sessions_own_transition_wakes_nobody(env):
    home = env["session"](ALERTS)
    tid = _handed_off(home.session_id)
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE task_events SET actor_session_id = ? WHERE task_id = ?", (home.session_id, tid))
    _tick(env)
    assert env["adapter"].handled == []
    assert env["adapter"].sent == []



def test_subscriber_sessions_completion_still_wakes_the_alerts_home(env):
    """Prism P2 07f3aaae5246: a completion the #apollo subscriber's own session made is no echo for
    the #alerts home. Self-echo is judged against the home, so the home is woken once, nothing posts."""
    from gateway import kanban_watchers as kw

    apollo = env["apollo"].session_id
    # The actor's recorded origin is the #apollo subscriber itself (chat, participant, profile).
    env["monkeypatch"].setattr(kw, "_session_origin", lambda sid: {
        "platform": "discord", "chat_id": APOLLO, "user_id": HUMAN, "profile": "default",
    } if sid == apollo else None)
    home = env["session"](ALERTS)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, boomerang_wake=False, title="alerts-born card", assignee="worker",
                             session_id=home.session_id)
        kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=APOLLO, chat_type="group",
                           user_id=HUMAN, delivery_mode="notify+wake")
    with kb.connect_closing() as conn, kb.write_txn(conn):
        kb._append_event(conn, tid, "completed", {"summary": "closed from the #apollo session"})
        conn.execute("UPDATE task_events SET actor_session_id = ? WHERE task_id = ?",
                     (apollo, tid))
    _tick(env)
    adapter = env["adapter"]
    assert _chats(adapter.handled) == [ALERTS], adapter.handled
    assert adapter.handled[0].metadata["gateway_session_id"] == home.session_id
    assert adapter.sent == []


# t_10556779 (Ace 2026-11-21 20:38): a notice with no live owning session goes to the agent inbox.


def test_card_with_no_home_goes_to_agent_inbox(env, inbox):
    tid = _handed_off(None)
    _tick(env)
    assert env["adapter"].sent == []
    assert [(r[0], r[4]) for r in inbox] == [(tid, "no-home")]


def test_sub_in_the_homes_own_chat_still_posts(env, inbox):
    """Home not live, but the sub IS the home's chat: today's post stays."""
    rows = {"20261121_gone_apollo": {"source": "discord", "session_key": None,
                                     "origin_json": '{"platform": "discord", "chat_id": "%s"}' % APOLLO}}
    env["monkeypatch"].setattr(hr, "read_session_row", lambda sid: rows.get(sid))
    _handed_off("20261121_gone_apollo")
    _tick(env)
    assert [s["chat_id"] for s in env["adapter"].sent] == [APOLLO]
    assert inbox == []


def test_operator_home_still_posts(env, inbox):
    _handed_off("operator:ace")
    _tick(env)
    assert [s["chat_id"] for s in env["adapter"].sent] == [APOLLO]
    assert inbox == []


def test_no_owner_kill_switch_posts(env, inbox):
    env["monkeypatch"].setenv(ni.KILL_SWITCH_ENV, "post")
    _handed_off("20261115_gone_000000")
    _tick(env)
    assert [s["chat_id"] for s in env["adapter"].sent] == [APOLLO]
    assert inbox == []


def test_failed_inbox_write_posts_the_line(env, monkeypatch):
    monkeypatch.setattr(ni, "write_inbox", lambda *a: False)
    _handed_off("20261115_gone_000000")
    _tick(env)
    assert [s["chat_id"] for s in env["adapter"].sent] == [APOLLO]


def test_write_inbox_uses_the_fleet_agent_log(tmp_path, monkeypatch):
    import json

    home = tmp_path / "home"
    lib = home / "scripts" / "lib"
    lib.mkdir(parents=True)
    out = tmp_path / "agent-log.jsonl"
    (lib / "agent_inbox.py").write_text(
        "import json\n"
        "def write_agent_log(root, producer, sev, message, **extra):\n"
        f"    open({str(out)!r}, 'a').write(json.dumps(dict(producer=producer, sev=sev, text=message, **extra)) + '\\n')\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert ni.write_inbox("t_abc", "default", "blocked", "line", "home-not-live") is True
    row = json.loads(out.read_text())
    assert row == {"producer": "kanban-notifier", "sev": "info", "text": "line", "task_id": "t_abc",
                   "board": "default", "kind": "blocked", "why": "home-not-live"}
    (lib / "agent_inbox.py").unlink()
    assert ni.write_inbox("t_abc", "default", "blocked", "line", "home-not-live") is False


# Prism round 1 on #519.


def test_sub_in_the_homes_own_thread_is_the_homes_chat():
    """746a097dbf67: the own-chat check reads the sub's thread_id, not a session_id."""
    home = hr.HomeResolution(hr.HomeTarget("telegram", "-100", "42"))
    sub = {"platform": "telegram", "chat_id": "-100", "thread_id": "42", "task_id": "t_x"}
    assert ni.no_live_owner(None, sub, "20261121_gone_thread", home, lambda sid: {"session_key": None}) is None
    other = dict(sub, thread_id="7")
    assert ni.no_live_owner(None, other, "20261121_gone_thread", home,
                            lambda sid: {"session_key": None}) == "home-not-live"


def test_raw_home_with_no_row_here_is_unknown_not_offline():
    """fbd4a6131be6: a raw id this state.db has no row for may be a secondary profile's live session."""
    sub = {"platform": "discord", "chat_id": APOLLO, "session_id": "", "task_id": "t_x"}
    home = hr.HomeResolution(None, "no-row")

    class Store:
        lookup_by_session_id = staticmethod(lambda sid: None)
        lookup_by_session_key = staticmethod(lambda key: None)

    assert ni.no_live_owner(Store(), sub, "20261121_beta_rotated", home, lambda sid: None) is None


def test_inbox_bound_lane_failure_does_not_silence_a_chat_bound_one(env, inbox):
    """68ab04922c22: lane-grouped failure notices post as before, covering every card in the group."""
    with kb.connect_closing() as conn:
        ids = []
        for home in ("20261115_gone_000000", "operator:ace"):
            tid = kb.create_task(conn, boomerang_wake=False, title="lane card", assignee="worker", session_id=home)
            kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=APOLLO, chat_type="group",
                               user_id=HUMAN, delivery_mode="notify")
            ids.append(tid)
    with kb.connect_closing() as conn, kb.write_txn(conn):
        for tid in ids:
            kb._append_event(conn, tid, "crashed", {"error": "provider unavailable"})
    _tick(env)
    sent = env["adapter"].sent
    assert inbox == []
    assert len(sent) == 1 and sent[0]["chat_id"] == APOLLO
    assert all(t in sent[0]["text"] for t in ids), sent[0]["text"]
