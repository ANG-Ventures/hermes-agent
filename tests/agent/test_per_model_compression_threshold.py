"""Tests for the per-model compression-threshold config resolver.

``compression.per_model_threshold`` is an optional config map of
model-id -> threshold fraction. It overrides the global
``compression.threshold`` for the matched model, and takes precedence over
the built-in ``_compression_threshold_for_model`` default.

The resolver (``agent.agent_init._resolve_per_model_threshold``) must:
- match exact model id, then case-insensitively
- accept fractions in (0, 1] only; ignore out-of-range / non-numeric
- return None for empty/missing maps, non-dict input, or no-match, so the
  caller falls through to the built-in default and then the global config.
"""

from __future__ import annotations

import pytest

from agent.agent_init import _resolve_per_model_threshold


def test_exact_match_wins() -> None:
    per_model = {"claude-opus-4-8": 0.85, "gpt-5.5": 0.7}
    assert _resolve_per_model_threshold(per_model, "claude-opus-4-8") == 0.85
    assert _resolve_per_model_threshold(per_model, "gpt-5.5") == 0.7


def test_case_insensitive_match() -> None:
    per_model = {"Claude-Opus-4-8": 0.85}
    assert _resolve_per_model_threshold(per_model, "claude-opus-4-8") == 0.85
    assert _resolve_per_model_threshold(per_model, "CLAUDE-OPUS-4-8") == 0.85


def test_exact_match_preferred_over_case_fold() -> None:
    # Two keys that fold to the same lowercase: the exact one must win.
    per_model = {"claude-opus-4-8": 0.6, "Claude-Opus-4-8": 0.9}
    assert _resolve_per_model_threshold(per_model, "claude-opus-4-8") == 0.6


def test_no_match_returns_none() -> None:
    per_model = {"gpt-5.5": 0.7}
    assert _resolve_per_model_threshold(per_model, "claude-opus-4-8") is None


@pytest.mark.parametrize("value", [1.0, 0.5, 0.01])
def test_boundary_values_accepted(value: float) -> None:
    assert _resolve_per_model_threshold({"m": value}, "m") == value


@pytest.mark.parametrize("value", [0.0, -0.1, 1.1, 2.0, 100])
def test_out_of_range_ignored(value) -> None:
    # Out of (0, 1] -> None so resolution falls through.
    assert _resolve_per_model_threshold({"m": value}, "m") is None


@pytest.mark.parametrize("value", ["high", None, [], {}, object()])
def test_non_numeric_ignored(value) -> None:
    assert _resolve_per_model_threshold({"m": value}, "m") is None


def test_numeric_string_is_coerced() -> None:
    # YAML can hand us a quoted number; float() coercion accepts it.
    assert _resolve_per_model_threshold({"m": "0.8"}, "m") == 0.8


@pytest.mark.parametrize("per_model", [None, {}, [], "not-a-dict", 42])
def test_empty_or_non_dict_map_returns_none(per_model) -> None:
    assert _resolve_per_model_threshold(per_model, "claude-opus-4-8") is None


@pytest.mark.parametrize("model", [None, "", 123, []])
def test_invalid_model_returns_none(model) -> None:
    assert _resolve_per_model_threshold({"claude-opus-4-8": 0.85}, model) is None


def test_non_string_keys_skipped_gracefully() -> None:
    # A non-string key must not crash the case-insensitive fallback scan.
    per_model = {123: 0.9, "claude-opus-4-8": 0.85}
    assert _resolve_per_model_threshold(per_model, "claude-opus-4-8") == 0.85
    assert _resolve_per_model_threshold(per_model, "other-model") is None
"""Tests for per-model compression threshold overrides.

Users who swap between models with very different context windows (e.g. a
256K model and a 1M model) need different compaction trigger points.
``compression.model_thresholds`` in config.yaml lets them set per-model
overrides that are resolved by longest substring match. The small-context
floor (75% for <512K models) still applies on top of per-model overrides.
"""

from unittest.mock import patch

from agent.context_compressor import ContextCompressor, resolve_model_threshold
from agent.context_engine import ContextEngine


# ---------------------------------------------------------------------------
# resolve_model_threshold helper
# ---------------------------------------------------------------------------

class TestResolveModelThreshold:
    def test_no_overrides_returns_default(self):
        assert resolve_model_threshold("glm-5.2", None, 0.50) == 0.50
        assert resolve_model_threshold("glm-5.2", {}, 0.50) == 0.50


    def test_exact_match(self):
        overrides = {"glm-5.2": 0.70}
        assert resolve_model_threshold("glm-5.2", overrides, 0.50) == 0.70


