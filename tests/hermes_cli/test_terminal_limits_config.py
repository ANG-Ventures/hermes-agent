"""terminal.max_foreground_timeout / terminal.disk_warning_gb must be CONFIG keys.

Both were env-var-only (TERMINAL_MAX_FOREGROUND_TIMEOUT / TERMINAL_DISK_WARNING_GB),
which contradicts the project rule that ``.env`` is for secrets and behavioral
settings live in ``config.yaml``.

The subtle half: ``FOREGROUND_MAX_TIMEOUT`` is a module constant evaluated at
IMPORT time, while the config->env bridge (``_ensure_terminal_env_bridged``)
runs on FIRST TOOL USE — after import. So merely adding the key to
``TERMINAL_CONFIG_ENV_MAP`` would have produced a config knob that parses,
validates, appears in ``hermes config``, and does nothing. The enforcement path
must re-read the env AFTER the bridge, which is what ``_foreground_max_timeout()``
exists for.
"""

from __future__ import annotations

import importlib

import pytest


def test_keys_are_bridged_to_their_env_vars():
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP

    assert TERMINAL_CONFIG_ENV_MAP["max_foreground_timeout"] == "TERMINAL_MAX_FOREGROUND_TIMEOUT"
    assert TERMINAL_CONFIG_ENV_MAP["disk_warning_gb"] == "TERMINAL_DISK_WARNING_GB"


def test_keys_have_defaults_matching_the_historical_env_defaults():
    """Adding a config key must not silently change existing behavior."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    terminal = DEFAULT_CONFIG["terminal"]
    assert terminal["max_foreground_timeout"] == 600
    assert terminal["disk_warning_gb"] == 500.0


def test_config_key_resolves_through_the_public_helper():
    from hermes_cli.config import terminal_config_env_var_for_key

    assert (
        terminal_config_env_var_for_key("terminal.max_foreground_timeout")
        == "TERMINAL_MAX_FOREGROUND_TIMEOUT"
    )
    assert (
        terminal_config_env_var_for_key("terminal.disk_warning_gb")
        == "TERMINAL_DISK_WARNING_GB"
    )


# --- the inert-config guard ------------------------------------------------


def test_enforcement_reads_env_AFTER_import_not_the_frozen_constant(monkeypatch):
    """The whole point: a value set after import must be honored.

    This is what makes the config key real rather than decorative. If the
    enforcement site read the module constant, this test fails.
    """
    import tools.terminal_tool as tt

    # Simulate the bridge running after import with a config-derived value.
    monkeypatch.setenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", "3600")
    assert tt._foreground_max_timeout() == 3600

    monkeypatch.setenv("TERMINAL_DISK_WARNING_GB", "42.5")
    assert tt._disk_warning_gb() == 42.5


def test_falls_back_to_the_import_time_value_when_env_absent(monkeypatch):
    import tools.terminal_tool as tt

    monkeypatch.delenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", raising=False)
    assert tt._foreground_max_timeout() == tt.FOREGROUND_MAX_TIMEOUT

    monkeypatch.delenv("TERMINAL_DISK_WARNING_GB", raising=False)
    assert tt._disk_warning_gb() == tt.DISK_USAGE_WARNING_THRESHOLD_GB


def test_garbage_env_value_falls_back_and_does_not_raise(monkeypatch):
    import tools.terminal_tool as tt

    monkeypatch.setenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", "not-a-number")
    assert tt._foreground_max_timeout() == tt.FOREGROUND_MAX_TIMEOUT

    monkeypatch.setenv("TERMINAL_DISK_WARNING_GB", "")
    assert tt._disk_warning_gb() == tt.DISK_USAGE_WARNING_THRESHOLD_GB


def test_enforcement_site_does_not_reference_the_frozen_constant():
    """Source contract — the only honest way to gate 'nobody regressed this'.

    An arithmetic test keeps passing if someone swaps the accessor back for
    the constant, because both hold the same value in a default environment.
    """
    import inspect
    import tools.terminal_tool as tt

    src = inspect.getsource(tt.terminal_tool)
    assert "_foreground_max_timeout()" in src, "enforcement must call the accessor"
    assert "> FOREGROUND_MAX_TIMEOUT" not in src, (
        "enforcement compared against the import-time constant — a config-set "
        "cap would be silently ignored"
    )
