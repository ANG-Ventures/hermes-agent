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


# --- W10-W14: S5 actionable-only wakes (t_4f6afb67) -----------------------------


def _red(env, pr, checks, *, head="a" * 40, inherited=None, state="open"):
    env["health"][(REPO, pr)] = {"state": state, "mergeable_state": "clean", "failing": list(checks),
                                 "head": head, "inherited_from_main": inherited}


def _rehandback(tid, pr: int):
    """A second review_requested on a card already in review (a worker re-handing back)."""
    with kb.connect_closing() as conn, kb.write_txn(conn), \
            kb.mutation_actor(session_ids=[WORKER_SID], profile="worker", surface="worker"):
        kb._append_event(conn, tid, "review_requested", {"summary": f"PR {REPO}#{pr} ready"})  # noqa: SLF001


def _reasons(tid) -> list:
    return [(r.payload["reason"], r.payload["path"]) for r in _receipts(tid)]


def test_w10_inherited_only_red_handback_is_zero_turns_and_a_receipt(env):
    """S5 (t_a716c180 05:05, 10-12): main's red inherited into the PR is not the owner's to fix.

    RED-PROVABLE: drop the inherited-only rule from red_handback_noise -> 1 owner-wake turn."""
    _red(env, 2110, ["lints-fast"], inherited="5cb9ca920abc")
    tid = _card(env)
    _handback(tid, 2110)
    turns = _tick(env)
    assert turns == [], _msg("W10", "an inherited-only red woke its owner", turns)
    assert ("inherited-red", "owner-wake") in _reasons(tid), _reasons(tid)


def test_w11_same_red_same_head_wakes_once(env):
    """S5 (t_1ac4c83b 03:48/04:03/04:20, 10-12): one (card, PR, head, red-set) is one wake.

    RED-PROVABLE: skip note_red_seen -> the second handback wakes again."""
    _red(env, 2111, ["logs-noise-emitters"])
    tid = _card(env)
    _handback(tid, 2111)
    first = list(_tick(env))
    assert _paths(first) == ["owner-wake"], _msg("W11", "the first red must wake once", first)
    env["runner"]._kanban_owner_wake_state.last_wake.clear()   # past the coalescing window
    _rehandback(tid, 2111)
    turns = _tick(env)[len(first):]
    assert turns == [], _msg("W11", "the same red at the same head woke twice", turns)
    assert ("duplicate-red", "owner-wake") in _reasons(tid), _reasons(tid)


def test_w12_new_head_or_new_red_wakes_again(env):
    """Guard for W11: a push (new head) with a red is new news."""
    _red(env, 2112, ["lint"], head="a" * 40)
    tid = _card(env)
    _handback(tid, 2112)
    first = list(_tick(env))
    env["runner"]._kanban_owner_wake_state.last_wake.clear()
    _red(env, 2112, ["lint"], head="b" * 40)
    _rehandback(tid, 2112)
    turns = _tick(env)[len(first):]
    assert _paths(turns) == ["owner-wake"], _msg("W12", "a red at a NEW head must wake", turns)


def test_w13_home_session_send_back_is_no_wake_but_the_line_posts(env):
    """S5 (12 turns in the CI session, 10-12): the operator's own request-changes, echoed back.

    RED-PROVABLE: drop is_own_echo -> one notifier turn."""
    tid = _card(env)
    _sub(tid)
    with kb.connect_closing() as conn, \
            kb.mutation_actor(session_ids=[env["group"].session_id], profile="default", surface="gateway"):
        kb.request_review(conn, tid, force=True, summary="handback")
    with kb.connect_closing() as conn, kb.write_txn(conn), \
            kb.mutation_actor(session_ids=[env["group"].session_id], profile="default", surface="gateway"):
        kb._append_event(conn, tid, "changes_requested", {"reason": "P1 real"})  # noqa: SLF001
    turns = _tick(env)
    assert turns == [], _msg("W13", "the home session's own send-back woke it", turns)
    assert ("own-session-echo", "notifier-wake") in _reasons(tid), _reasons(tid)


