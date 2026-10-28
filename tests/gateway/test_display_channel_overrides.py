"""``display.channels.<platform>.<chat_id>.<key>``: a per-channel display tier above the platform tier.

A decision channel (fleet #alerts runs a gateway session in-channel) wants final replies only, while the
same platform's home channel and working threads keep tool progress, interim text and diagnostics.
"""

import importlib
import sys
import time
import types

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.warning_notifications import DiagnosticText
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner

ALERTS = "1480000000000000001"
HOME = "1480000000000000002"

_FINAL_ONLY = {
    "tool_progress": "off",
    "interim_assistant_messages": False,
    "long_running_notifications": False,
    "suppress_warning_notifications": True,
}


class TestChannelTier:
    def test_channel_tier_beats_platform_tier(self):
        from gateway.display_config import resolve_display_setting

        cfg = {"display": {
            "platforms": {"discord": {"tool_progress": "all"}},
            "channels": {"discord": {ALERTS: {"tool_progress": "off"}}},
        }}
        assert resolve_display_setting(cfg, "discord", "tool_progress", chat_id=ALERTS) == "off"
        assert resolve_display_setting(cfg, "discord", "tool_progress", chat_id=HOME) == "all"
        # No chat id = platform-tier callers (``/verbose``) are unchanged.
        assert resolve_display_setting(cfg, "discord", "tool_progress") == "all"

    def test_thread_inherits_parent_channel(self):
        from gateway.display_config import resolve_display_setting

        cfg = {"display": {"channels": {"discord": {ALERTS: {"interim_assistant_messages": False}}}}}
        assert resolve_display_setting(
            cfg, "discord", "interim_assistant_messages", chat_id="thread-9", parent_id=ALERTS) is False

    def test_thread_own_entry_beats_parent(self):
        from gateway.display_config import resolve_display_setting

        cfg = {"display": {"channels": {"discord": {
            ALERTS: {"tool_progress": "off"}, "thread-9": {"tool_progress": "new"}}}}}
        assert resolve_display_setting(
            cfg, "discord", "tool_progress", chat_id="thread-9", parent_id=ALERTS) == "new"

    def test_absent_channel_falls_through_unchanged(self):
        from gateway.display_config import resolve_display_setting

        base = {"display": {"tool_progress": "new", "platforms": {"discord": {"busy_ack_detail": False}}}}
        with_channels = {"display": {**base["display"], "channels": {"discord": {ALERTS: {"tool_progress": "off"}}}}}
        for setting in ("tool_progress", "busy_ack_detail", "interim_assistant_messages", "reasoning_style"):
            assert (resolve_display_setting(with_channels, "discord", setting, chat_id=HOME)
                    == resolve_display_setting(base, "discord", setting))

    def test_unquoted_yaml_id_matches(self):
        from gateway.display_config import resolve_display_setting

        cfg = {"display": {"channels": {"discord": {int(ALERTS): {"tool_progress": "off"}}}}}
        assert resolve_display_setting(cfg, "discord", "tool_progress", chat_id=ALERTS) == "off"

    def test_channel_entry_is_platform_scoped(self):
        from gateway.display_config import resolve_display_setting

        cfg = {"display": {"channels": {"discord": {ALERTS: {"tool_progress": "off"}}}}}
        assert resolve_display_setting(cfg, "telegram", "tool_progress", chat_id=ALERTS) == (
            resolve_display_setting({}, "telegram", "tool_progress"))

    def test_unknown_key_ignored_with_one_warning(self, caplog):
        from gateway import display_config

        display_config._warned_channel_keys.clear()
        cfg = {"display": {"channels": {"discord": {ALERTS: {"tool_progres": "off"}}}}}
        with caplog.at_level("WARNING", logger="gateway.display_config"):
            for _ in range(3):
                assert display_config.resolve_display_setting(
                    cfg, "discord", "tool_progress", chat_id=ALERTS) == "all"
        assert sum("tool_progres" in r.getMessage() for r in caplog.records) == 1

    def test_tool_progress_provenance_from_channel_tier(self):
        from gateway.display_config import resolve_tool_progress

        cfg = {"display": {"channels": {"discord": {ALERTS: {"tool_progress": "off"}}}}}
        assert resolve_tool_progress(cfg, "discord", "all", chat_id=ALERTS) == ("off", True)
        assert resolve_tool_progress(cfg, "discord", "all", chat_id=HOME) == ("all", True)

    def test_warning_notifications_channel_scoped(self):
        from gateway.warning_notifications import warning_notifications_enabled

        cfg = {"display": {"channels": {"discord": {ALERTS: {"suppress_warning_notifications": True}}}}}
        assert warning_notifications_enabled("discord", cfg, chat_id=ALERTS) is False
        assert warning_notifications_enabled("discord", cfg, chat_id="t", parent_id=ALERTS) is False
        assert warning_notifications_enabled("discord", cfg, chat_id=HOME) is True
        assert warning_notifications_enabled("discord", cfg) is True


