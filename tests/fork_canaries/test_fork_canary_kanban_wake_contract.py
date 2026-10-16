"""Fork canary: kanban WAKE CONTRACT, one event -> exactly N owner turns (t_0ea2883c).

Surfaces: ``gateway/kanban_watchers.py`` (per-subscription notifier),
``gateway/kanban_owner_wake.py`` (owner-wake) and ``gateway/kanban_notify_suppress.py``
(the redundancy predicate both ask). Three wake defects shipped in four days with
every per-feature test green: self-close echoes (t_4c7da648 #1901), the suppressor
never firing in Discord threads (t_d654c294 #2019), a red handback waking twice
(t_b1d26df7 #2021). None of this exists upstream (no kanban there).

Each test drives ONE real notifier tick (which runs the owner-wake tick at its end)
against a temp board and a real ``SessionStore``. Turns are counted at the one seam
both paths end in, ``adapter.handle_message``; ``metadata["kanban_owner_wake"]``
says which path produced a turn. PR health is the only stub (no GitHub).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from gateway import kanban_home_route as hr
from gateway import kanban_notify_suppress as ns
from gateway import kanban_owner_wake as ow
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB

FEATURE = "kanban-wake-contract"
CHAT = "1553876718639390760"
THREAD = "1558761637211803680"
HUMAN = "117431298246705156"
WORKER_SID = "20261015_000001_worker"
REPO = "ANG-Ventures/hermes-agent"


class Adapter:
    """Acknowledges an internal wake the way a live platform adapter does."""

    def __init__(self) -> None:
        self.sent: list = []
        self.handled: list = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})

    async def handle_message(self, event):
        event._gateway_accepted = True
        self.handled.append(event)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    for var in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_SESSION_ID",
                "HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    assert str(tmp_path) in str(kb.kanban_db_path())
    kb.init_db()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = SessionDB(db_path=tmp_path / "state.db")
    group = store.get_or_create_session(SessionSource(
        platform=Platform.DISCORD, chat_id=CHAT, chat_type="group", user_id=HUMAN))
    thread = store.get_or_create_session(SessionSource(
        platform=Platform.DISCORD, chat_id=THREAD, chat_type="thread", thread_id=THREAD,
        user_id=HUMAN))
    rows = {WORKER_SID: {"source": "kanban", "session_key": None}}
    for entry, origin in (
        (group, {"platform": "discord", "chat_id": CHAT, "chat_type": "group", "user_id": HUMAN}),
        # t_d654c294: a Discord thread keeps thread_id == chat_id on both rows
        (thread, {"platform": "discord", "chat_id": THREAD, "thread_id": THREAD,
                  "chat_type": "thread", "user_id": HUMAN}),
    ):
        rows[entry.session_id] = {"session_key": entry.session_key, "profile_name": "default",
                                  "origin_json": json.dumps(origin)}
    monkeypatch.setattr(hr, "read_session_row", lambda sid: rows.get(sid))
    health: dict = {}
    monkeypatch.setattr(ow, "default_pr_health", lambda: (lambda repo, n: health.get((repo, n))))
    adapter = Adapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner.session_store = store
    runner._active_profile_name = lambda: "default"
    return {"group": group, "thread": thread, "adapter": adapter, "runner": runner,
            "health": health, "monkeypatch": monkeypatch}


def _tick(env) -> list:
    """One notifier tick; its tail runs the owner-wake tick. Returns all turns."""
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


def _path(turn) -> str:
    return "owner-wake" if (turn.metadata or {}).get("kanban_owner_wake") else "notifier"


def _paths(turns) -> list:
    return [_path(t) for t in turns]


def _card(env, home="group", **kw) -> str:
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title=kw.pop("title", "card"), assignee="worker",
                              session_id=env[home].session_id, **kw)


def _sub(tid, *, chat=CHAT, thread="", chat_type="group", mode="notify+wake"):
    with kb.connect_closing() as conn:
        kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=chat, thread_id=thread,
                           chat_type=chat_type, user_id=HUMAN, delivery_mode=mode)


def _thread_sub(tid, **kw):
    _sub(tid, chat=THREAD, thread=THREAD, chat_type="thread", **kw)


def _receipts(tid) -> list:
    with kb.connect_closing() as conn:
        return [e for e in kb.list_events(conn, tid) if e.kind == ns.EVENT_KIND]


def _handback(tid, pr: int):
    with kb.connect_closing() as conn, \
            kb.mutation_actor(session_ids=[WORKER_SID], profile="worker", surface="worker"):
        kb.request_review(conn, tid, force=True, summary=f"PR {REPO}#{pr} ready")


def _close(tid, *, sid, profile="default", summary="closed"):
    with kb.connect_closing() as conn, \
            kb.mutation_actor(session_ids=[sid], profile=profile, surface="gateway"):
        kb.complete_task(conn, tid, summary=summary)


def _msg(n: str, why: str, turns) -> str:
    return (f"FORK CANARY {FEATURE} {n}: {why}; turns={len(turns)} paths={_paths(turns)} "
            f"texts={[t.text[:160] for t in turns]}")


# --- W1-W3: review_requested -------------------------------------------------


def test_w1_green_handback_with_wake_sub_is_one_notifier_turn(env):
    """Baseline: a green handback is the notifier's one turn; owner-wake stays out."""
    env["health"][(REPO, 2101)] = {"state": "open", "mergeable_state": "clean", "failing": []}
    tid = _card(env)
    _sub(tid)
    _handback(tid, 2101)
    turns = _tick(env)
    assert _paths(turns) == ["notifier"], _msg("W1", "green handback must be exactly one notifier turn", turns)
    assert "cannot land" not in turns[0].text