def test_w14_worker_needs_input_block_still_wakes_and_kill_switch_restores(env):
    """S5 keeps the actionable set: a worker's needs_input block wakes once; with
    actionable_only off, W10's inherited red wakes again."""
    tid = _card(env)
    _sub(tid)
    with kb.connect_closing() as conn, kb.write_txn(conn), \
            kb.mutation_actor(session_ids=[WORKER_SID], profile="worker", surface="worker"):
        kb._append_event(conn, tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})  # noqa: SLF001
    turns = list(_tick(env))
    assert _paths(turns) == ["notifier"], _msg("W14", "a worker's needs_input block must wake once", turns)
    from hermes_constants import get_hermes_home
    (get_hermes_home() / "config.yaml").write_text(
        "kanban:\n  notify_suppress:\n    actionable_only: false\n")
    _red(env, 2114, ["lints-fast"], inherited="5cb9ca920abc")
    t2 = _card(env)
    _handback(t2, 2114)
    more = _tick(env)[len(turns):]
    assert _paths(more) == ["owner-wake"], _msg("W14", "actionable_only:false must restore the wake", more)


# --- W15-W17: terminal-pending review cards (t_26ebe71a) ----------------------


HEAD_TP = "3806e0d8a3f043f550f0d5858e89926f5cd5129b"


def _approved_and_landing(env, tid, *, land=True):
    """An operator APPROVE at HEAD_TP on the card and (``land``) an open land request for it at that head."""
    for pr in (2115, 2116, 2117):   # green: the handback itself is not what these rows count
        env["health"][(REPO, pr)] = {"state": "open", "mergeable_state": "clean", "failing": [], "head": HEAD_TP}
    rec = {"verdict": "APPROVE", "head_sha": HEAD_TP, "reviewer": "human:apollo", "findings": 0, "items": []}
    with kb.connect_closing() as conn:
        kb.add_comment(conn, tid, "default", "review_coverage: " + json.dumps(rec))
    if land:
        d = kb.kanban_home() / "var" / "land-requests"
        d.mkdir(parents=True, exist_ok=True)
        (d / "01CANARYW15.json").write_text(json.dumps(
            {"ulid": "01CANARYW15", "repo": REPO, "pr": 2115, "review_head_sha": HEAD_TP, "card": tid}))


def _gave_up(tid):
    """The 10-13 shape: the TTL sweep's breaker trip on a review card (dispatcher-written, no session)."""
    with kb.connect_closing() as conn, kb.write_txn(conn):
        kb._append_event(conn, tid, "gave_up", {  # noqa: SLF001
            "failures": 2, "effective_limit": 2, "limit_source": "dispatcher", "retry_status": "review",
            "error": "stale_lock=mac-studio-m3u:4224", "trigger_outcome": "reclaimed"})


@pytest.fixture
def tp_env(env, tmp_path):
    env["monkeypatch"].setenv("HERMES_KANBAN_HOME", str(tmp_path / "board-home"))
    (tmp_path / "board-home").mkdir()
    return env


def test_w15_gave_up_on_approved_landing_card_with_wake_sub_is_zero_turns_and_a_receipt(tp_env):
    """t_26ebe71a (t_75fdedd4 07:11, 10-13): a review card approved at its head with a land in flight is the
    lander's. RED-PROVABLE: drop the terminal-pending branch in the notifier -> one notifier gave_up turn."""
    env = tp_env
    tid = _card(env)
    _sub(tid)
    _approved_and_landing(env, tid)
    _handback(tid, 2115)
    _tick(env)
    before = len(env["adapter"].handled)
    _gave_up(tid)
    turns = _tick(env)[before:]
    assert turns == [], _msg("W15", "a gave_up on an approved+landing card woke its owner", turns)
    assert ("terminal-pending", "notifier-wake") in _reasons(tid), _reasons(tid)