class _ChromeAgent:
    """Two tool calls, one interim line, one wall-budget DiagnosticText, then the final reply."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.status_callback = None
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
        self.tool_progress_callback("tool.started", "terminal", "pwd", {})
        time.sleep(0.35)
        if self.interim_assistant_callback:
            self.interim_assistant_callback("checking the queue first")
        time.sleep(0.2)
        if self.status_callback:
            self.status_callback("lifecycle", DiagnosticText(
                "⏳ still waiting for a first reply: 124s, 2 lanes tried"))
        self.tool_progress_callback("tool.started", "read_file", "/tmp/x", {})
        time.sleep(0.35)
        return {"final_response": "final answer", "messages": [], "api_calls": 1}


async def _run_turn(monkeypatch, tmp_path, chat_id):
    import hermes_yaml as yaml

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"display": {
        "platforms": {"discord": {"tool_progress": "all"}},
        "channels": {"discord": {ALERTS: _FINAL_ONLY}},
    }}), encoding="utf-8")
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _ChromeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")

    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr("gateway.warning_notifications.effective_user_config",
                        lambda: yaml.safe_load((tmp_path / "config.yaml").read_text()))

    source = SessionSource(platform=Platform.DISCORD, chat_id=chat_id, chat_type="group", thread_id=None)
    result = await runner._run_agent(
        message="tick", context_prompt="", history=[], source=source,
        session_id=f"s-{chat_id}", session_key=f"agent:main:discord:group:{chat_id}",
    )
    return result, adapter


def _visible(adapter):
    return [m["content"] for m in adapter.sent] + [e["content"] for e in adapter.edits]


@pytest.mark.asyncio
async def test_final_only_channel_gets_no_chrome(monkeypatch, tmp_path):
    result, adapter = await _run_turn(monkeypatch, tmp_path, ALERTS)
    assert result["final_response"] == "final answer"
    # The final reply is the caller's single delivery; the turn itself posted nothing.
    assert _visible(adapter) == []


@pytest.mark.asyncio
async def test_other_channel_keeps_progress_interim_and_diagnostic(monkeypatch, tmp_path):
    result, adapter = await _run_turn(monkeypatch, tmp_path, HOME)
    assert result["final_response"] == "final answer"
    text = "\n".join(_visible(adapter))
    assert "pwd" in text or "terminal" in text
    assert "checking the queue first" in text
    assert "still waiting for a first reply" in text


class TestChannelScopedPolicyGates:
    """Wake muting and async notices read the destination's channel tier too (Prism bb57d1fdac5c)."""

    _CFG = {"display": {"suppress_warning_notifications": True,
                        "channels": {"discord": {ALERTS: {"suppress_warning_notifications": False}}}}}

    def _wake(self, chat_id, parent_id=None):
        source = SessionSource(platform=Platform.DISCORD, chat_id=chat_id, chat_type="group",
                               parent_chat_id=parent_id)
        return types.SimpleNamespace(internal=True, source=source,
                                     metadata={"notification_category": "diagnostic"})

    def test_channel_opt_in_unmutes_diagnostic_wake_reply(self):
        from gateway.warning_notifications import diagnostic_turn_muted, diagnostic_wake_muted

        assert diagnostic_wake_muted(self._wake(ALERTS), self._CFG) is False
        assert diagnostic_wake_muted(self._wake("thread-9", parent_id=ALERTS), self._CFG) is False
        assert diagnostic_wake_muted(self._wake(HOME), self._CFG) is True
        meta = {"notification_category": "diagnostic"}
        assert diagnostic_turn_muted(meta, Platform.DISCORD, self._CFG, source=self._wake(ALERTS).source) is False
        assert diagnostic_turn_muted(meta, Platform.DISCORD, self._CFG) is True

    @pytest.mark.asyncio
    async def test_async_present_reads_channel_tier(self):
        from gateway.warning_notifications import present_notification

        sent = []

        async def send():
            sent.append(1)

        assert await present_notification(send, platform="discord", user_config=self._CFG, chat_id=ALERTS) is True
        assert await present_notification(send, platform="discord", user_config=self._CFG, chat_id=HOME) is False
        assert sent == [1]
