"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
    session_has_kanban_terminal_tool,
)
from hermes_cli import kanban_db as kb


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_STOP_NUDGE",
        "HERMES_KANBAN_OWNER_PID",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# ── Ownership gate: the nudge may only fire where a terminal tool EXISTS ──
# HERMES_KANBAN_* is ambient process env, inherited by every child a worker
# spawns. The kanban TOOL gate (tools/kanban_tools.py::_check_kanban_mode)
# withholds kanban_complete from a non-owning process, so a nudge fired there
# demands a tool the model does not have. Measured 2026-09-17 (card
# t_8acd8da3): 6/6 deep-research fan-out one-shots launched from a kanban
# worker burned their nudge budget refusing, and the refusal became the last
# assistant message — their reports (0-byte .md) never reached stdout.
# These tests pin the two gates to the SAME predicate.


def _owner_pid_is(monkeypatch, value):
    monkeypatch.setenv("HERMES_KANBAN_OWNER_PID", str(value))


def test_inheriting_child_is_not_nudged(clear_kanban_env):
    """A child that inherited the env but not the identity must not be nudged."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_8acd8da3")
    # Owner pid names a DIFFERENT process, so this process does not own the grant.
    _owner_pid_is(clear_kanban_env, os.getpid() + 1)
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_owning_worker_is_still_nudged(clear_kanban_env):
    """Positive control: the real dispatcher-spawned worker keeps the guard."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_8acd8da3")
    _owner_pid_is(clear_kanban_env, os.getpid())
    assert kanban_stop_nudge_enabled() is True
    assert build_kanban_stop_nudge(messages=[]) is not None


def test_unclaimed_pending_grant_is_not_nudged(clear_kanban_env):
    """An issued-but-never-claimed grant belongs to nobody, so nudges nobody."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_8acd8da3")
    _owner_pid_is(clear_kanban_env, "pending")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_missing_owner_stamp_fails_open(clear_kanban_env):
    """Back-compat: no stamp at all (hand-driven / pre-stamp dispatcher) still nudges."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_8acd8da3")
    assert kanban_stop_nudge_enabled() is True
    assert build_kanban_stop_nudge(messages=[]) is not None


def test_nudge_gate_agrees_with_kanban_tool_gate(clear_kanban_env):
    """The invariant behind the fix: never nudge where the tool is withheld.

    Asserts the two gates against the SAME env, so a future edit that
    re-derives ownership locally in either file fails here.
    """
    import tools.kanban_tools as kanban_tools

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_8acd8da3")
    for owner in (os.getpid(), os.getpid() + 1, "pending"):
        _owner_pid_is(clear_kanban_env, owner)
        tool_exposed = kanban_tools._is_dispatcher_owned_worker()
        assert kanban_stop_nudge_enabled() is tool_exposed, (
            f"owner={owner}: nudge fires without a terminal tool exposed"
        )


def test_non_owner_with_kanban_tools_exposed_is_not_nudged(clear_kanban_env):
    """The gap the TOOLSET gate alone does not close.

    ``session_has_kanban_terminal_tool`` asks whether the model CAN satisfy the
    nudge. That is necessary but not sufficient: a child of a worker can see
    kanban tools and still not own the card. The codex
    ``hermes_tools_mcp_server`` callback hardcodes ``kanban_complete`` into its
    tool list, and orchestrator profiles enable the kanban toolset outright.
    Nudging such a child pressures a NON-OWNER toward a terminal call on its
    parent's card — the 2026-08-12 ``t_09b90233`` failure, arriving through the
    stop-guard rather than the tool gate.

    Measured: with only the toolset gate this case nudged (True); the ownership
    gate is what makes it silent.
    """
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_parent_card")
    _owner_pid_is(clear_kanban_env, os.getpid() + 1)  # a DIFFERENT process owns it
    kanban_tools_exposed = [
        {"function": {"name": "kanban_complete"}},
        {"function": {"name": "WebSearch"}},
    ]
    # The toolset gate is satisfied — the tool really is there.
    assert session_has_kanban_terminal_tool(kanban_tools_exposed) is True
    # ...and the nudge must STILL not fire, because this process is not the addressee.
    assert build_kanban_stop_nudge(messages=[], tools=kanban_tools_exposed) is None