def test_w16_gave_up_on_approved_landing_card_without_sub_is_zero_owner_wakes(tp_env):
    """RED-PROVABLE: drop terminal_pending_items from the owner-wake tick -> one owner-wake stuck turn."""
    env = tp_env
    tid = _card(env)
    _approved_and_landing(env, tid)
    _handback(tid, 2116)
    _gave_up(tid)
    turns = _tick(env)
    assert turns == [], _msg("W16", "owner-wake fired stuck on an approved+landing card", turns)
    assert ("terminal-pending", "owner-wake") in _reasons(tid), _reasons(tid)


def test_w16b_owner_wake_terminal_pending_honours_the_kill_switch(tp_env):
    """Prism 93c774f94d98: ``notify_suppress.enabled: false`` (or ``actionable_only: false``) restores the stuck
    wake on the owner-wake path too, as it does on the notifier's. RED-PROVABLE: drop the cfg gate -> 0 turns."""
    env = tp_env
    from hermes_constants import get_hermes_home
    (get_hermes_home() / "config.yaml").write_text("kanban:\n  notify_suppress:\n    enabled: false\n")
    tid = _card(env)
    _approved_and_landing(env, tid)
    _handback(tid, 2116)
    _gave_up(tid)
    turns = _tick(env)
    assert _paths(turns) == ["owner-wake"], _msg("W16b", "kill switch off must restore the owner wake", turns)
    assert ("terminal-pending", "owner-wake") not in _reasons(tid), _reasons(tid)


def test_w17_gave_up_on_approved_card_with_no_land_still_wakes(tp_env):
    """Guard for W15/W16: an APPROVE alone, no land in flight, is a card nobody is landing; the owner hears it."""
    env = tp_env
    tid = _card(env)
    _approved_and_landing(env, tid, land=False)
    _handback(tid, 2117)
    _gave_up(tid)
    turns = _tick(env)
    assert _paths(turns) == ["owner-wake"], _msg("W17", "a gave_up with no land in flight must wake once", turns)


# --- W18-W20: Boomerang v2 auto-dispatch (t_0736cfd7, spec §5.5) ---------------------------


def _auto_on(env, on=True):
    """The emergency stop file is the only switch (no config knob)."""
    from gateway import kanban_boomerang_wake as ah
    stop = ah.stop_file_path()
    if on:
        stop.unlink(missing_ok=True)
    else:
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("")


@pytest.fixture
def auto_env(env):
    from gateway import kanban_boomerang_wake as ah

    seen: list = []

    async def record(_runner, entry, _state):
        seen.append(entry)

    keys = lambda: {e["key"] for e in seen}  # noqa: E731 -- recorded children stay "running"
    env["monkeypatch"].setattr(ah, "run_entry", record)
    env["monkeypatch"].setattr(ah, "_INFLIGHT", {})
    env["monkeypatch"].setattr(ah, "_alive", lambda key: key in keys())
    env["monkeypatch"].setattr(ah, "_LAST_ENABLED", {})
    env["monkeypatch"].setattr(ah, "post_logs", lambda text: None)
    env["dispatched"] = seen
    _auto_on(env)
    return env


def _auto_card(env, **kw) -> str:
    return _card(env, boomerang_wake=True, **kw)


def test_w18_boomerang_wake_red_handback_is_one_dispatch_zero_turns(auto_env):
    """One event through both paths (notifier sub + owner-wake reason e) = ONE child, no turn.

    RED-PROVABLE: drop the claim check in owner_wake_filter -> two dispatches."""
    env = auto_env
    env["health"][(REPO, 2118)] = {"state": "open", "mergeable_state": "clean", "failing": ["lint"]}
    tid = _auto_card(env)
    _sub(tid)
    _handback(tid, 2118)
    turns = _tick(env)
    assert turns == [], _msg("W18", "a boomerang-wake red handback still woke the owner", turns)
    assert len(env["dispatched"]) == 1, f"FORK CANARY {FEATURE} W18: dispatches={env['dispatched']}"


