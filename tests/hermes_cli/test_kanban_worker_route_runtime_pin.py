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
    kanban_worker_route._live_state.update(agent=None, cursor=0)
    yield
    kanban_worker_route._card_pin_cache.clear()
    kanban_worker_route._live_state.update(agent=None, cursor=0)


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


def test_card_pinned_worker_refuses_same_provider_other_model(board):
    """t_1d2ba891: a model pin pins the MODEL; another model on the lane is refused."""
    from agent.error_classifier import FailoverReason

    tid, _ = board
    agent = _runtime_agent("openai-codex",
                           [{"provider": "openai-codex", "model": "gpt-6-astra"}])
    assert _failover(agent, FailoverReason.server_error) is False
    assert agent.model == "gpt-6-sol-900k"
    refused = _events(tid, "worker_route_pin_refused")
    assert refused[0][1]["to_provider"] == "openai-codex"
    assert refused[0][1]["to_model"] == "gpt-6-astra"
    assert _events(tid, "worker_route_substituted") == []


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


# --- t_6ea895ca: a POOL-face pin may fail over to the sibling pool, same model

def _pin_card(tid, provider, model):
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET provider_override=?, model_override=? WHERE id=?",
                     (provider, model, tid))
        conn.commit()


def _pool_agent(provider, chain, model="claude-opus-5-5"):
    agent = _runtime_agent(provider, chain)
    agent.model = model
    agent._primary_runtime["model"] = model
    return agent


def test_pool_pin_fails_over_to_sibling_pool_same_model(board):
    """claude-bpr pin -> claude-apr, identical model: allowed, recorded."""
    from agent.error_classifier import FailoverReason

    tid, run_id = board
    _pin_card(tid, "claude-bpr", "claude-opus-5-5")
    agent = _pool_agent("claude-bpr",
                        [{"provider": "claude-apr", "model": "claude-opus-5-5"}])
    assert _failover(agent, FailoverReason.overloaded) is True
    assert agent.provider == "claude-apr"
    assert _events(tid, "worker_route_pin_refused") == []
    sub = _events(tid, "worker_route_substituted")
    assert [r for r, _ in sub] == [run_id]
    assert sub[0][1]["stage"] == "runtime"
    assert sub[0][1]["from_provider"] == "claude-bpr"
    assert sub[0][1]["from_model"] == "claude-opus-5-5"
    assert sub[0][1]["to_provider"] == "claude-apr"
    assert sub[0][1]["to_model"] == "claude-opus-5-5"


def test_pool_pin_refuses_sibling_pool_with_a_different_model(board):
    from agent.error_classifier import FailoverReason

    tid, _ = board
    _pin_card(tid, "claude-bpr", "claude-opus-5-5")
    agent = _pool_agent("claude-bpr",
                        [{"provider": "claude-apr", "model": "claude-sonnet-5"}])
    assert _failover(agent, FailoverReason.overloaded) is False
    assert agent.provider == "claude-bpr"
    refused = _events(tid, "worker_route_pin_refused")
    assert refused[0][1]["to_provider"] == "claude-apr"
    assert _events(tid, "worker_route_substituted") == []


def test_pool_pin_refuses_non_pool_provider(board):
    """claude-bpr pin -> openai-codex: still refused."""
    from agent.error_classifier import FailoverReason

    tid, _ = board
    _pin_card(tid, "claude-bpr", "claude-opus-5-5")
    agent = _pool_agent("claude-bpr",
                        [{"provider": "openai-codex", "model": "claude-opus-5-5"}])
    assert _failover(agent, FailoverReason.overloaded) is False
    assert agent.provider == "claude-bpr"
    refused = _events(tid, "worker_route_pin_refused")
    assert refused[0][1]["provider"] == "claude-bpr"
    assert refused[0][1]["to_provider"] == "openai-codex"
    assert _events(tid, "worker_route_substituted") == []


@pytest.mark.parametrize("pin, target", [
    ("claude-apx-3", "claude-apr"),   # single sub -> its own pool
    ("claude-apx-3", "claude-bpr"),   # single sub -> the other pool
    ("claude-bpx-7", "claude-apr"),
])
def test_single_sub_pin_refuses_pool_failover(board, pin, target):
    """A pin to ONE sub is not a pool face: no pool substitution, same model or not."""
    from agent.error_classifier import FailoverReason

    tid, _ = board
    _pin_card(tid, pin, "claude-opus-5-5")
    agent = _pool_agent(pin, [{"provider": target, "model": "claude-opus-5-5"}])
    assert _failover(agent, FailoverReason.overloaded) is False
    assert agent.provider == pin
    refused = _events(tid, "worker_route_pin_refused")
    assert refused[0][1]["provider"] == pin
    assert refused[0][1]["to_provider"] == target
    assert _events(tid, "worker_route_substituted") == []


