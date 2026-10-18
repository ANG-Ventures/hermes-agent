"""WhatsApp must not auto-enable on a display/behaviour key alone (fork, t_83ff59d4).

A top-level ``whatsapp: {gateway_restart_notification: false}`` (22 fleet profiles) bridged a key into
``PlatformConfig.extra``; ``_plugin_is_configured`` probes ``is_connected`` with a transient
``enabled=True``, and the old ``_is_connected`` returned True for ``enabled and extra``, so the probe
defeated its own gate and 7 gateways booted DEGRADED with ``whatsapp_not_paired``. These tests drive the
real ``load_gateway_config`` against a temp HERMES_HOME.
"""

import os

import pytest

from gateway.config import Platform, PlatformConfig, load_gateway_config

_PREFIXES = ("WHATSAPP_", "GATEWAY_RELAY")


@pytest.fixture
def home(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # Deps present (node installed), as on the fleet gateways; conftest disables lazy installs, which
    # would otherwise make check_fn False and hide the bug behind the dependency gate.
    from hermes_cli.plugins import discover_plugins
    from gateway.platform_registry import platform_registry

    discover_plugins()
    entry = platform_registry.get("whatsapp")
    monkeypatch.setattr(entry, "check_fn", lambda: True)
    return hermes_home


def _load(home, yaml_text):
    (home / "config.yaml").write_text(yaml_text, encoding="utf-8")
    return load_gateway_config()


def _wa(cfg):
    return cfg.platforms.get(Platform.WHATSAPP)


def test_display_key_only_does_not_enable(home):
    cfg = _load(home, "whatsapp:\n  gateway_restart_notification: false\n")
    wa = _wa(cfg)
    assert wa is None or not wa.enabled
    assert Platform.WHATSAPP not in cfg.get_connected_platforms()


@pytest.mark.parametrize("key", ["require_mention: true", "dm_policy: allowlist", "typing_indicator: false"])
def test_behaviour_keys_do_not_enable(home, key):
    cfg = _load(home, f"whatsapp:\n  {key}\n")
    wa = _wa(cfg)
    assert wa is None or not wa.enabled
    assert Platform.WHATSAPP not in cfg.get_connected_platforms()


def test_env_opt_in_still_enables(home, monkeypatch):
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    cfg = _load(home, "whatsapp:\n  gateway_restart_notification: false\n")
    assert _wa(cfg) is not None and _wa(cfg).enabled
    assert Platform.WHATSAPP in cfg.get_connected_platforms()


@pytest.mark.parametrize("yaml_text", [
    "whatsapp:\n  enabled: true\n",
    "whatsapp:\n  enabled: true\n  gateway_restart_notification: false\n",
    "platforms:\n  whatsapp:\n    enabled: true\n",
])
def test_explicit_yaml_enable_still_enables(home, yaml_text):
    # enabled => the adapter starts (and reports not-paired itself); "connected" additionally needs pairing.
    cfg = _load(home, yaml_text)
    assert _wa(cfg) is not None and _wa(cfg).enabled


def test_explicit_disable_wins(home, monkeypatch):
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    cfg = _load(home, "whatsapp:\n  enabled: false\n  gateway_restart_notification: false\n")
    wa = _wa(cfg)
    assert wa is None or not wa.enabled


def test_paired_creds_on_disk_enable(home):
    session = home / "wa-session"
    session.mkdir()
    (session / "creds.json").write_text("{}", encoding="utf-8")
    cfg = _load(home, f"whatsapp:\n  session_path: {session}\n")
    assert _wa(cfg) is not None and _wa(cfg).enabled
    assert Platform.WHATSAPP in cfg.get_connected_platforms()


def test_is_connected_unit(home):
    from plugins.platforms.whatsapp.adapter import _is_connected

    # the exact transient view _plugin_is_configured builds
    assert not _is_connected(PlatformConfig(enabled=True, extra={"gateway_restart_notification": False}))
    assert not _is_connected(PlatformConfig(enabled=True, extra={"require_mention": True, "dm_policy": "open"}))
    assert not _is_connected(None)


def test_is_connected_env_and_creds_unit(home, monkeypatch):
    from plugins.platforms.whatsapp.adapter import _is_connected

    monkeypatch.setenv("WHATSAPP_ENABLED", "yes")
    assert _is_connected(PlatformConfig(enabled=True))
    monkeypatch.setenv("WHATSAPP_ENABLED", "false")
    assert not _is_connected(PlatformConfig(enabled=True))
    session = home / "s"
    session.mkdir()
    (session / "creds.json").write_text("{}", encoding="utf-8")
    assert _is_connected(PlatformConfig(enabled=True, extra={"session_path": str(session)}))
