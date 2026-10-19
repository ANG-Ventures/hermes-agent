"""Boomerang v2 P1: a successful MAIN-turn call on a pool lane records the seat that
served it (``agent.last_served_by`` / ``last_served_at``), read from the
``pool_headers`` stamp at ``_record_successful_api_call`` (spec D-3, P0.1)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers as cch


def _agent(provider: str):
    return SimpleNamespace(
        _current_turn_id="turn-1", provider=provider, model="claude-test",
        api_mode="anthropic_messages", is_subagent=False,
    )


def _response(**pool_headers):
    usage = SimpleNamespace(input_tokens=10, output_tokens=2,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(usage=usage, pool_headers=dict(pool_headers))


@pytest.fixture(autouse=True)
def _no_blackbox(monkeypatch):
    monkeypatch.setattr("plugins.blackbox.record_api_call", lambda **row: None)


def test_main_turn_captures_served_by():
    agent = _agent("claude-alr")
    cch._record_successful_api_call(agent, _response(**{"x-pool-served-by": "sub-vps-16"}))
    assert agent.last_served_by == "sub-vps-16"
    first_at = agent.last_served_at
    assert isinstance(first_at, float) and first_at > 0

    # The latest served call wins: a failover to another seat moves the pin target.
    cch._record_successful_api_call(agent, _response(**{"x-pool-served-by": "sub-vps-2"}))
    assert agent.last_served_by == "sub-vps-2"
    assert agent.last_served_at >= first_at


@pytest.mark.parametrize("provider", ["claude-bpr", "openrouter", "anthropic"])
def test_non_pool_provider_leaves_fields_untouched(provider):
    agent = _agent(provider)
    cch._record_successful_api_call(agent, _response(**{"x-pool-served-by": "sub-vps-16"}))
    assert not hasattr(agent, "last_served_by")
    assert not hasattr(agent, "last_served_at")


@pytest.mark.parametrize("headers", [{}, {"x-pool-served-by": "bad seat/with spaces"}])
def test_absent_or_invalid_seat_keeps_last_known(headers):
    agent = _agent("claude-apr")
    agent.last_served_by, agent.last_served_at = "sub-vps-7", 123.0
    cch._record_successful_api_call(agent, _response(**headers))
    assert (agent.last_served_by, agent.last_served_at) == ("sub-vps-7", 123.0)


def test_fork_child_logs_served_by_and_read_share_once(caplog):
    """The observable a fork turn leaves (README §5, smoke step 3): ONE line, first response only."""
    agent = _agent("claude-alr")
    agent._boomerang_fork_log_pending = True
    agent.session_id = "child-1"
    resp = SimpleNamespace(usage=SimpleNamespace(input_tokens=4, output_tokens=2, cache_read_input_tokens=990,
                                                 cache_creation_input_tokens=6),
                           pool_headers={"x-pool-served-by": "sub-vps-13"})
    with caplog.at_level("INFO", logger="tools.delegate_tool"):
        cch._record_successful_api_call(agent, resp)
        cch._record_successful_api_call(agent, resp)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("boomerang.fork ")]
    assert lines == ["boomerang.fork served_by=sub-vps-13 read_share=0.9900 prefix_tokens=1000 child_session=child-1"]


def test_non_fork_agent_never_logs_the_fork_line(caplog):
    agent = _agent("claude-alr")
    with caplog.at_level("INFO", logger="tools.delegate_tool"):
        cch._record_successful_api_call(agent, _response(**{"x-pool-served-by": "sub-vps-13"}))
    assert not [r for r in caplog.records if r.getMessage().startswith("boomerang.fork ")]
