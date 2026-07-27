from __future__ import annotations

import importlib
import inspect
import subprocess
import sys
from pathlib import Path


_HELPER_NAMES = (
    "_STARTUP_RESTORE_DRAIN_TIMEOUT_SECS_DEFAULT",
    "_restart_loop_threshold",
    "_restart_loop_window_secs",
    "_restart_initiated_ttl_secs",
    "_startup_restore_drain_timeout_secs",
    "_AGENT_CONFIG_ENV_BRIDGE",
    "_bridge_agent_config_to_env",
    "_SAFE_RESTART_INSPECTION_VERBS",
    "_command_invokes_safe_restart",
    "_RESTART_INITIATED_DIRNAME",
    "_restart_initiated_filename",
)


def test_restart_policy_import_is_one_way():
    repo = Path(__file__).resolve().parents[2]
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import gateway.fork_ext.restart_policy; "
            "assert 'gateway.run' not in sys.modules",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_breadcrumb_contract_block_documented_in_source():
    """The F2 contract block is the single documented source of truth."""
    restart_policy = importlib.import_module("gateway.fork_ext.restart_policy")

    assert "F2 BREADCRUMB CONTRACT" in inspect.getsource(restart_policy)


def test_gateway_run_reexports_restart_policy_helpers():
    restart_policy = importlib.import_module("gateway.fork_ext.restart_policy")
    gateway_run = importlib.import_module("gateway.run")

    for name in _HELPER_NAMES:
        assert getattr(gateway_run, name) is getattr(restart_policy, name), name