def test_w19_boomerang_wake_needs_input_still_one_notifier_turn(auto_env):
    """INV-5: a judgment event on an opted-in card is today's one turn, never a child."""
    env = auto_env
    tid = _auto_card(env)
    _sub(tid)
    with kb.connect_closing() as conn, kb.write_txn(conn):
        kb._append_event(conn, tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})  # noqa: SLF001
    turns = _tick(env)
    assert _paths(turns) == ["notifier"] and env["dispatched"] == [], _msg("W19", "needs_input boomerang-woken", turns)


def test_w20_kill_switch_off_auto_card_wakes_as_today(auto_env):
    """INV-7: stop file present -> a boomerang-wake card is today's one turn, no claim."""
    env = auto_env
    _auto_on(env, False)
    tid = _auto_card(env)
    _sub(tid)
    _close(tid, sid=WORKER_SID, profile="worker", summary="worker finished")
    turns = _tick(env)
    assert _paths(turns) == ["notifier"] and env["dispatched"] == [], _msg("W20", "switch off still dispatched", turns)


# --- W21-W22: a mechanical own-repo handback (every open Prism key answered) -> a fork child (t_6e9f48cc) ---


def _answered_handback(env, pr, lines, open_keys):
    """A green own-repo PR whose handback SUMMARY carries ``lines``; ``round`` reads ``open_keys``."""
    from gateway import kanban_prism_answers as pa
    from hermes_constants import get_default_hermes_root

    scripts = get_default_hermes_root() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "prism_soft_gate.py").write_text(
        "import re\n"
        "VERDICT_LINE = re.compile(r'prism-verdict:\\s*([A-Za-z0-9_.\\-]+)\\s+(not-a-bug|false|by-design)\\b', re.I)\n"
        "FIXED_LINE = re.compile(r'prism-verdict:\\s*([A-Za-z0-9_.\\-]+)\\s+fixed\\s+(?:in\\s+)?([0-9a-f]{7,40})\\b', re.I)\n")
    env["monkeypatch"].setattr(pa, "_GRAMMAR", {})
    env["monkeypatch"].setattr(pa, "open_keys_fn", lambda repo, n, head, card: list(open_keys))
    env["health"][(REPO, pr)] = {"state": "open", "mergeable_state": "clean", "failing": [], "head": "e" * 40}
    tid = _auto_card(env)
    _sub(tid)
    with kb.connect_closing() as conn, \
            kb.mutation_actor(session_ids=[WORKER_SID], profile="worker", surface="worker"):
        kb.request_review(conn, tid, force=True, summary=f"PR {REPO}#{pr} ready\n" + "\n".join(lines))
    return tid


def test_w21_all_keys_answered_handback_is_one_fork_dispatch_zero_turns(auto_env):
    """RED-PROVABLE: drop the own_repo_review_all_keys_answered row from kanban_wake_disposition._TABLE
    (or stop setting prism_all_answered in kanban_auto_handle.review_payload) -> 0 dispatches."""
    env = auto_env
    _answered_handback(env, 2131, ["prism-verdict: 5ef38967cc14 fixed " + "e" * 40,
                                   "prism-verdict: f1a50a36ff7f not-a-bug unreachable"],
                       ["5ef38967cc14", "f1a50a36ff7f"])
    turns = _tick(env)
    rows = [(e["row"], e["mode"]) for e in env["dispatched"]]
    assert rows == [("own_repo_review_all_keys_answered", "fork")] and turns == [], \
        _msg("W21", f"an all-answered handback did not fork exactly once: {rows}", turns)


def test_w22_one_unanswered_key_keeps_todays_merge_pass_row(auto_env):
    """RED-PROVABLE: make kanban_prism_answers.all_answered ignore keys -> a dispatch appears."""
    env = auto_env
    _answered_handback(env, 2132, ["prism-verdict: 5ef38967cc14 fixed " + "e" * 40],
                       ["5ef38967cc14", "f1a50a36ff7f"])
    turns = _tick(env)
    assert env["dispatched"] == [], _msg("W22", "an unanswered key was auto-handled", turns)
    assert _paths(turns) == ["notifier"], _msg("W22", "the green handback must wake as today", turns)


