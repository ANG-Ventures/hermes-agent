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
