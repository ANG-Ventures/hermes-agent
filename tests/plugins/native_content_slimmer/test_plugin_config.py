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


def test_config_parses_classifier_and_gc_knobs() -> None:
    cfg = load_slimmer_config(
        {
            "plugins": {
                "native_content_slimmer": {
                    "enabled": True,
                    "mode": "active_lossless",
                    "artifact_ttl_days": 3,
                    "artifact_max_bytes_per_profile": 12345,
                    "artifact_gc_on_start": False,
                    "artifact_gc_on_session_end": True,
                    "artifact_gc_on_session_reset": False,
                    "artifact_gc_after_write_every": 7,
                    "artifact_gc_mode": "async_best_effort",
                    "min_bytes": 42,
                    "preview_bytes": 17,
                    "allow_tools": ["custom_tool"],
                    "deny_tools": ["blocked_tool"],
                    "deny_on_status": ["error", "blocked"],
                    "secret_policy": "no_store_pass_through",
                }
            }
        }
    )

    assert cfg.enabled is True
    assert cfg.mode == "active_lossless"
    assert cfg.valid is True
    assert cfg.artifact_ttl_days == 3
    assert cfg.artifact_max_bytes_per_profile == 12345
    assert cfg.artifact_gc_on_start is False
    assert cfg.artifact_gc_on_session_end is True
    assert cfg.artifact_gc_on_session_reset is False
    assert cfg.artifact_gc_after_write_every == 7
    assert cfg.artifact_gc_mode == "async_best_effort"
    assert cfg.min_bytes == 42
    assert cfg.preview_bytes == 17
    assert cfg.allow_tools == frozenset({"custom_tool"})
    assert cfg.deny_tools == frozenset({"blocked_tool"})
    assert cfg.deny_on_status == frozenset({"error", "blocked"})
    assert cfg.secret_policy == "no_store_pass_through"


def test_config_knobs_take_effect_in_classifier_path(tmp_path) -> None:
    cfg = load_slimmer_config(
        {
            "plugins": {
                "native_content_slimmer": {
                    "enabled": True,
                    "mode": "active_lossless",
                    "min_bytes": 10,
                    "preview_bytes": 40,
                    "allow_tools": ["custom_tool"],
                    "deny_tools": [],
                    "deny_on_status": [],
                }
            }
        }
    )
    ctx = FakeContext()
    assert cfg.valid is True
    # Use the runtime directly so this test is independent of global plugin discovery.
    from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
    from plugins.native_content_slimmer.store import ArtifactStore

    hooks = NativeContentSlimmerHooks(
        cfg,
        store=ArtifactStore(tmp_path / "artifacts"),
        secret=b"config-knobs-test-secret",
    )
    raw = "custom-HEAD\n" + ("custom-middle\n" * 20) + "custom-TAIL\n"

    marker = hooks.transform_tool_result(
        tool_name="custom_tool",
        result=raw,
        status="error",
        session_id="sess-config",
        tool_call_id="call-config",
    )

    assert marker is not None
    parsed = parse_marker(marker)
    assert parsed is not None
    assert int(parsed.fields["shown_bytes"]) <= cfg.preview_bytes + 64
    assert hooks.telemetry_records[0]["tool_status"] == "error"
    assert ctx.tools == []


def test_invalid_config_knobs_fail_closed_to_disabled() -> None:
    cfg = load_slimmer_config(
        {
            "plugins": {
                "native_content_slimmer": {
                    "enabled": True,
                    "mode": "active_lossless",
                    "min_bytes": -1,
                    "artifact_gc_mode": "blocking",
                }
            }
        }
    )

    assert cfg.enabled is False
    assert cfg.mode == DEFAULT_MODE
    assert cfg.valid is False
    assert "min_bytes must be a non-negative integer" in cfg.errors
    assert "artifact_gc_mode must be async_best_effort" in cfg.errors


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
