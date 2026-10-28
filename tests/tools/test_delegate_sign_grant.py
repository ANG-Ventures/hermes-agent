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
    parent = SimpleNamespace(model=MODEL, session_id="parent-sid", _delegate_depth=0, _dispatched_model=MODEL)
    child = SimpleNamespace(model=child_model, session_id="child-sid", _subagent_id="sa-0-abc",
                            _context_inherited="summary")
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
    with delegated_child_context("child-sid"), kb.connect_closing() as conn, pytest.raises(ksg.GrantRefused, match="revoked"):
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
    parent.model = parent._dispatched_model = MODEL
    parent.session_id = "parent-sid"
    seen = {}

    def _run(task_index, goal, child, parent_agent, **_kw):
        with kb.connect_closing() as conn:
            seen["live"] = conn.execute("SELECT COUNT(*) FROM sign_grants WHERE revoked_at IS NULL").fetchone()[0]
        seen["goal"] = goal
        return {"task_index": task_index, "status": "completed", "summary": "ok", "api_calls": 1,
                "duration_seconds": 0.1}

    def _child(**kw):
        return SimpleNamespace(model=MODEL, session_id="child-sid", _subagent_id="sa-0-x", _context_inherited="summary")

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


def _facade_agent():
    from unittest.mock import MagicMock, patch

    from run_agent import AIAgent

    with patch("model_tools.get_tool_definitions", return_value=[]), \
            patch("run_agent.check_toolset_requirements", return_value={}), patch("run_agent.OpenAI"):
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent.model = agent._dispatched_model = MODEL
    agent._delegate_depth = 0
    return agent


@pytest.mark.parametrize("exit_kind", ["finalized", "content_filter", "raises"])
def test_every_parent_turn_exit_revokes_its_grants(board, exit_kind):
    """Prism d8077e51e319: the grant ends with the parent's turn on EVERY exit, not only the ones that
    reach finalize_turn: the turn facade's outer finally is the one place a normal end, an early
    return (content_filter refusal with no fallback) and an exception all pass."""
    from unittest.mock import patch

    agent = _facade_agent()
    _parent, child = _agents()
    seen = {}

    def fake_loop(ag, *_a, **_kw):
        g = dsg.mint_for_child(_task(board), child, ag)
        dsg.register_turn_grants(ag, [g])
        seen["g"] = g
        assert len(_live_rows(g["grant_id"])) == 1
        if exit_kind == "raises":
            raise RuntimeError("provider exploded")
        if exit_kind == "content_filter":
            return {"final_response": "Response blocked by content_filter", "messages": [], "api_calls": 1,
                    "completed": False, "failed": True, "error": "content_filter"}
        return {"final_response": "done", "messages": [], "api_calls": 1, "completed": True}

    with patch("hermes_cli.lifecycle.invoke_hook", lambda *_a, **_kw: []), \
            patch("agent.conversation_loop.run_conversation", fake_loop):
        try:
            agent.run_conversation("go")
        except RuntimeError:
            assert exit_kind == "raises"
    assert _live_rows(seen["g"]["grant_id"]) == []


def test_a_failing_turn_end_revoke_never_replaces_the_turn_result(board, monkeypatch, caplog):
    """Prism 9c5349bddc34: the revoke runs from the turn's outer finally, so it must never raise there. A turn
    that produced a result returns it even when every revoke fails; the failure is logged naming the grant."""
    import logging
    from unittest.mock import patch

    agent = _facade_agent()
    _parent, child = _agents()
    seen = {}

    def fake_loop(ag, *_a, **_kw):
        g = dsg.mint_for_child(_task(board), child, ag)
        dsg.register_turn_grants(ag, [g])
        seen["g"] = g
        return {"final_response": "done", "messages": [], "api_calls": 1, "completed": True}

    monkeypatch.setattr(dsg, "revoke", lambda grants: [str(x["grant_id"]) for x in grants])
    with patch("hermes_cli.lifecycle.invoke_hook", lambda *_a, **_kw: []), \
            patch("agent.conversation_loop.run_conversation", fake_loop), \
            caplog.at_level(logging.ERROR, logger=dsg.logger.name):
        result = agent.run_conversation("go")
    assert result["final_response"] == "done"
    assert seen["g"]["grant_id"] in caplog.text and "revoke_failed" in caplog.text
    assert agent.__dict__["_sign_grants"][0]["revoke_failed"] is True