def test_w2_red_handback_with_wake_sub_is_one_turn_naming_the_red(env):
    """t_b1d26df7 (#2021): the notifier's turn carries the red; owner-wake does not add a second.

    RED-PROVABLE: revert #2021 -> two turns (notifier review_requested + owner-wake red_handback)."""
    env["health"][(REPO, 2102)] = {"state": "open", "mergeable_state": "clean",
                                   "failing": ["logs-noise-emitters"]}
    tid = _card(env)
    _sub(tid)
    _handback(tid, 2102)
    turns = _tick(env)
    assert _paths(turns) == ["notifier"], _msg("W2", "red handback with a wake sub woke != once (t_b1d26df7)", turns)
    assert "red: logs-noise-emitters" in turns[0].text, _msg("W2", "the one turn does not name the red check", turns)


def test_w3_red_handback_without_sub_is_one_owner_wake_turn(env):
    """t_b1d26df7 guard: with no sub the owner-wake still carries the red alone."""
    env["health"][(REPO, 2103)] = {"state": "open", "mergeable_state": "clean", "failing": ["lint"]}
    tid = _card(env)
    _handback(tid, 2103)
    turns = _tick(env)
    assert _paths(turns) == ["owner-wake"], _msg("W3", "red handback without a sub must be one owner-wake turn", turns)
    assert f"cannot land ({REPO}#2103 red: lint)" in turns[0].text


# --- W4-W6: completed --------------------------------------------------------


def test_w4_home_session_self_close_in_group_chat_is_zero_turns_and_a_receipt(env):
    """t_4c7da648 (#1901): the home session's own close is not news to the home chat.

    RED-PROVABLE: kanban.notify_suppress disabled / predicate bypassed -> 1 turn, no receipt."""
    tid = _card(env)
    _sub(tid)
    _close(tid, sid=env["group"].session_id)
    turns = _tick(env)
    assert turns == [], _msg("W4", "operator self-close echoed into the home group chat (t_4c7da648)", turns)
    assert env["adapter"].sent == [], f"FORK CANARY {FEATURE} W4: chat line posted {env['adapter'].sent}"
    rs = _receipts(tid)
    assert [(r.payload["reason"], r.payload["path"]) for r in rs] == [("self-session", "notifier")], (
        f"FORK CANARY {FEATURE} W4: expected one self-session notifier receipt, got {[r.payload for r in rs]}")


