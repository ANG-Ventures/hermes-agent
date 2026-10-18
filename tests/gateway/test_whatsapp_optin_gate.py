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
    # An explicit YAML enable must reach get_connected_platforms() (the set the gateway starts adapters
    # for), not just set .enabled: asserting only .enabled let Prism b7cecd77117e through.
    cfg = _load(home, yaml_text)
    assert _wa(cfg) is not None and _wa(cfg).enabled
    assert Platform.WHATSAPP in cfg.get_connected_platforms()


def test_probe_view_is_not_an_opt_in(home):
    # The env pass's synthetic enabled=True view must not read as configured; a real enabled config must.
    from gateway.config_env import _ENABLEMENT_PROBE
    from gateway.platform_registry import platform_registry

    check = platform_registry.get("whatsapp").is_connected
    view = PlatformConfig(enabled=True, extra={"gateway_restart_notification": False})
    token = _ENABLEMENT_PROBE.set(True)
    try:
        assert not check(view)
    finally:
        _ENABLEMENT_PROBE.reset(token)
    assert check(view)


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


def _in_probe(fn, *a):
    from gateway.config_env import _ENABLEMENT_PROBE
    token = _ENABLEMENT_PROBE.set(True)
    try:
        return fn(*a)
    finally:
        _ENABLEMENT_PROBE.reset(token)


def test_is_connected_unit(home):
    from plugins.platforms.whatsapp.adapter import _is_connected

    # the exact transient view _plugin_is_configured builds, inside the probe it builds it in
    assert not _in_probe(_is_connected, PlatformConfig(enabled=True, extra={"gateway_restart_notification": False}))
    assert not _in_probe(_is_connected, PlatformConfig(enabled=True, extra={"require_mention": True, "dm_policy": "open"}))
    assert not _is_connected(None)


def test_is_connected_env_unit(home, monkeypatch):
    from plugins.platforms.whatsapp.adapter import _is_connected

    monkeypatch.setenv("WHATSAPP_ENABLED", "yes")
    assert _is_connected(PlatformConfig(enabled=True))
    monkeypatch.setenv("WHATSAPP_ENABLED", "false")
    assert not _in_probe(_is_connected, PlatformConfig(enabled=True, extra={"gateway_restart_notification": False}))


def test_setup_wizard_status_is_not_fooled_by_its_own_probe(home):
    # Prism 8ab4519d8689: the wizard's _platform_status built PlatformConfig(enabled=True) outside the probe
    # flag, so a fresh home read WhatsApp as "configured" and the any-configured check entered service install.
    from gateway.platform_registry import platform_registry
    from hermes_cli.gateway_setup_wizard import _platform_status

    assert _platform_status({"_registry_entry": platform_registry.get("whatsapp")}) == "not configured"


def test_setup_wizard_status_honors_env_opt_in(home, monkeypatch):
    from gateway.platform_registry import platform_registry
    from hermes_cli.gateway_setup_wizard import _platform_status

    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    assert _platform_status({"_registry_entry": platform_registry.get("whatsapp")}) == "configured"


def test_no_synthetic_is_connected_caller_bypasses_the_probe():
    # Every non-test caller that builds a synthetic enabled=True view for is_connected goes through
    # probe_is_connected (one owner). A new caller that hand-rolls it fails here.
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    pat = re.compile(r"is_connected\(\s*PlatformConfig\(\s*enabled=True")
    hits = [str(p.relative_to(root)) for d in ("gateway", "hermes_cli", "plugins", "agent")
            for p in (root / d).rglob("*.py") if pat.search(p.read_text(encoding="utf-8", errors="ignore"))]
    assert hits == ["gateway/config_env.py"], hits


@pytest.mark.parametrize("value", ["false", " false ", "off", "OFF", "0", "no", "disabled", "", " "])
def test_set_false_env_beats_yaml_enable(home, monkeypatch, value):
    # Prism 8ab4519d8689: a SET non-truthy WHATSAPP_ENABLED is an opt-out even over a YAML enable.
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
    # the other half of 81ee3ffaa7bc: UNSET (not empty) keeps the YAML enable
    cfg = _load(home, "platforms:\n  whatsapp:\n    enabled: true\n")
    assert Platform.WHATSAPP in cfg.get_connected_platforms()
