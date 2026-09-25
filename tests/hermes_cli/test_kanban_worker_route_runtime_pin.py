"""A CARD-pinned kanban worker never fails over to another provider mid-turn.

t_ed0289e3: #1055 refused the AUTH-time fallback for a ``--provider``-pinned
worker, but the runtime failover in ``try_activate_fallback`` only recorded
the swap, so a card pinned to ``openai-codex`` to escape a bridge fault still
ran on ``claude-bpr`` after the first codex error. Lane overrides and
capped-pool dispatch rungs also spawn ``--provider`` but are never persisted
to the card, so they keep runtime fallback.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.delenv("HERMES_KANBAN_OWNER_PID", raising=False)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="pinned", assignee="a",
                             model_override="gpt-6-sol-900k",
                             provider_override="openai-codex")
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) "
                     "VALUES (?, 'a', 'running', ?)", (tid, int(time.time())))
        run_id = conn.execute("SELECT max(id) FROM task_runs").fetchone()[0]
        conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid, run_id


def _events(tid, kind):
    with kb.connect_closing() as conn:
        return [(r[0], json.loads(r[1])) for r in conn.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (tid, kind))]



@pytest.fixture(autouse=True)
def _fresh_card_pin_cache():
    from hermes_cli import kanban_worker_route

    kanban_worker_route._card_pin_cache.clear()
    yield
    kanban_worker_route._card_pin_cache.clear()


def _runtime_agent(provider, chain):
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.run_agent.test_fallback_reasoning_override import _make_reasoning_agent

    agent = _make_reasoning_agent()
    agent.provider = provider
    agent.model = "gpt-6-sol-900k"
    agent._primary_runtime["provider"] = provider
    agent._primary_runtime["model"] = "gpt-6-sol-900k"
    agent._fallback_chain = chain
    agent._rate_limit_backoff_count = 0
    # Real recursion so a skipped entry advances the chain (MagicMock would
    # return a truthy mock and fake a successful swap).
    agent._try_activate_fallback = (
        lambda *a, **k: try_activate_fallback(agent, *a, **k))
    return agent


def _failover(agent, reason):
    from types import SimpleNamespace
    from unittest.mock import patch

    from agent.chat_completion_helpers import try_activate_fallback

    client = SimpleNamespace(api_key="fb-key", base_url="https://api.anthropic.com",
                             _custom_headers={})
    with patch("agent.auxiliary_client.resolve_provider_client",
               return_value=(client, "claude-opus-5-5")), \
            patch("agent.credential_pool.load_pool", return_value=None), \
            patch("hermes_cli.config.load_config",
                  return_value={"model": {"default": "gpt-6-sol-900k",
                                          "provider": "openai-codex"}}):
        return try_activate_fallback(agent, reason)


def test_card_pinned_worker_refuses_midturn_failover_and_requeues(board):
    """Pinned worker + mid-turn provider error => no swap, pin_refused, requeue."""
    from agent.error_classifier import FailoverReason
    from hermes_cli.kanban_worker_exit import EXIT_CLASS_PINNED_PROVIDER, WorkerExit
    from hermes_cli.kanban_worker_route import apply_pin_refusal_to_result

    tid, run_id = board
    agent = _runtime_agent("openai-codex",
                           [{"provider": "claude-bpr", "model": "claude-opus-5-5"}])

    assert _failover(agent, FailoverReason.server_error) is False
    assert agent.provider == "openai-codex"  # never swapped onto the bridge
    assert _events(tid, "worker_route_substituted") == []
    refused = _events(tid, "worker_route_pin_refused")
    assert [r for r, _ in refused] == [run_id]
    assert refused[0][1]["stage"] == "runtime"
    assert refused[0][1]["provider"] == "openai-codex"
    assert refused[0][1]["to_provider"] == "claude-bpr"
    assert refused[0][1]["rate_limited"] is False

    # A second failover in the same run does not spam the board.
    agent._fallback_index = 0
    assert _failover(agent, FailoverReason.server_error) is False
    assert len(_events(tid, "worker_route_pin_refused")) == 1

    # The turn then fails on the pinned provider: exit retry-preserving.
    result = apply_pin_refusal_to_result(agent, {
        "failed": True, "failure_reason": "server_error",
        "failure_retryable": True, "error": "HTTP 500 from codex",
    })
    exc = WorkerExit(result)
    assert exc.code == kb.KANBAN_RATE_LIMIT_EXIT_CODE
    assert exc.exit_class == EXIT_CLASS_PINNED_PROVIDER


def test_card_pinned_worker_keeps_quota_reason_and_nonretryable_failures(board):
    from agent.error_classifier import FailoverReason
    from hermes_cli.kanban_worker_exit import EXIT_CLASS_QUOTA, WorkerExit
    from hermes_cli.kanban_worker_route import apply_pin_refusal_to_result

    tid, _ = board
    agent = _runtime_agent("openai-codex",
                           [{"provider": "claude-bpr", "model": "claude-opus-5-5"}])
    assert _failover(agent, FailoverReason.rate_limit) is False
    assert _events(tid, "worker_route_pin_refused")[0][1]["rate_limited"] is True

    quota = apply_pin_refusal_to_result(agent, {
        "failed": True, "failure_reason": "rate_limit", "failure_retryable": True})
    assert quota["failure_reason"] == "rate_limit"
    assert WorkerExit(quota).exit_class == EXIT_CLASS_QUOTA

    # A deterministic failure is still a task failure (no infinite requeue).
    broken = apply_pin_refusal_to_result(agent, {
        "failed": True, "failure_reason": "format_error", "failure_retryable": False})
    assert broken["failure_reason"] == "format_error"
    assert WorkerExit(broken).code == 1


def test_card_pinned_worker_allows_same_provider_fallback(board):
    from agent.error_classifier import FailoverReason

    tid, _ = board
    agent = _runtime_agent("openai-codex", [
        {"provider": "claude-bpr", "model": "claude-opus-5-5"},
        {"provider": "openai-codex", "model": "gpt-6-astra"},
    ])
    assert _failover(agent, FailoverReason.server_error) is True
    assert agent.provider == "openai-codex"
    assert agent.model == "gpt-6-astra"


def test_lane_override_is_not_a_card_pin(board, monkeypatch):
    """A board-wide lane override spawns ``--provider`` on every worker but is
    never persisted to the card, so runtime fallback still works."""
    from agent.error_classifier import FailoverReason

    _, _ = board
    with kb.connect_closing() as conn:
        lane_tid = kb.create_task(conn, title="lane-routed", assignee="a")
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) "
                     "VALUES (?, 'a', 'running', ?)", (lane_tid, int(time.time())))
        lane_run = conn.execute("SELECT max(id) FROM task_runs").fetchone()[0]
        conn.commit()
        # The dispatcher's lane route mutates only the in-memory claim.
        task = kb.get_task(conn, lane_tid)
        lane = kb.LaneModelOverride(
            assignee="a", provider="openai-codex", model="gpt-6-sol-900k",
            created_at=int(time.time()), expires_at=int(time.time()) + 600)
        assert kb.apply_lane_model_override(task, lane) is not None
        assert task.provider_override == "openai-codex"
        assert kb.get_task(conn, lane_tid).provider_override is None
    monkeypatch.setenv("HERMES_KANBAN_TASK", lane_tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(lane_run))

    agent = _runtime_agent("openai-codex",
                           [{"provider": "claude-bpr", "model": "claude-opus-5-5"}])
    assert _failover(agent, FailoverReason.server_error) is True
    assert agent.provider == "claude-bpr"
    assert _events(lane_tid, "worker_route_pin_refused") == []
    sub = _events(lane_tid, "worker_route_substituted")
    assert [r for r, _ in sub] == [lane_run]


def test_dispatch_fallback_rung_off_the_pin_keeps_runtime_fallback(board):
    """Card pins codex but the capped-pool rung spawned it on another lane:
    the run is not on its pin, so it is not refused a failover."""
    from agent.error_classifier import FailoverReason

    tid, _ = board
    agent = _runtime_agent("claude-bpr",
                           [{"provider": "claude-apx-1", "model": "claude-opus-5-5"}])
    assert _failover(agent, FailoverReason.server_error) is True
    assert _events(tid, "worker_route_pin_refused") == []
