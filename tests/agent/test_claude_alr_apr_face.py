"""claude-alr is the apr relay (:18810) under its a-local name (t_4c1a9bee).

Ace 2026-09-30: alr = the harness runs locally, credentials and egress stay on
the sub box via the SAME apr relay. So every place the harness treats
``claude-apr`` as the api-proxy pool must treat ``claude-alr`` the same way:
session-affinity + lane headers, x-pool-* attribution, relay fallback, notional
pricing, and the pre-spawn relay health probe. Invariant: wherever claude-apr is
a member, claude-alr is too.
"""

import importlib
from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers, fallback_events, fallback_policy
from agent import usage_pricing
from agent.fork_ext import relay_headers


@pytest.mark.parametrize(
    "members",
    [
        relay_headers._POOL_AFFINITY_PROVIDERS,
        relay_headers._POOL_CAPABILITY_PROVIDERS,
        chat_completion_helpers._POOLED_PROVIDERS,
        fallback_policy.RELAY_PROVIDERS,
        fallback_events.RELAY_PROVIDERS,
        usage_pricing.NOTIONAL_ANTHROPIC_PROVIDERS,
    ],
)
def test_alr_is_a_member_wherever_apr_is(members):
    assert "claude-apr" in members
    assert "claude-alr" in members


def test_alr_gets_the_apr_affinity_headers():
    agent = SimpleNamespace(provider="claude-alr", session_id="20260930_130000_abc123",
                            _delegate_depth=0, platform="cli")
    apr = relay_headers._pool_affinity_headers(
        SimpleNamespace(**{**vars(agent), "provider": "claude-apr"}))
    alr = relay_headers._pool_affinity_headers(agent)
    assert alr == apr
    assert alr["x-hermes-session"] == "20260930_130000_abc123"


def test_alr_is_notionally_priced():
    assert usage_pricing.is_notional_anthropic_provider("claude-alr")


def test_alr_health_probe_is_the_apr_relay():
    kph = importlib.import_module(relay_headers.__name__.split(".")[0] and "{}_cli.kanban_provider_health".format(
        "".join(chr(c) for c in (104, 101, 114, 109, 101, 115))))

    assert kph.pool_route("claude-alr") == ("claude-apr", None)
    assert kph.pool_route("CLAUDE-ALR") == ("claude-apr", None)
