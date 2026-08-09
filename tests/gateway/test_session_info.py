"""Tests for GatewayRunner._format_session_info — session config surfacing."""

import pytest
from unittest.mock import patch

from gateway.run import GatewayRunner


@pytest.fixture()
def runner():
    """Create a bare GatewayRunner without __init__."""
    return GatewayRunner.__new__(GatewayRunner)


def _patch_info(tmp_path, config_yaml, model, runtime):
    """Return a context-manager stack that patches _format_session_info deps."""
    cfg_path = tmp_path / "config.yaml"
    if config_yaml is not None:
        cfg_path.write_text(config_yaml)
    return (
        patch("gateway.run._hermes_home", tmp_path),
        patch("gateway.run._resolve_gateway_model", return_value=model),
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value=runtime),
    )


class TestFormatSessionInfo:

    def test_includes_model_name(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: anthropic/claude-opus-4.6\n  provider: openrouter\n",
                                  "anthropic/claude-opus-4.6",
                                  {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "k"})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "claude-opus-4.6" in info


    def test_config_context_length(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: test-model\n  context_length: 32768\n",
                                  "test-model",
                                  {"provider": "custom", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "32K" in info
        assert "config" in info

    def test_default_fallback_hint(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: unknown-model-xyz\n",
                                  "unknown-model-xyz",
                                  {"provider": "", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "256K" in info
        assert "model.context_length" in info

    def test_local_endpoint_shown(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(
            tmp_path,
            "model:\n  default: qwen3:8b\n  provider: custom\n  base_url: http://localhost:11434/v1\n  context_length: 8192\n",
            "qwen3:8b",
            {"provider": "custom", "base_url": "http://localhost:11434/v1", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "localhost:11434" in info
        assert "8K" in info

    def test_named_custom_provider_keeps_context_pin_without_model_base_url(
        self, runner, tmp_path
    ):
        """Session-reset banner must honor model.context_length for named custom providers.

        Repro: /status shows 262144 from config while the reset banner said
        ``131K tokens (detected)`` because empty model.base_url + runtime URL
        falsely cleared the pin and fell through to the Qwen family default.
        """
        model = "custom-local-agentw/Qwen-AgentWorld-35B-A3B-Q5_K_XL"
        config_yaml = (
            "model:\n"
            f"  default: {model}\n"
            "  provider: custom-local-agentw\n"
            "  context_length: 262144\n"
            "custom_providers:\n"
            "  - name: custom-local-agentw\n"
            "    base_url: http://127.0.0.1:8080/v1\n"
            "    models: {}\n"
        )
        p1, p2, p3 = _patch_info(
            tmp_path,
            config_yaml,
            model,
            {
                "provider": "custom-local-agentw",
                "base_url": "http://127.0.0.1:8080/v1",
                "api_key": "",
            },
        )
        with p1, p2, p3, patch(
            "hermes_cli.config.get_compatible_custom_providers",
            return_value=[
                {
                    "name": "custom-local-agentw",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "models": {},
                }
            ],
        ), patch(
            "agent.model_metadata.get_model_context_length",
            side_effect=lambda *args, **kwargs: (
                kwargs.get("config_context_length")
                if kwargs.get("config_context_length")
                else 131072
            ),
        ):
            info = runner._format_session_info()
        assert "262K" in info
        assert "config" in info
        assert "131K" not in info


class TestFormatSessionInfoReasoning:
    """The ◆ Reasoning: row reflects the effective reasoning effort."""

    def _run(self, runner, tmp_path, reasoning_cfg):
        p1, p2, p3 = _patch_info(
            tmp_path, "model:\n  default: test-model\n  provider: openrouter\n",
            "test-model", {"provider": "openrouter", "base_url": "", "api_key": ""})
        with p1, p2, p3, patch.object(
            type(runner), "_load_reasoning_config", return_value=reasoning_cfg
        ):
            return runner._format_session_info()

    def test_explicit_effort_shown(self, runner, tmp_path):
        info = self._run(runner, tmp_path, {"enabled": True, "effort": "xhigh"})
        assert "◆ Reasoning: xhigh" in info

    def test_none_when_disabled(self, runner, tmp_path):
        info = self._run(runner, tmp_path, {"enabled": False})
        assert "◆ Reasoning: none" in info

    def test_default_when_unset(self, runner, tmp_path):
        # parse_reasoning_effort returns None for unset → default (medium)
        info = self._run(runner, tmp_path, None)
        assert "◆ Reasoning: medium (default)" in info

    def test_row_ordered_between_provider_and_context(self, runner, tmp_path):
        info = self._run(runner, tmp_path, {"enabled": True, "effort": "high"})
        lines = info.splitlines()
        prov = next(i for i, l in enumerate(lines) if l.startswith("◆ Provider:"))
        reas = next(i for i, l in enumerate(lines) if l.startswith("◆ Reasoning:"))
        ctx = next(i for i, l in enumerate(lines) if l.startswith("◆ Context:"))
        assert prov < reas < ctx

    def test_reasoning_resolution_failure_omits_row(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(
            tmp_path, "model:\n  default: test-model\n  provider: openrouter\n",
            "test-model", {"provider": "openrouter", "base_url": "", "api_key": ""})
        with p1, p2, p3, patch.object(
            type(runner), "_load_reasoning_config", side_effect=RuntimeError("boom")
        ):
            info = runner._format_session_info()
        # Banner still renders; the reasoning row is simply omitted.
        assert "◆ Model:" in info
        assert "◆ Context:" in info
        assert "Reasoning" not in info


class TestReasoningEffortLabel:
    """The shared _reasoning_effort_label helper — single source of truth for
    the reasoning-effort display string used by both the /new reset banner and
    the /model switch confirmation."""

    def test_none_is_medium_default(self):
        assert GatewayRunner._reasoning_effort_label(None) == "medium (default)"

    def test_disabled_is_none(self):
        assert GatewayRunner._reasoning_effort_label({"enabled": False}) == "none"

    def test_explicit_effort(self):
        assert GatewayRunner._reasoning_effort_label(
            {"enabled": True, "effort": "xhigh"}
        ) == "xhigh"

    def test_enabled_without_effort_defaults_medium(self):
        assert GatewayRunner._reasoning_effort_label({"enabled": True}) == "medium"
