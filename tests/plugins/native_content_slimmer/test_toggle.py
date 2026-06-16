"""PRD #1.5 Phase 5 — CLI toggle round-trips through load_slimmer_config."""

from __future__ import annotations

from plugins.native_content_slimmer.config import load_slimmer_config
from plugins.native_content_slimmer.toggle import apply_toggle, diff_block

import pytest


def _roundtrip(mode, base=None, **kw):
    cfg_dict = apply_toggle(mode, base or {}, **kw)
    return load_slimmer_config(cfg_dict)


def test_off_disables():
    cfg = _roundtrip("off")
    assert cfg.enabled is False


def test_shadow_enables_shadow():
    cfg = _roundtrip("shadow")
    assert cfg.enabled is True
    assert cfg.mode == "shadow"


def test_active_enables_active_lossless():
    cfg = _roundtrip("active")
    assert cfg.enabled is True
    assert cfg.mode == "active_lossless"


def test_invalid_mode_fails_closed():
    with pytest.raises(ValueError):
        apply_toggle("bogus", {})


def test_allow_deny_passthrough():
    cfg = _roundtrip("active", allow_tools=["terminal", "web_extract"], deny_tools=["memory"])
    assert cfg.enabled is True
    assert cfg.allow_tools == frozenset({"terminal", "web_extract"})
    assert "memory" in cfg.deny_tools


def test_only_plugin_block_changes():
    base = {"model": {"default": "claude-opus-4-8"}, "plugins": {"other": {"x": 1}}}
    new = apply_toggle("active", base)
    # untouched top-level + sibling plugin survive
    assert new["model"] == {"default": "claude-opus-4-8"}
    assert new["plugins"]["other"] == {"x": 1}
    assert new["plugins"]["native_content_slimmer"]["mode"] == "active_lossless"


def test_diff_shows_change():
    base = {"plugins": {"native_content_slimmer": {"enabled": False, "mode": "shadow"}}}
    new = apply_toggle("active", base)
    d = diff_block(base, new)
    assert "active_lossless" in d
    assert "=>" in d  # a change marker is present


def test_toggle_does_not_mutate_input():
    base = {"plugins": {"native_content_slimmer": {"enabled": False}}}
    apply_toggle("active", base)
    # original untouched (pure function)
    assert base["plugins"]["native_content_slimmer"] == {"enabled": False}