# --- t_1d2ba891: a card pin with a model pins the MODEL on the same provider

def test_fable_pin_refuses_opus_on_same_provider_and_requeues(board):
    """subs-ace:t_cc62f8dd: claude-bpr/claude-fable-5-1 pin -> claude-bpr/claude-opus-5-5
    failover (timeout/rate_limit/overloaded) must be refused, not substituted."""
    from agent.error_classifier import FailoverReason
    from hermes_cli.kanban_worker_exit import EXIT_CLASS_PINNED_PROVIDER, WorkerExit
    from hermes_cli.kanban_worker_route import apply_pin_refusal_to_result

    tid, run_id = board
    _pin_card(tid, "claude-bpr", "claude-fable-5-1")
    agent = _pool_agent("claude-bpr",
                        [{"provider": "claude-bpr", "model": "claude-opus-5-5"}],
                        model="claude-fable-5-1")
    assert _failover(agent, FailoverReason.overloaded) is False
    assert (agent.provider, agent.model) == ("claude-bpr", "claude-fable-5-1")
    assert _events(tid, "worker_route_substituted") == []
    refused = _events(tid, "worker_route_pin_refused")
    assert [r for r, _ in refused] == [run_id]
    payload = refused[0][1]
    assert (payload["stage"], payload["provider"], payload["model"]) == (
        "runtime", "claude-bpr", "claude-fable-5-1")
    assert (payload["to_provider"], payload["to_model"]) == ("claude-bpr", "claude-opus-5-5")

    # The three reasons seen on runs 293/300/308 all end as a requeue, never
    # a counted failure; a reason without its own class gets the pin class.
    for reason, error in (("timeout", "request timed out"),
                          ("rate_limit", "HTTP 429"),
                          ("overloaded", "HTTP 529 overloaded")):
        result = apply_pin_refusal_to_result(agent, {
            "failed": True, "failure_reason": reason,
            "failure_retryable": True, "error": error})
        exc = WorkerExit(result)
        assert exc.code == kb.KANBAN_RATE_LIMIT_EXIT_CODE, reason
        assert exc.exit_class is not None, reason
    timeout = apply_pin_refusal_to_result(agent, {
        "failed": True, "failure_reason": "timeout", "failure_retryable": True,
        "error": "request timed out"})
    assert timeout["failure_reason"] == "pinned_provider_unavailable"
    assert WorkerExit(timeout).exit_class == EXIT_CLASS_PINNED_PROVIDER


def test_fable_pin_allows_sibling_pool_same_model(board):
    from agent.error_classifier import FailoverReason

    tid, _ = board
    _pin_card(tid, "claude-bpr", "claude-fable-5-1")
    agent = _pool_agent("claude-bpr",
                        [{"provider": "claude-apr", "model": "claude-fable-5-1"}],
                        model="claude-fable-5-1")
    assert _failover(agent, FailoverReason.overloaded) is True
    assert (agent.provider, agent.model) == ("claude-apr", "claude-fable-5-1")
    assert _events(tid, "worker_route_pin_refused") == []


def test_fable_pin_refuses_sibling_pool_opus(board):
    from agent.error_classifier import FailoverReason

    tid, _ = board
    _pin_card(tid, "claude-bpr", "claude-fable-5-1")
    agent = _pool_agent("claude-bpr",
                        [{"provider": "claude-apr", "model": "claude-opus-5-5"}],
                        model="claude-fable-5-1")
    assert _failover(agent, FailoverReason.overloaded) is False
    assert agent.provider == "claude-bpr"


def test_no_pin_same_provider_model_failover_unchanged(board):
    from agent.error_classifier import FailoverReason

    tid, run_id = board
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET provider_override=NULL, model_override=NULL WHERE id=?",
                     (tid,))
        conn.commit()
    agent = _pool_agent("claude-bpr",
                        [{"provider": "claude-bpr", "model": "claude-opus-5-5"}],
                        model="claude-fable-5-1")
    assert _failover(agent, FailoverReason.overloaded) is True
    assert agent.model == "claude-opus-5-5"
    assert _events(tid, "worker_route_pin_refused") == []
    assert [r for r, _ in _events(tid, "worker_route_substituted")] == [run_id]


