"""Virtual-clock recovery wiring: a remote quota is not the relay credential's quota."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.agent.test_relay_error_class import RelayError
from tests.run_agent.test_pool_capacity_503_retry import _make_agent, _response, _run


def quota_error(status=503, retry_after="1783", cls="quota_seat"):
    headers = {"retry-after": retry_after}
    if cls is not None:
        headers["x-relay-error-class"] = cls
    return RelayError(status, {"error": "no eligible sub for the requested model"}, headers)


def make_agent(statuses, *, fallback=True, provider="claude-alr"):
    # No SDK/network probing during construction; only the wire/client is synthetic.
    with patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()), \
         patch("agent.model_metadata.get_model_context_length", return_value=200000):
        agent = _make_agent(statuses)
    agent.provider = provider
    agent._primary_runtime["provider"] = provider
    # A relay key says nothing about its remote seats. Two available local keys
    # deliberately reproduce the eager-fallback deferral instead of mocking it.
    pool = MagicMock()
    pool.provider = provider
    pool.has_available.return_value = True
    pool.entries.return_value = [SimpleNamespace(id="one", runtime_api_key="primary-key", last_status=None),
                                SimpleNamespace(id="two", runtime_api_key="other-key", last_status=None)]
    pool.current.return_value = pool.entries.return_value[0]
    pool.mark_exhausted_and_rotate.return_value = None
    agent._credential_pool = pool
    agent._fallback_chain = ([{"provider": lane, "model": agent.model,
                               "base_url": f"https://{lane}.example/v1"}
                              for lane in ("claude-dtlr", "claude-btpr", "claude-bpr", "cpa")]
                             if fallback else [])
    return agent


def run(agent, error, sleeps):
    attempts = []
    # The rig's API-call mock is wrapped solely to record the actual live route.
    original = agent._invoke_api_request_error_hook
    def failed(**kwargs):
        attempts.append((agent.provider, agent.model))
        return original(**kwargs)
    with patch.object(agent, "_invoke_api_request_error_hook", side_effect=failed):
        # _run supplies the synthetic transport/client; fallback selection is real.
        result, activate, call = _run(agent, [error, _response("served")], sleeps)
    if result.get("completed"):
        attempts.append((agent.provider, agent.model))
    return result, activate, call, attempts


@pytest.mark.parametrize("status", [503, 429])
@pytest.mark.parametrize("fallback", [True, False])
def test_long_quota_never_retries_primary(status, fallback):
    statuses = []
    agent = make_agent(statuses, fallback=fallback)
    sleeps = []
    pool = agent._credential_pool
    result, activate, call, attempts = run(agent, quota_error(status), sleeps)
    assert sum(sleeps) == 0
    if fallback:
        assert result["completed"] is True
        assert activate.called
        assert call.call_count == 2
        assert [p for p, _ in attempts] == ["claude-alr", "claude-dtlr"]
        assert any(kind == "lifecycle" for kind, _ in statuses)
    else:
        assert call.call_count == 1
        assert result["failed"] is True
        assert result["final_response"]
    pool.mark_exhausted_and_rotate.assert_not_called()
