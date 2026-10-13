"""Virtual-clock recovery wiring: a remote quota is not the relay credential's quota."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.agent.test_relay_error_class import RelayError
from tests.run_agent.test_pool_capacity_503_retry import _FakeClock, _fallback_client, _make_agent, _response


def quota_error(status=503, retry_after="1783", cls="quota_seat"):
    headers = {"retry-after": retry_after}
    if cls is not None:
        headers["x-relay-error-class"] = cls
    return RelayError(status, {"error": "no eligible sub for the requested model"}, headers)


def make_agent(statuses, *, fallback=True, provider="claude-alr"):
    # No SDK/network probing during construction; only the wire/client is synthetic.
    with patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()), \
         patch("agent.context_compressor.get_model_context_length", return_value=200000), \
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
    clock = _FakeClock(sleeps)

    def wire(*args, **kwargs):
        attempts.append((agent.provider, agent.model))
        if len(attempts) == 1:
            raise error
        return _response("served", agent.model)

    agent._disable_streaming = True
    with (
        patch("agent.chat_completion_helpers.should_use_direct_api_call", return_value=True),
        patch.object(agent, "_interruptible_api_call", side_effect=wire) as call,
        patch.object(agent, "_try_activate_fallback", wraps=agent._try_activate_fallback) as activate,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.auxiliary_client.resolve_provider_client",
              side_effect=lambda *args, **kw: (_fallback_client(), kw.get("model", agent.model))),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch("hermes_cli.config.read_raw_config", return_value={"model": {"announce_route_change": True}}),
        patch("agent.retry_utils.jittered_backoff", return_value=2.0),
        patch("agent.turn_recovery.time", clock),
    ):
        result = agent.run_conversation("hello")
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


@pytest.mark.parametrize("cls", ["quota_model", "quota_seat"])
@pytest.mark.parametrize("provider", ["claude-alr", "claude-dtlr", "claude-btpr", "claude-bpr"])
def test_native_next_lane_and_same_model(provider, cls):
    agent = make_agent([], provider=provider)
    chain = ["claude-alr", "claude-dtlr", "claude-btpr", "claude-bpr", "cpa"]
    next_lane = chain[chain.index(provider) + 1]
    agent._fallback_chain = agent._fallback_chain[chain.index(provider):]
    chosen_model = agent.model
    sleeps = []
    result, _, call, attempts = run(agent, quota_error(cls=cls), sleeps)
    assert result["completed"] is True
    assert call.call_count == 2
    assert attempts == [(provider, chosen_model), (next_lane, chosen_model)]
    assert sum(sleeps) == 0


@pytest.mark.parametrize("relay_class", [None, "upstream_passthrough"])
def test_passthrough_weekly_rejection_is_structured_quota(relay_class):
    error = RelayError(429, {"error": {"type": "rate_limit_error", "message": "usage limit"}},
                       {"retry-after": "570984", "anthropic-ratelimit-unified-7d_oi-status": "rejected"})
    if relay_class:
        error.response.headers["x-relay-error-class"] = relay_class
    agent = make_agent([])
    sleeps = []
    result, _, call, attempts = run(agent, error, sleeps)
    assert result["completed"] is True
    assert call.call_count == 2
    assert [p for p, _ in attempts] == ["claude-alr", "claude-dtlr"]
    assert sum(sleeps) == 0


@pytest.mark.parametrize("provider,error,wait", [
    ("anthropic", RelayError(429, {"error": "rate limit"}, {"retry-after": "1783"}), 600),
    ("anthropic", quota_error(), 600),  # relay header on a nonrelay is not our authority
    ("claude-alr", RelayError(503, {"error": "temporarily overloaded"}, {"retry-after": "15"}), 15),
    ("claude-alr", RelayError(503, {"error": "draining-for-deploy"},
                              {"x-relay-error-class": "quota_seat", "retry-after": "15"}), 15),
    ("claude-alr", quota_error(retry_after="30"), 30),
    ("claude-alr", quota_error(retry_after="171"), 171),
    ("claude-alr", quota_error(retry_after="600"), 600),
    ("claude-alr", RelayError(429, {"error": "quota exhausted"}, {"retry-after": "1783"}), 600),
    ("claude-alr", RelayError(429, {"error": "rate limit"},
                              {"retry-after": "1783", "anthropic-ratelimit-unified-overage-status": "rejected"}), 600),
])
def test_recoverable_or_unproven_errors_keep_existing_wait(provider, error, wait):
    agent = make_agent([], provider=provider)
    sleeps = []
    result, activate, call, attempts = run(agent, error, sleeps)
    assert result["completed"] is True
    assert call.call_count == 2
    activate.assert_not_called()
    assert [p for p, _ in attempts] == [provider, provider]
    assert sum(sleeps) == pytest.approx(wait, abs=0.21)


@pytest.mark.parametrize("cls", ["conn", "pool_pressure", "rate_upstream", "unknown"])
def test_relay_class_precedes_quota_header_or_text(cls):
    from agent.error_classifier import classify_api_error
    from agent.fork_ext.relay_quota import relay_quota_outlasts_retry_window
    error = quota_error(cls=cls)
    error.response.headers["anthropic-ratelimit-unified-7d-status"] = "rejected"
    assert not relay_quota_outlasts_retry_window(
        SimpleNamespace(provider="claude-alr"), error, classify_api_error(error, provider="claude-alr"))


@pytest.mark.parametrize("hint", [None, "", "soon", "nan", "0", "-1"])
def test_unusable_hint_is_not_a_known_long_recovery(hint):
    from agent.error_classifier import classify_api_error
    from agent.fork_ext.relay_quota import relay_quota_outlasts_retry_window
    error = quota_error(retry_after=hint)
    assert not relay_quota_outlasts_retry_window(
        SimpleNamespace(provider="claude-alr"), error, classify_api_error(error, provider="claude-alr"))


@pytest.mark.parametrize("status", [503, 429])
def test_single_relay_key_uses_next_lane_without_sleep(status):
    agent = make_agent([])
    agent._credential_pool.entries.return_value = agent._credential_pool.entries.return_value[:1]
    sleeps = []
    result, _, call, attempts = run(agent, quota_error(status), sleeps)
    assert result["completed"] is True
    assert call.call_count == 2
    assert [p for p, _ in attempts] == ["claude-alr", "claude-dtlr"]
    assert sum(sleeps) == 0


def test_unusable_configured_fallback_fails_loud_without_retry():
    agent = make_agent([])
    sleeps = []
    # Real resolver failure: chain exists but none of its clients can be built.
    with patch("agent.chat_completion_helpers._fallback_api_mode_hint", side_effect=RuntimeError("unavailable")):
        result, _, call, attempts = run(agent, quota_error(), sleeps)
    assert result["failed"] is True
    assert result["final_response"]
    assert call.call_count == 1
    assert sum(sleeps) == 0


def test_long_quota_stays_exhausted_when_capacity_policy_widens_ceiling():
    agent = make_agent([])
    agent._capacity_retry_attempts = 10
    agent._capacity_retry_max_wait_s = 3600
    sleeps = []
    result, _, call, attempts = run(agent, quota_error(cls="quota_model"), sleeps)
    assert result["completed"] is True
    assert [p for p, _ in attempts] == ["claude-alr", "claude-dtlr"]
    assert call.call_count == 2
    assert sum(sleeps) == 0


@pytest.mark.parametrize("body", [
    {"relay_error_class": "quota_seat", "error": {"message": "quota"}},
    {"error": {"relay_error_class": "quota_seat", "message": "quota"}},
])
def test_structured_stream_class_with_retry_header_uses_same_handoff(body):
    agent = make_agent([])
    sleeps = []
    result, _, call, attempts = run(agent, RelayError(200, body, {"retry-after": "1783"}), sleeps)
    assert result["completed"] is True
    assert [p for p, _ in attempts] == ["claude-alr", "claude-dtlr"]
    assert call.call_count == 2
    assert sum(sleeps) == 0
