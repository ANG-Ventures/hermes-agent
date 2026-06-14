from __future__ import annotations

from collections.abc import Callable
from typing import Any

from plugins.native_content_slimmer import register
from plugins.native_content_slimmer.config import (
    DEFAULT_MODE,
    NativeContentSlimmerConfig,
    load_slimmer_config,
)


class FakeContext:
    def __init__(self) -> None:
        self.hooks: list[tuple[str, Callable[..., Any]]] = []

    def register_hook(self, name: str, callback: Callable[..., Any]) -> None:
        self.hooks.append((name, callback))


def test_default_config_is_disabled_shadow() -> None:
    cfg = load_slimmer_config({})

    assert cfg == NativeContentSlimmerConfig(
        enabled=False,
        mode="shadow",
        valid=True,
        errors=(),
    )


def test_missing_plugin_block_uses_disabled_shadow_defaults() -> None:
    cfg = load_slimmer_config({"plugins": {}})

    assert cfg.enabled is False
    assert cfg.mode == DEFAULT_MODE
    assert cfg.valid is True
    assert cfg.errors == ()


def test_valid_config_can_enable_shadow_mode() -> None:
    cfg = load_slimmer_config(
        {"plugins": {"native_content_slimmer": {"enabled": True, "mode": "shadow"}}}
    )

    assert cfg.enabled is True
    assert cfg.mode == "shadow"
    assert cfg.valid is True
    assert cfg.errors == ()


def test_valid_config_accepts_active_lossless_mode() -> None:
    cfg = load_slimmer_config(
        {
            "plugins": {
                "native_content_slimmer": {
                    "enabled": True,
                    "mode": "active_lossless",
                }
            }
        }
    )

    assert cfg.enabled is True
    assert cfg.mode == "active_lossless"
    assert cfg.valid is True


def test_non_mapping_plugin_block_fails_closed_to_disabled() -> None:
    cfg = load_slimmer_config({"plugins": {"native_content_slimmer": True}})

    assert cfg.enabled is False
    assert cfg.mode == DEFAULT_MODE
    assert cfg.valid is False
    assert cfg.errors == ("plugins.native_content_slimmer must be a mapping",)


def test_non_boolean_enabled_fails_closed_to_disabled() -> None:
    cfg = load_slimmer_config(
        {"plugins": {"native_content_slimmer": {"enabled": "true", "mode": "shadow"}}}
    )

    assert cfg.enabled is False
    assert cfg.mode == DEFAULT_MODE
    assert cfg.valid is False
    assert "enabled must be a boolean" in cfg.errors


def test_invalid_mode_fails_closed_to_disabled() -> None:
    cfg = load_slimmer_config(
        {"plugins": {"native_content_slimmer": {"enabled": True, "mode": "lossy"}}}
    )

    assert cfg.enabled is False
    assert cfg.mode == DEFAULT_MODE
    assert cfg.valid is False
    assert "mode must be one of: active_lossless, shadow" in cfg.errors


def test_register_skips_hooks_by_default() -> None:
    ctx = FakeContext()

    cfg = register(ctx, config={})

    assert cfg.enabled is False
    assert ctx.hooks == []


def test_register_skips_hooks_when_config_is_invalid() -> None:
    ctx = FakeContext()

    cfg = register(
        ctx,
        config={"plugins": {"native_content_slimmer": {"enabled": True, "mode": "lossy"}}},
    )

    assert cfg.enabled is False
    assert cfg.valid is False
    assert ctx.hooks == []


def test_register_wires_dual_hooks_when_explicitly_enabled() -> None:
    ctx = FakeContext()

    cfg = register(
        ctx,
        config={
            "plugins": {
                "native_content_slimmer": {
                    "enabled": True,
                    "mode": "shadow",
                }
            }
        },
    )

    assert cfg.enabled is True
    assert [name for name, _ in ctx.hooks] == [
        "transform_terminal_output",
        "transform_tool_result",
    ]
    assert all(callback() is None for _, callback in ctx.hooks)
