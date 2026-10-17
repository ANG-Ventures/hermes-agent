"""``providers.<name>.preserve_thinking`` must resolve on the route the agent actually runs.

A named ``providers:`` relay does not keep its config key in ``agent.provider``: the runtime
resolver returns ``provider="custom"`` with the key in ``requested_provider``, and session
restore / ``/model`` paths carry ``custom:<name>``. An exact ``providers.get(agent.provider)``
silently resolved the flag OFF on every one of those (Prism debcd100ad5f on #2036). These tests
go through the real config loader and the real runtime resolver, not a monkeypatched map.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.anthropic_adapter import _BINDING_CONTROLS_BETA

RELAY = "http://127.0.0.1:18810/anthropic"
_CONFIG = f"""\
model:
  provider: relay
  default: claude-fable-5-1
providers:
  relay:
    base_url: {RELAY}
    transport: anthropic_messages
    api_key: test
    preserve_thinking: true
    preserve_thinking_window: contiguous
"""


@pytest.fixture
def relay_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_CONFIG)
    return tmp_path


def _signed(text, sig):
    return {"type": "thinking", "thinking": text, "signature": sig}


def _conversation():
    return [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "done one", "reasoning_details": [_signed("plan one", "sig-1")]},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "done two", "reasoning_details": [_signed("plan two", "sig-2")]},
        {"role": "user", "content": "third"},
    ]


def _resolved_agent():
    from run_agent import AIAgent
    from hermes_cli.runtime_provider import resolve_runtime_provider

    rt = resolve_runtime_provider(requested="relay")
    assert rt["provider"] == "custom" and rt["requested_provider"] == "relay"  # the premise of the bug
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key=rt["api_key"], base_url=rt["base_url"], provider=rt["provider"],
            requested_provider=rt["requested_provider"], model="claude-fable-5-1",
            api_mode=rt["api_mode"], quiet_mode=True, skip_context_files=True, skip_memory=True,
            reasoning_config={"effort": "medium"},
        )
    return agent


def test_resolved_named_relay_sends_signed_thinking_and_binding_controls(relay_home):
    agent = _resolved_agent()
    kw = agent._build_api_kwargs(_conversation())
    sigs = [b.get("signature") for m in kw["messages"] if m["role"] == "assistant" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "thinking"]
    assert sigs == ["sig-1", "sig-2"], "flag + contiguous window must survive provider='custom'"
    assert kw["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert _BINDING_CONTROLS_BETA in kw["extra_headers"]["anthropic-beta"].split(",")


@pytest.mark.parametrize("identity", ["relay", "custom:relay", "RELAY"])
def test_every_identity_of_the_entry_resolves(relay_home, identity):
    from agent.preserved_thinking import preserve_thinking_enabled, preserve_thinking_window

    assert preserve_thinking_enabled(identity) is True
    assert preserve_thinking_window(identity) == "contiguous"


def test_bare_custom_resolves_only_through_the_unique_base_url(relay_home):
    from agent.preserved_thinking import preserve_thinking_enabled

    assert preserve_thinking_enabled("custom", base_url=RELAY + "/") is True
    assert preserve_thinking_enabled("custom") is False
    assert preserve_thinking_enabled("custom", base_url="http://127.0.0.1:9999/anthropic") is False
    # A named built-in on the same URL is never matched by URL: only bare ``custom`` falls back.
    assert preserve_thinking_enabled("anthropic", base_url=RELAY) is False


def test_two_entries_on_one_url_is_ambiguous_and_off(relay_home):
    from agent.preserved_thinking import preserve_thinking_enabled

    (relay_home / "config.yaml").write_text(_CONFIG + f"  relay2:\n    base_url: {RELAY}\n    transport: anthropic_messages\n")
    assert preserve_thinking_enabled("custom", base_url=RELAY) is False
    assert preserve_thinking_enabled("custom", "relay", RELAY) is True


def test_compressor_on_resolved_named_relay_strips_tail_thinking(relay_home):
    agent = _resolved_agent()
    cc = agent.context_compressor
    assert cc.provider == "custom"
    tail = cc._assemble_tail(_conversation(), compress_end=2, tail_start=2, summary_indices=set())
    assert [m.get("reasoning_details") for m in tail if m["role"] == "assistant"] == [[]]


def test_flag_keys_do_not_warn_as_unknown(relay_home, caplog, monkeypatch):
    import hermes_cli.config_providers as cp
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.setattr(cp, "_PROVIDER_NORMALIZE_WARNED", set())  # warn-once dedup is process-global
    with caplog.at_level("WARNING"):
        resolve_runtime_provider(requested="relay")
    assert not [r for r in caplog.records if "unknown config keys" in r.getMessage()]
