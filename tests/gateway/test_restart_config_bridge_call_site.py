"""Call-site tests for the fork restart-policy config bridge (t_a68658ec, row 9 of
docs/sync/fork-call-site-coverage.md).

``tests/gateway/test_fork_ext_restart_policy.py`` and ``test_stale_lease_wait_config.py`` call
``gateway.fork_ext.restart_policy._bridge_agent_config_to_env`` directly. That proves the helper,
not the upstream call site that feeds it: a parity merge that drops or re-wires the call in
``gateway/run.py::_bridge_config_to_env`` keeps both files green. Every test here drives the REAL
startup function ``_bridge_config_to_env`` with a config dict and reads the result through the
live reader the gateway uses.

Fork-owned keys = ``_AGENT_CONFIG_ENV_BRIDGE`` minus what upstream's own bridges carry
(``_AGENT_ENV_BRIDGE`` and ``_bridge_max_turns_to_env``). Only the fork call bridges these, so
dropping it leaves a stale env var (or the code default) in force. Per key:

1. ``test_wiring[<key>]``: after the call site runs, the env var holds ``str(config value)`` and
   wins over a stale preset (the fork rule, PR #18413: config wins).
2. ``test_effect[<key>]``: the live reader returns each configured value; with the fork call
   dropped at the call site the reader falls back to the stale preset (upstream shape).

No test here reads source text.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types

import pytest

import gateway.fork_ext.restart_policy as rp
import gateway.run as gw_run
from gateway.config import GatewayConfig

_WHERE = "call site = gateway/run.py::_bridge_config_to_env"


class _Store:
    """Session store stand-in: records whether stale-flag clearing ran."""

    def __init__(self):
        self.calls = []

    async def clear_stale_resume_pending(self, stale_after):
        self.calls.append(stale_after)
        return 1


def _stale_clear_effect():
    store = _Store()
    asyncio.run(gw_run._clear_stale_resume_pending_flags(store))
    return "cleared" if store.calls else "skipped"


def _stale_lease_wait_effect(monkeypatch):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    return gw_run.GatewayRunner(GatewayConfig())._turn_leases._stale_wait


# key -> (two config values, stale env preset, reader(monkeypatch), expected per config value,
#         expected under the stale preset). The stale expectation differs from the FIRST config
#         value's expectation, so a dropped call is visible.
_CASES = {
    "restart_initiated_ttl_secs": (
        (1800, 3600), "120", lambda mp: gw_run._restart_initiated_ttl_secs(), (1800.0, 3600.0), 120.0),
    "restart_loop_threshold": (
        (7, 9), "2", lambda mp: gw_run._restart_loop_threshold(), (7, 9), 2),
    "restart_loop_window_secs": (
        (900, 1200), "60", lambda mp: gw_run._restart_loop_window_secs(), (900.0, 1200.0), 60.0),
    "auto_resume_max_attempts": (
        (0, 4), "5", lambda mp: gw_run._auto_resume_max_attempts(), (0, 4), 5),
    "resume_interrupted_turns": (
        ("always", "auto"), "prompt", lambda mp: gw_run._resume_interrupted_turns_mode(),
        ("always", "auto"), "prompt"),
    "resume_flag_stale_clear": (
        (False, True), "true", lambda mp: _stale_clear_effect(), ("skipped", "cleared"), "cleared"),
    "gateway_stale_lease_wait": (
        (45, 120), "5", _stale_lease_wait_effect, (45.0, 120.0), 5.0),
}
_FORK_KEYS = sorted(_CASES)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Every bridged env var starts unset and is restored after the test."""
    monkeypatch.setattr(gw_run, "_hermes_home", tmp_path)
    for env_var in set(rp._AGENT_CONFIG_ENV_BRIDGE.values()) | set(gw_run._AGENT_ENV_BRIDGE.values()):
        monkeypatch.setenv(env_var, "")
        monkeypatch.delenv(env_var)


def _env(key):
    return rp._AGENT_CONFIG_ENV_BRIDGE[key]


def _drive_call_site(key, value):
    gw_run._bridge_config_to_env({"agent": {key: value}})


def _drop_fork_call(monkeypatch):
    """What a parity merge that loses the call does: the helper is never invoked."""
    monkeypatch.setattr(rp, "_bridge_agent_config_to_env", lambda agent_cfg: None)


def test_every_fork_only_key_has_a_call_site_pair():
    upstream_bridged = set(gw_run._AGENT_ENV_BRIDGE) | {"max_turns"}
    fork_only = set(rp._AGENT_CONFIG_ENV_BRIDGE) - upstream_bridged
    missing = sorted(fork_only - set(_CASES))
    assert not missing, f"fork-only bridge keys without a call-site test pair: {missing}"


def test_call_site_passes_the_agent_section_to_the_fork_helper(monkeypatch):
    seen = []
    real = rp._bridge_agent_config_to_env
    monkeypatch.setattr(rp, "_bridge_agent_config_to_env", lambda cfg: (seen.append(cfg), real(cfg)))
    agent_cfg = {"restart_loop_threshold": 7, "resume_interrupted_turns": "always"}
    gw_run._bridge_config_to_env({"agent": agent_cfg, "display": {}})
    assert seen == [agent_cfg], f"{_WHERE} must hand the agent: section to the fork bridge once"


@pytest.mark.parametrize("key", _FORK_KEYS)
def test_wiring(key, monkeypatch):
    values, stale, _reader, _expected, _stale_expected = _CASES[key]
    monkeypatch.setenv(_env(key), stale)
    _drive_call_site(key, values[0])
    got = gw_run.os.environ.get(_env(key))
    assert got == str(values[0]), (
        f"agent.{key}: env after {_WHERE} is {got!r}, fork rule (config wins) gives {str(values[0])!r}")


@pytest.mark.parametrize("key", _FORK_KEYS)
def test_effect(key, monkeypatch):
    values, stale, reader, expected, stale_expected = _CASES[key]
    for value, want in zip(values, expected):
        monkeypatch.setenv(_env(key), stale)
        _drive_call_site(key, value)
        got = reader(monkeypatch)
        assert got == want, f"agent.{key}={value!r}: live reader gave {got!r}, want {want!r}"

    monkeypatch.setenv(_env(key), stale)
    _drop_fork_call(monkeypatch)
    _drive_call_site(key, values[0])
    got = reader(monkeypatch)
    assert got == stale_expected != expected[0], (
        f"agent.{key}: with the fork call dropped the reader must regress to the stale preset "
        f"{stale_expected!r}, got {got!r}")


def test_restart_breadcrumb_frozen_contract_is_consumed(monkeypatch, tmp_path):
    """Replaces the source-text check on the F2 contract comment. A crumb written to the frozen
    contract (dir ``.restart_initiated``, filename ``sha256(key)[:8]``, JSON ``{session_key, ts,
    boot_id}``), computed here without the gateway's helpers, must be consumed as authoritative."""
    from tests.gateway.restart_test_helpers import make_restart_runner

    runner, _adapter = make_restart_runner()
    session_key = "agent:main:telegram:dm:42"
    crumb_dir = tmp_path / ".restart_initiated"
    crumb_dir.mkdir(mode=0o700)
    crumb = crumb_dir / hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:8]
    import time

    crumb.write_text(json.dumps(
        {"session_key": session_key, "ts": time.time(), "boot_id": runner._current_boot_id()}),
        encoding="utf-8")

    assert runner._consume_restart_initiated_breadcrumb(session_key) is True
    assert not crumb.exists(), "a consumed breadcrumb must be unlinked"
