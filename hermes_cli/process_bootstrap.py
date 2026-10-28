"""Child-visible start-path environment shared by the gateway and the cron script runner (spec §5.6, S4).

A cron script child's env is built from its PARENT's ``os.environ`` (``cron/scheduler_script.py``). So every
``os.environ`` write the gateway makes on its start path is visible to every script it runs. The cron
script runner (``cron/script_runner.py``) runs the same scripts and must hand them the same env (I5). This
module is the one place those writes happen; ``gateway/run.py`` and the runner both call it.
``tests/gateway/test_env_write_sites.py`` (AC-17) fails on an ``os.environ`` write anywhere in
``gateway/`` that ``tests/gateway/env_write_allowlist.txt`` does not list with a reason.

Role ``"gateway"`` sets ``_HERMES_GATEWAY=1`` in this process. Role ``"runner"`` does not: gateway-only
code paths branch on that marker in-process. The runner adds it to each script child's env instead
(``apply_child_env``), so the child sees what a gateway-started script sees.

Import cost: this module is imported at the top of ``gateway/run.py``; keep module-level imports light.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, MutableMapping, Optional

from gateway.fork_ext.restart_policy import _AGENT_CONFIG_ENV_BRIDGE
from gateway.media_policy import _ALLOW_DIRS_ENV, _FLAG_ENVS, _TRUST_RECENT_SECONDS_ENV

GATEWAY_MARKER_ENV = "_HERMES_GATEWAY"

# Bridge config.yaml values into env so os.getenv() picks them up. config.yaml unconditionally wins
# over .env for these keys; a `not in os.environ` guard would let stale .env entries shadow config.
_AGENT_ENV_BRIDGE = {
    "gateway_timeout": "HERMES_AGENT_TIMEOUT",
    "gateway_turn_lease_timeout": "HERMES_TURN_LEASE_TIMEOUT",
    "gateway_timeout_warning": "HERMES_AGENT_TIMEOUT_WARNING",
    "gateway_notify_interval": "HERMES_AGENT_NOTIFY_INTERVAL",
    "session_stall_timeout": "HERMES_SESSION_STALL_TIMEOUT",
    "restart_drain_timeout": "HERMES_RESTART_DRAIN_TIMEOUT",
    "cron_drain_timeout": "HERMES_CRON_DRAIN_TIMEOUT",
    "gateway_auto_continue_freshness": "HERMES_AUTO_CONTINUE_FRESHNESS",
    "gateway_startup_restore_drain_timeout": "HERMES_STARTUP_RESTORE_DRAIN_TIMEOUT",
    "gateway_startup_warmup_timeout": "HERMES_STARTUP_WARMUP_TIMEOUT"}
# config-authoritative knobs for the session-search index (env stays the cross-process carrier).
_SESSIONS_ENV_BRIDGE = {"cjk_fts": "HERMES_CJK_FTS", "search_slow_ms": "HERMES_SEARCH_SLOW_MS"}
_DISPLAY_ENV_BRIDGE = {
    "busy_input_mode": "HERMES_GATEWAY_BUSY_INPUT_MODE",
    "busy_text_mode": "HERMES_GATEWAY_BUSY_TEXT_MODE",
    "busy_ack_enabled": "HERMES_GATEWAY_BUSY_ACK_ENABLED"}
_TERMINAL_ENV_MAP = {
    "backend": "TERMINAL_ENV",
    "degraded_mode": "TERMINAL_DEGRADED_MODE",
    "cwd": "TERMINAL_CWD",
    "timeout": "TERMINAL_TIMEOUT",
    "home_mode": "TERMINAL_HOME_MODE",
    "lifetime_seconds": "TERMINAL_LIFETIME_SECONDS",
    "docker_image": "TERMINAL_DOCKER_IMAGE",
    "docker_forward_env": "TERMINAL_DOCKER_FORWARD_ENV",
    "singularity_image": "TERMINAL_SINGULARITY_IMAGE",
    "modal_image": "TERMINAL_MODAL_IMAGE",
    "daytona_image": "TERMINAL_DAYTONA_IMAGE",
    "vercel_runtime": "TERMINAL_VERCEL_RUNTIME",
    "vercel_image": "TERMINAL_VERCEL_IMAGE",
    "ssh_host": "TERMINAL_SSH_HOST",
    "ssh_user": "TERMINAL_SSH_USER",
    "ssh_port": "TERMINAL_SSH_PORT",
    "ssh_key": "TERMINAL_SSH_KEY",
    "container_cpu": "TERMINAL_CONTAINER_CPU",
    "container_memory": "TERMINAL_CONTAINER_MEMORY",
    "container_disk": "TERMINAL_CONTAINER_DISK",
    "container_persistent": "TERMINAL_CONTAINER_PERSISTENT",
    "docker_volumes": "TERMINAL_DOCKER_VOLUMES",
    "docker_env": "TERMINAL_DOCKER_ENV",
    "docker_extra_args": "TERMINAL_DOCKER_EXTRA_ARGS",
    "docker_shm_size": "TERMINAL_DOCKER_SHM_SIZE",
    "docker_mount_cwd_to_workspace": "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE",
    "docker_network": "TERMINAL_DOCKER_NETWORK",
    "docker_run_as_host_user": "TERMINAL_DOCKER_RUN_AS_HOST_USER",
    "docker_snap_compat": "TERMINAL_DOCKER_SNAP_COMPAT",
    "docker_persist_across_processes": "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES",
    "docker_shared_container_key": "TERMINAL_DOCKER_SHARED_CONTAINER_KEY",
    "docker_orphan_reaper": "TERMINAL_DOCKER_ORPHAN_REAPER",
    "sandbox_dir": "TERMINAL_SANDBOX_DIR",
    "persistent_shell": "TERMINAL_PERSISTENT_SHELL"}

# Every NAMED key the start path can put in os.environ (frozen). Three families have no fixed names and
# are not listed: every key of ``<home>/.env`` + the project ``.env``; the top-level config.yaml scalars
# (fallback only); ``AUXILIARY_<TASK>_{PROVIDER,MODEL,BASE_URL,API_KEY}``. ``load_gateway_config()`` adds
# the platform plugins' ``apply_yaml_config_fn`` env (``gateway.platforms._shared.yaml_env_setter``).
GATEWAY_CHILD_VISIBLE_ENV = frozenset({
    GATEWAY_MARKER_ENV, "HERMES_QUIET", "HERMES_EXEC_ASK", "HERMES_MAX_ITERATIONS",
    "HERMES_TIMEZONE", "HERMES_REDACT_SECRETS", "HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED",
    "HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", _TRUST_RECENT_SECONDS_ENV, _ALLOW_DIRS_ENV,
    *(env for _key, env in _FLAG_ENVS),
    *_AGENT_ENV_BRIDGE.values(), *_AGENT_CONFIG_ENV_BRIDGE.values(), *_SESSIONS_ENV_BRIDGE.values(),
    *_DISPLAY_ENV_BRIDGE.values(), *_TERMINAL_ENV_MAP.values(),
    # gateway.config_loader.bridge_core_env_settings (inside load_gateway_config()).
    "GATEWAY_ALLOW_ALL_USERS", "TELEGRAM_REQUIRE_MENTION", "SIGNAL_REQUIRE_MENTION",
})

_ROLE: Optional[str] = None  # set by mark_gateway_process / bootstrap_cron_parent_env
_PROJECT_ENV = Path(__file__).resolve().parents[1] / ".env"


def mark_gateway_process() -> None:
    """Gateway role: ``_HERMES_GATEWAY=1`` in this process (a lazily imported cli.py
    load_cli_config() must not clobber TERMINAL_CWD)."""
    global _ROLE
    _ROLE = "gateway"
    os.environ[GATEWAY_MARKER_ENV] = "1"


def apply_child_env(env: MutableMapping[str, str]) -> None:
    """Runner role only: a script child gets the gateway marker its gateway-started twin inherits."""
    if _ROLE == "runner":
        env[GATEWAY_MARKER_ENV] = "1"


def set_quiet() -> None:
    os.environ["HERMES_QUIET"] = "1"  # gateway runs quiet: no debug output, cwd used directly


def apply_start_env() -> None:
    """``start_gateway``'s prelude: exec approval prompts and the messaging terminal cwd. Set at start,
    not import, so incidental ``gateway.run`` imports from CLI code don't poison it."""
    os.environ["HERMES_EXEC_ASK"] = "1"
    from gateway.cwd_placeholder import CWD_PLACEHOLDERS, resolve_placeholder_terminal_cwd

    configured_cwd = os.environ.get("TERMINAL_CWD", "")
    if not configured_cwd or configured_cwd in CWD_PLACEHOLDERS:
        resolved_cwd = resolve_placeholder_terminal_cwd(
            configured_cwd=configured_cwd,
            terminal_backend=os.environ.get("TERMINAL_ENV", ""),
            messaging_cwd=os.getenv("MESSAGING_CWD"),
            docker_mount_cwd_to_workspace=os.getenv(
                "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "false").lower()
            in {"true", "1", "yes"},
            home_fallback=str(Path.home()))
        if resolved_cwd is None:
            os.environ.pop("TERMINAL_CWD", None)
        else:
            os.environ["TERMINAL_CWD"] = resolved_cwd


def seed_turn_lease_timeout() -> None:
    """Internal bridge, not a config source: seed from the canonical default after dotenv so an ambient
    process/.env value can never control lease safety."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    os.environ["HERMES_TURN_LEASE_TIMEOUT"] = str(DEFAULT_CONFIG["agent"]["gateway_turn_lease_timeout"])


def _bridge_section_to_env(section: Any, mapping: Dict[str, str]) -> None:
    """Export every present ``mapping`` key of a config section as ``str(value)``."""
    if isinstance(section, dict):
        for cfg_key, env_var in mapping.items():
            if cfg_key in section:
                os.environ[env_var] = str(section[cfg_key])


def _bridge_max_turns_to_env(agent_cfg: Any) -> None:
    """Bridge ``agent.max_turns`` preserving its raw spelling ("none", "unlimited", "120"); Python None
    (`null` / bare `key:`) clears a stale bridge instead, since str(None) -> "None" would map to the
    unlimited sentinel rather than "absent = default"."""
    if not isinstance(agent_cfg, dict) or "max_turns" not in agent_cfg:
        return
    raw = agent_cfg["max_turns"]
    if raw is not None:
        os.environ["HERMES_MAX_ITERATIONS"] = str(raw)
    elif "HERMES_MAX_ITERATIONS" in os.environ:
        del os.environ["HERMES_MAX_ITERATIONS"]


def _bridge_terminal_config_to_env(_terminal_cfg: dict) -> None:
    """Bridge nested ``terminal.*`` config to TERMINAL_* env vars (config.yaml overrides .env here)."""
    from hermes_cli.config import _is_ssh_remote_tilde_cwd

    _terminal_backend = str(
        _terminal_cfg.get("backend") or os.environ.get("TERMINAL_ENV") or "").strip().lower()
    for _cfg_key, _env_var in _TERMINAL_ENV_MAP.items():
        if _cfg_key not in _terminal_cfg:
            continue
        _val = _terminal_cfg[_cfg_key]
        if _cfg_key == "cwd":
            # Placeholders (".", "auto", "cwd") resolve to Path.home() later; only explicit paths bridge.
            if str(_val) in {".", "auto", "cwd"}:
                continue
            # Expand "~" for local/container cwd so Popen never gets a literal "~/" (kernel rejects it);
            # SSH cwd is interpreted by the remote shell: keep "~". Predicate shared w/ terminal_tool.
            if isinstance(_val, str) and not _is_ssh_remote_tilde_cwd(_terminal_backend, _val.strip()):
                _val = os.path.expanduser(_val)
        os.environ[_env_var] = json.dumps(_val) if isinstance(_val, (list, dict)) else str(_val)


def _bridge_auxiliary_config_to_env(_auxiliary_cfg: dict) -> None:
    """Bridge auxiliary model/endpoint overrides (vision, approval, plugins); compression reads yaml."""
    _aux_bridged_keys = {"vision", "approval"}
    try:
        from hermes_cli.plugins import get_plugin_auxiliary_tasks
        for _entry in get_plugin_auxiliary_tasks():
            _aux_bridged_keys.add(_entry["key"])
    except Exception:
        pass  # plugin discovery failure must not break startup; built-in bridging stays intact
    for _task_key in _aux_bridged_keys:
        _task_cfg = _auxiliary_cfg.get(_task_key, {})
        if not isinstance(_task_cfg, dict):
            continue
        _upper = _task_key.upper()
        _prov = str(_task_cfg.get("provider", "")).strip()
        if _prov and _prov != "auto":
            os.environ[f"AUXILIARY_{_upper}_PROVIDER"] = _prov
        for _field, _suffix in (("model", "MODEL"), ("base_url", "BASE_URL"), ("api_key", "API_KEY")):
            _value = str(_task_cfg.get(_field, "")).strip()
            if _value:
                os.environ[f"AUXILIARY_{_upper}_{_suffix}"] = _value


def _bridge_config_to_env(_cfg: dict) -> None:
    """Export config.yaml settings to the env vars os.getenv() consumers read."""
    for _key, _val in _cfg.items():  # top-level scalars: fallback only, never override .env
        if isinstance(_val, (str, int, float, bool)) and _key not in os.environ:
            os.environ[_key] = str(_val)
    _terminal_cfg = _cfg.get("terminal", {})
    if _terminal_cfg and isinstance(_terminal_cfg, dict):
        _bridge_terminal_config_to_env(_terminal_cfg)
    _auxiliary_cfg = _cfg.get("auxiliary", {})
    if _auxiliary_cfg and isinstance(_auxiliary_cfg, dict):
        _bridge_auxiliary_config_to_env(_auxiliary_cfg)
    # config.yaml is the documented, authoritative source for these settings — it unconditionally wins over
    # .env values. Previously the guards below read `if X not in os.environ` and let stale .env entries
    # (e.g. HERMES_MAX_ITERATIONS=60 written by an old `hermes setup` run) silently shadow the user's
    # current config. See PR #18413 / the 60-vs-500 max_turns incident.
    _agent_cfg = _cfg.get("agent", {})
    # Fork restart/resume knobs (restart_policy._AGENT_CONFIG_ENV_BRIDGE: resume_flag_stale_clear,
    # resume_interrupted_turns, restart_loop_*, auto_resume_max_attempts, ...) bridge through the
    # same config-wins rule; the funnel's str(None) max_turns is corrected by _bridge_max_turns_to_env.
    from gateway.fork_ext.restart_policy import _bridge_agent_config_to_env
    _bridge_agent_config_to_env(_agent_cfg)
    _bridge_max_turns_to_env(_agent_cfg)
    _bridge_section_to_env(_agent_cfg, _AGENT_ENV_BRIDGE)
    _bridge_section_to_env(_cfg.get("sessions", {}), _SESSIONS_ENV_BRIDGE)
    _display_cfg = _cfg.get("display", {})
    _bridge_section_to_env(_display_cfg, _DISPLAY_ENV_BRIDGE)
    # Documented service-manager override: env wins when set (other display bridges stay config-first).
    if (isinstance(_display_cfg, dict) and "busy_steer_ack_enabled" in _display_cfg
            and "HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED" not in os.environ):
        os.environ["HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED"] = str(_display_cfg["busy_steer_ack_enabled"])
    _tz_cfg = _cfg.get("timezone", "")
    if _tz_cfg and isinstance(_tz_cfg, str):
        os.environ["HERMES_TIMEZONE"] = _tz_cfg.strip()
    _security_cfg = _cfg.get("security", {})
    if isinstance(_security_cfg, dict) and _security_cfg.get("redact_secrets") is not None:
        os.environ["HERMES_REDACT_SECRETS"] = str(_security_cfg["redact_secrets"]).lower()
    # Media policy uses the shared bridge so standalone entrypoints (`hermes cron run`) match.
    _gateway_cfg = _cfg.get("gateway", {})
    if isinstance(_gateway_cfg, dict):
        from gateway.media_policy import apply_media_policy_env
        apply_media_policy_env(_cfg)
        _trust_recent_seconds = _gateway_cfg.get("trust_recent_files_seconds")
        if _trust_recent_seconds is not None:
            os.environ["HERMES_MEDIA_TRUST_RECENT_SECONDS"] = str(_trust_recent_seconds)
        # platform_connect_timeout is an escape hatch, unlike the bridges above: env WINS if already set.
        if ("platform_connect_timeout" in _gateway_cfg
                and not os.environ.get("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "").strip()):
            os.environ["HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT"] = str(_gateway_cfg["platform_connect_timeout"])


def _load_bridge_config(config_path: Path) -> dict:
    """Effective USER config (no defaults) for the presence-sensitive env bridge: only keys the user
    or the managed layer wrote get bridged, else all of DEFAULT_CONFIG would be exported."""
    from hermes_cli.config_effective import load_user_config_effective
    return load_user_config_effective(config_path)


def load_and_bridge_config(config_path: Path) -> dict:
    """Read ``config_path`` and bridge it into os.environ. Returns the config ({} when absent/broken)."""
    if not config_path.exists():
        return {}
    try:
        cfg = _load_bridge_config(config_path)
        _bridge_config_to_env(cfg)
        return cfg
    except Exception as _bridge_err:
        # stderr, not logger: the gateway's module logger is not initialized yet at import time.
        print(
            f"  Warning: config.yaml → env bridge failed: {type(_bridge_err).__name__}: {_bridge_err}",
            file=sys.stderr)
        print(
            "  Gateway will fall back to .env values, which may not match "
            "your current config.yaml. Run `hermes doctor` to investigate.",
            file=sys.stderr)
        return {}


def load_dotenv_files(hermes_home: Path, project_env: Optional[Path] = None) -> None:
    """Load ``<home>/.env`` (then the project ``.env``): user-managed env files must override stale shell
    exports on restart."""
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv(hermes_home=hermes_home, project_env=project_env or _PROJECT_ENV)


def bootstrap_cron_parent_env(config_path: Path, *, role: str,
                              project_env: Optional[Path] = None) -> dict:
    """Every child-visible start-path write, in the gateway's order, for ``role``.

    ``"gateway"``: marker, dotenv, lease seed, config bridge (``gateway/run.py`` import time; the quiet
    flag and ``apply_start_env`` follow at their own call sites). ``"runner"``: the same minus the marker,
    then ``load_gateway_config()`` (the core + plugin env bridges ``GatewayRunner`` runs at start),
    ``apply_start_env`` and the quiet flag. Returns the bridged config.
    """
    global _ROLE
    if role not in ("gateway", "runner"):
        raise ValueError(f"unknown bootstrap role {role!r}")
    if role == "gateway":
        mark_gateway_process()
    else:
        _ROLE = "runner"
    config_path = Path(config_path)
    load_dotenv_files(config_path.parent, project_env)
    seed_turn_lease_timeout()
    cfg = load_and_bridge_config(config_path)
    if role == "runner":
        try:
            from gateway.config import load_gateway_config
            load_gateway_config()
        except Exception as exc:
            print(f"  Warning: gateway config env bridge failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
        apply_start_env()
        set_quiet()
    return cfg
