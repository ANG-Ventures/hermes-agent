"""t_814ad411: the harness picks a pool lane's wire with the pools row ``transport``
(dual-wire spec 2026-10-20 §3.1/§3.2/§6, Ace amendment 02:01: no variant names).

  DW-1  an Anthropic-wire request carries the profile's per-request extras: a stateful
        bridge row keys on ``metadata.user_id`` and keeps x-hermes-session + the mode
        header (the Anthropic client never sees the profile's default_headers).
  DW-2  a profile that declares ``replays_reasoning_details`` keeps signed
        reasoning_details on an OpenAI-wire request to a loopback relay; every other
        route still strips them (the #70233 guard is unchanged).
  DW-3  alr/apr in OpenAI mode keep the apr relay's affinity + lane headers; other
        chat_completions providers get none.
  DW-4  a fallback chain that mixes transports rebuilds the client per entry and parses
        each attempt in its own wire.
"""

from __future__ import annotations

import types

import pytest

from agent import chat_completion_helpers as cch
from agent.transports import get_transport


SID = "20261020_030000_abcdef12"


class _Profile:
    """Stand-in for the plugin's SessionRoutingProfile on an anthropic_messages row."""

    default_headers = {"x-hermes-bpx-mode": "tui"}

    def build_anthropic_kwargs_extras(self, **ctx):
        user = f"hermes-sess:{ctx['session_id']}"
        return {"metadata": {"user_id": user},
                "extra_headers": {**self.default_headers, "x-hermes-session": ctx["session_id"]}}


class _Transport:
    def build_kwargs(self, **kw):
        return {"model": kw["model"], "messages": kw["messages"], "max_tokens": kw["max_tokens"]}


def _anthropic_agent(provider):
    return types.SimpleNamespace(
        provider=provider, requested_provider=provider, model="claude-haiku-5-5",
        session_id=SID, _delegate_depth=0, platform="cli", base_url="http://127.0.0.1:18811/anthropic",
        _anthropic_base_url="http://127.0.0.1:18811/anthropic", context_compressor=None,
        _ephemeral_max_output_tokens=None, max_tokens=64, _is_anthropic_oauth=False,
        _oauth_1m_beta_disabled=False, _first_request_extra_headers=None,
        _get_transport=lambda: _Transport(),
        _prepare_anthropic_messages_for_api=lambda m: m,
        _anthropic_preserve_dots=lambda: False,
    )


def test_dw1_anthropic_wire_request_carries_session_metadata_and_mode(monkeypatch):
    import providers
    monkeypatch.setattr(providers, "get_provider_profile", lambda name: _Profile())
    kw = cch._build_anthropic_kwargs(_anthropic_agent("claude-btpr"),
                                     [{"role": "user", "content": "hi"}], None, None, {})
    assert kw["metadata"] == {"user_id": f"hermes-sess:{SID}"}
    eh = kw["extra_headers"]
    assert eh["x-hermes-session"] == SID
    assert eh["x-hermes-bpx-mode"] == "tui"


def test_dw1_existing_headers_win_and_default_hook_adds_nothing(monkeypatch):
    import providers
    from providers.base import ProviderProfile

    class P(_Profile):
        def build_anthropic_kwargs_extras(self, **ctx):
            return {"extra_headers": {"x-hermes-session": "profile-loses"}}

    monkeypatch.setattr(providers, "get_provider_profile", lambda name: P())
    kw = cch._build_anthropic_kwargs(_anthropic_agent("claude-alr"),
                                     [{"role": "user", "content": "hi"}], None, None, {})
    assert kw["extra_headers"]["x-hermes-session"] == SID      # affinity header wins

    monkeypatch.setattr(providers, "get_provider_profile",
                        lambda name: ProviderProfile(name="x"))
    kw = cch._build_anthropic_kwargs(_anthropic_agent("anthropic"),
                                     [{"role": "user", "content": "hi"}], None, None, {})
    assert "metadata" not in kw and "extra_headers" not in kw