def test_a_child_model_change_revokes_its_grant_before_the_call(board):
    """Prism 6b322f502e4d: a grant is bound to the model it was minted for. The child's first request
    on another model (a fallback activation) revokes it at the dispatch edge, from inside the child's
    read-only board fence, so the fallback model's granted verdict is refused; the same model keeps it."""
    from agent.delegation_context import delegated_child_context

    parent, child = _agents()
    g = dsg.mint_for_child(_task(board), child, parent)
    sref = kb.derive_session_ref("child-sid")
    with delegated_child_context("child-sid"):
        dsg.revoke_on_model_drift(child, MODEL.upper())  # same model, other spelling: still live
        assert len(_live_rows(g["grant_id"])) == 1
        dsg.revoke_on_model_drift(child, "claude-sonnet-5")  # try_activate_fallback moved it
        assert _live_rows(g["grant_id"]) == []
        with kb.connect_closing() as conn, pytest.raises(ksg.GrantRefused, match="revoked"):
            kb.add_comment(conn, board, "apollo", "verdict: looks right", session_ref=sref, grant_id=g["grant_id"])
        with kb.connect_closing() as conn:
            assert conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == 0


def test_perform_api_call_revokes_on_the_model_it_actually_sends(board, monkeypatch):
    """The dispatch edge reads the model in the outgoing request, so a fallback swap is caught on its first call."""
    from types import SimpleNamespace as NS

    from agent import turn_api_call

    parent, child = _agents()
    g = dsg.mint_for_child(_task(board), child, parent)
    calls = []

    class _Child:
        pass

    agent = _Child()
    agent.__dict__.update(child.__dict__, provider="anthropic", base_url="", api_mode="anthropic_messages",
                          platform="subagent", _disable_streaming=True, _has_stream_consumers=lambda: False,
                          _interruptible_api_call=lambda kw: calls.append(kw["model"]) or NS(choices=[]),
                          _has_pending_redirect=lambda: False)
    monkeypatch.setattr("hermes_cli.middleware.run_llm_execution_middleware",
                        lambda kw, fn, **_k: fn(kw))
    monkeypatch.setattr("agent.relay_llm.execute", lambda kw, fn, **_k: fn(kw))
    monkeypatch.setattr("agent.turn_body_budget.terminal_request_body", lambda _a, kw: kw)
    monkeypatch.setattr("agent.agent_runtime_helpers.note_steer_consumed", lambda _a: None)

    def _call(model):
        return turn_api_call.perform_api_call(
            agent, api_kwargs={"model": model}, _original_api_kwargs={"model": model}, _llm_middleware_trace=[],
            _moa_prepared_request=None, _retry=None, thinking_spinner=None, retry_count=0, api_call_count=1,
            api_request_id="r", effective_task_id="t", turn_id="turn", interrupted=False)

    _call(MODEL)
    assert len(_live_rows(g["grant_id"])) == 1
    _call("claude-sonnet-5")
    assert _live_rows(g["grant_id"]) == [] and calls == [MODEL, "claude-sonnet-5"]


