"""Per-provider ``preserve_thinking`` flag (preserved thinking on the fleet's own Anthropic relays).

``_manage_thinking_signatures`` strips every signed thinking block for any endpoint whose base_url
is not anthropic.com. A relay in front of Anthropic (``127.0.0.1:18810``) validates the same
signatures, so the strip only throws reasoning away. ``providers.<name>.preserve_thinking: true``
treats that provider as first-party for the strip decision ONLY, and sends the binding controls
(``thinking.block_binding.prefix_mismatch_behavior: drop_block`` + its beta) so a prefix edit costs
a dropped block, never a 400. Every other endpoint keeps today's behaviour.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import agent.preserved_thinking as pt
from agent.anthropic_adapter import _BINDING_CONTROLS_BETA, build_anthropic_kwargs
from agent.anthropic_message_convert import convert_messages_to_anthropic

RELAY = "http://127.0.0.1:18810/anthropic"
DIRECT = "https://api.anthropic.com"


def _signed(text: str, sig: str) -> dict:
    return {"type": "thinking", "thinking": text, "signature": sig}


def _assistant(text: str, sig: str, reply: str) -> dict:
    return {"role": "assistant", "content": reply, "reasoning_details": [_signed(text, sig)]}


def _conversation() -> list:
    return [
        {"role": "user", "content": "first"},
        _assistant("plan one", "sig-1", "done one"),
        {"role": "user", "content": "second"},
        _assistant("plan two", "sig-2", "done two"),
    ]


def _thinking_sigs(messages: list) -> list:
    return [
        b.get("signature")
        for m in messages
        if m["role"] == "assistant" and isinstance(m["content"], list)
        for b in m["content"]
        if b.get("type") == "thinking"
    ]


@pytest.fixture
def providers_cfg(monkeypatch):
    """Point the flag resolver at an in-memory ``providers:`` map."""
    def _set(providers: dict):
        monkeypatch.setattr(pt, "_load_providers_config", lambda: providers)
    return _set


# ── the strip decision ────────────────────────────────────────────────────────────────────────────

def test_unflagged_relay_strips_every_signed_block():
    _, out = convert_messages_to_anthropic(_conversation(), base_url=RELAY, model="claude-fable-5-1")
    assert _thinking_sigs(out) == []


def test_flagged_relay_keeps_latest_turn_signed_block():
    _, out = convert_messages_to_anthropic(
        _conversation(), base_url=RELAY, model="claude-fable-5-1", preserve_thinking=True,
    )
    assert _thinking_sigs(out) == ["sig-2"]


def test_direct_anthropic_unchanged_by_flag():
    _, plain = convert_messages_to_anthropic(_conversation(), base_url=DIRECT, model="claude-fable-5-1")
    _, flagged = convert_messages_to_anthropic(
        _conversation(), base_url=DIRECT, model="claude-fable-5-1", preserve_thinking=True,
    )
    assert plain == flagged
    assert _thinking_sigs(plain) == ["sig-2"]


def test_contiguous_window_keeps_signed_blocks_on_every_turn():
    _, out = convert_messages_to_anthropic(
        _conversation(), base_url=RELAY, model="claude-fable-5-1",
        preserve_thinking=True, thinking_window="contiguous",
    )
    assert _thinking_sigs(out) == ["sig-1", "sig-2"]


def test_contiguous_window_drops_unsigned_blocks_on_older_turns():
    """An older turn's UNSIGNED thinking has nothing the API can bind: drop it (today's bytes),
    never demote it to text, so the contiguous window changes nothing but the signed blocks."""
    conv = _conversation()
    conv[1]["reasoning_details"] = [{"type": "thinking", "thinking": "unsigned"}]
    _, out = convert_messages_to_anthropic(
        conv, base_url=RELAY, model="claude-fable-5-1", preserve_thinking=True, thinking_window="contiguous",
    )
    assert out[1]["content"] == [{"type": "text", "text": "done one"}]
    assert _thinking_sigs(out) == ["sig-2"]


def test_other_third_party_endpoint_still_strips_when_flag_off():
    _, out = convert_messages_to_anthropic(
        _conversation(), base_url="https://api.minimax.io/anthropic", model="MiniMax-M2",
    )
    assert _thinking_sigs(out) == []


# ── flag resolution ───────────────────────────────────────────────────────────────────────────────

def test_flag_resolves_from_providers_section(providers_cfg):
    providers_cfg({"claude-alr": {"preserve_thinking": True}})
    assert pt.preserve_thinking_enabled("claude-alr") is True
    assert pt.preserve_thinking_enabled("claude-apr") is False
    assert pt.preserve_thinking_enabled(None) is False


def test_flag_requires_a_real_boolean(providers_cfg):
    providers_cfg({"claude-alr": {"preserve_thinking": "yes"}})
    assert pt.preserve_thinking_enabled("claude-alr") is False


def test_window_defaults_to_latest_and_needs_the_flag(providers_cfg):
    providers_cfg({"a": {"preserve_thinking": True}, "b": {"preserve_thinking": True, "preserve_thinking_window": "contiguous"},
                   "c": {"preserve_thinking_window": "contiguous"}, "d": {"preserve_thinking": True, "preserve_thinking_window": "bogus"}})
    assert pt.preserve_thinking_window("a") == "latest"
    assert pt.preserve_thinking_window("b") == "contiguous"
    assert pt.preserve_thinking_window("c") == "latest"
    assert pt.preserve_thinking_window("d") == "latest"


def test_config_read_failure_is_flag_off(monkeypatch):
    def _boom():
        raise RuntimeError("no config")
    monkeypatch.setattr(pt, "_load_providers_config", _boom)
    assert pt.preserve_thinking_enabled("claude-alr") is False


# ── binding controls on the wire ──────────────────────────────────────────────────────────────────

def _kwargs(**over):
    base = dict(
        model="claude-fable-5-1", messages=_conversation(), tools=None, max_tokens=1024,
        reasoning_config={"effort": "medium"}, base_url=RELAY,
    )
    base.update(over)
    return build_anthropic_kwargs(**base)


def test_drop_block_binding_and_beta_present_iff_flag_on():
    on = _kwargs(preserve_thinking=True)
    assert on["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert _BINDING_CONTROLS_BETA in on["extra_headers"]["anthropic-beta"].split(",")
    assert "interleaved-thinking-2025-05-14" in on["extra_headers"]["anthropic-beta"].split(",")

    off = _kwargs()
    assert "block_binding" not in off["thinking"]
    assert _BINDING_CONTROLS_BETA not in (off.get("extra_headers") or {}).get("anthropic-beta", "")


def test_binding_controls_never_sent_without_adaptive_thinking():
    disabled = _kwargs(preserve_thinking=True, reasoning_config={"enabled": False})
    assert "block_binding" not in disabled.get("thinking", {})
    assert _BINDING_CONTROLS_BETA not in (disabled.get("extra_headers") or {}).get("anthropic-beta", "")
    haiku = _kwargs(preserve_thinking=True, model="claude-haiku-5-5")
    assert "thinking" not in haiku
    assert _BINDING_CONTROLS_BETA not in (haiku.get("extra_headers") or {}).get("anthropic-beta", "")


def test_binding_beta_composes_with_fast_mode_header():
    kw = _kwargs(preserve_thinking=True, base_url=DIRECT, model="claude-opus-5-5", fast_mode=True)
    betas = kw["extra_headers"]["anthropic-beta"].split(",")
    assert _BINDING_CONTROLS_BETA in betas and "fast-mode-2026-02-01" in betas
    assert kw["extra_body"]["speed"] == "fast"


def test_binding_mode_is_never_error():
    assert pt.PREFIX_MISMATCH_BEHAVIOR == "drop_block"


# ── compaction boundary ───────────────────────────────────────────────────────────────────────────

def test_boundary_strip_removes_thinking_from_kept_row_without_mutating_original():
    row = {
        "role": "assistant", "content": "x",
        "reasoning_details": [_signed("t", "sig"), {"type": "other", "x": 1}],
        "anthropic_content_blocks": [_signed("t", "sig"), {"type": "text", "text": "x"}],
    }
    original = {k: (list(v) if isinstance(v, list) else v) for k, v in row.items()}
    out = pt.strip_thinking_at_boundary(dict(row))
    assert out["reasoning_details"] == [{"type": "other", "x": 1}]
    assert out["anthropic_content_blocks"] == [{"type": "text", "text": "x"}]
    assert row == original


def test_boundary_strip_leaves_rows_without_thinking_alone():
    row = {"role": "user", "content": "hi"}
    assert pt.strip_thinking_at_boundary(dict(row)) == row


def test_compaction_tail_strips_thinking_when_provider_flagged(providers_cfg):
    from agent.context_compressor import ContextCompressor

    providers_cfg({"claude-alr": {"preserve_thinking": True}})
    cc = ContextCompressor(provider="claude-alr", model="claude-fable-5-1", quiet_mode=True)
    messages = _conversation()
    tail = cc._assemble_tail(messages, compress_end=2, tail_start=2, summary_indices=set())
    assert [m.get("reasoning_details") for m in tail if m["role"] == "assistant"] == [[]]
    # The live transcript still carries the block: only the compaction copy lost it.
    assert messages[3]["reasoning_details"] == [_signed("plan two", "sig-2")]


def test_compaction_tail_keeps_thinking_when_provider_unflagged(providers_cfg):
    from agent.context_compressor import ContextCompressor

    providers_cfg({})
    cc = ContextCompressor(provider="anthropic", model="claude-fable-5-1", quiet_mode=True)
    tail = cc._assemble_tail(_conversation(), compress_end=2, tail_start=2, summary_indices=set())
    assert [m.get("reasoning_details") for m in tail if m["role"] == "assistant"] == [[_signed("plan two", "sig-2")]]


# ── input_transformations logging ─────────────────────────────────────────────────────────────────

def test_input_transformations_logged_at_info_with_replayed_count(caplog):
    api_kwargs = {"messages": convert_messages_to_anthropic(
        _conversation(), base_url=RELAY, preserve_thinking=True, thinking_window="contiguous")[1]}
    message = SimpleNamespace(input_transformations=[
        {"type": "thinking_dropped", "reason": "prefix_binding_mismatch", "path": "messages.1.content.0"},
    ])
    with caplog.at_level(logging.INFO, logger=pt.logger.name):
        summary = pt.log_input_transformations(message, api_kwargs, log_prefix="[t] ")
    assert summary == {"replayed_thinking_blocks": 2, "dropped": 1, "reasons": {"prefix_binding_mismatch": 1}}
    record = next(r for r in caplog.records if "input_transformations" in r.getMessage())
    assert record.levelno == logging.INFO
    assert "replayed=2" in record.getMessage() and "prefix_binding_mismatch" in record.getMessage()


def test_input_transformations_absent_is_silent(caplog):
    with caplog.at_level(logging.DEBUG, logger=pt.logger.name):
        assert pt.log_input_transformations(SimpleNamespace(), {"messages": []}, log_prefix="") is None
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]
