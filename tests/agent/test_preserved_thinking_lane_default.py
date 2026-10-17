"""``preserve_thinking`` is a LANE property: a registered provider profile carries the default.

Ace 2026-10-17: the flag lives on the lane (the fleet pin factory stamps every a-family
Anthropic-wire pin), not in each worker profile's config. Order: explicit config key (either
direction) > registered profile attribute > off. Fail-closed rules stay: non-bool config, an
unreadable config, a chat_completions face -> off.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import agent.preserved_thinking as pt


def _profile(api_mode="anthropic_messages", **attrs):
    return SimpleNamespace(api_mode=api_mode, **attrs)


@pytest.fixture
def lane(monkeypatch):
    """Point the resolver at an in-memory config map and an in-memory provider registry."""
    getattr(pt, "_logged_sources", set()).clear()
    state = {"providers": {}, "registry": {}}
    monkeypatch.setattr(pt, "_load_providers_config", lambda: state["providers"])
    import providers as reg
    monkeypatch.setattr(reg, "get_provider_profile", lambda name: state["registry"].get(name))
    return state


def test_lane_profile_with_no_config_key_is_enabled(lane):
    lane["registry"]["claude-apx-3"] = _profile(preserve_thinking=True, preserve_thinking_window="latest")
    assert pt.preserve_thinking_enabled("claude-apx-3") is True
    assert pt.resolve_preserve_thinking("claude-apx-3") == (True, "latest", "lane")


def test_lane_window_is_honoured(lane):
    lane["registry"]["claude-alr"] = _profile(preserve_thinking=True, preserve_thinking_window="contiguous")
    assert pt.preserve_thinking_window("claude-alr") == "contiguous"


def test_config_false_overrides_the_lane(lane):
    lane["registry"]["claude-alr"] = _profile(preserve_thinking=True)
    lane["providers"]["claude-alr"] = {"preserve_thinking": False}
    assert pt.preserve_thinking_enabled("claude-alr") is False
    assert pt.resolve_preserve_thinking("claude-alr") == (False, "latest", "config")


def test_config_true_turns_on_a_provider_with_no_lane_default(lane):
    lane["providers"]["relay"] = {"preserve_thinking": True}
    assert pt.resolve_preserve_thinking("relay") == (True, "latest", "config")


def test_non_bool_config_is_off_and_does_not_fall_through_to_the_lane(lane):
    lane["registry"]["claude-alr"] = _profile(preserve_thinking=True)
    lane["providers"]["claude-alr"] = {"preserve_thinking": "yes"}
    assert pt.resolve_preserve_thinking("claude-alr") == (False, "latest", "config")


def test_chat_completions_face_is_off_even_if_flagged(lane):
    lane["registry"]["claude-bpx-3"] = _profile(api_mode="chat_completions", preserve_thinking=True)
    assert pt.preserve_thinking_enabled("claude-bpx-3") is False
    assert pt.resolve_preserve_thinking("claude-bpx-3") == (False, "latest", "off")


def test_bpx_profile_without_attribute_is_off(lane):
    lane["registry"]["claude-bpr"] = _profile(api_mode="chat_completions")
    assert pt.resolve_preserve_thinking("claude-bpr") == (False, "latest", "off")


def test_unreadable_config_is_off_even_on_a_lane(lane, monkeypatch):
    lane["registry"]["claude-alr"] = _profile(preserve_thinking=True)

    def _boom():
        raise RuntimeError("no config")
    monkeypatch.setattr(pt, "_load_providers_config", _boom)
    assert pt.resolve_preserve_thinking("claude-alr") == (False, "latest", "off")


def test_custom_slug_resolves_the_lane(lane):
    lane["registry"]["claude-alr"] = _profile(preserve_thinking=True)
    assert pt.preserve_thinking_enabled("custom:claude-alr") is True
    assert pt.preserve_thinking_enabled("custom") is False


def test_source_logged_once_per_route_at_info(lane, caplog):
    lane["registry"]["claude-alr"] = _profile(preserve_thinking=True)
    with caplog.at_level(logging.INFO, logger=pt.logger.name):
        for _ in range(3):
            pt.preserve_thinking_enabled("claude-alr")
    lines = [r.getMessage() for r in caplog.records if "preserve_thinking: route=" in r.getMessage()]
    assert lines == ["preserve_thinking: route=claude-alr source=lane enabled=True window=latest"]
