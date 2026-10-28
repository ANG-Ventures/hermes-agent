"""delegate_task(sign_grant=...) spawn half (t_19c567a8): who may mint, and the grant ends with the child.

The board half (validation of every granted write) is tests/hermes_cli/test_kanban_sign_grants.py.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sign_grants as ksg
from tools import delegate_tool_sign_grant as dsg

MODEL = "claude-opus-5-5"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in [k for k in os.environ if k.startswith("HERMES_KANBAN") or k == "HERMES_DELEGATED_CHILD_CONTEXT"]:
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    assert str(kb.kanban_db_path()).startswith(str(tmp_path))
    (home / "config.yaml").write_text("kanban:\n  sign_grants: 'on'\n")
    with kb.connect_closing() as conn:
        card = kb.create_task(conn, title="review card")
    monkeypatch.setattr("hermes_cli.kanban_identity.operator_label_proven", lambda name: True)
    monkeypatch.setattr("hermes_cli.kanban_identity.worker_ancestor_profile", lambda: None)
    monkeypatch.setattr(dsg, "_ledger", lambda event: None)
    return card


def _agents(child_model=MODEL):
    parent = SimpleNamespace(model=MODEL, session_id="parent-sid", _delegate_depth=0)
    child = SimpleNamespace(model=child_model, session_id="child-sid", _subagent_id="sa-0-abc")
    return parent, child


def _task(card, **kw):
    t = {"goal": "review", "inherit_context": True,
         "sign_grant": {"cards": [card], "verbs": ["verdict", "approve"], "expires_in_min": 30}}
    t.update(kw)
    return t


def _live_rows(grant_id):
    with kb.connect_closing() as conn:
        return conn.execute("SELECT * FROM sign_grants WHERE grant_id = ? AND revoked_at IS NULL",
                            (grant_id,)).fetchall()


def test_mint_binds_the_child_session_and_revoke_ends_it(board):
    parent, child = _agents()
    task = _task(board)
    g = dsg.mint_for_child(task, child, parent)
    rows = _live_rows(g["grant_id"])
    assert len(rows) == 1 and rows[0]["child_session_ref"] == kb.derive_session_ref("child-sid")
    assert g["grant_id"] in task["goal"]
    dsg.revoke([g])
    assert _live_rows(g["grant_id"]) == []


@pytest.mark.parametrize("case", ["no_inherit", "model_mismatch", "nested_parent", "unproven_by", "worker"])
def test_mint_refusals_write_no_grant(board, monkeypatch, case):
    parent, child = _agents(child_model="claude-sonnet-5" if case == "model_mismatch" else MODEL)
    task = _task(board, inherit_context=False) if case == "no_inherit" else _task(board)
    if case == "nested_parent":
        parent._delegate_depth = 1
    if case == "unproven_by":
        monkeypatch.setattr("hermes_cli.kanban_identity.operator_label_proven", lambda name: False)
    if case == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    with pytest.raises(dsg.SignGrantError):
        dsg.mint_for_child(task, child, parent)
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sign_grants").fetchone()[0] == 0


def test_task_without_sign_grant_mints_nothing(board):
    parent, child = _agents()
    assert dsg.mint_for_child({"goal": "x"}, child, parent) is None


def test_revoked_grant_refuses_the_child_write(board):
    from agent.delegation_context import delegated_child_context

    parent, child = _agents()
    g = dsg.mint_for_child(_task(board), child, parent)
    dsg.revoke([g])
    with delegated_child_context(), kb.connect_closing() as conn, pytest.raises(ksg.GrantRefused, match="revoked"):
        kb.add_comment(conn, board, "apollo", "looks right", session_ref=kb.derive_session_ref("child-sid"),
                       grant_id=g["grant_id"])


def test_delegate_task_grant_is_live_through_the_parent_turn_and_revoked_at_its_end(board, monkeypatch):
    """Real delegate_task path: the grant is live while the child runs AND after it returns (the parent may
    run the merge gate in the same turn); the parent's turn end revokes it."""
    import json
    from unittest.mock import patch

    from tools.delegate_tool import delegate_task
    from tests.tools.test_delegate import _make_mock_parent

    parent = _make_mock_parent()
    parent.model = MODEL
    parent.session_id = "parent-sid"
    seen = {}

    def _run(task_index, goal, child, parent_agent, **_kw):
        with kb.connect_closing() as conn:
            seen["live"] = conn.execute("SELECT COUNT(*) FROM sign_grants WHERE revoked_at IS NULL").fetchone()[0]
        seen["goal"] = goal
        return {"task_index": task_index, "status": "completed", "summary": "ok", "api_calls": 1,
                "duration_seconds": 0.1}

    def _child(**kw):
        return SimpleNamespace(model=MODEL, session_id="child-sid", _subagent_id="sa-0-x")

    with patch("tools.delegate_tool._run_single_child", side_effect=_run), \
            patch("tools.delegate_tool._build_child_preserving_parent_tools", side_effect=_child):
        out = json.loads(delegate_task(
            tasks=[{"goal": "review it", "inherit_context": True,
                    "sign_grant": {"cards": [board], "verbs": ["approve"]}}],
            parent_agent=parent))
        refused = json.loads(delegate_task(
            tasks=[{"goal": "review it", "inherit_context": True,
                    "sign_grant": {"cards": [board], "verbs": ["approve"]}}],
            background=True, parent_agent=parent))
    assert out["results"][0]["status"] == "completed", out
    assert seen["live"] == 1 and "signing grant g-" in seen["goal"]
    assert "background=false" in refused.get("error", ""), refused

    def _live():
        with kb.connect_closing() as conn:
            return conn.execute("SELECT COUNT(*) FROM sign_grants WHERE revoked_at IS NULL").fetchone()[0]

    assert _live() == 1
    dsg.revoke_turn_grants(parent)
    assert _live() == 0
    dsg.revoke_turn_grants(parent)  # idempotent: nothing left to revoke


