"""t_f40dc54a: providers that own their transcript never receive the
harness-authored interrupt-close row; every other provider still does.

Live incident (2026-09-25, sub-vps-1, bpx): after a gateway restart the
harness closed the interrupted turn with an assistant row the resident CLI
session never wrote. The bridge's continuation gate saw an assistant reply
after the last tool call that was not on disk ("final-reply-produced-elsewhere")
and re-sent the whole 828k history into a fresh session -- one full
prompt-cache rewrite per restart per session.
"""
from dataclasses import replace

import pytest

from agent.message_sanitization import (
    close_interrupted_tool_sequence,
    is_interrupt_close_row,
    provider_owns_transcript,
)
from providers.base import ProviderProfile


def _history():
    msgs = [
        {"role": "user", "content": "run the migration"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function",
                            "function": {"name": "shell", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "halfway"},
    ]
    assert close_interrupted_tool_sequence(msgs) is True
    msgs.append({"role": "user", "content": "status?"})
    return msgs


def test_row_detection():
    msgs = _history()
    assert is_interrupt_close_row(msgs[3])
    assert not is_interrupt_close_row({"role": "assistant", "content": "Operation interrupted."})
    assert not is_interrupt_close_row({"role": "user", "finish_reason": "interrupt_close"})
    assert is_interrupt_close_row({"role": "assistant", "content": "x", "finish_reason": "interrupt_close"})


def test_default_profile_does_not_own_transcript():
    assert ProviderProfile(name="x").owns_transcript is False


@pytest.fixture
def registry(monkeypatch):
    import providers

    profiles = {
        "bridge-lane": ProviderProfile(name="bridge-lane", owns_transcript=True),
        "native-lane": ProviderProfile(name="native-lane", api_mode="anthropic_messages"),
    }
    monkeypatch.setattr(providers, "get_provider_profile", lambda n: profiles.get(n))
    return profiles


def test_provider_owns_transcript_lookup(registry):
    assert provider_owns_transcript("bridge-lane") is True
    assert provider_owns_transcript("native-lane") is False
    assert provider_owns_transcript("unknown") is False
    assert provider_owns_transcript(None) is False


def test_lookup_fails_open(monkeypatch):
    import providers

    def boom(_):
        raise RuntimeError("registry down")

    monkeypatch.setattr(providers, "get_provider_profile", boom)
    assert provider_owns_transcript("bridge-lane") is False


def _wire(provider, msgs):
    """Mirror of the conversation_loop filter (same two helpers)."""
    omit = provider_owns_transcript(provider)
    return [m for m in msgs if not (omit and is_interrupt_close_row(m))]


def test_bridge_lane_omits_close_row_native_keeps_it(registry):
    msgs = _history()
    bridge = _wire("bridge-lane", msgs)
    native = _wire("native-lane", msgs)
    assert [m["role"] for m in bridge] == ["user", "assistant", "tool", "user"]
    assert not any(is_interrupt_close_row(m) for m in bridge)
    assert [m["role"] for m in native] == ["user", "assistant", "tool", "assistant", "user"]
    # persisted history untouched
    assert is_interrupt_close_row(msgs[3])


def test_conversation_loop_wires_the_filter():
    """Contract: the send-path loop consults both helpers before cloning rows."""
    import inspect

    from agent import conversation_loop

    src = inspect.getsource(conversation_loop)
    assert "provider_owns_transcript(agent.provider)" in src
    assert "is_interrupt_close_row(msg)" in src
