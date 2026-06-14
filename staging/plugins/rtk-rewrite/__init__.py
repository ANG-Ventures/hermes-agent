"""Hermes plugin adapter for RTK command rewriting.

All rewrite logic lives in RTK's Rust ``rtk rewrite`` command; this module
only bridges Hermes ``pre_tool_call`` payloads to that command and fails open.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any

import yaml


ACCEPTED_REWRITE_RETURN_CODES = {0, 3}
EXPECTED_PASSTHROUGH_RETURN_CODES = {1, 2}
_FENCED_COMMANDS = {"docker", "kubectl"}
_QUOTED_OP_REF_RE = re.compile(r"(['\"])op://.*?\1")
_OP_REF_RE = re.compile(r"op://[^\s'\"`]+")
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+/=-]{10,})")
_SENSITIVE_FLAG_RE = re.compile(
    r"(?i)(--(?:api-?key|token|secret|password|auth)(?:=|\s+))([^\s'\"]+)"
)

logger = logging.getLogger(__name__)

_rtk_available = None
_rtk_missing_warned = False


def register(ctx):
    """Register the Hermes pre-tool callback."""
    if not _check_rtk():
        return

    ctx.register_hook("pre_tool_call", _pre_tool_call)


def _check_rtk():
    """Return whether the rtk binary is in PATH, warning once when missing."""
    global _rtk_available, _rtk_missing_warned

    if _rtk_available is None:
        _rtk_available = shutil.which("rtk") is not None

    if not _rtk_available and not _rtk_missing_warned:
        _warn("rtk binary not found in PATH; Hermes hook not registered")
        _rtk_missing_warned = True

    return _rtk_available


def _pre_tool_call(tool_name=None, args=None, **kwargs):
    """Rewrite mutable Hermes terminal command args when RTK provides a change."""
    try:
        if tool_name != "terminal" or not isinstance(args, dict):
            return

        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return

        try:
            result = subprocess.run(
                ["rtk", "rewrite", command],
                shell=False,
                timeout=2,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            _warn("rtk rewrite timed out")
            _emit_observability(args, _event(command, "passthrough", None, command, kwargs, "timeout"))
            return

        rewritten = result.stdout.strip()
        if result.returncode not in ACCEPTED_REWRITE_RETURN_CODES:
            if result.returncode not in EXPECTED_PASSTHROUGH_RETURN_CODES:
                details = f"rtk rewrite failed with exit {result.returncode}"
                stderr = result.stderr.strip()
                if stderr:
                    details = f"{details}: {stderr}"
                _warn(details)
            outcome = "fenced" if _is_fenced_command(command) else "passthrough"
            _emit_observability(args, _event(command, outcome, result.returncode, command, kwargs))
            return

        outcome = "passthrough"
        output_command = command
        if rewritten and rewritten != command:
            args["command"] = rewritten
            outcome = "rewritten"
            output_command = rewritten

        _emit_observability(args, _event(command, outcome, result.returncode, output_command, kwargs))
    except Exception as e:
        _warn(str(e))
        return


def _event(
    command: str,
    outcome: str,
    rc: int | None,
    output_command: str,
    hook_kwargs: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "source": "rtk",
        "cmd": _scrub_command(command),
        "outcome": outcome,
        "rc": rc,
        "input_bytes": len(command.encode("utf-8")),
        "output_bytes": len(output_command.encode("utf-8")),
    }
    tool_call_id = hook_kwargs.get("tool_call_id")
    if tool_call_id:
        event["tool_call_id"] = str(tool_call_id)
    if error:
        event["error"] = error
    return event


def _emit_observability(args: dict[str, Any], event: dict[str, Any]) -> None:
    _emit_debug_log(event)
    _emit_blackbox_event(args, event)


def _emit_debug_log(event: dict[str, Any]) -> None:
    if not _compression_debug_enabled():
        return
    try:
        logger.debug(
            "rtk_rewrite_command %s",
            json.dumps(event, sort_keys=True, separators=(",", ":")),
        )
    except Exception:
        return


def _emit_blackbox_event(args: dict[str, Any], event: dict[str, Any]) -> None:
    try:
        from plugins.blackbox import emit_compression_tool_event

        emit_compression_tool_event(args, event)
    except Exception:
        return


def _compression_debug_enabled() -> bool:
    env = os.getenv("HERMES_COMPRESSION_DEBUG_LOG")
    if env is not None:
        return _truthy(env)
    try:
        path = _hermes_home() / "config.yaml"
        if not path.exists():
            return False
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        compression = data.get("compression")
        if not isinstance(compression, dict):
            return False
        return _truthy(compression.get("debug_log"))
    except Exception:
        return False


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _scrub_command(command: str) -> str:
    try:
        from agent.redact import redact_sensitive_text

        scrubbed = redact_sensitive_text(command, force=True)
    except Exception:
        scrubbed = str(command)
    scrubbed = _QUOTED_OP_REF_RE.sub(lambda m: m.group(1) + "op://***" + m.group(1), scrubbed)
    scrubbed = _OP_REF_RE.sub("op://***", scrubbed)
    scrubbed = _BEARER_RE.sub(lambda m: m.group(1) + "***", scrubbed)
    scrubbed = _SENSITIVE_FLAG_RE.sub(lambda m: m.group(1) + "***", scrubbed)
    return scrubbed


def _command_name(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.strip().split()
    return parts[0] if parts else ""


def _is_fenced_command(command: str) -> bool:
    return _command_name(command) in _FENCED_COMMANDS


def _warn(message):
    print(f"rtk: hermes plugin warning: {message}", file=sys.stderr)
