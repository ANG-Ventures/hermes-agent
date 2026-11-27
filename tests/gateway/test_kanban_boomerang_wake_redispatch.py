"""t_1c893466: a ``redispatch`` verdict on a card in ``review`` hands it to the implementer the
review handoff names, never back to the claim-time (reviewer) assignee.

Measured 2026-11-12 (t_38246203, t_6dc9df18, t_b17af713): the executor sent the card back, then
reassigned it to ``human:apollo`` (the reviewer); the ``tasks_human_ready_*`` lifecycle trigger
flipped it to ``todo`` + ``needs_operator`` and the gateway logged ``handled action=redispatch``
with no worker. These drive the real ``_write_action`` / ``execute_verdict`` on a board that has
the lifecycle triggers installed (``kb.init_db``)."""
from __future__ import annotations

import json

import pytest

from gateway import kanban_boomerang_wake as ah
from hermes_cli import kanban_db as kb

WORKER_SID = "sess-worker-1c893466"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_DELETED_WAL_CHECK", "registry")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    for var in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_SESSION_ID",
                "HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return tmp_path


def _review_card(reviewer: str = "human:apollo", implementer: str = "daedalus") -> str:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="redispatch from review", assignee=implementer)
        with kb.mutation_actor(session_ids=[WORKER_SID], profile=implementer, surface="worker"):
            assert kb.request_review(conn, tid, force=True, summary="PR o/r#1 ready", reviewer=reviewer)
        task = kb.get_task(conn, tid)
        assert (task.status, task.assignee) == ("review", reviewer)
        last = conn.execute("SELECT max(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
    return tid, int(last)


def _entry(tid: str, event_id: int, *, assignee: str, status: str = "review") -> dict:
    return {"task_id": tid, "delegation_id": "dlg-1c893466", "event_id": event_id, "assignee": assignee,
            "status": status, "row": "own_repo_review_red", "child_session": "", "key": f"k-{tid}"}


def _kinds(tid: str) -> list:
    with kb.connect_closing() as conn:
        return [e.kind for e in kb.list_events(conn, tid)]


def test_redispatch_from_review_goes_to_the_implementer_not_the_reviewer(board):
    tid, ev = _review_card()
    with kb.connect_closing() as conn:
        ah._write_action(kb, conn, _entry(tid, ev, assignee="human:apollo"),
                         {"action": "redispatch", "summary": "CI red is ours; fix lint"})
        task = kb.get_task(conn, tid)
    assert task.status == "ready", task.status
    assert task.assignee == "daedalus" and kb.is_worker_assignee(task.assignee)
    kinds = _kinds(tid)
    assert "needs_operator" not in kinds
    assert "changes_requested" in kinds and kinds.count(ah.RECEIPT_KIND) == 1


def test_redispatch_with_no_worker_implementer_writes_no_receipt(board):
    """The review handoff names no worker: nothing is dispatchable, so no receipt (no 'handled' line)."""
    tid, ev = _review_card()
    with kb.connect_closing() as conn, kb.write_txn(conn):
        row = conn.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
                           "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        payload = json.loads(row[1])
        payload["implementer"] = "human:operator"
        conn.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row[0]))
    with kb.connect_closing() as conn:
        with pytest.raises(RuntimeError, match="no worker implementer"):
            ah._write_action(kb, conn, _entry(tid, ev, assignee="human:apollo"),
                             {"action": "redispatch", "summary": "fix it"})
        task = kb.get_task(conn, tid)
    assert task.status == "review", "a refused redispatch must leave the card where it was"
    assert ah.RECEIPT_KIND not in _kinds(tid)


def test_redispatch_on_a_human_todo_card_surfaces_with_a_plain_reason(board, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="operator card", assignee="human:apollo", boomerang_wake=True)
        assert kb.get_task(conn, tid).status == "todo"
        ev = conn.execute("SELECT max(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
    entry = {**_entry(tid, int(ev), assignee="human:apollo", status="todo"), "row": "changes_requested",
             "verdict_nonce": "0a1b2c", "state": ah.STATE_RUNNING}
    state = ah.BoomerangWakeState()
    monkeypatch.setattr(state, "get_claim", lambda key: entry)
    monkeypatch.setattr(state, "update_claim", lambda *a, **k: None)
    body = {"action": "redispatch", "evidence": ["pytest"], "summary": "redo it"}
    final = f"<<<BOOMERANG-VERDICT nonce=0a1b2c>>>\n{json.dumps(body)}\n<<<END-VERDICT>>>"
    res = ah.execute_verdict(entry, final, state=state)
    assert res["ok"] is False and res.get("surface") is True, res
    assert "requeue refused" not in res["reason"]
    assert "human:apollo" in res["reason"] and "no worker" in res["reason"]
    assert res["summary"] == res["reason"] and res["reason"] in res["detail"], "the owner reads summary/detail"
    assert ah.RECEIPT_KIND not in _kinds(tid)
