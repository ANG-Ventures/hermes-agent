"""Kanban events wake the OWNING operator session (t_1ae0c35b, Ace 2026-10-03 11:05).

Driven through the REAL notifier tick (``GatewayRunner._kanban_notifier_watcher``)
against a REAL ``SessionStore`` (the fake session registry is a store on
tmp_path holding the owner's live session). The handoff turn is captured off
the adapter the wake hands it to. PR health is the one stub (no GitHub).
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
import yaml

from gateway import kanban_home_route as hr
from gateway import kanban_owner_wake as ow
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn
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
    assert not ev.metadata.get("gateway_session_strict"), (
        "strict pin drops a queued wake after a compression rotation (Prism P1)")
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
        kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=OTHER_CHAT,
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
        kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=CHAT,
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


# --- cancelled duplicate check runs (t_65e5d76f) ------------------------------

# The real set on ANG-Ventures/hermes-home#2694 head 0a6680ba789e: two
# concurrency-cancelled override_lint runs and the success that superseded them,
# each in its own check suite, all workflow 368437559 / pull_request.
_SHA_2694 = "0a6680ba789e159616aa8a0b3bd876937c5c54c1"
_RUNS_2694 = [
    {"id": 111348374621, "name": "override_lint / override_lint", "status": "completed",
     "conclusion": "cancelled", "check_suite": {"id": 100688518781}},
    {"id": 111348374885, "name": "override_lint / override_lint", "status": "completed",
     "conclusion": "cancelled", "check_suite": {"id": 100688518866}},
    {"id": 111348380187, "name": "override_lint / override_lint", "status": "completed",
     "conclusion": "success", "check_suite": {"id": 100688523611}},
]
_WF_2694 = {"workflow_runs": [
    {"check_suite_id": s, "workflow_id": 368437559, "event": "pull_request"}
    for s in (100688518781, 100688518866, 100688523611)]}


def _fake_gh(monkeypatch, runs, workflow_runs, calls=None):
    def fake(path):
        if calls is not None:
            calls.append(path)
        if path == "repos/ANG-Ventures/hermes-home/pulls/2694":
            return {"state": "open", "merged_at": None, "mergeable_state": "clean",
                    "head": {"sha": _SHA_2694}}
        if path.startswith(f"repos/ANG-Ventures/hermes-home/commits/{_SHA_2694}/check-runs"):
            return {"check_runs": runs}
        if path.startswith("repos/ANG-Ventures/hermes-home/actions/runs?head_sha="):
            return workflow_runs
        raise AssertionError(path)
    monkeypatch.setattr(ow, "_gh_json", fake)


def test_pr_2694_superseded_cancels_are_not_red(monkeypatch):
    _fake_gh(monkeypatch, _RUNS_2694, _WF_2694)
    health = ow.query_pr_health("ANG-Ventures/hermes-home", 2694)
    assert health["failing"] == []
    assert ow.pr_is_red_or_dirty(health) is None


def test_pr_2694_unreadable_workflow_map_fails_closed(monkeypatch):
    _fake_gh(monkeypatch, _RUNS_2694, None)
    health = ow.query_pr_health("ANG-Ventures/hermes-home", 2694)
    assert health["failing"] == ["override_lint / override_lint"] * 2


def test_all_pass_head_skips_the_workflow_read(monkeypatch):
    calls: list = []
    runs = [dict(r, conclusion="success") for r in _RUNS_2694]
    _fake_gh(monkeypatch, runs, _WF_2694, calls)
    assert ow.query_pr_health("ANG-Ventures/hermes-home", 2694)["failing"] == []
    assert not any("actions/runs" in c for c in calls)


def _run(rid, concl, suite, name="lint"):
    return {"id": rid, "name": name, "conclusion": concl, "check_suite": {"id": suite}}


def test_red_check_names_rules():
    wf = {1: (10, "pull_request"), 2: (10, "pull_request"), 3: (20, "pull_request"),
          4: (10, "push")}
    # all runs of a check cancelled: stays red
    assert ow.red_check_names([_run(1, "cancelled", 1), _run(2, "cancelled", 2)], wf) == ["lint"]
    # a later failure is never hidden by an earlier success
    assert ow.red_check_names([_run(1, "success", 1), _run(2, "failure", 2)], wf) == ["lint"]
    # a re-run success supersedes an earlier failure of the SAME check
    assert ow.red_check_names([_run(1, "failure", 1), _run(2, "success", 2)], wf) == []
    # skipped is no verdict: the failure still decides
    assert ow.red_check_names([_run(1, "failure", 1), _run(2, "skipped", 2)], wf) == ["lint"]
    # another workflow's same-named success hides nothing
    assert ow.red_check_names([_run(1, "failure", 1), _run(3, "success", 3)], wf) == ["lint"]
    assert ow.red_check_names([_run(1, "cancelled", 1), _run(3, "success", 3)], wf) == ["lint"]
    # a push run is not a re-run of the pull_request run
    assert ow.red_check_names([_run(1, "failure", 1), _run(4, "success", 4)], wf) == ["lint"]
    # one suite: the newest run decides
    assert ow.red_check_names([_run(1, "failure", 1), _run(2, "success", 1)]) == []
    assert ow.red_check_names([_run(1, "timed_out", 1, "e2e")]) == ["e2e"]


def test_heartbeat_is_never_a_trigger_pure():
    assert "heartbeat" not in ow.SCAN_KINDS
    assert ow.is_stuck("heartbeat", {}) is False
    assert ow.is_stuck("reclaimed", {"heartbeat_stale": False}) is False


# --- Prism round 1 (P1s) ------------------------------------------------------


def _held_state(env, t0):
    state = ow.WakeState(Path(get_hermes_home()) / "gateway" / "held.json")
    state.cursors = {"default": 0}
    return state


def test_p1_classification_failure_does_not_advance_cursor(env, monkeypatch):
    tid = _card(env)
    _event(tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})
    state = _held_state(env, 0)
    real = ow._classify_row
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(ow, "_classify_row", flaky)
    assert asyncio.run(ow.tick(env["runner"], now=1000.0, state=state)) == 0
    assert asyncio.run(ow.tick(env["runner"], now=1001.0, state=state)) == 1, "retried, not lost"
    assert "ruling?" in env["adapter"].handled[-1].text


def test_p1_rehomed_while_held_does_not_wake_old_owner(env):
    tid = _card(env)
    _event(tid, "blocked", {"kind": "needs_input", "reason": "first"})
    state = _held_state(env, 0)
    assert asyncio.run(ow.tick(env["runner"], now=1000.0, state=state)) == 1
    _event(tid, "blocked", {"kind": "needs_input", "reason": "second"})
    asyncio.run(ow.tick(env["runner"], now=1001.0, state=state))  # held in the window
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", ("20261003_cron_000000", tid))
    assert asyncio.run(ow.tick(env["runner"], now=1000.0 + ow.COALESCE_SECONDS + 1, state=state)) == 0
    assert len(env["adapter"].handled) == 1, "the former owner is not told it owns the card"


def test_p1_wake_off_added_while_held_is_honoured(env):
    tid = _card(env)
    _event(tid, "blocked", {"kind": "needs_input", "reason": "first"})
    state = _held_state(env, 0)
    assert asyncio.run(ow.tick(env["runner"], now=1000.0, state=state)) == 1
    _event(tid, "blocked", {"kind": "needs_input", "reason": "second"})
    asyncio.run(ow.tick(env["runner"], now=1001.0, state=state))
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("x\nwake: off\n", tid))
    assert asyncio.run(ow.tick(env["runner"], now=1000.0 + ow.COALESCE_SECONDS + 1, state=state)) == 0
    assert state.pending == {}


def test_p1_fresh_event_survives_expiry_of_old_ones(env):
    tid = _card(env)
    state = _held_state(env, 0)
    key = f"default|{tid}"
    state.pending[key] = {"board": "default", "task_id": tid, "home_sid": env["owner"].session_id,
                          "title": "card", "items": [{"event_id": 1, "kind": "crashed",
                                                      "actor_sid": "", "trigger": "stuck",
                                                      "reason": "old", "queued_at": 0.0}]}
    _event(tid, "blocked", {"kind": "needs_input", "reason": "fresh ask"})
    now = ow.PENDING_MAX_AGE_SECONDS + 100.0
    assert asyncio.run(ow.tick(env["runner"], now=now, state=state)) == 1
    text = env["adapter"].handled[-1].text
    assert "fresh ask" in text and "old" not in text


def test_p1_sub_for_another_participant_does_not_suppress_owner_wake(env):
    tid = _card(env)
    with kb.connect_closing() as conn:
        kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=CHAT,
                           chat_type="group", user_id="220000000000000001",
                          delivery_mode="notify+wake")
    _event(tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})
    handled = _tick(env)
    owner_turns = [e for e in handled if (e.metadata or {}).get("kanban_owner_wake")]
    assert len(owner_turns) == 1, "a sub naming another participant wakes THAT session, not the owner"


def test_wake_off_card_is_never_queued(env):
    tid = _card(env, body="wake: off")
    _event(tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})
    state = _held_state(env, 0)
    ow.scan(state, 1000.0, None)
    assert state.pending == {}, "an opted-out card is filtered at scan, not just at delivery"


# --- Prism round 2 (P1s) ------------------------------------------------------


def test_p1r2_unknown_pr_health_is_retried_not_consumed(env):
    tid = _card(env)
    with kb.connect_closing() as conn:
        kb.request_review(conn, tid, force=True, summary="PR ANG-Ventures/hermes-agent#1702")
    state = _held_state(env, 0)
    assert asyncio.run(ow.tick(env["runner"], now=1000.0, state=state)) == 0, "GitHub down"
    env["health"][("ANG-Ventures/hermes-agent", 1702)] = {
        "state": "open", "mergeable_state": "dirty", "failing": []}
    assert asyncio.run(ow.tick(env["runner"], now=1001.0, state=state)) == 1, "retried once readable"
    assert "dirty" in env["adapter"].handled[-1].text


def test_p1r2_profile_gate_applies_to_the_owner_session(env):
    store = env["store"]
    worker_owned = store.get_or_create_session(SessionSource(
        platform=Platform.DISCORD, chat_id=OTHER_CHAT, chat_type="group", user_id=HUMAN,
        profile="daedalus",
    ))
    tid = _card(env, session=worker_owned.session_id)
    _event(tid, "blocked", {"kind": "needs_input", "reason": "ruling?"})
    assert _tick(env) == [], "an owner on a non-operator profile is not woken by an operator gateway"
    assert env["runner"]._kanban_owner_wake_state.pending == {}, "dropped at the gate, not held"


def test_p1r2_state_write_is_fsynced(env, monkeypatch):
    synced = []
    real = ow.os.fsync
    monkeypatch.setattr(ow.os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    ow.WakeState(Path(get_hermes_home()) / "gateway" / "fs.json").write_owner_wake_state(1.0)
    assert len(synced) == 2, "file and directory are both fsynced"


def test_worker_only_gateway_skips_the_scan(env, monkeypatch):
    env["runner"]._active_profile_name = lambda: "daedalus"
    monkeypatch.setattr(ow, "scan", lambda *a, **k: (_ for _ in ()).throw(AssertionError("scanned")))
    assert asyncio.run(ow.tick(env["runner"], now=1.0, state=_held_state(env, 0))) == 0