_SIGNED = [{"type": "reasoning.text", "text": "t", "signature": "sig", "format": "anthropic-claude-v1"}]
_HIST = [{"role": "user", "content": "q"},
         {"role": "assistant", "content": None, "reasoning_details": _SIGNED,
          "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
         {"role": "tool", "tool_call_id": "c1", "content": "r"}]


@pytest.mark.parametrize("replays,kept", [(True, True), (False, False)])
def test_dw2_replays_flag_keeps_signed_reasoning_on_a_loopback_relay(replays, kept):
    prof = types.SimpleNamespace(native_reasoning_details_type=None, replays_reasoning_details=replays)
    out = get_transport("chat_completions").convert_messages(
        _HIST, model="claude-haiku-5-5", base_url="http://127.0.0.1:18810/v1", provider_profile=prof)
    assert ("reasoning_details" in out[1]) is kept
    if kept:
        assert out[1]["reasoning_details"] == _SIGNED


def test_dw3_alr_openai_mode_keeps_pool_affinity_headers():
    a = types.SimpleNamespace(provider="claude-alr", session_id=SID, _delegate_depth=0, platform="cli")
    kw = cch._merge_pool_affinity_headers(a, {"extra_headers": {"x-hermes-accepts": "error-class-v2"}})
    assert kw["extra_headers"]["x-hermes-session"] == SID
    assert kw["extra_headers"]["x-hermes-lane"]
    assert kw["extra_headers"]["x-hermes-accepts"] == "error-class-v2"
    b = types.SimpleNamespace(provider="claude-bpr", session_id=SID, _delegate_depth=0, platform="cli")
    assert cch._merge_pool_affinity_headers(b, {}) == {}


def test_dw4_fallback_chain_mixing_transports_parses_each_attempt_in_its_own_wire(tmp_path, monkeypatch):
    """Spec §6: a chain entry's wire is its row's transport (the row's path names it, the
    lane table refuses a disagreeing pair). Each hop rebuilds the client and the next
    attempt is parsed by that wire's transport."""
    import agent.auxiliary_client as ac
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_route_change_sink_e2e import _fake_agent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  announce_route_change: false\n")
    ac.clear_runtime_main()

    urls = {"claude-dtlr": "http://127.0.0.1:18816/v1",
            "claude-alr": "http://127.0.0.1:18810/anthropic",
            "claude-bpr": "http://127.0.0.1:18811/v1"}
    monkeypatch.setattr(ac, "resolve_provider_client", lambda provider, model, **kw: (
        types.SimpleNamespace(api_key="k", base_url=urls[provider], _custom_headers=None,
                              default_headers=None), model), raising=False)
    monkeypatch.setattr(cch, "get_model_context_length", lambda *a, **k: 200_000, raising=False)
    built = []
    monkeypatch.setattr("agent.anthropic_adapter.build_anthropic_client",
                        lambda key, base_url=None, **kw: built.append(base_url) or object())
    ac.set_runtime_main("claude-dtlr", "claude-haiku-5-5", base_url=urls["claude-dtlr"],
                        api_key="k", api_mode="chat_completions")
    a = _fake_agent(model="claude-haiku-5-5", provider="claude-dtlr", base_url=urls["claude-dtlr"],
                    api_mode="chat_completions")
    a._fallback_chain = [{"provider": "claude-alr", "model": "claude-haiku-5-5"},
                         {"provider": "claude-bpr", "model": "claude-haiku-5-5"}]
    a.fallback_model = list(a._fallback_chain)
    a._provider_model_requires_responses_api = lambda *x, **k: False   # a relay, not OpenAI

    anthropic_resp = types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text="from-alr")], stop_reason="end_turn")
    openai_resp = types.SimpleNamespace(choices=[types.SimpleNamespace(
        finish_reason="stop", message=types.SimpleNamespace(content="from-bpr", tool_calls=None))],
        usage=None)

    assert try_activate_fallback(a) is True
    assert (a.provider, a.api_mode) == ("claude-alr", "anthropic_messages")
    assert a._anthropic_base_url == urls["claude-alr"] and built == [urls["claude-alr"]]
    assert get_transport(a.api_mode).normalize_response(anthropic_resp).content == "from-alr"

    assert try_activate_fallback(a) is True
    assert (a.provider, a.api_mode) == ("claude-bpr", "chat_completions")
    assert get_transport(a.api_mode).normalize_response(openai_resp).content == "from-bpr"