def test_w5_home_session_self_close_in_discord_thread_is_zero_turns_and_a_receipt(env):
    """t_d654c294 (#2019): a thread sub (thread_id == chat_id) is still the home chat.

    RED-PROVABLE: revert #2019 -> the self-close echoes into the thread."""
    tid = _card(env, home="thread")
    _thread_sub(tid)
    _close(tid, sid=env["thread"].session_id)
    turns = _tick(env)
    assert turns == [], _msg("W5", "operator self-close echoed into its Discord thread (t_d654c294)", turns)
    assert env["adapter"].sent == [], f"FORK CANARY {FEATURE} W5: chat line posted {env['adapter'].sent}"
    rs = _receipts(tid)
    assert [(r.payload["reason"], r.payload["path"]) for r in rs] == [("self-session", "notifier")], (
        f"FORK CANARY {FEATURE} W5: expected one self-session notifier receipt, got {[r.payload for r in rs]}")


def test_w6_foreign_actor_close_on_thread_homed_card_is_one_turn(env):
    """Prism P1 03f8b8156055: someone else's close is news; never suppressed."""
    tid = _card(env, home="thread")
    _thread_sub(tid)
    _close(tid, sid=WORKER_SID, profile="worker", summary="worker finished")
    turns = _tick(env)
    assert _paths(turns) == ["notifier"], _msg("W6", "a foreign close on a thread-homed card must wake once", turns)
    assert _receipts(tid) == []


# --- W7-W9: existing contracts ------------------------------------------------


def test_w7_needs_input_block_with_wake_sub_is_one_notifier_turn(env):
    """SUB_WAKE_KINDS: the sub's wake covers a block; owner-wake defers."""
    tid = _card(env)
    _sub(tid)
    with kb.connect_closing() as conn, kb.write_txn(conn):
        kb._append_event(conn, tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})  # noqa: SLF001
    turns = _tick(env)
    assert _paths(turns) == ["notifier"], _msg("W7", "a needs_input block with a wake sub must wake once", turns)


def _gated(env):
    parent = _card(env, title="parent")
    child = _card(env, title="child", parents=[parent])
    _sub(parent)
    return parent, child


def test_w8_last_blocker_done_wakes_even_when_the_operator_closed_it(env):
    """NEVER_SUPPRESS_TRIGGERS: the operator's own profile closing the last blocker is
    self-operator for the close line, yet the 'children ready' owner-wake still fires.

    RED-PROVABLE: drop last_blocker_done from NEVER_SUPPRESS_TRIGGERS -> 0 turns."""
    parent, child = _gated(env)
    with kb.connect_closing() as conn, kb.mutation_actor(session_ids=[], profile="default", surface="cli"):
        kb.complete_task(conn, parent, summary="operator closed the last blocker")
    turns = _tick(env)
    assert _paths(turns) == ["owner-wake"], _msg("W8", "last_blocker_done must wake the owner once", turns)
    assert parent in turns[0].text and child in turns[0].text


def test_w8b_last_blocker_done_wakes_when_the_home_session_closed_it(env):
    """Decided (t_f3323c8a): 'children ready' is news even to the session that
    closed the last blocker, so the home session's own close wakes it once.

    RED-PROVABLE: let the own-session cut drop protected triggers -> 0 turns."""
    parent, _child = _gated(env)
    _close(parent, sid=env["group"].session_id, summary="operator closed the last blocker")
    turns = _tick(env)
    assert _paths(turns) == ["owner-wake"], _msg("W8b", "home-session close of last blocker", turns)


def test_w8c_worker_closing_last_blocker_with_wake_sub_is_one_turn(env):
    """The notifier's 'completed' wake carries the children-ready line, so the
    owner-wake's last_blocker_done does not add a second turn (t_f3323c8a).

    RED-PROVABLE: drop the ready ids from mark_carried -> two turns."""
    parent, child = _gated(env)
    _close(parent, sid=WORKER_SID, profile="worker", summary="worker finished")
    turns = _tick(env)
    assert len(turns) == 1, _msg("W8c", "one completed event must be one turn", turns)
    assert child in turns[0].text, _msg("W8c", "the one turn must name the unblocked child", turns)


def test_w9_wake_only_sub_is_never_suppressed(env):
    """FleetReview d9c37a52d911: a wake-only sub has no chat echo to cut; the wake IS
    the delivery, so the home session's own close still wakes once."""
    tid = _card(env)
    _sub(tid, mode="wake")
    _close(tid, sid=env["group"].session_id)
    turns = _tick(env)
    assert _paths(turns) == ["notifier"], _msg("W9", "a delivery_mode='wake' sub must always wake once", turns)
    assert _receipts(tid) == []
