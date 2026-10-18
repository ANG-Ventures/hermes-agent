"""WhatsApp must not auto-enable on a display/behaviour key alone.

A top-level ``whatsapp: {gateway_restart_notification: false}`` bridged a key into
``PlatformConfig.extra``; ``_plugin_is_configured`` probes ``is_connected`` with a transient
``enabled=True``, and the old ``_is_connected`` returned True for ``enabled and extra``, so the probe
defeated its own gate and the gateway booted DEGRADED with ``whatsapp_not_paired``. These tests drive the
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
    # Deps present (node installed); conftest disables lazy installs, which
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
    # The gateway starts adapters for get_connected_platforms(), so .enabled alone is not enough: the
    # plugin's is_connected must also accept a real enabled config (it only rejects the probe's synthetic one).
    cfg = _load(home, yaml_text)
    assert _wa(cfg) is not None and _wa(cfg).enabled
    assert Platform.WHATSAPP in cfg.get_connected_platforms()


def test_explicit_disable_wins(home, monkeypatch):
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    cfg = _load(home, "whatsapp:\n  enabled: false\n  gateway_restart_notification: false\n")
    wa = _wa(cfg)
    assert wa is None or not wa.enabled


@pytest.mark.parametrize("env", [None, "false"])
def test_paired_creds_are_not_an_opt_in(home, monkeypatch, env):
    # Removing / falsing WHATSAPP_ENABLED is the documented disable; a leftover paired session must not undo it.
    if env is not None:
        monkeypatch.setenv("WHATSAPP_ENABLED", env)
    session = home / "wa-session"
    session.mkdir()
    (session / "creds.json").write_text("{}", encoding="utf-8")
    cfg = _load(home, f"whatsapp:\n  session_path: {session}\n")
    wa = _wa(cfg)
    assert wa is None or not wa.enabled
    assert Platform.WHATSAPP not in cfg.get_connected_platforms()


def _probe(fn, view):
    """Call *fn* the way the env pass does: inside the enablement probe."""
    from gateway.config_env import _ENABLEMENT_PROBE

    token = _ENABLEMENT_PROBE.set(True)
    try:
        return fn(view)
    finally:
        _ENABLEMENT_PROBE.reset(token)


def test_is_connected_unit(home):
    from plugins.platforms.whatsapp.adapter import _is_connected

    # the exact transient view _plugin_is_configured builds, inside the probe it builds it in
    assert not _probe(_is_connected, PlatformConfig(enabled=True, extra={"gateway_restart_notification": False}))
    assert not _probe(_is_connected, PlatformConfig(enabled=True, extra={"require_mention": True, "dm_policy": "open"}))
    assert not _is_connected(None)


def test_probe_view_is_not_an_opt_in_but_a_real_enabled_config_is(home):
    from plugins.platforms.whatsapp.adapter import _is_connected

    view = PlatformConfig(enabled=True, extra={"gateway_restart_notification": False})
    assert not _probe(_is_connected, view)
    assert _is_connected(view)
    assert not _is_connected(PlatformConfig(enabled=False))


def test_is_connected_env_unit(home, monkeypatch):
    from plugins.platforms.whatsapp.adapter import _is_connected

    monkeypatch.setenv("WHATSAPP_ENABLED", "yes")
    assert _is_connected(PlatformConfig(enabled=True))
    monkeypatch.setenv("WHATSAPP_ENABLED", "false")
    assert not _probe(_is_connected, PlatformConfig(enabled=True, extra={"gateway_restart_notification": False}))


def test_setup_wizard_status_is_not_fooled_by_its_own_probe(home):
    # The setup wizard's status line used to build its own PlatformConfig(enabled=True) outside the probe,
    # so a fresh home read WhatsApp as "configured".
    from gateway.platform_registry import platform_registry
    from hermes_cli.gateway_setup_wizard import _platform_status

    assert _platform_status({"_registry_entry": platform_registry.get("whatsapp")}) == "not configured"


def test_setup_wizard_status_honors_env_opt_in(home, monkeypatch):
    from gateway.platform_registry import platform_registry
    from hermes_cli.gateway_setup_wizard import _platform_status

    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    assert _platform_status({"_registry_entry": platform_registry.get("whatsapp")}) == "configured"


def test_synthetic_is_connected_view_has_one_owner():
    # Every non-test caller that builds a synthetic enabled=True view for is_connected goes through
    # gateway.config_env.probe_is_connected, so no caller can forget the probe flag.
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    pat = re.compile(r"is_connected\(\s*PlatformConfig\(\s*enabled=True")
    hits = sorted(str(f.relative_to(root)) for d in ("gateway", "hermes_cli", "plugins", "agent")
                  for f in (root / d).rglob("*.py") if pat.search(f.read_text(encoding="utf-8", errors="ignore")))
    assert hits == ["gateway/config_env.py"], hits


@pytest.mark.parametrize("value", ["false", " false ", "off", "OFF", "0", "no", "disabled", "", " "])
def test_set_non_truthy_env_beats_yaml_enable(home, monkeypatch, value):
    # A SET WHATSAPP_ENABLED is authoritative: any non-truthy value, empty included, is an opt-out.
    monkeypatch.setenv("WHATSAPP_ENABLED", value)
    cfg = _load(home, "platforms:\n  whatsapp:\n    enabled: true\n")
    assert Platform.WHATSAPP not in cfg.get_connected_platforms()
    assert _wa(cfg) is None or not _wa(cfg).enabled


@pytest.mark.parametrize("value", ["true", " TRUE ", "1", "yes"])
def test_set_truthy_env_enables(home, monkeypatch, value):
    monkeypatch.setenv("WHATSAPP_ENABLED", value)
    cfg = _load(home, "")
    assert Platform.WHATSAPP in cfg.get_connected_platforms()


def test_unset_env_lets_yaml_enable_through(home):
    # Only UNSET (not empty) leaves the decision to config.yaml.
    cfg = _load(home, "platforms:\n  whatsapp:\n    enabled: true\n")
    assert Platform.WHATSAPP in cfg.get_connected_platforms()
