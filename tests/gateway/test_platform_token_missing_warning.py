"""A messaging platform that is wanted but whose token is blank must be named at startup.

A blank ``DISCORD_BOT_TOKEN`` never enables the adapter (the env-enable step only fires on a
non-empty value), so the existing "enabled but empty" warning in ``_validate_gateway_config``
never sees it and the gateway quietly starts without that platform. These tests drive the real
``load_gateway_config()`` against a temp ``HERMES_HOME``.
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
    # token line kept in .env but emptied, home channel still set
    _, hits = _load(tmp_path, monkeypatch, caplog,
                    env={"DISCORD_BOT_TOKEN": "", "DISCORD_HOME_CHANNEL": "123456789"})
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
