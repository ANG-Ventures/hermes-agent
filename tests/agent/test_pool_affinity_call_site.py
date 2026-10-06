"""Relay-pool affinity + lane headers on the BUILT request (t_67fc3115, row 7 of
docs/sync/fork-call-site-coverage.md).

``tests/agent/test_pool_affinity_header.py`` and the relay-lane canary call the fork
helper ``_pool_affinity_headers`` directly. A parity merge that drops or re-wires the
call in ``agent/chat_completion_helpers.py::_build_anthropic_kwargs`` keeps both green.
This file drives the real call site (``AIAgent._build_api_kwargs`` on the
``anthropic_messages`` route) and gives each header two tests that go red on exactly its
own regression:

1. ``test_wiring_<header>``: the helper is called at the call site with the LIVE agent
   and a main-turn ``aux_task`` (None), and the header VALUE on the built
   ``extra_headers`` equals what the fork rule derives from that agent.
2. ``test_wire_<header>``: varying only that header's input changes the built header the
   way the fork specifies; with the helper's output dropped at the call site the header
   is absent (the upstream shape).

Plus one pool-scope test: a non-pool provider on the same route carries none of them.
No test here reads source text.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import agent.chat_completion_helpers as cch
from run_agent import AIAgent

_SESSION = "x-hermes-session"
_LANE = "x-hermes-lane"
_LANE_SRC = "x-hermes-lane-src"
_ALL = (_SESSION, _LANE, _LANE_SRC)
_MSGS = [{"role": "user", "content": "hi"}]
_WHERE = "call site = agent/chat_completion_helpers.py::_build_anthropic_kwargs"


@pytest.fixture(autouse=True)
def _no_session_platform_env(monkeypatch):
    # The lane rule falls back to this env var when agent.platform is empty; pin it
    # absent so every derivation below comes from the agent alone.
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)


def _agent(provider="claude-apr", session_id="20261004_120000_aaaaaa", platform="discord",
           delegate_depth=0):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="k", base_url="http://127.0.0.1:18810/anthropic", provider=provider,
            model="claude-test", api_mode="anthropic_messages", quiet_mode=True,
            skip_context_files=True, skip_memory=True, session_id=session_id,
        )
    agent.api_mode = "anthropic_messages"
    agent.provider = provider
    agent.session_id = session_id
    agent.platform = platform
    agent._delegate_depth = delegate_depth
    return agent


def _headers(agent) -> dict:
    kw = agent._build_api_kwargs(list(_MSGS))
    return dict(kw.get("extra_headers") or {})


def _spy(monkeypatch):
    """Wrap the helper at the binding the call site reads; record (agent, args, kwargs, out)."""
    calls = []
    real = cch._pool_affinity_headers

    def spy(agent, *args, **kwargs):
        out = real(agent, *args, **kwargs)
        calls.append((agent, args, kwargs, dict(out)))
        return out

    monkeypatch.setattr(cch, "_pool_affinity_headers", spy)
    return calls


def _drop_at_call_site(monkeypatch):
    monkeypatch.setattr(cch, "_pool_affinity_headers", lambda agent, *a, **k: {})


def _assert_main_turn_call(calls, agent):
    assert len(calls) == 1, f"{_WHERE}: expected one _pool_affinity_headers call, got {len(calls)}"
    got_agent, args, kwargs, _out = calls[0]
    assert got_agent is agent, f"{_WHERE}: helper must read the LIVE agent"
    aux = args[0] if args else kwargs.get("aux_task")
    assert aux is None, f"{_WHERE}: a main turn passes aux_task=None, got {aux!r}"


# ── x-hermes-session ────────────────────────────────────────────────────────


def test_wiring_x_hermes_session(monkeypatch):
    agent = _agent(session_id="20261004_120000_wiring")
    calls = _spy(monkeypatch)
    h = _headers(agent)
    _assert_main_turn_call(calls, agent)
    # Fork rule: the header carries the live session id verbatim.
    assert h.get(_SESSION) == "20261004_120000_wiring", f"{_WHERE}: {_SESSION} = {h.get(_SESSION)!r}"


def test_wire_x_hermes_session(monkeypatch):
    agent = _agent(session_id="parent_sid")
    assert _headers(agent).get(_SESSION) == "parent_sid"
    agent.session_id = "child_sid_after_compaction"  # only input varied
    assert _headers(agent).get(_SESSION) == "child_sid_after_compaction", (
        f"{_WHERE}: {_SESSION} must follow the live session id per request")
    _drop_at_call_site(monkeypatch)
    assert _SESSION not in _headers(agent), f"{_WHERE}: dropped at call site -> upstream shape"


# ── x-hermes-lane ───────────────────────────────────────────────────────────


def test_wiring_x_hermes_lane(monkeypatch):
    agent = _agent(platform="discord", delegate_depth=0)
    calls = _spy(monkeypatch)
    h = _headers(agent)
    _assert_main_turn_call(calls, agent)
    # Fork rule: top-level main turn on a live messaging platform -> interactive.
    assert h.get(_LANE) == "interactive", f"{_WHERE}: {_LANE} = {h.get(_LANE)!r}"


def test_wire_x_hermes_lane(monkeypatch):
    agent = _agent(platform="discord", delegate_depth=0)
    assert _headers(agent).get(_LANE) == "interactive"
    agent._delegate_depth = 1  # only input varied: a subagent turn
    assert _headers(agent).get(_LANE) == "background", f"{_WHERE}: subagent turn must be background"
    agent._delegate_depth = 0
    agent.platform = "cron"  # only input varied: a scheduled principal
    assert _headers(agent).get(_LANE) == "background", f"{_WHERE}: cron turn must be background"
    agent.platform = "discord"
    _drop_at_call_site(monkeypatch)
    assert _LANE not in _headers(agent), f"{_WHERE}: dropped at call site -> upstream shape"


# ── x-hermes-lane-src ───────────────────────────────────────────────────────


def test_wiring_x_hermes_lane_src(monkeypatch):
    agent = _agent(platform="telegram", delegate_depth=0)
    calls = _spy(monkeypatch)
    h = _headers(agent)
    _assert_main_turn_call(calls, agent)
    # Fork rule: the classifier inputs, main turn -> aux_task "-".
    assert h.get(_LANE_SRC) == "platform=telegram;delegate_depth=0;aux_task=-", (
        f"{_WHERE}: {_LANE_SRC} = {h.get(_LANE_SRC)!r}")


def test_wire_x_hermes_lane_src(monkeypatch):
    agent = _agent(platform="telegram", delegate_depth=0)
    assert _headers(agent).get(_LANE_SRC) == "platform=telegram;delegate_depth=0;aux_task=-"
    agent._delegate_depth = 2  # only input varied
    assert _headers(agent).get(_LANE_SRC) == "platform=telegram;delegate_depth=2;aux_task=-", (
        f"{_WHERE}: {_LANE_SRC} must report the live delegate depth")
    _drop_at_call_site(monkeypatch)
    assert _LANE_SRC not in _headers(agent), f"{_WHERE}: dropped at call site -> upstream shape"


# ── pool scope ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("provider", ["anthropic", "claude-apx-2", "claude-bpx-3"])
def test_wire_non_pool_provider_carries_none(provider):
    h = _headers(_agent(provider=provider))
    leaked = [k for k in _ALL if k in h]
    assert not leaked, f"{_WHERE}: non-pool provider {provider!r} carried {leaked}"
