"""Fork-owned gateway restart policy helpers."""

from __future__ import annotations

import os
from typing import Any

# Default bound for how long ``_finish_startup_restore`` waits on boot
# auto-resume turns before releasing the inbound gate (see
# ``_startup_restore_drain_timeout_secs``). 30s is comfortably longer than a
# normal resume turn's first response yet short enough that one pathologically
# long resumed turn can't hold every channel's inbound queued for minutes.
# Override via ``config.yaml`` ``agent.gateway_startup_restore_drain_timeout``.
_STARTUP_RESTORE_DRAIN_TIMEOUT_SECS_DEFAULT = 30.0


def _restart_loop_threshold() -> int:
    raw = os.environ.get("HERMES_RESTART_LOOP_THRESHOLD")
    try:
        value = int(raw) if raw not in (None, "") else 3
    except (TypeError, ValueError):
        value = 3
    return max(1, min(value, 100))


def _restart_loop_window_secs() -> float:
    raw = os.environ.get("HERMES_RESTART_LOOP_WINDOW_SECS")
    try:
        value = float(raw) if raw not in (None, "") else 300.0
    except (TypeError, ValueError):
        value = 300.0
    return max(1.0, min(value, 86400.0))


def _restart_initiated_ttl_secs() -> float:
    """Freshness backstop for an F2 restart-initiator breadcrumb (D-5/I-5).

    The boot_id check (I-4) is the PRIMARY guard against a stale breadcrumb
    marking a later turn; this TTL is only a janitor for orphaned crumbs whose
    boot somehow matches but that were never consumed. Floor is generous enough
    to never make a same-boot in-turn write→gate latency self-discard.
    """
    raw = os.environ.get("HERMES_RESTART_INITIATED_TTL_SECS")
    try:
        value = float(raw) if raw not in (None, "") else 600.0
    except (TypeError, ValueError):
        value = 600.0
    return max(60.0, min(value, 86400.0))


def _startup_restore_drain_timeout_secs() -> float:
    """Maximum lifetime of the global startup-restore inbound gate.

    While startup restore is in progress the gateway QUEUES every inbound
    message (``_queue_startup_restore_event``) instead of processing it, so no
    channel gets a reply until the gate opens. The watchdog starts when the gate
    is armed, covering platform connection, resume scheduling, and the bounded
    wait in ``_finish_startup_restore``. Queue replay runs under a separate
    background owner only after the gate opens.

    Duplicate-agent safety does NOT depend on the gate:
    ``_schedule_resume_pending_sessions`` claims each session's
    ``_running_agents`` slot SYNCHRONOUSLY before replay, so a
    message drained while a resume turn is still running queues behind that slot
    rather than spawning a second agent.

    Reads ``HERMES_STARTUP_RESTORE_DRAIN_TIMEOUT`` (bridged from ``config.yaml``
    ``agent.gateway_startup_restore_drain_timeout`` at gateway startup, same
    pattern as the other agent.* knobs). Non-positive disables both the absolute
    watchdog and the finish wait bound, intentionally restoring the pre-fix
    "wait forever" behavior.
    """
    raw = os.environ.get("HERMES_STARTUP_RESTORE_DRAIN_TIMEOUT")
    if raw is None or raw == "":
        return float(_STARTUP_RESTORE_DRAIN_TIMEOUT_SECS_DEFAULT)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(_STARTUP_RESTORE_DRAIN_TIMEOUT_SECS_DEFAULT)


# Map of agent.* config keys → the HERMES_* env var the gateway reads them
# through. config.yaml is the authoritative, documented surface and
# UNCONDITIONALLY wins over a pre-set env var (PR #18413 — a stale .env must
# never shadow current config). Bridged at gateway startup (the startup block in
# ``gateway.run``) via _bridge_agent_config_to_env so the mapping is
# single-sourced and unit-testable. New agent timeout/threshold knobs go here,
# not inline.
_AGENT_CONFIG_ENV_BRIDGE: dict[str, str] = {
    "max_turns": "HERMES_MAX_ITERATIONS",
    "gateway_timeout": "HERMES_AGENT_TIMEOUT",
    "gateway_timeout_warning": "HERMES_AGENT_TIMEOUT_WARNING",
    "gateway_notify_interval": "HERMES_AGENT_NOTIFY_INTERVAL",
    "restart_drain_timeout": "HERMES_RESTART_DRAIN_TIMEOUT",
    "gateway_auto_continue_freshness": "HERMES_AUTO_CONTINUE_FRESHNESS",
    "resume_flag_stale_clear": "HERMES_RESUME_FLAG_STALE_CLEAR",
    "resume_interrupted_turns": "HERMES_RESUME_INTERRUPTED_TURNS",
    "gateway_startup_restore_drain_timeout": "HERMES_STARTUP_RESTORE_DRAIN_TIMEOUT",
    "restart_loop_threshold": "HERMES_RESTART_LOOP_THRESHOLD",
    "restart_loop_window_secs": "HERMES_RESTART_LOOP_WINDOW_SECS",
    "restart_initiated_ttl_secs": "HERMES_RESTART_INITIATED_TTL_SECS",
}


