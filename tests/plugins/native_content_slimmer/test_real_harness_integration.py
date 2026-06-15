from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.native_content_slimmer.marker import MARKER_TOKEN, parse_marker
from plugins.native_content_slimmer.store import raw_byte_len


def _large_terminal_payload() -> str:
    return "terminal-live-HEAD\n" + (("terminal-live-middle-" + "x" * 80 + "\n") * 650) + "terminal-live-TAIL\n"


def _write_native_slimmer_config(home: Path, *, mode: str = "active_lossless") -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "\n".join(
            [
                "plugins:",
                "  enabled:",
                "    - native_content_slimmer",
                "  native_content_slimmer:",
                "    enabled: true",
                f"    mode: {mode}",
                "    min_bytes: 100",
                "    preview_bytes: 120",
                "    artifact_gc_on_start: false",
                "    artifact_gc_after_write_every: 0",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _clear_plugin_manager() -> None:
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager._plugins.clear()
    manager._hooks.clear()
    manager._plugin_tool_names.clear()
    manager._cli_commands.clear()
    manager._plugin_commands.clear()
    manager._plugin_skills.clear()
    manager._aux_tasks.clear()
    manager._context_engine = None
    manager._discovered = False


def test_native_slimmer_loads_through_real_plugin_manager(monkeypatch, tmp_path) -> None:
    from hermes_cli.plugins import PluginContext, VALID_HOOKS, discover_plugins, get_plugin_manager

    home = tmp_path / "home"
    _write_native_slimmer_config(home)
    token = set_hermes_home_override(home)
    captured_tools: list[dict[str, Any]] = []
    original_register_tool = PluginContext.register_tool
    register_tool_signature = inspect.signature(original_register_tool)

    def spy_register_tool(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        bound = register_tool_signature.bind(self, *args, **kwargs)
        captured_tools.append({key: value for key, value in bound.arguments.items() if key != "self"})
        return original_register_tool(self, *args, **kwargs)

    monkeypatch.setattr(PluginContext, "register_tool", spy_register_tool)
    try:
        discover_plugins(force=True)
        manager = get_plugin_manager()
        loaded = manager._plugins.get("native_content_slimmer")

        assert loaded is not None
        assert loaded.enabled is True
        assert loaded.error is None
        assert manager.has_hook("transform_terminal_output") is True
        assert manager.has_hook("transform_tool_result") is True
        assert set(manager._hooks).issubset(VALID_HOOKS)
        assert captured_tools, "native_content_slimmer did not register expand_artifact"
        assert captured_tools[0]["name"] == "expand_artifact"
        assert captured_tools[0]["toolset"] == "native_content_slimmer"
        assert "schema" in captured_tools[0]
        assert "handler" in captured_tools[0]
        assert set(captured_tools[0]).issubset(set(register_tool_signature.parameters) - {"self"})
    finally:
        reset_hermes_home_override(token)
        _clear_plugin_manager()


def test_terminal_live_dispatch_consumes_marker_and_records_real_transcript_delta(monkeypatch, tmp_path) -> None:
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from model_tools import handle_function_call
    import tools.terminal_tool as terminal_tool

    home = tmp_path / "home"
    _write_native_slimmer_config(home)
    token = set_hermes_home_override(home)
    raw = _large_terminal_payload()

    class FakeEnv:
        cwd = str(tmp_path)
        env: dict[str, str] = {}

        def execute(self, command: str, **kwargs):  # type: ignore[no-untyped-def]
            return {"output": raw, "returncode": 0}

    def fake_env_config() -> dict[str, Any]:
        return {
            "env_type": "local",
            "cwd": str(tmp_path),
            "timeout": 30,
            "host_cwd": str(tmp_path),
            "local_persistent": False,
            "docker_image": "",
            "singularity_image": "",
            "modal_image": "",
            "daytona_image": "",
        }

    monkeypatch.setattr(terminal_tool, "_get_env_config", fake_env_config)
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type: {"approved": True},
    )
    with terminal_tool._env_lock:
        terminal_tool._active_environments.clear()
        terminal_tool._last_activity.clear()
        terminal_tool._active_environments["default"] = FakeEnv()
        terminal_tool._last_activity["default"] = 1.0

    try:
        discover_plugins(force=True)
        manager = get_plugin_manager()
        result = json.loads(
            handle_function_call(
                "terminal",
                {"command": "emit large output"},
                task_id="terminal-task",
                session_id="sess-live-terminal",
                tool_call_id="call-live-terminal",
                turn_id="turn-live-terminal",
                api_request_id="api-live-terminal",
            )
        )
        transcript = result["output"]

        assert MARKER_TOKEN in transcript
        assert raw not in transcript
        parsed = parse_marker(transcript)
        assert parsed is not None
        assert parsed.fields["session_id"] == "sess-live-terminal"
        assert parsed.fields["tool_call_id"] == "call-live-terminal"

        runtime = manager._hooks["transform_terminal_output"][0].__self__
        event = runtime.telemetry_records[-1]
        assert event["tool_name"] == "terminal"
        assert event["raw_source"] == "pre-truncation-terminal"
        assert event["saved_bytes"] == raw_byte_len(raw) - raw_byte_len(transcript)
    finally:
        reset_hermes_home_override(token)
        _clear_plugin_manager()
        with terminal_tool._env_lock:
            terminal_tool._active_environments.clear()
            terminal_tool._last_activity.clear()