# ---------------------------------------------------------------------------
# ContextCompressor integration
# ---------------------------------------------------------------------------

class TestContextCompressorModelThresholds:
    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_init_large_context_with_override(self, _mock):
        """Large context (>=512K) + per-model override: override applies directly."""
        cc = ContextCompressor(
            model="glm-5.2",
            threshold_percent=0.50,
            model_thresholds={"glm-5.2": 0.40},
            quiet_mode=True,
        )
        # 1M context >= 512K, so no small-context floor — override wins
        assert cc.threshold_percent == 0.40
        assert cc.threshold_tokens == int(1_000_000 * 0.40)


    @patch("agent.context_compressor.get_model_context_length", return_value=256_000)
    def test_init_no_model_thresholds_dict(self, _mock):
        """Empty model_thresholds dict = backward compatible."""
        cc = ContextCompressor(
            model="glm-5.2",
            threshold_percent=0.50,
            quiet_mode=True,
        )
        # Resolve while mock is active (lazy init defers floor past __init__).
        _ = cc.context_length
        # 256K < 512K → floored at 0.75
        assert cc.threshold_percent == 0.75
        assert cc.model_thresholds == {}


    @patch("agent.context_compressor.get_model_context_length")
    def test_update_model_re_resolves_threshold(self, mock_ctx):
        """Switching models re-resolves the per-model threshold + re-applies floor."""
        mock_ctx.return_value = 256_000
        cc = ContextCompressor(
            model="glm-5.2",
            threshold_percent=0.50,
            model_thresholds={"glm-5.2": 0.80, "glm-5.2-1M": 0.25},
            quiet_mode=True,
        )
        # 256K < 512K → floor at 0.75; override 0.80 > 0.75, so 0.80 wins
        assert cc.threshold_percent == 0.80

        # Switch to the 1M model (large context, no floor)
        mock_ctx.return_value = 1_000_000
        cc.update_model(
            model="glm-5.2-1M",
            context_length=1_000_000,
        )
        # 1M >= 512K → no floor; override 0.25 applies directly
        assert cc.threshold_percent == 0.25
        assert cc.threshold_tokens == int(1_000_000 * 0.25)


# ---------------------------------------------------------------------------
# ContextEngine base class
# ---------------------------------------------------------------------------

class TestContextEngineModelThresholds:
    def test_base_class_update_model_applies_overrides(self):
        """The base-class update_model() applies model_thresholds if set."""
        class TestEngine(ContextEngine):
            @property
            def name(self):
                return "test"

            def update_from_response(self, usage):
                pass

            def should_compress(self, prompt_tokens=None):
                return False

            def compress(self, messages, current_tokens=None, focus_topic=None):
                return messages

        engine = TestEngine()
        engine.threshold_percent = 0.50
        engine._config_threshold_percent = 0.50
        engine.context_length = 0
        engine.model_thresholds = {"glm-5.2-1M": 0.25}

        engine.update_model(model="glm-5.2-1M", context_length=1_000_000)
        assert engine.threshold_percent == 0.25
        assert engine.threshold_tokens == int(1_000_000 * 0.25)


class TestProviderScopedKeys:
    def test_scoped_key_applies_only_on_its_provider(self):
        overrides = {"openai-codex:astra": 0.85}
        assert resolve_model_threshold("gpt-6-astra", overrides, 0.50, "openai-codex") == 0.85
        # Same slug via another route keeps the global value; the bare-key path is unchanged.
        assert resolve_model_threshold("openai/gpt-6-astra", overrides, 0.50, "openrouter") == 0.50
        assert resolve_model_threshold("openai/gpt-6-astra", {"astra": 0.85}, 0.50, "openrouter") == 0.85
        # Specificity is judged on the model substring: the bare 900k key beats the shorter scoped one;
        # a scoped key beats the bare key with the identical substring.
        both = {"openai-codex:astra": 0.85, "astra-900k": 0.50, "astra": 0.30}
        assert resolve_model_threshold("gpt-6-astra-900k", both, 0.50, "openai-codex") == 0.50
        assert resolve_model_threshold("gpt-6-astra", both, 0.50, "openai-codex") == 0.85

    @patch("agent.context_compressor.get_model_context_length", return_value=1_100_000)
    def test_compressor_switch_between_routes_rescopes(self, _mock):
        cc = ContextCompressor(
            model="gpt-6-astra", threshold_percent=0.50, provider="openai-codex",
            model_thresholds={"openai-codex:astra": 0.85}, quiet_mode=True,
        )
        assert cc.threshold_percent == 0.85
        cc.update_model(model="openai/gpt-6-astra", context_length=1_100_000, provider="openrouter")
        assert cc.threshold_percent == 0.50
