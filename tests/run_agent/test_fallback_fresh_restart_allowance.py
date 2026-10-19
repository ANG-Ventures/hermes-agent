"""A provider fallback starts with a fresh rebuilt-restart allowance (t_5e3d611f).

2026-10-18 22:12 chiron: the primary model was capped on every relay, the fallback chain
walked past it, and ``Fallback activated: claude-fable-5-1 -> claude-opus-5-5`` was logged.
The turn then ended with ``rebuilt_restart_limit_exceeded`` (api_calls=1, response_len=0):
the opus fallback was never called. The per-turn restart cap (``max_retries``, 3) was shared
by every fallback hop, so the hops that failed before opus used it up.

The cap exists to stop a restart flag that keeps re-arming WITHOUT moving anywhere. A hop that
moved the turn to a new route is progress through a finite chain, so it gets its own allowance.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.run_agent.test_pool_capacity_503_retry import (
    PoolCapacity503,
    _make_agent,
    _response,
    _run,
)
from agent.turn_iteration_prep import apply_retry_restarts
from agent.turn_retry_state import TurnRetryState

MAX_RETRIES = 3


def _chain(n: int) -> list[dict]:
    """``n`` distinct fallback routes (distinct providers/models so none is skipped as the
    same backend as the current one)."""
    return [
        {"provider": f"fb{i}", "model": f"fallback/model-{i}", "base_url": f"https://fb{i}.example/v1"}
        for i in range(n)
    ]


def _capped_chain_agent(hops: int):
    statuses: list[tuple[str, str]] = []
    agent = _make_agent(statuses)
    agent._capacity_retry_attempts = 1
    agent._capacity_retry_max_wait_s = 0.0
    agent._fallback_chain = _chain(hops)
    agent._fallback_index = 0
    return agent


@pytest.mark.parametrize("capped_calls", [MAX_RETRIES + 1, 2 * MAX_RETRIES + 1])
def test_fallback_after_more_capped_hops_than_max_retries_answers(capped_calls):
    """More capped attempts than MAX_RETRIES, spread over distinct routes (the primary, the
    fallbacks, and the sticky policy's one mid-turn return to the primary); the next call
    works. The turn must be answered, not end rebuilt_restart_limit_exceeded."""
    agent = _capped_chain_agent(2 * MAX_RETRIES + 2)
    assert agent._api_max_retries == MAX_RETRIES

    capped = PoolCapacity503(retry_after="600")  # longer than any capacity budget: leave now
    outcomes = [capped] * capped_calls + [_response("nonce-ok")]
    sleeps: list[float] = []
    result, activate, call = _run(agent, outcomes, sleeps)

    assert result.get("final_response") == "nonce-ok", result.get("turn_exit_reason") or result
    assert result["completed"] is True
    assert call.call_count == capped_calls + 1  # every capped attempt, then the answer
    assert activate.call_count >= capped_calls - 1
    assert agent._fallback_activated is True


def _restart_actions(routes_cycle: list[tuple[str, str, str]]) -> list[str]:
    budget = SimpleNamespace(refunds=0)
    budget.refund = lambda: setattr(budget, "refunds", budget.refunds + 1)
    agent = SimpleNamespace(iteration_budget=budget)
    restart_routes: set = set()
    restart_count, actions = 0, []
    while len(actions) < 50 and (not actions or actions[-1] != "break"):
        agent.provider, agent.model, agent.base_url = routes_cycle[len(actions) % len(routes_cycle)]
        _retry = TurnRetryState()
        _retry.restart_with_rebuilt_messages = True
        v = apply_retry_restarts(
            agent, _retry=_retry, response=None, interrupted=False, messages=[],
            conversation_history=[], user_message="hi", api_kwargs={}, current_turn_user_idx=0,
            final_response=None, retry_count=0, max_retries=MAX_RETRIES, api_call_count=1,
            restart_count=restart_count, length_continue_retries=0,
            _preflight_compression_blocked=True, _turn_exit_reason="unknown",
            restart_routes=restart_routes,
        )
        actions.append(v.action)
        restart_count = v.restart_count
    return actions


def test_restart_that_does_not_move_the_route_stays_bounded():
    """The runaway backstop survives: re-arming the rebuilt restart while the route never
    changes still ends the turn after MAX_RETRIES refunds."""
    assert _restart_actions([("p", "m", "u")]) == ["continue"] * MAX_RETRIES + ["break"]


def test_ping_pong_between_two_routes_stays_bounded():
    """primary <-> fallback flapping: each route's first visit is fresh, every repeat counts,
    so the turn still ends instead of refunding forever."""
    actions = _restart_actions([("a", "m", "u"), ("b", "m", "u")])
    assert actions[-1] == "break"
    assert len(actions) <= 2 + MAX_RETRIES
