from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.native_content_slimmer import register
from plugins.native_content_slimmer.config import (
    DEFAULT_MODE,
    NativeContentSlimmerConfig,
    load_slimmer_config,
)
from plugins.native_content_slimmer.marker import parse_marker


def _large_payload(label: str = "register") -> str:
    return f"{label}-HEAD\n" + (f"{label}-middle evidence line\n" * 900) + f"{label}-TAIL\n"


class FakeContext:
    def __init__(self) -> None:
        self.hooks: list[tuple[str, Callable[..., Any]]] = []
        self.tools: list[dict[str, Any]] = []

    def register_hook(self, name: str, callback: Callable[..., Any]) -> None:
        self.hooks.append((name, callback))

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)


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


def test_register_wires_dual_hooks_when_explicitly_enabled(tmp_path) -> None:
    token = set_hermes_home_override(tmp_path / "home")
    try:
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
    finally:
        reset_hermes_home_override(token)


def test_register_active_lossless_wires_expand_tool_and_marker_round_trips(tmp_path) -> None:
    token = set_hermes_home_override(tmp_path / "home")
    try:
        ctx = FakeContext()

        cfg = register(
            ctx,
            config={
                "plugins": {
                    "native_content_slimmer": {
                        "enabled": True,
                        "mode": "active_lossless",
                    }
                }
            },
        )

        assert cfg.enabled is True
        assert cfg.mode == "active_lossless"
        assert [name for name, _ in ctx.hooks] == [
            "transform_terminal_output",
            "transform_tool_result",
        ]
        assert [tool["name"] for tool in ctx.tools] == ["expand_artifact"]

        raw = _large_payload()
        marker = ctx.hooks[1][1](
            tool_name="web_extract",
            result=raw,
            status="success",
            session_id="sess-register",
            tool_call_id="call-register",
        )
        assert marker is not None
        parsed = parse_marker(marker)
        assert parsed is not None

        expanded = json.loads(ctx.tools[0]["handler"]({"id": parsed.fields["id"]}, session_id="sess-register"))
        assert expanded["ok"] is True
        assert expanded["content"] == raw
    finally:
        reset_hermes_home_override(token)


def test_register_active_lossless_falls_back_to_shadow_when_expand_tool_cannot_register(caplog, tmp_path) -> None:
    class HookOnlyContext:
        def __init__(self) -> None:
            self.hooks: list[tuple[str, Callable[..., Any]]] = []

        def register_hook(self, name: str, callback: Callable[..., Any]) -> None:
            self.hooks.append((name, callback))

    token = set_hermes_home_override(tmp_path / "home")
    try:
        ctx = HookOnlyContext()

        cfg = register(
            ctx,
            config={
                "plugins": {
                    "native_content_slimmer": {
                        "enabled": True,
                        "mode": "active_lossless",
                    }
                }
            },
        )

        assert cfg.enabled is True
        assert cfg.mode == "shadow"
        assert [name for name, _ in ctx.hooks] == [
            "transform_terminal_output",
            "transform_tool_result",
        ]
        assert "active_lossless" in caplog.text
        assert "shadow" in caplog.text
    finally:
        reset_hermes_home_override(token)