@pytest.mark.parametrize("sent", [MODEL, "claude-sonnet-5"])
def test_mint_binds_the_model_the_parent_actually_dispatched(board, monkeypatch, sent):
    """Prism c37b9eed0ae3: middleware may send another model (next_api_kwargs["model"]) while
    agent.model stays the configured one. The grant binds the model the delegating call was
    DISPATCHED on, so a child on the configured model is refused when the parent ran another."""
    from types import SimpleNamespace as NS

    from agent import turn_api_call

    _p, child = _agents()

    class _Parent:
        pass

    parent = _Parent()
    parent.__dict__.update(model=MODEL, session_id="parent-sid", _delegate_depth=0, provider="anthropic",
                           base_url="", api_mode="anthropic_messages", platform="cli", _disable_streaming=True,
                           _has_stream_consumers=lambda: False, _has_pending_redirect=lambda: False,
                           _interruptible_api_call=lambda kw: NS(choices=[]))
    monkeypatch.setattr("hermes_cli.middleware.run_llm_execution_middleware",
                        lambda kw, fn, **_k: fn({**kw, "model": sent}))
    monkeypatch.setattr("agent.relay_llm.execute", lambda kw, fn, **_k: fn(kw))
    monkeypatch.setattr("agent.turn_body_budget.terminal_request_body", lambda _a, kw: kw)
    monkeypatch.setattr("agent.agent_runtime_helpers.note_steer_consumed", lambda _a: None)
    turn_api_call.perform_api_call(
        parent, api_kwargs={"model": MODEL}, _original_api_kwargs={"model": MODEL}, _llm_middleware_trace=[],
        _moa_prepared_request=None, _retry=None, thinking_spinner=None, retry_count=0, api_call_count=1,
        api_request_id="r", effective_task_id="t", turn_id="turn", interrupted=False)
    assert parent.model == MODEL
    if sent == MODEL:
        assert dsg.mint_for_child(_task(board), child, parent)
        return
    with pytest.raises(dsg.SignGrantError, match="claude-sonnet-5"):
        dsg.mint_for_child(_task(board), child, parent)
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sign_grants").fetchone()[0] == 0


def test_mint_refuses_when_no_dispatched_model_is_known(board):
    """No dispatch stamp on the parent: the model that produced the delegation cannot be established."""
    parent, child = _agents()
    del parent._dispatched_model
    with pytest.raises(dsg.SignGrantError, match="dispatched"):
        dsg.mint_for_child(_task(board), child, parent)


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


def test_provider_spelling_of_the_same_model_is_not_drift(board):
    """Prism 9b929567c500: minted on the provider spelling, called on the wire spelling: no drift."""
    parent = SimpleNamespace(model="anthropic/claude-opus-4.6", session_id="parent-sid", _delegate_depth=0,
                             _dispatched_model="anthropic/claude-opus-4.6")
    child = SimpleNamespace(model="anthropic/claude-opus-4.6", session_id="child-sid", _subagent_id="sa-0-abc",
                            _context_inherited="summary")
    g = dsg.mint_for_child(_task(board), child, parent)
    dsg.revoke_on_model_drift(child, "claude-opus-4-6")
    assert len(_live_rows(g["grant_id"])) == 1
    dsg.revoke_on_model_drift(child, "claude-sonnet-4-6")
    assert _live_rows(g["grant_id"]) == []


@pytest.mark.parametrize("fails", [1, 99])
def test_turn_end_keeps_unrevoked_grants_registered(board, monkeypatch, fails):
    """Prism 53347c16a468: a grant leaves the turn registry only once revoked. One failure is retried;
    a persistent failure keeps it registered, marked revoke_failed, for the next turn end."""
    parent, child = _agents()
    g = dsg.mint_for_child(_task(board), child, parent)
    dsg.register_turn_grants(parent, [g])
    real, calls = dsg.revoke, []

    def flaky(grants):
        calls.append(len(grants))
        return [str(x["grant_id"]) for x in grants] if len(calls) <= fails else real(grants)

    monkeypatch.setattr(dsg, "revoke", flaky)
    if fails == 1:
        dsg.revoke_turn_grants(parent)
        assert "_sign_grants" not in parent.__dict__ and _live_rows(g["grant_id"]) == []
    else:
        dsg.revoke_turn_grants(parent)  # never raises: it runs from the turn's outer finally (Prism 9c5349bddc34)
        held = parent.__dict__["_sign_grants"]
        assert [x["grant_id"] for x in held] == [g["grant_id"]] and held[0]["revoke_failed"] is True
        assert len(_live_rows(g["grant_id"])) == 1


