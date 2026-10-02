"""Loud startup line when a messaging platform is wanted but its token is blank (t_99485a6c).

A blank DISCORD_BOT_TOKEN never enables the adapter, so the gateway skipped Discord
silently for 17 days on a break-glass profile. These tests drive the real
load_gateway_config() against a temp home.
"""

import logging

from gateway.config import load_gateway_config

MARK = "PLATFORM TOKEN MISSING"


def _load(tmp_path, monkeypatch, caplog, yaml_text=None, env=None):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    if yaml_text is not None:
        (hermes_home / "config.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    for name in ("DISCORD_BOT_TOKEN", "DISCORD_HOME_CHANNEL", "TELEGRAM_BOT_TOKEN",
                 "TELEGRAM_HOME_CHANNEL", "SLACK_BOT_TOKEN", "SLACK_HOME_CHANNEL",
                 "GATEWAY_RELAY_URL"):
        monkeypatch.delenv(name, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    with caplog.at_level(logging.WARNING, logger="gateway.config"):
        config = load_gateway_config()
    return config, [r for r in caplog.records if MARK in r.getMessage()]


def test_blank_token_with_home_channel_logs_error(tmp_path, monkeypatch, caplog):
    # the 09-13 Aegis shape: DISCORD_BOT_TOKEN= blank, home channel still set
    _, hits = _load(tmp_path, monkeypatch, caplog,
                    env={"DISCORD_BOT_TOKEN": "", "DISCORD_HOME_CHANNEL": "1511273099503599746"})
    assert len(hits) == 1
    rec = hits[0]
    assert rec.levelno == logging.ERROR
    msg = rec.getMessage()
    assert "discord" in msg and "DISCORD_BOT_TOKEN is present but empty" in msg
    assert "DISCORD_HOME_CHANNEL is set" in msg


def test_blank_token_key_alone_logs_error(tmp_path, monkeypatch, caplog):
    _, hits = _load(tmp_path, monkeypatch, caplog, env={"TELEGRAM_BOT_TOKEN": ""})
    assert [h for h in hits if "telegram" in h.getMessage()]


def test_yaml_enabled_without_token_logs_error(tmp_path, monkeypatch, caplog):
    _, hits = _load(tmp_path, monkeypatch, caplog,
                    yaml_text="platforms:\n  slack:\n    enabled: true\n")
    assert [h for h in hits if "platforms.slack.enabled is true" in h.getMessage()]


def test_explicit_disable_is_silent(tmp_path, monkeypatch, caplog):
    _, hits = _load(tmp_path, monkeypatch, caplog,
                    yaml_text="platforms:\n  discord:\n    enabled: false\n",
                    env={"DISCORD_BOT_TOKEN": "", "DISCORD_HOME_CHANNEL": "1"})
    assert hits == []


def test_token_present_is_silent(tmp_path, monkeypatch, caplog):
    _, hits = _load(tmp_path, monkeypatch, caplog,
                    env={"DISCORD_BOT_TOKEN": "fake-token-for-test", "DISCORD_HOME_CHANNEL": "1"})
    assert hits == []


def test_unconfigured_platform_is_silent(tmp_path, monkeypatch, caplog):
    _, hits = _load(tmp_path, monkeypatch, caplog)
    assert hits == []
