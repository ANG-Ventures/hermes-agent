"""Internal-wake chrome stays out of act-now page channels (t_c40d3277).

A /loop watch homed in fleet #alerts posted "🔔 wake · internal\\n💻 terminal: cd ..." tool
bubbles (70/day) and "⏳ Working — N min" heartbeats (35/day) there on every tick. Those are
chrome, not pages: an internal wake in a muted chat runs with the scheduled-heartbeat quiet
display. Human turns and other chats are unchanged.
"""

import importlib
import sys
import types

import pytest

from gateway.config import Platform
from gateway.response_filters import INTERNAL_NOTIFICATION_DISPLAY_KIND, wake_chrome_muted
from gateway.session import SessionSource
from tests.gateway.test_run_progress_topics import FakeAgent, ProgressCaptureAdapter, _make_runner

ALERTS = "1480528231286181948"
INTERNAL = INTERNAL_NOTIFICATION_DISPLAY_KIND


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("DISCORD_ALERTS_CHANNEL", raising=False)


def _src(chat_id=ALERTS, platform=Platform.DISCORD):
    return types.SimpleNamespace(chat_id=chat_id, platform=platform)


# ── pure helper ─────────────────────────────────────────────────────────────────────────────

def test_internal_wake_in_alerts_is_muted():
    assert wake_chrome_muted(INTERNAL, _src(), {}) is True


def test_human_turn_in_alerts_is_not_muted():
    assert wake_chrome_muted(None, _src(), {}) is False


def test_internal_wake_in_other_chat_is_not_muted():
    assert wake_chrome_muted(INTERNAL, _src("1111"), {}) is False


def test_non_discord_same_id_not_muted_by_default():
    assert wake_chrome_muted(INTERNAL, _src(platform=Platform.TELEGRAM), {}) is False


def test_env_override_names_the_alerts_channel(monkeypatch):
    monkeypatch.setenv("DISCORD_ALERTS_CHANNEL", "2222")
    assert wake_chrome_muted(INTERNAL, _src("2222"), {}) is True
    assert wake_chrome_muted(INTERNAL, _src(), {}) is False


def test_config_list_replaces_default_and_empty_disables():
    cfg = {"display": {"wake_chrome_muted_chats": ["3333"]}}
    assert wake_chrome_muted(INTERNAL, _src("3333", Platform.TELEGRAM), cfg) is True
    assert wake_chrome_muted(INTERNAL, _src(), cfg) is False
    assert wake_chrome_muted(INTERNAL, _src(), {"display": {"wake_chrome_muted_chats": []}}) is False


# ── through _run_agent ──────────────────────────────────────────────────────────────────────

async def _run(monkeypatch, tmp_path, *, chat_id, display_kind):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")
    import hermes_yaml as yaml
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"display": {"platforms": {"discord": {"tool_progress": "all"}}}}),
        encoding="utf-8",
    )
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = ProgressCaptureAdapter(platform=Platform.DISCORD)
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(platform=Platform.DISCORD, chat_id=chat_id, chat_type="group")
    result = await runner._run_agent(
        message="[/loop wakeup #82, every 15m] check #alerts and #logs",
        context_prompt="", history=[], source=source,
        session_id="loop-session", session_key=f"agent:main:discord:group:{chat_id}",
        persist_user_display_kind=display_kind,
    )
    assert result["final_response"] == "done"
    return adapter


@pytest.mark.asyncio
async def test_internal_wake_in_alerts_posts_no_tool_progress(monkeypatch, tmp_path):
    adapter = await _run(monkeypatch, tmp_path, chat_id=ALERTS, display_kind=INTERNAL)
    assert adapter.sent == []
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_internal_wake_elsewhere_keeps_tool_progress(monkeypatch, tmp_path):
    adapter = await _run(monkeypatch, tmp_path, chat_id="1111", display_kind=INTERNAL)
    assert adapter.sent
    assert adapter.sent[0]["content"].startswith("🔔 wake · internal")


@pytest.mark.asyncio
async def test_human_turn_in_alerts_keeps_tool_progress(monkeypatch, tmp_path):
    adapter = await _run(monkeypatch, tmp_path, chat_id=ALERTS, display_kind=None)
    assert adapter.sent