@pytest.mark.parametrize("where", ["second_child", "after_mint"])
def test_a_failed_multi_child_mint_revokes_what_it_minted(board, monkeypatch, where):
    """Prism cd87b39be0ea: any exception during the mint (not only SignGrantError) revokes every grant
    already minted before it propagates."""
    parent, child = _agents()
    _p2, child2 = _agents()
    child2.session_id = "child-sid-2"
    minted = []
    real_mint = dsg.mint_for_child

    if where == "second_child":
        def mint(t, c, p, **kw):
            if c is child2:
                raise KeyboardInterrupt
            g = real_mint(t, c, p, **kw)
            minted.append(g)
            return g

        monkeypatch.setattr(dsg, "mint_for_child", mint)
    else:
        def ledger(event):
            minted.append(event)
            raise RuntimeError("ledger down")

        monkeypatch.setattr(dsg, "_ledger", ledger)
    with pytest.raises((KeyboardInterrupt, RuntimeError)):
        dsg.mint_for_children([(0, _task(board), child), (1, _task(board), child2)], parent)
    with kb.connect_closing() as conn:
        live = conn.execute("SELECT COUNT(*) FROM sign_grants WHERE revoked_at IS NULL").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM sign_grants").fetchone()[0]
    assert total >= 1 and live == 0


@pytest.mark.parametrize("profile,token,minted", [("daedalus", False, False), ("default", False, True),
                                                   ("daedalus", True, True)])
def test_only_the_sessions_own_identity_may_be_granted(board, monkeypatch, profile, token, minted):
    """Prism 08a8789d8e59: operator_label_proven passes for every profile the multiplexer serves (it
    is process-scoped, and the stub here stands in for that), so a depth-0 daedalus session must not
    mint apollo authority. The session's own profile decides; only the operator token speaks for another."""
    monkeypatch.setattr("hermes_cli.profiles.current_profile_name", lambda default=None: profile)
    monkeypatch.setattr(kb, "_operator_token_state", lambda: "ok" if token else "absent")
    parent, child = _agents()
    task = _task(board)
    if minted:
        assert dsg.mint_for_child(task, child, parent)["by"] == "apollo"
        return
    with pytest.raises(dsg.SignGrantError, match="daedalus"):
        dsg.mint_for_child(task, child, parent)
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM sign_grants").fetchone()[0] == 0


@pytest.mark.parametrize("inherit_max_tokens, minted", [(50000, True), ("not-an-int", False)])
def test_a_child_whose_context_fold_failed_gets_no_grant(board, inherit_max_tokens, minted):
    """Prism 8283ee6dbefe: inherit_context=true is only the REQUEST. When the summary fold fails
    at construction the child carries none of the parent's rulings, so the mint is refused."""
    import json
    from unittest.mock import MagicMock, patch

    from tools.delegate_tool import delegate_task
    from tests.tools.test_delegate import _make_mock_parent

    parent = _make_mock_parent()
    parent.model = MODEL
    parent.session_id = "parent-sid"
    parent.prefill_messages = None
    parent._session_messages = [{"role": "user", "content": "RULING: ship only on green CI"}]

    def _fake_aiagent(**kw):
        child = MagicMock()
        child.model = kw.get("model")
        child.session_id = "child-sid"
        child._subagent_id = "sa-0-x"
        child._delegate_depth = 1
        child.run_conversation.return_value = {"final_response": "done", "completed": True, "api_calls": 1}
        return child

    cfg = {"boomerang": {"inherit_max_tokens": inherit_max_tokens}}
    with patch("run_agent.AIAgent", side_effect=_fake_aiagent), \
            patch("tools.delegate_tool._load_config", return_value=cfg):
        out = json.loads(delegate_task(
            tasks=[{"goal": "review it", "inherit_context": True,
                    "sign_grant": {"cards": [board], "verbs": ["approve"]}}],
            parent_agent=parent))
    with kb.connect_closing() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM sign_grants").fetchone()[0]
    if minted:
        assert rows == 1, out
        dsg.revoke_turn_grants(parent)
    else:
        assert rows == 0, out
        assert "sign_grant refused" in out.get("error", "") and "inherit" in out["error"], out