def test_unreadable_board_fails_open(board, monkeypatch):
    from agent.error_classifier import FailoverReason
    from hermes_cli import kanban_db

    tid, _ = board
    _pin_card(tid, "claude-bpr", "claude-fable-5-1")

    def _boom(*a, **k):
        raise RuntimeError("board unreadable")

    monkeypatch.setattr(kanban_db, "get_task", _boom)
    agent = _pool_agent("claude-bpr",
                        [{"provider": "claude-bpr", "model": "claude-opus-5-5"}],
                        model="claude-fable-5-1")
    assert _failover(agent, FailoverReason.overloaded) is True
    assert agent.model == "claude-opus-5-5"


def test_model_only_pin_refuses_another_model(board):
    """``set-model --model X`` without a provider still pins the model."""
    from agent.error_classifier import FailoverReason

    tid, run_id = board
    _pin_card(tid, None, "claude-fable-5-1")
    agent = _pool_agent("claude-bpr",
                        [{"provider": "claude-bpr", "model": "claude-opus-5-5"},
                         {"provider": "openai-codex", "model": "gpt-6-sol-900k"}],
                        model="claude-fable-5-1")
    assert _failover(agent, FailoverReason.overloaded) is False
    assert (agent.provider, agent.model) == ("claude-bpr", "claude-fable-5-1")
    refused = _events(tid, "worker_route_pin_refused")
    assert [r for r, _ in refused] == [run_id]
    assert refused[0][1]["provider"] == "claude-bpr"
    assert refused[0][1]["to_model"] == "claude-opus-5-5"
    assert _events(tid, "worker_route_substituted") == []


def test_model_only_pin_allows_same_model_elsewhere(board):
    from agent.error_classifier import FailoverReason

    tid, _ = board
    _pin_card(tid, None, "claude-fable-5-1")
    agent = _pool_agent("claude-bpr",
                        [{"provider": "claude-apx-1", "model": "claude-fable-5-1"}],
                        model="claude-fable-5-1")
    assert _failover(agent, FailoverReason.overloaded) is True
    assert (agent.provider, agent.model) == ("claude-apx-1", "claude-fable-5-1")
    assert _events(tid, "worker_route_pin_refused") == []


def test_next_dispatch_write_does_not_change_running_policy(board):
    """A non-live set-model after the run's first loop boundary applies to the
    NEXT dispatch only: the running worker keeps the pin it spawned on."""
    from agent.error_classifier import FailoverReason
    from hermes_cli.kanban_worker_route import apply_pending_live_route

    tid, _ = board
    _pin_card(tid, "claude-bpr", "claude-fable-5-1")
    agent = _pool_agent("claude-bpr",
                        [{"provider": "claude-bpr", "model": "claude-opus-5-5"}],
                        model="claude-fable-5-1")
    agent._delegate_depth = 0
    apply_pending_live_route(agent, iteration=0)  # first loop boundary
    _pin_card(tid, "claude-bpr", "claude-opus-5-5")  # next-dispatch write
    assert _failover(agent, FailoverReason.overloaded) is False
    assert agent.model == "claude-fable-5-1"
    assert _events(tid, "worker_route_substituted") == []


def test_live_pin_on_the_serving_route_refreshes_the_snapshot(board):
    """``set-model --live`` onto the route already serving switches nothing but
    still pins the run: a later failover to another model is refused."""
    from agent.error_classifier import FailoverReason
    from hermes_cli.kanban_worker_route import apply_pending_live_route

    tid, run_id = board
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET provider_override=NULL, model_override=NULL, "
                     "current_run_id=? WHERE id=?", (run_id, tid))
        conn.commit()
    agent = _runtime_agent("openai-codex",
                           [{"provider": "openai-codex", "model": "gpt-6-astra"}])
    agent._delegate_depth = 0
    apply_pending_live_route(agent, iteration=0)  # snapshot: no pin
    _pin_card(tid, "openai-codex", "gpt-6-sol-900k")
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, kb.ROUTE_CHANGED_EVENT, {
                "touch_model": True, "touch_effort": False,
                "model": "gpt-6-sol-900k", "provider": "openai-codex"}, run_id=run_id)
    apply_pending_live_route(agent, iteration=1)
    assert agent.model == "gpt-6-sol-900k"
    assert _failover(agent, FailoverReason.server_error) is False
    assert agent.model == "gpt-6-sol-900k"
