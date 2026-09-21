"""Execute physical-call serializers and callable display consumers.

Thin snapshot renderer pins are unit contracts. Producer flag propagation is
covered by stacked PR #797, not claimed by constructing CanonicalUsage here.
"""
from types import SimpleNamespace

import pytest

from agent.usage_pricing import (
    USAGE_UNKNOWN_FIELDS, normalize_usage, prompt_tokens_unknown,
    cache_stats_line, verbose_token_usage_log_args,
)

from gateway.slash_commands import render_thin_last_turn_lines
WIRES = [
    {"prompt_tokens": None, "completion_tokens": 50, "total_tokens": None,
     "prompt_tokens_unavailable": True, "unavailable": True},
    {"prompt_tokens": None, "completion_tokens": 50, "total_tokens": None,
     "prompt_tokens_unavailable": True, "total_tokens_unavailable": True,
     "unavailable": True,
     "prompt_tokens_details": {"cached_tokens": 100, "cache_creation_tokens": None}},
    {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
     "unavailable": True},
    {"prompt_tokens": 150, "completion_tokens": None, "total_tokens": None,
     "output_tokens_unavailable": True},
    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
     "prompt_tokens_details": {"cached_tokens": 0, "cache_creation_tokens": 0}},
]


@pytest.mark.parametrize("wire", WIRES)
def test_moa_physical_calls_preserve_unknown_into_blackbox(wire):
    from agent.conversation_loop import _build_moa_pricing_calls
    from plugins.blackbox.cost import compute_turn_cost
    usage = normalize_usage(wire)
    calls = _build_moa_pricing_calls([], usage, aggregator_model="claude-sonnet-4-5",
                                    aggregator_provider="anthropic", aggregator_base_url=None)
    for key in USAGE_UNKNOWN_FIELDS:
        assert calls[0].get(key, False) == getattr(usage, key)
    cost, status, _ = compute_turn_cost("default", "moa", None, [{"pricing_calls": calls}])
    if usage.total_tokens_unknown:
        assert cost is None
        assert status == "unknown"
    else:
        assert cost == 0
        assert status == "priced_zero"


@pytest.mark.parametrize("wire", WIRES + [
    {"prompt_tokens": 100, "completion_tokens": 50,
     "prompt_tokens_details": {"cached_tokens": 100, "cache_creation_tokens": 0}},
])
def test_console_cache_block_honors_unknown(wire):
    usage = normalize_usage(wire)
    text = cache_stats_line(usage, usage.prompt_tokens)
    if prompt_tokens_unknown(usage):
        assert "unknown" in text
        assert "% hit" not in text
        assert "0 written" not in text
    elif usage.cache_read_tokens:
        assert "100/100 tokens (100% hit, 0 written)" in text
    else:
        assert text is None


@pytest.mark.parametrize("wire", WIRES)
def test_real_moa_advisor_execution(wire, monkeypatch, tmp_path):
    from agent.moa_loop import MoAClient
    from plugins.blackbox.cost import compute_turn_cost

    (tmp_path / "config.yaml").write_text(
        "moa:\n  default_preset: default\n  presets:\n    default:\n"
        "      enabled: true\n      reference_models:\n"
        "        - provider: anthropic\n          model: claude-sonnet-4-5\n"
        "      aggregator:\n        provider: anthropic\n        model: claude-sonnet-4-5\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("agent.moa_loop.call_llm", lambda **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="advice", tool_calls=[]),
                                 finish_reason="stop")], usage=wire, model="claude-sonnet-4-5"))
    client = MoAClient("default")
    client.chat.completions.create(model="default", messages=[{"role": "user", "content": "test"}])
    calls = client.consume_reference_pricing_calls()
    assert len(calls) == 1
    usage = normalize_usage(wire)
    for key in USAGE_UNKNOWN_FIELDS:
        assert calls[0].get(key, False) == getattr(usage, key)
    cost, status, _ = compute_turn_cost("default", "moa", None, [{"pricing_calls": calls}])
    assert (cost is None) == normalize_usage(wire).total_tokens_unknown
    assert status == ("unknown" if cost is None else "priced_zero")


