"""Shutdown notices describe intent and recovery policy, not a promised outcome."""
from unittest.mock import MagicMock

import pytest

from agent import i18n
from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.session import build_session_key
from tests.gateway.restart_test_helpers import (
    RestartTestAdapter, make_restart_runner, make_restart_source,
)


@pytest.fixture
def notice_runner(monkeypatch):
    monkeypatch.setenv("HERMES_LANGUAGE", "en")
    monkeypatch.setattr("gateway.drain_control.drain_notification_suppressed", lambda: False)
    return make_restart_runner()


def activate(runner, source):
    key = build_session_key(source)
    runner._running_agents[key] = MagicMock()
    runner._cache_session_source(key, source)


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("mode", ["prompt", "auto", "always"])
async def test_active_shutdown_copy_is_conditional_and_policy_aware(notice_runner, monkeypatch, restart, mode):
    runner, adapter = notice_runner
    source = make_restart_source(thread_id="42")
    source.message_id = "command"
    activate(runner, source)
    runner._restart_requested = restart
    runner._restart_command_source = source if restart else None
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", mode)

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent_calls) == 1
    _, text, metadata = adapter.sent_calls[0]
    assert ("Hermes is restarting" if restart else "Hermes is shutting down") in text
    assert "may be interrupted" in text  # the drain may also finish the turn
    assert "If Hermes comes back online" in text
    assert "send any message" not in text.lower()
    assert "will resume" not in text.lower()
    if mode == "prompt":
        assert "you can ask to continue" in text
        assert "automatically" not in text
    else:
        assert "may continue automatically" in text
        assert "recovery checks" in text
        assert "you can ask" not in text
    assert metadata["thread_id"] == "42"
    assert metadata["_interim_send"] is True
    if restart:
        assert metadata["telegram_reply_to_message_id"] == "command"
    assert runner._restart_requested is restart


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_idle_home_does_not_claim_a_current_task_or_relaunch(notice_runner, restart):
    runner, adapter = notice_runner
    runner._restart_requested = restart
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id="home", name="home",
    )

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent_calls) == 1
    text = adapter.sent_calls[0][1]
    assert ("Hermes is restarting" if restart else "Hermes is shutting down") in text
    assert "task" not in text and "work" not in text
    assert "online" not in text and "message" not in text


@pytest.mark.asyncio
async def test_active_home_receives_one_active_notice_not_idle_broadcast(notice_runner):
    runner, adapter = notice_runner
    source = make_restart_source()
    activate(runner, source)
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id=source.chat_id, name="home",
    )
    await runner._notify_active_sessions_of_shutdown()
    assert len(adapter.sent) == 1
    assert "may be interrupted" in adapter.sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["prompt", "auto", "always", None, "invalid"])
@pytest.mark.parametrize("cached", [False, True])
async def test_secondary_policy_and_language_are_scoped_even_without_cached_source(
    notice_runner, tmp_path, monkeypatch, mode, cached,
):
    runner, primary = notice_runner
    secondary = RestartTestAdapter()
    home = tmp_path / "secondary"
    home.mkdir()
    policy = f"agent:\n  resume_interrupted_turns: {mode}\n" if mode else ""
    (home / "config.yaml").write_text(policy + "display:\n  language: fr\n")
    runner._profile_adapters = {"secondary": {Platform.TELEGRAM: secondary}}
    runner.config.multiplex_profiles = True
    runner._served_profile_homes = {"secondary": home}
    monkeypatch.delenv("HERMES_LANGUAGE")
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "prompt" if mode in {"auto", "always"} else "always")
    if cached:
        source = make_restart_source(chat_id="42")
        source.profile = "secondary"
        monkeypatch.setattr(runner, "_resolve_profile_home_for_source", lambda src: home if src.profile else tmp_path)
        activate(runner, source)
    else:
        # Parsed-key fallback has no cached source; it must still bind the owning profile.
        runner._running_agents["agent:secondary:telegram:dm:42"] = MagicMock()

    await runner._notify_active_sessions_of_shutdown()

    assert primary.sent == []
    assert len(secondary.sent) == 1
    assert "Hermes s’arrête" in secondary.sent[0]
    if mode in {"auto", "always"}:
        assert "peut reprendre automatiquement" in secondary.sent[0]
        assert "contrôles de récupération" in secondary.sent[0]
    else:
        assert "demander" in secondary.sent[0]
        assert "automatiquement" not in secondary.sent[0]
    # A -> B -> A: the scope must not leak into the following primary notice.
    runner._running_agents.clear()
    activate(runner, make_restart_source())
    await runner._notify_active_sessions_of_shutdown()
    assert ("you can ask to continue" if mode in {"auto", "always"} else "may continue automatically") in primary.sent[0]


@pytest.mark.asyncio
async def test_secondary_active_opt_out_does_not_borrow_primary_setting(notice_runner):
    runner, primary = notice_runner
    secondary = RestartTestAdapter()
    runner._profile_adapters = {"secondary": {Platform.TELEGRAM: secondary}}
    runner._profile_configs = {"secondary": GatewayConfig(platforms={
        Platform.TELEGRAM: PlatformConfig(enabled=True, gateway_restart_notification=False),
    })}
    runner._running_agents["agent:secondary:telegram:dm:42"] = MagicMock()
    await runner._notify_active_sessions_of_shutdown()
    assert primary.sent == secondary.sent == []


@pytest.mark.asyncio
async def test_restart_requester_still_gets_answer_in_muted_route(notice_runner, monkeypatch):
    runner, adapter = notice_runner
    source = make_restart_source(thread_id="42")
    activate(runner, source)
    runner._restart_requested = True
    runner._restart_command_source = source
    monkeypatch.setattr("gateway.response_filters.lifecycle_notice_muted", lambda *a, **kw: True)
    monkeypatch.setattr("gateway.warning_notifications.effective_user_config",
                        lambda: {"display": {"suppress_warning_notifications": True}})
    await runner._notify_active_sessions_of_shutdown()
    assert len(adapter.sent) == 1
    assert "Hermes is restarting" in adapter.sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("suppression", ["opt_out", "muted", "diagnostic"])
async def test_notice_suppression_preserved(notice_runner, monkeypatch, suppression):
    runner, adapter = notice_runner
    source = make_restart_source()
    activate(runner, source)
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id=source.chat_id, name="home",
    )
    if suppression == "opt_out":
        runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    elif suppression == "muted":
        monkeypatch.setattr("gateway.response_filters.lifecycle_notice_muted", lambda *a, **kw: True)
    else:
        monkeypatch.setattr("gateway.warning_notifications.effective_user_config",
                            lambda: {"display": {"suppress_warning_notifications": True}})
    await runner._notify_active_sessions_of_shutdown()
    assert adapter.sent == []


@pytest.mark.parametrize("lang", i18n.SUPPORTED_LANGUAGES)
def test_notice_catalogs_have_policy_fragments_and_neutral_home_copy(lang):
    catalog = i18n._load_bundled(lang)
    prefix = "gateway.shutdown."
    for key in ("notice_shutdown", "notice_restart", "active_interruption", "continue_prompt", "continue_auto"):
        assert catalog[prefix + key]
        assert i18n.t(prefix + key, lang=lang) != prefix + key
    # Titles must not reuse the previous long active-task + mandatory-ping translation.
    assert len(catalog[prefix + "notice_shutdown"]) < len(catalog[prefix + "active_interruption"]) + 45
