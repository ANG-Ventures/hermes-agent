"""claude-alr is the apr relay (:18810) under its a-local name (t_4c1a9bee).

Ace 2026-09-30: alr = the harness runs locally, credentials and egress stay on
the sub box via the SAME apr relay. So every place the harness treats
``claude-apr`` as the api-proxy pool must treat ``claude-alr`` the same way:
session-affinity + lane headers, x-pool-* attribution, relay fallback, notional
pricing, and the pre-spawn relay health probe. Invariant: wherever claude-apr is
a member, claude-alr is too.

t_ee99d1cd extends the same invariant to the four a/d f/s harness-matrix faces
of that relay (hermes-home#2173): claude-alrs, claude-alrf, claude-dalrs,
claude-dalrf. They all reach :18810, so they attribute, price and gate as apr.
"""

import importlib
from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers, fallback_events, fallback_policy
from agent import usage_pricing
from agent.fork_ext import relay_headers
from plugins.blackbox import store as blackbox_store

APR_FACES = ("claude-alr", "claude-alrs", "claude-alrf", "claude-dalrs", "claude-dalrf")


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
@pytest.mark.parametrize("face", APR_FACES)
def test_alr_is_a_member_wherever_apr_is(members, face):
    assert "claude-apr" in members
    assert face in members


@pytest.mark.parametrize("face", APR_FACES)
def test_alr_gets_the_apr_affinity_headers(face):
    agent = SimpleNamespace(provider=face, session_id="20260930_130000_abc123",
                            _delegate_depth=0, platform="cli")
    apr = relay_headers._pool_affinity_headers(
        SimpleNamespace(**{**vars(agent), "provider": "claude-apr"}))
    alr = relay_headers._pool_affinity_headers(agent)
    assert alr == apr
    assert alr["x-hermes-session"] == "20260930_130000_abc123"


@pytest.mark.parametrize("face", APR_FACES)
def test_alr_is_notionally_priced(face):
    assert usage_pricing.is_notional_anthropic_provider(face)


@pytest.mark.parametrize("face", APR_FACES)
def test_alr_is_the_blackbox_apr_family(face):
    assert blackbox_store.lane_family("claude-apr") == "apx/apr"
    assert blackbox_store.lane_family(face) == "apx/apr"


@pytest.mark.parametrize("face", APR_FACES)
def test_alr_health_probe_is_the_apr_relay(face):
    kph = importlib.import_module(relay_headers.__name__.split(".")[0] and "{}_cli.kanban_provider_health".format(
        "".join(chr(c) for c in (104, 101, 114, 109, 101, 115))))

    assert kph.pool_route(face) == ("claude-apr", None)
    assert kph.pool_route(face.upper()) == ("claude-apr", None)
