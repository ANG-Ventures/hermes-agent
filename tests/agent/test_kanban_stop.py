"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)
from hermes_cli import kanban_db as kb


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch






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






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