def test_parent_turn_end_revokes_its_grants(board, monkeypatch):
    """agent/turn_finalizer.finalize_turn is what ends the grant (§5: never outlive the parent turn)."""
    from agent.turn_finalizer import finalize_turn
    from tests.agent.test_turn_finalizer_final_response_persistence import FakeAgent

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent.model, agent._delegate_depth = MODEL, 0
    _parent, child = _agents()
    g = dsg.mint_for_child(_task(board), child, agent)
    dsg.register_turn_grants(agent, [g])
    assert len(_live_rows(g["grant_id"])) == 1
    finalize_turn(agent, final_response="done", api_call_count=1, interrupted=False, failed=False,
                  messages=[{"role": "user", "content": "go"}, {"role": "assistant", "content": "done"}],
                  conversation_history=[], effective_task_id="task", turn_id="turn", user_message="go",
                  original_user_message="go", _should_review_memory=False, _turn_exit_reason="text_response")
    assert _live_rows(g["grant_id"]) == []


def test_model_dispatch_path_runs_a_granted_delegation_synchronously(board):
    """The LIVE model path (AIAgent._dispatch_delegate_task) forwards sign_grant and does not background it, so a
    top-level session can actually mint; an ungranted top-level call keeps backgrounding."""
    from unittest.mock import patch

    from run_agent import AIAgent

    agent = SimpleNamespace(_delegate_depth=0)
    seen = []
    with patch("tools.delegate_tool.delegate_task", side_effect=lambda **kw: seen.append(kw) or "{}"):
        AIAgent._dispatch_delegate_task(agent, {"tasks": [{"goal": "g", "sign_grant": {"cards": [board]}}]})
        AIAgent._dispatch_delegate_task(agent, {"tasks": [{"goal": "g"}], "sign_grant": {"cards": [board]}})
        AIAgent._dispatch_delegate_task(agent, {"tasks": [{"goal": "g"}]})
    assert [kw["background"] for kw in seen] == [False, False, True]
    assert seen[1]["sign_grant"] == {"cards": [board]}