def test_owning_worker_with_kanban_tools_is_nudged(clear_kanban_env):
    """Positive control for the pair: both gates open ⇒ the guard still works."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_8acd8da3")
    _owner_pid_is(clear_kanban_env, os.getpid())
    tools = [{"function": {"name": "kanban_complete"}}]
    assert session_has_kanban_terminal_tool(tools) is True
    assert build_kanban_stop_nudge(messages=[], tools=tools) is not None


def test_delegated_child_context_is_not_nudged(clear_kanban_env):
    """A delegate_task child shares the process, so pid ownership passes — but
    the delegated-child marker must still suppress the nudge."""
    from agent.delegation_context import delegated_child_context

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_8acd8da3")
    _owner_pid_is(clear_kanban_env, os.getpid())
    assert kanban_stop_nudge_enabled() is True
    with delegated_child_context("child-session"):
        assert kanban_stop_nudge_enabled() is False
        assert build_kanban_stop_nudge(messages=[]) is None






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def _tool_def(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


# ── Inherited HERMES_KANBAN_TASK in a child process without kanban tools ──
# A kanban worker that shells out to `hermes -z ... -t web` (research fan-out,
# nested one-shots) leaks HERMES_KANBAN_TASK into the child. The child's model
# has no kanban_* tool, so the nudge is unsatisfiable: it can only reply
# "I cannot call that" — replacing the real answer on stdout. Verified live
# 2026-09-17 (six research workers lost their reports this way).

def test_no_nudge_when_session_exposes_no_kanban_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_inherited")
    web_only = [_tool_def("web_search"), _tool_def("web_extract")]
    assert session_has_kanban_terminal_tool(web_only) is False
    assert build_kanban_stop_nudge(messages=[], attempts=0, tools=web_only) is None


@pytest.mark.parametrize("terminal", sorted([
    "kanban_complete", "kanban_request_review", "kanban_request_changes", "kanban_block",
]))
def test_nudge_when_any_kanban_terminal_tool_is_exposed(clear_kanban_env, terminal):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_real_worker")
    tools = [_tool_def("web_search"), _tool_def(terminal)]
    assert session_has_kanban_terminal_tool(tools) is True
    assert build_kanban_stop_nudge(messages=[], attempts=0, tools=tools) is not None


def test_unknown_toolset_keeps_legacy_nudge(clear_kanban_env):
    # tools=None == "caller didn't say" → guard behaves exactly as before.
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_legacy")
    assert session_has_kanban_terminal_tool(None) is True
    assert build_kanban_stop_nudge(messages=[], attempts=0, tools=None) is not None


def test_empty_toolset_does_not_nudge(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_no_tools")
    assert build_kanban_stop_nudge(messages=[], attempts=0, tools=[]) is None


def test_conversation_loop_passes_agent_tools_to_stop_guard():
    # The loop must thread the live toolset into the guard, otherwise the
    # module-level fix above is inert in production.
    import inspect
    from agent import conversation_loop

    src = inspect.getsource(conversation_loop)
    call = src[src.index("_kanban_nudge = build_kanban_stop_nudge("):]
    call = call[: call.index("except Exception")]
    assert 'tools=getattr(agent, "tools", None)' in call


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


@pytest.fixture
def worker_run(clear_kanban_env, tmp_path):
    clear_kanban_env.setenv("HERMES_HOME", str(tmp_path))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    with closing(kb.connect()) as conn:
        task_id = kb.create_task(conn, title="Stop guard race", assignee="builder")
        task = kb.claim_task(conn, task_id, claimer="builder:1")
        assert task is not None
        clear_kanban_env.setenv("HERMES_KANBAN_TASK", task_id)
        clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
        yield conn, task


def _terminal_messages(tool_name):
    return [{
        "role": "assistant",
        "tool_calls": [{
            "id": "terminal-call",
            "type": "function",
            "function": {"name": tool_name, "arguments": "{}"},
        }],
    }]


@pytest.mark.parametrize("outcome", ["review_requested", "changes_requested"])
@pytest.mark.parametrize("retain_tool_history", [False, True])
def test_no_nudge_for_handoff_after_successor_claims(
    worker_run, monkeypatch, outcome, retain_tool_history,
):
    conn, task = worker_run
    assert kb.request_review(
        conn, task.id, reviewer="reviewer", summary="Implementation verified",
        expected_run_id=task.current_run_id,
    )
    origin_run_id = task.current_run_id
    tool_name = "kanban_request_review"
    successor = kb.claim_review_task(conn, task.id, claimer="reviewer:1")
    assert successor is not None
    assert successor.current_run_id is not None
    if outcome == "changes_requested":
        origin_run_id = successor.current_run_id
        tool_name = "kanban_request_changes"
        assert kb.request_changes(
            conn, task.id, reason="Add boundary coverage", expected_run_id=origin_run_id,
        )[0]
        successor = kb.claim_task(conn, task.id, claimer="builder:2")
        assert successor is not None
    assert origin_run_id is not None
    assert successor.current_run_id is not None
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(origin_run_id))
    assert successor.current_run_id != origin_run_id
    current_task = kb.get_task(conn, task.id)
    assert current_task is not None and current_task.status == "running"
    origin = kb.get_run(conn, origin_run_id)
    assert origin is not None
    assert origin.outcome == outcome
    assert origin.ended_at is not None

    messages = _terminal_messages(tool_name) if retain_tool_history else []
    assert build_kanban_stop_nudge(messages=messages) is None
    successor_run = kb.get_run(conn, successor.current_run_id)
    assert successor_run is not None and successor_run.outcome is None


@pytest.mark.parametrize("tool_name", [
    "kanban_complete", "kanban_block", "kanban_request_review", "kanban_request_changes",
])
def test_unfinished_run_still_nudged_after_terminal_tool_attempt(worker_run, tool_name):
    conn, task = worker_run
    messages = _terminal_messages(tool_name)
    messages.append({
        "role": "tool", "name": tool_name, "tool_call_id": "terminal-call",
        "content": '{"ok": false, "error": "handoff refused"}',
    })
    run = kb.get_run(conn, task.current_run_id)
    assert run is not None and run.outcome is None
    nudge = build_kanban_stop_nudge(messages=messages)
    assert nudge is not None
    assert task.id in nudge


def test_successor_without_handoff_is_still_nudged(worker_run, monkeypatch):
    conn, task = worker_run
    assert kb.request_review(
        conn, task.id, reviewer="reviewer", expected_run_id=task.current_run_id,
    )
    successor = kb.claim_review_task(conn, task.id, claimer="reviewer:1")
    assert successor is not None
    assert successor.current_run_id is not None
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(successor.current_run_id))
    run = kb.get_run(conn, successor.current_run_id)
    assert run is not None and run.outcome is None
    assert build_kanban_stop_nudge(messages=[]) is not None


@pytest.mark.parametrize("tool_name", ["kanban_request_review", "kanban_request_changes"])
def test_legacy_unpinned_worker_accepts_review_handoffs(clear_kanban_env, tool_name):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    assert build_kanban_stop_nudge(messages=_terminal_messages(tool_name)) is None


@pytest.mark.parametrize("attempts", [0, 1, 2])
def test_unfinished_run_nudge_budget(worker_run, attempts):
    nudge = build_kanban_stop_nudge(messages=[], attempts=attempts)
    assert (nudge is not None) == (attempts < 2)


def test_non_worker_does_not_nudge(clear_kanban_env):
    assert build_kanban_stop_nudge(messages=[]) is None


@pytest.mark.parametrize("handoff", ["complete", "block", "review"])
def test_closed_run_needs_no_tool_history(worker_run, handoff):
    conn, task = worker_run
    if handoff == "complete":
        assert kb.complete_task(conn, task.id, expected_run_id=task.current_run_id)
    elif handoff == "block":
        assert kb.block_task(
            conn, task.id, reason="Missing input", expected_run_id=task.current_run_id,
        )
    else:
        assert kb.request_review(conn, task.id, expected_run_id=task.current_run_id)
    assert build_kanban_stop_nudge(messages=[]) is None


@pytest.mark.parametrize("run_id", ["", "not-an-id", "-1", "999999"])
def test_invalid_or_missing_run_does_not_accept_tool_history(worker_run, monkeypatch, run_id):
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    assert build_kanban_stop_nudge(messages=_terminal_messages("kanban_complete")) is not None


def test_terminal_run_for_another_task_does_not_suppress_nudge(worker_run):
    conn, task = worker_run
    assert kb.request_review(conn, task.id, expected_run_id=task.current_run_id)
    assert build_kanban_stop_nudge(task_id="t_other", messages=[]) is not None


def test_unreadable_board_does_not_accept_tool_history(worker_run, monkeypatch, tmp_path, caplog):
    missing_db = tmp_path / "missing.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(missing_db))
    assert build_kanban_stop_nudge(messages=_terminal_messages("kanban_complete")) is not None
    assert "Could not verify kanban worker run closure" in caplog.text
    assert not missing_db.exists()


def test_stop_check_closes_read_connection(worker_run, monkeypatch):
    connections = []
    connect_readonly = kb.connect_readonly

    def track_connection():
        conn = connect_readonly()
        connections.append(conn)
        return conn

    monkeypatch.setattr(kb, "connect_readonly", track_connection)
    for _ in range(3):
        assert build_kanban_stop_nudge(messages=[]) is not None
    assert len(connections) == 3
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


@pytest.mark.parametrize("handoff", [None, "review_requested", "changes_requested"])
def test_conversation_loop_enforces_originating_run(worker_run, monkeypatch, handoff):
    from run_agent import AIAgent

    conn, task = worker_run
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            session_id="kanban-stop-test", api_key="test-key",
            base_url="https://example.invalid/v1", provider="openai-compat",
            model="test/model", max_iterations=1, quiet_mode=True,
            skip_context_files=True, skip_memory=True,
        )
    agent._cached_system_prompt = "stable test prompt"
    # A real dispatcher-spawned worker exposes the kanban terminal tools; the
    # stop guard is (correctly) inert when they are absent from the toolset.
    agent.tools = [_tool_def("kanban_complete")]
    agent._session_db = None
    agent._session_json_enabled = False
    agent.save_trajectories = False
    agent.compression_enabled = False
    agent._cleanup_task_resources = lambda *_a, **_kw: None
    agent._save_trajectory = lambda *_a, **_kw: None

    def model_call(_kwargs):
        if handoff is not None:
            assert kb.request_review(
                conn, task.id, reviewer="reviewer", expected_run_id=task.current_run_id,
            )
            successor = kb.claim_review_task(conn, task.id, claimer="reviewer:1")
            assert successor is not None
            if handoff == "changes_requested":
                monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(successor.current_run_id))
                assert kb.request_changes(
                    conn, task.id, reason="Add coverage", expected_run_id=successor.current_run_id,
                )[0]
                assert kb.claim_task(conn, task.id, claimer="builder:2") is not None
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="Handoff report", tool_calls=None),
                finish_reason="stop",
            )],
            model="test/model", usage=None,
        )

    agent._interruptible_api_call = model_call
    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("work kanban task")

    assert result["final_response"] == "Handoff report"
    assert result["completed"] is (handoff is not None)
    assert getattr(agent, "_kanban_stop_nudges", 0) == (1 if handoff is None else 0)
    if handoff is not None:
        assert not any(message.get("_kanban_stop_synthetic") for message in result["messages"])


def test_stop_guard_persists_candidate_but_not_nudge(worker_run, monkeypatch):
    """t_4eeb0202: the pre-nudge assistant text is real model output and must
    reach the durable transcript; only the synthetic nudge is stripped.

    Before the fix both were flagged ``_kanban_stop_synthetic`` and dropped, so
    state.db showed a bare kanban_block that seemed to answer nothing."""
    from unittest.mock import MagicMock

    from agent.kanban_stop import build_kanban_stop_nudge as _real_build
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            session_id="kanban-stop-persist", api_key="test-key",
            base_url="https://example.invalid/v1", provider="openai-compat",
            model="test/model", max_iterations=2, quiet_mode=True,
            skip_context_files=True, skip_memory=True,
        )
    agent._cached_system_prompt = "stable test prompt"
    agent.tools = [_tool_def("kanban_complete")]
    agent._session_db = MagicMock()
    agent._session_db_created = True
    agent._session_json_enabled = False
    agent.save_trajectories = False
    agent.compression_enabled = False
    agent._cleanup_task_resources = lambda *_a, **_kw: None
    agent._save_trajectory = lambda *_a, **_kw: None

    candidate = "I've stopped as you asked."
    nudges = []

    def spy_build(**kwargs):
        text = _real_build(**kwargs)
        if text:
            nudges.append(text)
        return text

    monkeypatch.setattr("agent.kanban_stop.build_kanban_stop_nudge", spy_build)
    # Two turns, like the incident: the narrated stop, then the model's reply
    # to the nudge. Only the SECOND text can be re-added by the finalizer's
    # "delivered final_response => assistant row" safety net, so the first
    # candidate reaching the store proves the stop-guard itself persisted it.
    replies = iter([candidate, "Still narrating instead of calling the tool."])
    agent._interruptible_api_call = lambda _kwargs: SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=next(replies), tool_calls=None),
            finish_reason="stop",
        )],
        model="test/model", usage=None,
    )
    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("work kanban task")

    assert nudges, "stop guard must have fired for this test to mean anything"
    assert getattr(agent, "_kanban_stop_nudges", 0) == 2

    # In-memory: the candidate is not flagged; the nudge is.
    cand_rows = [m for m in result["messages"]
                 if m.get("role") == "assistant" and m.get("content") == candidate]
    assert cand_rows and not any(m.get("_kanban_stop_synthetic") for m in cand_rows)
    assert cand_rows[0].get("finish_reason") == "kanban_terminal_required"
    nudge_rows = [m for m in result["messages"] if m.get("content") == nudges[0]]
    assert nudge_rows and all(m.get("_kanban_stop_synthetic") for m in nudge_rows)

    # Durable: what reached the session store.
    persisted = [
        msg
        for _args, kwargs in agent._session_db.append_messages_batch.call_args_list
        for msg in kwargs["messages"]
    ]
    persisted_text = [m.get("content") for m in persisted]
    assert candidate in persisted_text
    assert not set(nudges) & set(persisted_text)
    assert not any(m.get("_kanban_stop_synthetic") for m in persisted)


def test_resume_replay_collapses_kanban_candidate():
    """With the nudge stripped, a resumed transcript has candidate -> next
    assistant back to back. Replay must let the later turn supersede the
    candidate (same as verify-on-stop), not union them into one message."""
    from agent.agent_runtime_helpers import repair_message_sequence

    later = {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "kanban_block", "arguments": "{}"}}],
    }
    messages = [
        {"role": "user", "content": "work kanban task"},
        {"role": "assistant", "content": "I've stopped as you asked.",
         "finish_reason": "kanban_terminal_required"},
        later,
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    repair_message_sequence(SimpleNamespace(), messages)
    assistants = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["content"] == ""
    assert assistants[0]["tool_calls"][0]["function"]["name"] == "kanban_block"






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




