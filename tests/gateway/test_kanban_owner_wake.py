"""Kanban events wake the OWNING operator session (t_1ae0c35b, Ace 2026-10-03 11:05).

Driven through the REAL notifier tick (``GatewayRunner._kanban_notifier_watcher``)
against a REAL ``SessionStore`` (the fake session registry is a store on
tmp_path holding the owner's live session). The handoff turn is captured off
the adapter the wake hands it to. PR health is the one stub (no GitHub).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from gateway import kanban_home_route as hr
from gateway import kanban_owner_wake as ow
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_cli import kanban_db as kb
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

CHAT = "1553876718639390760"     # #cc-native
OTHER_CHAT = "1502228850338435153"
HUMAN = "117431298246705156"


class Adapter:
    def __init__(self) -> None:
        self.sent: list = []
        self.handled: list = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})

    async def handle_message(self, event):
        self.handled.append(event)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    for var in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    assert str(tmp_path) in str(kb.kanban_db_path())
    kb.init_db()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = SessionDB(db_path=tmp_path / "state.db")
    owner = store.get_or_create_session(SessionSource(
        platform=Platform.DISCORD, chat_id=CHAT, chat_type="group", user_id=HUMAN,
    ))
    adapter = Adapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner.session_store = store
    runner._active_profile_name = lambda: "default"
    health: dict = {}
    # The one state.db read: worker / cron rows have no gateway session key.
    rows = {"20261003_cron_000000": {"source": "cron", "session_key": None},
            "20261003_110000_worker": {"source": "kanban", "session_key": None}}
    monkeypatch.setattr(hr, "read_session_row", lambda sid: rows.get(sid))
    monkeypatch.setattr(ow, "default_pr_health", lambda: (lambda repo, n: health.get((repo, n))))
    return {"store": store, "owner": owner, "adapter": adapter, "runner": runner,
            "health": health, "monkeypatch": monkeypatch}


def _cfg(**kv):
    path = get_hermes_home() / "config.yaml"
    cfg = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    cfg.setdefault("kanban", {}).update(kv)
    path.write_text(yaml.safe_dump(cfg))


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
    return env["adapter"].handled


def _card(env, *, session="owner", title="card", body=None, parents=()):
    sid = env["owner"].session_id if session == "owner" else session
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title=title, assignee="worker", session_id=sid,
                              body=body, parents=parents)


def _event(tid, kind, payload=None):
    with kb.connect_closing() as conn, kb.write_txn(conn):
        kb._append_event(conn, tid, kind, payload)


def _one_turn(env, handled):
    assert len(handled) == 1, [e.text for e in handled]
    ev = handled[0]
    assert ev.internal and ev.allow_gateway_control is False
    assert ev.metadata["gateway_session_key"] == env["owner"].session_key
    assert ev.metadata["gateway_session_id"] == env["owner"].session_id
    assert ev.source.chat_id == CHAT
    assert "Resolve it and get the chain back on track; reply in the home chat" in ev.text
    return ev.text


# --- the five triggers --------------------------------------------------------


def test_a_needs_input_block_wakes_owner_with_reason(env):
    tid = _card(env, title="Phase 4 bracket")
    _event(tid, "blocked", {"kind": "needs_input",
                            "reason": "Need a ruling: E+D, or close as FAIL."})
    text = _one_turn(env, _tick(env))
    assert tid in text and "Phase 4 bracket" in text and "needs_input" in text
    assert '"Need a ruling: E+D, or close as FAIL."' in text


def test_b_precondition_block_wakes_owner_with_reason(env):
    tid = _card(env)
    reason = "Respawn me at or after 07:45 PT 10-03 to collect the verdict."
    _event(tid, "blocked", {"kind": "dependency", "reason": reason})
    text = _one_turn(env, _tick(env))
    assert "precondition" in text and f'"{reason}"' in text


@pytest.mark.parametrize(("kind", "payload"), [
    ("crashed", {"error": "pid 1 exited with code 1"}),
    ("gave_up", {"error": "retries exhausted"}),
    ("stalled", {"progress_age_seconds": 900}),
    ("reclaimed", {"heartbeat_stale": True}),
])
def test_c_stuck_wakes_owner(env, kind, payload):
    tid = _card(env)
    _event(tid, kind, payload)
    text = _one_turn(env, _tick(env))
    assert f"stuck ({kind})" in text and tid in text


def test_d_last_blocker_done_names_ready_children(env):
    p1 = _card(env, title="parent one")
    p2 = _card(env, title="parent two")
    child = _card(env, title="child", parents=[p1, p2])
    with kb.connect_closing() as conn:
        kb.complete_task(conn, p1, summary="first parent done")
    assert _tick(env) == [], "a parent that is NOT the last blocker wakes nobody"
    with kb.connect_closing() as conn:
        kb.complete_task(conn, p2, summary="second parent done")
    text = _one_turn(env, _tick(env))
    assert p2 in text and child in text and "arm or dispatch" in text


def test_e_red_or_dirty_handback_wakes_owner(env):
    env["health"][("ANG-Ventures/hermes-agent", 1700)] = {
        "state": "open", "mergeable_state": "dirty", "failing": ["tests (3.11)"]}
    tid = _card(env)
    with kb.connect_closing() as conn:
        kb.request_review(conn, tid, force=True,
                          summary="PR ANG-Ventures/hermes-agent#1700 ready")
    text = _one_turn(env, _tick(env))
    assert "ANG-Ventures/hermes-agent#1700" in text
    assert "red: tests (3.11)" in text and "dirty" in text


# --- the three non-triggers -----------------------------------------------------


def test_heartbeat_does_not_wake(env):
    tid = _card(env)
    _event(tid, "heartbeat", {"note": "epoch 3"})
    assert _tick(env) == []


def test_green_handback_does_not_wake(env):
    env["health"][("ANG-Ventures/hermes-agent", 1701)] = {
        "state": "open", "mergeable_state": "clean", "failing": []}
    tid = _card(env)
    with kb.connect_closing() as conn:
        kb.request_review(conn, tid, force=True,
                          summary="PR ANG-Ventures/hermes-agent#1701 green")
    assert _tick(env) == []


def test_done_without_gated_children_does_not_wake(env):
    tid = _card(env)
    with kb.connect_closing() as conn:
        kb.complete_task(conn, tid, summary="shipped")
    assert _tick(env) == []


def test_plain_block_without_precondition_does_not_wake(env):
    tid = _card(env)
    _event(tid, "blocked", {"kind": "dependency", "reason": "flaky"})
    assert _tick(env) == []


# --- coalescing ---------------------------------------------------------------


def test_burst_is_one_turn_and_window_holds_then_lists(env):
    a = _card(env, title="card A")
    b = _card(env, title="card B")
    _event(a, "crashed", {"error": "boom"})
    _event(a, "blocked", {"kind": "needs_input", "reason": "pick one"})
    _event(b, "gave_up", {"error": "out"})
    runner = env["runner"]
    t0 = 1_000_000.0
    state = ow.WakeState(Path(get_hermes_home()) / "gateway" / "t.json")
    state.cursors = {"default": 0}
    n = asyncio.run(ow.tick(runner, now=t0, state=state))
    handled = env["adapter"].handled
    assert n == 1 and len(handled) == 1, "N events across cards for one session = one turn"
    assert a in handled[0].text and b in handled[0].text
    assert "stuck (crashed)" in handled[0].text and "pick one" in handled[0].text
    _event(a, "blocked", {"kind": "needs_input", "reason": "second ask"})
    assert asyncio.run(ow.tick(runner, now=t0 + 60, state=state)) == 0, "inside the 10-min window"
    assert asyncio.run(ow.tick(runner, now=t0 + ow.COALESCE_SECONDS + 1, state=state)) == 1
    assert "second ask" in handled[-1].text and len(handled) == 2


# --- fallback / opt-outs ------------------------------------------------------


@pytest.mark.parametrize("session", [None, "20261003_cron_000000", "operator:apollo",
                                     "20261003_110000_worker"])
def test_card_without_live_operator_home_gets_line_only(env, session):
    tid = _card(env, session=session)
    with kb.connect_closing() as conn:
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=OTHER_CHAT,
                          chat_type="group", user_id=HUMAN, delivery_mode="notify")
    _event(tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})
    assert _tick(env) == [], "no turn for a card with no live operator home"
    assert [m["chat_id"] for m in env["adapter"].sent] == [OTHER_CHAT], "today's line still posts"
    assert env["runner"]._kanban_owner_wake_state.pending == {}, "dropped, not held for retry"


def test_non_operator_gateway_never_wakes(env):
    env["runner"]._active_profile_name = lambda: "daedalus"
    tid = _card(env)
    _event(tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})
    assert _tick(env) == []


def test_card_body_wake_off_opts_out(env):
    tid = _card(env, body="do the thing\nwake: off\n")
    _event(tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})
    assert _tick(env) == []


def test_config_knob_off_disables(env):
    _cfg(wake_owner_session=False)
    tid = _card(env)
    _event(tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})
    assert _tick(env) == []


def test_event_made_by_owner_session_is_not_news(env):
    tid = _card(env)
    _event(tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE task_events SET actor_session_id = ? WHERE task_id = ?",
                     (env["owner"].session_id, tid))
    assert _tick(env) == []


def test_notify_wake_sub_on_owner_chat_is_not_doubled(env):
    tid = _card(env)
    with kb.connect_closing() as conn:
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=CHAT,
                          chat_type="group", user_id=HUMAN, delivery_mode="notify+wake")
    _event(tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})
    handled = _tick(env)
    owner_turns = [e for e in handled if (e.metadata or {}).get("kanban_owner_wake")]
    assert owner_turns == [], "the sub's own wake covers a plain block"
    assert len(handled) == 1, "the existing notify+wake path still wakes once"


def test_restart_does_not_replay_history(env):
    tid = _card(env)
    _event(tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})
    assert len(_tick(env)) == 1
    env["runner"]._kanban_owner_wake_state = None  # a fresh gateway life
    assert len(_tick(env)) == 1, "persisted cursor: no replay after restart"


# --- pure ---------------------------------------------------------------------


@pytest.mark.parametrize(("reason", "hit"), [
    ("Respawn me at or after 07:45 PT 10-03", True),
    ("waiting on PR#1651 to merge", True),
    ("deploy: needs the next gateway restart window", True),
    ("Unblock after the next gateway restart window", True),
    ("blocked on ANG-Ventures/hermes-agent#1651", True),
    ("flaky", False),
    ("tests fail", False),
])
def test_precondition_classifier(reason, hit):
    assert ow.names_precondition(reason) is hit


def test_pr_red_or_dirty_pure():
    assert ow.pr_is_red_or_dirty({"state": "open", "mergeable_state": "clean", "failing": []}) is None
    assert ow.pr_is_red_or_dirty({"state": "open", "mergeable_state": "blocked", "failing": []}) is None
    assert ow.pr_is_red_or_dirty({"state": "merged", "failing": ["x"]}) is None
    assert ow.pr_is_red_or_dirty(None) is None
    assert ow.pr_is_red_or_dirty({"state": "open", "failing": ["lint"]}) == "red: lint"


def test_heartbeat_is_never_a_trigger_pure():
    assert "heartbeat" not in ow.SCAN_KINDS
    assert ow.is_stuck("heartbeat", {}) is False
    assert ow.is_stuck("reclaimed", {"heartbeat_stale": False}) is False
