"""The gateway_stale_lease_wait knob must reach the registry, not stay default.

Guards the config-knob trap: a key declared in DEFAULT_CONFIG but missing from
the env bridge ships INERT — config.yaml says 30, the gateway waits 90, and
nothing errors. Proven through the production bridge function, never a
hand-built config object.
"""

import os

import pytest

from gateway.fork_ext.restart_policy import (
    _AGENT_CONFIG_ENV_BRIDGE,
    _bridge_agent_config_to_env,
)
from gateway.turn_lease import (
    DEFAULT_STALE_LEASE_WAIT,
    SessionTurnLeaseRegistry,
)
from hermes_cli.config_defaults import DEFAULT_CONFIG


@pytest.fixture(autouse=True)
def _restore_env():
    before = os.environ.get("HERMES_STALE_LEASE_WAIT")
    yield
    if before is None:
        os.environ.pop("HERMES_STALE_LEASE_WAIT", None)
    else:
        os.environ["HERMES_STALE_LEASE_WAIT"] = before


def test_knob_is_declared_in_default_config():
    """Absent here, `hermes config set agent.gateway_stale_lease_wait` warns."""
    assert "gateway_stale_lease_wait" in DEFAULT_CONFIG["agent"]
    assert DEFAULT_CONFIG["agent"]["gateway_stale_lease_wait"] == 90


def test_default_config_value_matches_the_code_default():
    """The two defaults must agree or the knob silently changes behavior."""
    assert float(DEFAULT_CONFIG["agent"]["gateway_stale_lease_wait"]) == (
        DEFAULT_STALE_LEASE_WAIT
    )


def test_knob_is_on_the_single_sourced_env_bridge():
    assert (
        _AGENT_CONFIG_ENV_BRIDGE["gateway_stale_lease_wait"]
        == "HERMES_STALE_LEASE_WAIT"
    )


def test_config_value_reaches_the_env_var_and_wins_over_a_preset():
    """config.yaml is authoritative over a pre-set env var (PR #18413 rule)."""
    os.environ["HERMES_STALE_LEASE_WAIT"] = "999"
    _bridge_agent_config_to_env({"gateway_stale_lease_wait": 30})
    assert os.environ["HERMES_STALE_LEASE_WAIT"] == "30"


def test_absent_key_leaves_a_preset_env_untouched():
    os.environ["HERMES_STALE_LEASE_WAIT"] = "42"
    _bridge_agent_config_to_env({"gateway_timeout": 60})
    assert os.environ["HERMES_STALE_LEASE_WAIT"] == "42"


@pytest.mark.parametrize("bad", [0, -5, None])
def test_invalid_stale_wait_falls_back_to_the_default(bad):
    """A nonsense value must not disable the bound or make it negative."""
    registry = SessionTurnLeaseRegistry(stale_wait=bad)
    assert registry._stale_wait == DEFAULT_STALE_LEASE_WAIT


def test_explicit_stale_wait_is_honored():
    assert SessionTurnLeaseRegistry(stale_wait=30)._stale_wait == 30.0
