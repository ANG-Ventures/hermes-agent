"""Fork canary: no plugin platform auto-enables on a display/behaviour key alone (t_83ff59d4).

``gateway/config_env._plugin_is_configured`` probes ``entry.is_connected`` with a transient
``PlatformConfig(enabled=True, extra=<the YAML block's bridged keys>)``. A plugin whose
``is_connected`` reads "enabled and any extra" defeats that gate with its own probe: whatsapp did, and
7 fleet gateways booted DEGRADED (``whatsapp_not_paired``) from ``whatsapp: {gateway_restart_notification:
false}``. Two layers:

1. Every registered plugin platform, clean env: the exact transient probe view with ONLY behaviour keys
   reads not-connected. A parity merge that reintroduces the shape in any plugin goes red here.
2. End to end through ``load_gateway_config`` for whatsapp, with positive controls.
"""

import os

import pytest

from gateway.config import Platform, PlatformConfig, load_gateway_config

# Display/behaviour keys a YAML block may carry; none of them is an opt-in.
BEHAVIOUR_EXTRAS = (
    {"gateway_restart_notification": False},
    {"typing_indicator": False},
    {"require_mention": True},
    {"dm_policy": "open"},
    {"reply_to_mode": "off"},
)


def _plugin_entries():
    from hermes_cli.plugins import discover_plugins
    from gateway.platform_registry import platform_registry

    discover_plugins()
    return {e.name: e for e in platform_registry.plugin_entries() if e.is_connected is not None}


@pytest.fixture
def clean_home(monkeypatch, tmp_path):
    entries = _plugin_entries()
    prefixes = tuple(f"{name.upper()}_" for name in entries) + ("GATEWAY_RELAY", "HASS_", "TWILIO_", "QQ_")
    for key in list(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.mark.parametrize("name", sorted(_plugin_entries()))
def test_behaviour_keys_never_read_as_configured(name, clean_home):
    entry = _plugin_entries()[name]
    for extra in BEHAVIOUR_EXTRAS:
        try:
            hit = entry.is_connected(PlatformConfig(enabled=True, extra=dict(extra)))
        except Exception:
            hit = False  # a raising probe is treated as not-configured by _plugin_is_configured
        assert not hit, f"{name}.is_connected reads {extra} as configured: the enablement probe defeats itself"


@pytest.fixture
def wa_home(clean_home, monkeypatch):
    # node present (as on the fleet); conftest's lazy-install off-switch would otherwise make check_fn
    # False and hide the bug behind the dependency gate.
    monkeypatch.setattr(_plugin_entries()["whatsapp"], "check_fn", lambda: True)
    return clean_home


def _load(home, text):
    (home / "config.yaml").write_text(text, encoding="utf-8")
    cfg = load_gateway_config()
    return cfg, cfg.platforms.get(Platform.WHATSAPP)


def test_whatsapp_display_key_only_is_not_enabled(wa_home):
    cfg, wa = _load(wa_home, "whatsapp:\n  gateway_restart_notification: false\n")
    assert wa is None or not wa.enabled
    assert Platform.WHATSAPP not in cfg.get_connected_platforms()


def test_whatsapp_env_opt_in_enables(wa_home, monkeypatch):
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    cfg, wa = _load(wa_home, "whatsapp:\n  gateway_restart_notification: false\n")
    assert wa is not None and wa.enabled
    assert Platform.WHATSAPP in cfg.get_connected_platforms()


def test_whatsapp_explicit_yaml_enable_enables(wa_home):
    _, wa = _load(wa_home, "whatsapp:\n  enabled: true\n")
    assert wa is not None and wa.enabled