@pytest.mark.parametrize("wire", WIRES)
def test_verbose_log_honors_unknown(wire):
    usage = normalize_usage(wire)
    prompt, completion, total = verbose_token_usage_log_args(
        usage, usage.prompt_tokens, usage.output_tokens, usage.total_tokens)
    if prompt_tokens_unknown(usage):
        assert prompt == "unknown"
    if usage.output_tokens_unknown:
        assert completion == "unknown"
    if usage.total_tokens_unknown:
        assert total == "unknown"


@pytest.mark.parametrize("flag", USAGE_UNKNOWN_FIELDS)
def test_thin_fallback_renderer_honors_unknown_flag(flag):
    # Renderer-only contract; #797 owns the real producer -> snapshot path.
    snap = {"input_tokens": 150, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "reasoning_tokens": 0, flag: True}
    text = "\n".join(render_thin_last_turn_lines(snap, "test"))
    if flag in ("input_tokens_unknown", "cache_read_tokens_unknown",
                "cache_write_tokens_unknown", "usage_unknown"):
        assert "Tokens in: unknown" in text
    if flag in ("output_tokens_unknown", "usage_unknown"):
        assert "Tokens out: unknown" in text
    assert "Total (billed in+out): unknown" in text


def test_verbose_log_measured_values_keep_comma_formatting():
    """MEASURED control for the sibling site: the verbose token log formatted
    measured counts with commas before the UNKNOWN routing, and must still."""
    usage = normalize_usage({"prompt_tokens": 120_000, "completion_tokens": 8_000,
                             "total_tokens": 128_000}, api_mode="chat_completions")
    assert not prompt_tokens_unknown(usage) and not usage.total_tokens_unknown
    args = verbose_token_usage_log_args(
        usage, usage.prompt_tokens, usage.output_tokens, usage.total_tokens)
    text = "Token usage: prompt=%s, completion=%s, total=%s" % args

    assert "prompt=120,000" in text      # not "120k"
    assert "completion=8,000" in text
    assert "total=128,000" in text
    assert "unknown" not in text


def test_thin_fallback_measured_values_keep_comma_formatting():
    """MEASURED control: routing the unknown case through the shared rule must
    not change how a measured count renders on this card. The card's vocabulary
    is comma-grouped (``241,500``), not magnitude-abbreviated (``241.5k``)."""
    snap = {"input_tokens": 120_000, "output_tokens": 8_000, "cache_read_tokens": 110_000,
            "cache_write_tokens": 2_000, "reasoning_tokens": 1_500}

    text = "\n".join(render_thin_last_turn_lines(snap, "test"))

    assert "232,000" in text                              # in billed, comma-grouped
    assert "9,500" in text                                # out billed (8,000 + 1,500 reasoning)
    assert "Total (billed in+out): 241,500" in text       # not "241.5k"
    assert "unknown" not in text                          # nothing measured reads as unknown


def test_thin_fallback_honors_aggregate_only_unknown_discriminator():
    snap = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens_unknown": True,
    }
    text = "\n".join(render_thin_last_turn_lines(snap, "test"))
    assert "Tokens in: 100 billed" in text
    assert "Tokens out: 20 billed" in text
    assert "Total (billed in+out): unknown" in text


@pytest.mark.parametrize("used", [0, 12_345])
def test_last_turn_context_window_never_formats_unknown_prompt_as_numeric(used):
    from plugins.blackbox.last_turn import render_last_turn_record

    text = "\n".join(
        render_last_turn_record(
            {
                "input_tokens": 0,
                "output_tokens": 40,
                "input_tokens_unknown": True,
                "context_used": used,
                "context_length": 200_000,
            }
        )
    )
    assert "Context window (last call): unknown/200k" in text
    assert "% of model max" not in text
