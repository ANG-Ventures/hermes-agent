"""Telegram startup diagnostics name the profile they are about, and a sibling's preflight read is not
this gateway's fault (t_da3c5285).

On 2026-11-24 13:17 argus's gateway logged "Platform 'telegram' is explicitly disabled by
platforms.telegram.enabled: false" (a key only chiron's and vulcan's config.yaml carry) and
"PLATFORM TOKEN MISSING: telegram ..." and then connected to Telegram 16 s later. Both lines came from
the multiplex preflight (``hermes_cli.gateway_migrate._profile_gateway_config``), which loads EVERY
profile's gateway config under that profile's own scope to decide whether this process may multiplex.
Those reads start no adapter, so their adapter-start diagnostics belong at DEBUG; a load that does
start adapters names the profile/home it loaded and reads credentials from that profile's scope.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import hermes_constants
from gateway import config_env
from gateway.config import load_gateway_config
from hermes_cli import gateway_migrate as gm

DISABLED = "explicitly disabled by platforms.telegram.enabled: false"
MISSING = "PLATFORM TOKEN MISSING"


def _profile(root: Path, name: str, env: str, yaml_text: str = "") -> Path:
    home = root / "profiles" / name
    home.mkdir(parents=True)
    (home / ".env").write_text(env, encoding="utf-8")
    (home / "config.yaml").write_text(yaml_text, encoding="utf-8")
    return home


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Default root + profile A (token, home channel: the launcher) + B (its own token, telegram
    disabled in config.yaml) + C (home channel, no token)."""
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text("", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None)
    monkeypatch.setattr(config_env, "_EXPLICIT_DISABLE_WARNED", set())
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL", "GATEWAY_RELAY_URL",
                 "GATEWAY_MULTIPLEX_PROFILES", "DISCORD_BOT_TOKEN", "DISCORD_HOME_CHANNEL"):
        monkeypatch.delenv(name, raising=False)
    a = _profile(root, "a", "TELEGRAM_BOT_TOKEN=111111:token-of-profile-a\nTELEGRAM_HOME_CHANNEL=-1001\n")
    b = _profile(root, "b", "TELEGRAM_BOT_TOKEN=222222:token-of-profile-b\n",
                 "platforms:\n  telegram:\n    enabled: false\n")
    c = _profile(root, "c", "TELEGRAM_HOME_CHANNEL=-1003\n")
    # A's launcher: HERMES_HOME is A, and A's .env is in the process env.
    monkeypatch.setenv("HERMES_HOME", str(a))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111111:token-of-profile-a")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-1001")
    monkeypatch.setattr(gm, "_installed_services", lambda home: [])
    monkeypatch.setattr(gm, "_live_gateway_pid", lambda home: None)
    return root, a, b, c


def _lines(caplog, mark):
    return [r for r in caplog.records if mark in r.getMessage() and r.levelno >= logging.WARNING]


def test_a_boot_with_preflight_over_siblings_logs_neither_line(fleet, caplog):
    root, a, b, c = fleet
    with caplog.at_level(logging.DEBUG, logger="gateway.config"):
        with gm._multiplex_read_mode():            # the preflight resolve_multiplex_mode runs on A's boot
            configs = {h.name: gm._profile_gateway_config(h) for h in (root, a, b, c)}
        own = load_gateway_config()                # A's own load
    assert configs["b"].platforms[config_env.Platform.TELEGRAM].enabled is False
    assert own.platforms[config_env.Platform.TELEGRAM].enabled is True
    assert _lines(caplog, DISABLED) == []
    assert _lines(caplog, MISSING) == []


def test_preflight_lines_are_kept_at_debug_and_name_the_profile(fleet, caplog):
    root, a, b, c = fleet
    with caplog.at_level(logging.DEBUG, logger="gateway.config"):
        with gm._multiplex_read_mode():
            gm._profile_gateway_config(b)
            gm._profile_gateway_config(c)
    dbg = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(DISABLED in m and str(b) in m for m in dbg), dbg
    assert any(MISSING in m and str(c) in m for m in dbg), dbg


def test_own_load_names_the_profile_home(fleet, monkeypatch, caplog):
    root, a, b, c = fleet
    monkeypatch.setenv("HERMES_HOME", str(c))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    with caplog.at_level(logging.WARNING, logger="gateway.config"):
        load_gateway_config()
    hits = _lines(caplog, MISSING)
    assert len(hits) == 1 and hits[0].levelno == logging.ERROR
    assert f"profile 'c' ({c})" in hits[0].getMessage()


def test_explicit_disable_reads_the_loaded_profiles_scope_not_the_process_env(fleet, monkeypatch, caplog):
    """B's own gateway: B's .env token beats nothing, B's config disables telegram -> warn, naming B."""
    root, a, b, c = fleet
    monkeypatch.setenv("HERMES_HOME", str(b))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "222222:token-of-profile-b")
    with caplog.at_level(logging.WARNING, logger="gateway.config"):
        load_gateway_config()
    hits = _lines(caplog, DISABLED)
    assert len(hits) == 1 and f"profile 'b' ({b})" in hits[0].getMessage()
