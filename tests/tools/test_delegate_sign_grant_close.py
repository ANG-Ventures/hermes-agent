"""A background batch that ends normally CLOSES its grant; every other end REVOKES it (t_4548202f, operator ruling).

CLOSED (the grant's own child returned ``completed``): no new write under the grant, but the rows it wrote stay
promoted by ``operator_rows_with_live_grants`` until ``expires_at``, so land-on-approve and the parent's fold can still
read a background APPROVE after the batch returned. REVOKED (error, timeout, interrupted, a raise, cancel, the orphan
sweep, an explicit revoke): the rows are void at once. Decided per child, journaled, and voided by the kill switch.
Drives the real delegate_task background dispatch over the real board; only the child build/run seams are patched.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from types import SimpleNamespace

import pytest

from tools.process_registry import process_registry
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sign_grants as ksg
from tools import delegate_tool_sign_grant as dsg

from tests.tools.test_delegate_sign_grant import MODEL, board  # noqa: F401  (fixture)
from tests.tools.test_delegate_sign_grant_background import (  # noqa: F401  (autouse fixture)
    _clean_queue_and_context, _dead_pid, _ok, _parent, _patch, _task,
)

PR = "ANG-Ventures/hermes-home#9001"


def _approve_body():
    rec = {"verdict": "APPROVE", "head_sha": "a" * 40, "pr": PR, "by": "apollo"}
    return "apollo manual: APPROVE @ aaaaaaa\nreview_coverage: " + json.dumps(rec)


def _stamp(card, gid, sid="child-sid"):
    """The child's APPROVE under its grant, written from the child's own session."""
    from agent.delegation_context import delegated_child_context

    with delegated_child_context(sid), kb.connect_closing() as conn:
        return kb.add_comment(conn, card, "apollo", _approve_body(), session_ref=kb.derive_session_ref(sid),
                              grant_id=gid)


def _state():
    with kb.connect_closing() as conn:
        return {r["child_id"]: dict(r) for r in conn.execute(
            "SELECT grant_id, child_id, closed_at, revoked_at, expires_at FROM sign_grants")}


def _promoted(card, now=None):
    with kb.connect_closing() as conn:
        return ksg.operator_rows_with_live_grants(conn, card, now=now)


def _held_gid(child):
    return child.__dict__["_held_sign_grants"][0]["grant_id"]


def _run_background(dt, parent, tasks):
    out = json.loads(dt.delegate_task(tasks=tasks, background=True, parent_agent=parent))
    assert out.get("status") == "dispatched", out
    return process_registry.completion_queue.get(timeout=20)


def test_a_completed_batch_closes_its_grant_and_its_approve_stands_until_expiry(board, monkeypatch, tmp_path):
    def body(task_index, goal, child=None, parent_agent=None, **kw):
        _stamp(board, _held_gid(child))
        return _ok(task_index, goal)

    dt, child = _patch(monkeypatch, body)
    evt = _run_background(dt, _parent(), [_task(board)])
    assert evt["status"] == "completed", evt
    row = _state()["sa-0-bg"]
    assert row["closed_at"] is not None and row["revoked_at"] is None, row
    assert not dsg._own_path()[0].exists()  # a closed grant leaves the owner file
    # A write after the batch ended is refused as closed ...
    with pytest.raises(ksg.GrantRefused, match=f"grant {row['grant_id']} is closed"):
        _stamp(board, row["grant_id"])
    # ... but the APPROVE written inside the batch is still the operator's for land-on-approve.
    rows = _promoted(board)
    assert [(r["author"], r["grant_id"]) for r in rows] == [("apollo", row["grant_id"])], rows
    # Expiry is the hard bound.
    assert _promoted(board, now=row["expires_at"]) == []
    assert len(_promoted(board, now=row["expires_at"] - 1)) == 1
    # The kill switch voids a closed grant's rows, no restart.
    cfg = tmp_path / ".hermes" / "config.yaml"
    cfg.write_text("kanban:\n  sign_grants: 'off'\n", encoding="utf-8")
    assert _promoted(board) == []
    cfg.write_text("kanban:\n  sign_grants: 'on'\n", encoding="utf-8")
    assert len(_promoted(board)) == 1
    # An explicit revoke of a closed grant still works and voids its rows.
    with kb.connect_closing() as conn:
        assert ksg.revoke(conn, row["grant_id"]) == 1
    assert _promoted(board) == []


@pytest.mark.parametrize("outcome", ["error", "timeout", "interrupted", "raise"])
def test_a_child_that_did_not_complete_revokes_its_grant_and_voids_its_stamp(board, monkeypatch, outcome):
    def body(task_index, goal, child=None, parent_agent=None, **kw):
        _stamp(board, _held_gid(child))
        if outcome == "raise":
            raise RuntimeError("child blew up after stamping")
        return dict(_ok(task_index, goal), status=outcome, exit_reason=outcome)

    dt, _child = _patch(monkeypatch, body)
    _run_background(dt, _parent(), [_task(board)])
    row = _state()["sa-0-bg"]
    assert row["revoked_at"] is not None and row["closed_at"] is None, row
    assert _promoted(board) == []


def test_cancel_revokes_and_voids_a_stamp_made_before_it(board, monkeypatch):
    import threading

    from tools import async_delegation

    stamped, release = threading.Event(), threading.Event()

    def body(task_index, goal, child=None, parent_agent=None, **kw):
        _stamp(board, _held_gid(child))
        stamped.set()
        release.wait(10)
        return _ok(task_index, goal)

    dt, _child = _patch(monkeypatch, body)
    out = json.loads(dt.delegate_task(tasks=[_task(board)], background=True, parent_agent=_parent()))
    assert out["status"] == "dispatched", out
    assert stamped.wait(10)
    assert len(_promoted(board)) == 1
    assert async_delegation.interrupt_for_session(session_key="parent-sess", reason="test_stop") >= 1
    assert _promoted(board) == []
    row = _state()["sa-0-bg"]
    assert row["revoked_at"] is not None and row["closed_at"] is None, row
    release.set()  # a completion after the cancel cannot close it: a revoked grant never closes (test below)


def test_a_mixed_batch_closes_the_completed_childs_grant_and_revokes_the_failed_ones(board, monkeypatch):
    import tools.delegate_tool as dt

    def body(task_index, goal, child=None, parent_agent=None, **kw):
        _stamp(board, _held_gid(child), sid=child.session_id)
        if task_index == 0:
            return _ok(task_index, goal)
        return dict(_ok(task_index, goal), status="error", error="boom")

    _dt, c0 = _patch(monkeypatch, body)
    _dt, c1 = _patch(monkeypatch, body)
    c1.session_id, c1._subagent_id = "child-sid-2", "sa-1-bg"
    built = iter([c0, c1])
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", lambda **kw: next(built))
    tasks = [dict(_task(board), goal=f"review PR {PR} at its head, part {n}") for n in (1, 2)]
    _run_background(dt, _parent(), tasks)
    state = _state()
    assert state["sa-0-bg"]["closed_at"] is not None and state["sa-0-bg"]["revoked_at"] is None, state
    assert state["sa-1-bg"]["revoked_at"] is not None and state["sa-1-bg"]["closed_at"] is None, state
    assert [r["grant_id"] for r in _promoted(board)] == [state["sa-0-bg"]["grant_id"]]


def test_the_orphan_sweep_revokes_and_voids_a_dead_batchs_stamp(board, monkeypatch):
    from gateway.run import GatewayRunner

    child = SimpleNamespace(model=MODEL, session_id="dead-child", _subagent_id="sa-dead", _context_inherited="summary")
    g = dsg.mint_for_child(_task(board), child, _parent(), batch_bound=True)
    _stamp(board, g["grant_id"], sid="dead-child")
    assert len(_promoted(board)) == 1
    root = dsg._batch_dir()
    root.mkdir(parents=True, exist_ok=True)
    pid = _dead_pid()
    (root / f"{pid}-1.json").write_text(json.dumps({"pid": pid, "created": 1.0, "grants": [
        {"grant_id": g["grant_id"], "board": g["board"], "delegation_id": "d-dead"}]}), encoding="utf-8")
    runner = SimpleNamespace(_current_boot_id=lambda: "boot-x", config=SimpleNamespace(multiplex_profiles=False))
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda multiplex=False: [])
    GatewayRunner._recover_async_delegations_once(runner)
    row = _state()["sa-dead"]
    assert row["revoked_at"] is not None and row["closed_at"] is None, row
    assert _promoted(board) == []


def test_journal_replay_restores_a_closed_grant_closed(board, monkeypatch, tmp_path):
    from hermes_cli import kanban_journal_replay

    monkeypatch.setenv("HERMES_KANBAN_JOURNAL_DIR", str(tmp_path / "journal"))
    child = SimpleNamespace(model=MODEL, session_id="child-sid", _subagent_id="sa-j", _context_inherited="summary")
    g = dsg.mint_for_child(_task(board), child, _parent(), batch_bound=True)
    snap = tmp_path / "snap.db"
    shutil.copyfile(str(kb.kanban_db_path()), str(snap))
    since = time.time() - 0.001
    with kb.connect_closing() as conn:
        assert ksg.close(conn, g["grant_id"], now=1234) == 1
        assert ksg.close(conn, g["grant_id"]) == 0  # idempotent
    out = tmp_path / "restored.db"
    res = kanban_journal_replay.replay_into(kb._journal_board_slug(), snap, out, since=since)
    assert res.grants_closed == 1
    rc = sqlite3.connect(str(out))
    rows = rc.execute("SELECT closed_at, revoked_at FROM sign_grants WHERE grant_id = ?", (g["grant_id"],)).fetchall()
    rc.close()
    assert rows == [(1234, None)], rows


def test_a_revoked_grant_never_closes(board):
    child = SimpleNamespace(model=MODEL, session_id="child-sid", _subagent_id="sa-r", _context_inherited="summary")
    g = dsg.mint_for_child(_task(board), child, _parent(), batch_bound=True)
    with kb.connect_closing() as conn:
        ksg.revoke(conn, g["grant_id"])
        assert ksg.close(conn, g["grant_id"]) == 0
    assert _state()["sa-r"]["closed_at"] is None


def test_a_pid_we_may_not_read_is_not_a_live_owner(monkeypatch):
    import psutil

    def denied(pid):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(psutil, "Process", denied)
    assert dsg._process_created(4242) is None
    assert dsg._owner_alive(4242, 1.0) is False
