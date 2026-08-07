from __future__ import annotations

import os
from contextlib import contextmanager


def _restart_policy_module():
    try:
        import gateway.fork_ext.restart_policy as restart_policy
    except ModuleNotFoundError:
        import gateway.run as restart_policy
    return restart_policy


_ENV_BY_KIND = {
    "restart_loop_threshold": "HERMES_RESTART_LOOP_THRESHOLD",
    "restart_loop_window_secs": "HERMES_RESTART_LOOP_WINDOW_SECS",
    "restart_initiated_ttl_secs": "HERMES_RESTART_INITIATED_TTL_SECS",
    "startup_restore_drain_timeout_secs": "HERMES_STARTUP_RESTORE_DRAIN_TIMEOUT",
}


@contextmanager
def _patched_env(values: dict[str, object], clear: set[str]):
    touched = set(clear) | set(values)
    original = {key: os.environ.get(key) for key in touched}
    present = {key for key in touched if key in os.environ}
    try:
        for key in clear:
            os.environ.pop(key, None)
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key in touched:
            if key in present:
                os.environ[key] = original[key]  # type: ignore[index]
            else:
                os.environ.pop(key, None)


def run_case(case: dict):
    restart_policy = _restart_policy_module()
    kind = case["kind"]
    env_output = {}

    if kind in _ENV_BY_KIND:
        env_var = _ENV_BY_KIND[kind]
        with _patched_env({env_var: case.get("value")}, {env_var}):
            value = getattr(restart_policy, f"_{kind}")()
    elif kind == "bridge_agent_config_to_env":
        mapping = restart_policy._AGENT_CONFIG_ENV_BRIDGE
        output_vars = set(mapping.values())
        with _patched_env(case.get("pre_env") or {}, output_vars):
            restart_policy._bridge_agent_config_to_env(case.get("agent_cfg"))
            env_output = {key: os.environ.get(key) for key in sorted(output_vars)}
        value = None
    elif kind == "command_invokes_safe_restart":
        if case.get("force_empty_tokens"):
            import shlex

            original_split = shlex.split
            try:
                shlex.split = lambda _value: []
                value = restart_policy._command_invokes_safe_restart(case.get("command") or "")
            finally:
                shlex.split = original_split
        else:
            value = restart_policy._command_invokes_safe_restart(case.get("command") or "")
    elif kind == "restart_initiated_filename":
        value = restart_policy._restart_initiated_filename(case.get("session_key") or "")
    else:
        raise AssertionError(f"unknown restart_policy case kind: {kind!r}")

    return {"return": value, "env": env_output, "messages": [], "db": []}