# --- W23: a wake ACCEPTED behind a busy session survives the drain's depth cap (t_f36d21e8) ---


@pytest.mark.asyncio
async def test_w23_two_wakes_queued_behind_a_depth_capped_turn_both_survive(monkeypatch, tmp_path):
    """2026-10-22 05:43-07:12 (session 20261018_220948_05ec4d95): the notifier logged ``woke agent
    for t_69c88a73`` at 05:43:23, the adapter accepted it behind a busy turn, and no turn for it ever
    ran (next G-LINT turn 07:12, from a later handback). At ``_MAX_INTERRUPT_DEPTH`` the drain
    re-seated the dequeued follow-up with ``merge_pending_message_event`` into the head slot that
    ``_promote_queued_event`` had just refilled from the overflow, so the overflow head was
    REPLACED (two internal TEXT events never merge). One accepted wake per handback must reach a turn.

    RED-PROVABLE: restore the plain ``merge_pending_message_event`` re-seat in
    ``GatewayRunner._run_agent_queued_followup`` -> wake B is gone."""
    import importlib
    import sys
    import types

    from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
    from gateway.config import PlatformConfig

    key = "agent:main:telegram:group:-1001:17585"
    src = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", thread_id="17585")

    class _Ad(BasePlatformAdapter):
        def __init__(self):
            super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            return True

        async def disconnect(self) -> None:
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
            return SendResult(success=True, message_id="m1")

        async def edit_message(self, chat_id, message_id, content) -> SendResult:
            return SendResult(success=True, message_id=message_id)

        async def send_typing(self, chat_id, metadata=None) -> None:
            return None

        async def stop_typing(self, chat_id) -> None:
            return None

        async def get_chat_info(self, chat_id: str):
            return {"id": chat_id}

    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    gateway_run = importlib.import_module("gateway.run")
    adapter = _Ad()
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    for name, value in (("_voice_mode", {}), ("_prefill_messages", []), ("_ephemeral_system_prompt", ""),
                        ("_reasoning_config", None), ("_provider_routing", {}), ("_fallback_model", None),
                        ("_session_db", None), ("_running_agents", {}), ("_session_run_generation", {})):
        setattr(runner, name, value)
    runner.hooks = types.SimpleNamespace(loaded_hooks=False)
    runner.config = types.SimpleNamespace(thread_sessions_per_user=False, group_sessions_per_user=False,
                                          stt_enabled=False)
    runner._MAX_INTERRUPT_DEPTH = 0  # this drain IS the capped one
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    def wake(tid):
        return MessageEvent(text=f"[kanban] Task {tid} handed off for review", message_type=MessageType.TEXT,
                            source=src, internal=True, allow_gateway_control=False,
                            metadata={"kanban_owner_wake": True})

    class StubAgent:
        def __init__(self, **kwargs):
            self.tools = []
            self._interrupt_requested = False

        @property
        def is_interrupted(self):
            return self._interrupt_requested

        def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
            # Both notifier wakes arrive while this turn runs; the runner FIFO accepts both.
            runner._enqueue_fifo(key, wake("t_A"), adapter)
            runner._enqueue_fifo(key, wake("t_B"), adapter)
            return {"final_response": "done", "messages": [], "api_calls": 1}

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = StubAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    await runner._run_agent(message="busy turn", context_prompt="", history=[], source=src,
                            session_id="sess-w23", session_key=key)

    head = adapter._pending_messages.get(key)
    overflow = list(runner._session_state(key).conversation.queued_events)
    texts = ([head.text] if head else []) + [e.text for e in overflow]
    assert texts == ["[kanban] Task t_A handed off for review", "[kanban] Task t_B handed off for review"], (
        f"FORK CANARY {FEATURE} W23: an accepted wake was dropped at the interrupt-depth cap; queue={texts}")
