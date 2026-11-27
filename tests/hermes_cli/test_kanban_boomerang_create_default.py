"""Boomerang wake is settled at card birth for EVERY producer (t_c24b1cc7).

Ace msg1569101606384050286: "Kanban should default to Boomerang Wake pretty much always".
Before this, the default reached only creates that also subscribed a routable chat, so
script/cron/worker/operator-home cards were stored with ``boomerang_wake`` NULL
(t_fd3d8779, t_9d013f44, t_eb7309e4, t_aa044abd). The contract:

- unset -> ``kanban.auto_subscribe_boomerang`` (default True), whoever the producer is;
- an explicit opt-out is stored as given and a later default-carrying create never flips it;
- the flag never invents a recipient: ``wake_route`` reads back whether a chat holds the wake.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_graph as kbg
from hermes_cli import kanban_db_notify as kbn

ROOT = Path(__file__).resolve().parents[2]
HOME_SID = "20261108_140000_home"

_IDENTITY_ENV = ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_CHAT_TYPE", "HERMES_SESSION_USER_ID",
                 "HERMES_SESSION_KEY", "HERMES_SESSION_ID", "HERMES_SESSION_THREAD_ID", "HERMES_KANBAN_TASK",
                 "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_OWNER_PID", "HERMES_KANBAN_DB")


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = Path(os.environ["HERMES_HOME"])
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in _IDENTITY_ENV:
        monkeypatch.delenv(key, raising=False)
    from gateway.session_context import reset_session_vars
    reset_session_vars()
    kb.init_db()
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        db.create_session(session_id=HOME_SID, source="discord", origin_json=json.dumps({
            "platform": "discord", "chat_id": "chat-home", "chat_type": "group",
            "user_id": "u1", "scope_id": "g1"}))
    finally:
        db.close()
    return home


def _stored(tid: str):
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT boomerang_wake FROM tasks WHERE id = ?", (tid,)).fetchone()
        created = [e.payload for e in kb.list_events(conn, tid) if e.kind == "created"][0]
        subs = [(s["chat_id"], s["delivery_mode"]) for s in kbn.list_notify_subs(conn, tid)]
    return row[0], created.get("boomerang_wake_source"), subs


def test_library_create_unset_stores_default_and_explicit_opt_out_survives(board):
    with kb.connect_closing() as conn:
        dflt = kb.create_task(conn, title="lib default", assignee="peer", session_id=HOME_SID)
        out = kb.create_task(conn, title="lib opt-out", assignee="peer", session_id=HOME_SID,
                             boomerang_wake=False, idempotency_key="opt-out-key")
        # A retried producer that omits the flag must return the SAME card, still opted out.
        again = kb.create_task(conn, title="lib opt-out", assignee="peer", session_id=HOME_SID,
                               idempotency_key="opt-out-key")
    assert _stored(dflt)[:2] == (1, "default")
    assert again == out
    assert _stored(out)[:2] == (0, "explicit")


def test_policy_off_is_honoured_and_recorded_as_default(board):
    (board / "config.yaml").write_text("kanban:\n  auto_subscribe_boomerang: false\n")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="policy off", assignee="peer", session_id=HOME_SID)
    assert _stored(tid)[:2] == (0, "default")


@pytest.mark.parametrize("home_args", [["--home", "operator"], ["--session", HOME_SID]])
def test_real_no_env_cli_create(board, home_args):
    """A real ``hermes kanban create`` subprocess with no kanban or gateway identity in env
    (a cron/script/pager-owner-card/main-ci-red-pager create)."""
    env = {k: v for k, v in os.environ.items() if k not in _IDENTITY_ENV}
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "--board", "default", "create", "no-env card",
         "--assignee", "peer", "--json", *home_args],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = json.loads(proc.stdout)
    level, source, subs = _stored(out["id"])
    assert (level, source) == (1, "default")
    if home_args[0] == "--home":
        # Operator pseudo-home has no chat: the card is at boomerang level, and the readback
        # says plainly that nothing will be woken (no invented recipient, no row).
        assert subs == []
        assert out["wake_route"]["waker"] is None and "no chat holds the wake" in out["wake_route"]["note"]
    else:
        assert subs == [("chat-home", "notify+wake")]
        assert out["wake_route"] == {"level": "boomerang wake", "waker": "discord:chat-home"}


def test_worker_child_create_gets_default(board, monkeypatch):
    from agent.delegation_context import is_dispatcher_owned_worker_context
    from tools import kanban_tools as kt

    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="worker parent", assignee="daedalus",
                                session_id=HOME_SID, boomerang_wake=False)
        kb.claim_task(conn, parent)
        task = kb.get_task(conn, parent)
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)
    assert is_dispatcher_owned_worker_context()
    created = json.loads(kt._handle_create(
        {"board": "default", "title": "worker child", "assignee": "peer"}))
    assert created.get("ok"), created
    assert created["boomerang_wake"] is True
    assert _stored(created["task_id"])[:2] == (1, "default")


def test_triage_decomposition_children_follow_root(board):
    with kb.connect_closing() as conn:
        on = kb.create_task(conn, title="root on", assignee="peer", session_id=HOME_SID, triage=True)
        off = kb.create_task(conn, title="root off", assignee="peer", session_id=HOME_SID,
                             triage=True, boomerang_wake=False)
        legacy = kb.create_task(conn, title="root legacy", assignee="peer", session_id=HOME_SID,
                                triage=True)
        conn.execute("UPDATE tasks SET boomerang_wake = NULL WHERE id = ?", (legacy,))
        kids = {root: kbg.decompose_triage_task(conn, root, root_assignee="peer",
                                               children=[{"title": f"child of {root}"}])[0]
                for root in (on, off, legacy)}
    assert _stored(kids[on])[:2] == (1, "inherited")
    assert _stored(kids[off])[:2] == (0, "inherited")
    assert _stored(kids[legacy])[:2] == (1, "default")


def test_persistence_roundtrip_and_explicit_flip(board):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="roundtrip", assignee="peer", session_id=HOME_SID)
    with kb.connect_closing() as conn:   # fresh connection: the stored value, not a cached object
        assert kb.get_task(conn, tid).boomerang_wake is True
        assert kb.set_task_boomerang_wake(conn, tid, False)
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).boomerang_wake is False