def _bridge_agent_config_to_env(agent_cfg: Any) -> None:
    """Bridge agent.* config values into their HERMES_* env vars (config wins).

    config.yaml is authoritative: a present config key OVERWRITES any pre-set
    env var (PR #18413). A key absent from config leaves the env var untouched,
    so an operator env survives until config is set. Single-sourced via
    _AGENT_CONFIG_ENV_BRIDGE; no-op on a non-dict.
    """
    if not isinstance(agent_cfg, dict):
        return
    for _cfg_key, _env_var in _AGENT_CONFIG_ENV_BRIDGE.items():
        if _cfg_key in agent_cfg:
            os.environ[_env_var] = str(agent_cfg[_cfg_key])


# Inspection verbs that READ the safe-restart script without EXECUTING it — a
# `terminal` command starting with one of these mentions the path but does not
# initiate a restart, so it must NOT trip the F2 self-loop detector (C1).
_SAFE_RESTART_INSPECTION_VERBS = (
    "cat", "grep", "egrep", "rg", "less", "more", "vim", "vi", "nano",
    "head", "tail", "echo", "ls", "stat", "wc", "diff", "bat", "view",
)


def _command_invokes_safe_restart(cmd: str) -> bool:
    """True only when a terminal command actually INVOKES the safe-restart skill
    script (not merely reads/mentions it).

    The safe-gateway-restart skill runs ``<python> .../safe-restart.py ...`` — so
    we require the literal ``safe-restart.py`` AND that the command isn't a bare
    inspection of the file (``cat safe-restart.py``, ``grep x safe-restart.py``,
    ``vim safe-restart.py`` …). This kills the false-positive where the agent
    merely reads the script in an unrelated turn (which, combined with drain
    interrupts, could otherwise contribute a spurious replay-mark). False
    negatives (renamed/aliased/wrapped invocation) are out of scope — the skill's
    literal invocation shape is a contract (see the call site).
    """
    if not cmd or "safe-restart.py" not in cmd:
        return False
    # Tokenize loosely; the first meaningful token decides intent. A pipeline or
    # &&-chain that contains an execution elsewhere still counts (we scan segments).
    import shlex
    for segment in cmd.replace("&&", "\n").replace("|", "\n").replace(";", "\n").splitlines():
        seg = segment.strip()
        if "safe-restart.py" not in seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        if not tokens:
            continue
        first = tokens[0].rsplit("/", 1)[-1]  # basename of argv0
        if first in _SAFE_RESTART_INSPECTION_VERBS:
            continue  # this segment only reads the file
        # any non-inspection segment that names the script = an execution
        return True
    return False


# ─── F2 BREADCRUMB CONTRACT (shared with safe-restart.py — keep byte-identical) ───
# The restart-initiator breadcrumb is a cross-repo file contract between this
# gateway (consumer) and the safe-gateway-restart skill's safe-restart.py
# (producer, in the hermes-home repo). The two cannot share a Python module (the
# skill is pure-stdlib standalone), so this block is the SINGLE documented source
# of truth and each repo has its own always-on conformance test against these
# frozen facts (gateway: test_gateway_breadcrumb_contract_matches_frozen; skill:
# the mirror in test_watcher.py):
#   • directory:  $HERMES_HOME/.restart_initiated/        (_RESTART_INITIATED_DIRNAME)
#   • filename:   sha256(session_key.encode("utf-8")).hexdigest()[:8]   (per-session)
#   • file JSON:  {"session_key": str, "ts": float, "boot_id": str}
#   • boot_id:    "{pid}:{psutil_create_time}", produced by the gateway ONLY
#                 (gateway/status.py:_compute_boot_id, persisted in gateway_state.json);
#                 the script copies the string verbatim, never recomputes it.
# A change to any of these MUST land in both repos together or a conformance test
# reddens. Full mechanism: spec 2026-06-22_f2-initiator-detection-authoritative-breadcrumb.md.
_RESTART_INITIATED_DIRNAME = ".restart_initiated"


def _restart_initiated_filename(session_key: str) -> str:
    """Per-session breadcrumb filename = sha8 of the session key.

    Per-session files mean the writer (script) and the consumer (gateway) never
    touch the same inode → no read-modify-write race / lost-update (a shared
    list would have one). The full key is also stored INSIDE the file and must
    hash back to this name (anti-forgery, I-8).
    """
    import hashlib

    return hashlib.sha256((session_key or "").encode("utf-8")).hexdigest()[:8]
