"""Bound fallback cascades when a route's local Tailscale prerequisite is down.

The gateway process multiplexes many sessions, but Tailscale state belongs to the
host, so the short-lived status cache is deliberately process-global.  Unknown
or ambiguous CLI results fail open: a provider timeout alone must never be
relabeled as a Tailscale outage.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess  # noqa: S404  # nosec B404 -- fixed absolute CLI allowlist
import sys
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_RELAY_PROVIDERS = frozenset({"claude-apr", "claude-bpr"})
_TAILSCALE_STATUS_TIMEOUT_S = 1.0
_TAILSCALE_UP_CACHE_TTL_S = 5.0
_TAILSCALE_DOWN_CACHE_TTL_S = 2.0
_TAILSCALE_UNKNOWN_CACHE_TTL_S = 1.0

@dataclass
class _TailscaleStatusCache:
    expires_at: float = 0.0
    value: Optional[bool] = None
    evidence: str = "not_checked"


_CACHE_LOCK = threading.Lock()
_CACHE = _TailscaleStatusCache()


def route_uses_tailscale(provider: str, base_url: str) -> bool:
    """Return whether a route has a known local Tailscale prerequisite."""
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider in _TAILSCALE_RELAY_PROVIDERS:
        return True

    try:
        host = urlsplit(str(base_url or "")).hostname
        address = ipaddress.ip_address(host or "")
    except (TypeError, ValueError):
        return False
    return isinstance(address, ipaddress.IPv4Address) and address in _TAILSCALE_NETWORK


def _parse_tailscale_status(
    returncode: int, stdout: str, stderr: str
) -> Optional[bool]:
    """Parse an explicit stopped/running signal; return None when ambiguous."""
    if "tailscale is stopped" in str(stderr or "").lower():
        return True

    try:
        payload = json.loads(stdout or "")
    except (TypeError, ValueError):
        # Some CLI versions return the stopped marker on stdout instead of
        # stderr. Only inspect it after JSON parsing fails: peer names live in
        # the status JSON and must not be able to spoof host state.
        if "tailscale is stopped" in str(stdout or "").lower():
            return True
        return None
    if not isinstance(payload, dict):
        return None

    backend_state = str(payload.get("BackendState") or "").strip().lower()
    if backend_state == "stopped":
        return True
    if returncode == 0 and backend_state == "running":
        return False
    return None


def _tailscale_binary() -> Optional[str]:
    candidates = [
        "/usr/bin/tailscale",
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
    ]
    if sys.platform == "darwin":
        candidates.insert(0, "/Applications/Tailscale.app/Contents/MacOS/Tailscale")

    for candidate in candidates:
        try:
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


def _probe_tailscale_status_uncached() -> tuple[Optional[bool], str]:
    binary = _tailscale_binary()
    if not binary:
        return None, "cli_unavailable"

    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603 -- allowlisted binary
            [binary, "status", "--json"],
            capture_output=True,
            check=False,
            text=True,
            timeout=_TAILSCALE_STATUS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None, "cli_timeout"
    except OSError as exc:
        return None, f"cli_error={type(exc).__name__}"

    down = _parse_tailscale_status(
        completed.returncode, completed.stdout, completed.stderr
    )
    if down is True:
        evidence = "backend_state=Stopped"
    elif down is False:
        evidence = "backend_state=Running"
    else:
        evidence = f"cli_indeterminate_rc={completed.returncode}"
    return down, evidence


def tailscale_status_down() -> tuple[Optional[bool], str]:
    """Return cached host Tailscale state as ``(down, evidence)``.

    The lock intentionally covers the bounded one-second probe: this is a rare
    routing preflight and single-flight behavior is preferable to many gateway
    sessions spawning the CLI simultaneously during the same outage.
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        if now < _CACHE.expires_at:
            return _CACHE.value, _CACHE.evidence

        value, evidence = _probe_tailscale_status_uncached()
        checked_at = time.monotonic()
        if value is True:
            ttl = _TAILSCALE_DOWN_CACHE_TTL_S
        elif value is False:
            ttl = _TAILSCALE_UP_CACHE_TTL_S
        else:
            ttl = _TAILSCALE_UNKNOWN_CACHE_TTL_S
        _CACHE.value = value
        _CACHE.evidence = evidence
        _CACHE.expires_at = checked_at + ttl
        return value, evidence


def _reset_cache_for_tests() -> None:
    with _CACHE_LOCK:
        _CACHE.expires_at = 0.0
        _CACHE.value = None
        _CACHE.evidence = "not_checked"


def reset_turn_state(agent) -> None:
    """Start a fresh per-turn outage-reporting episode for a reused agent."""
    agent._shared_transport_affected_routes = set()
    agent._shared_transport_summary_emitted = False
    agent._shared_transport_evidence = "not_checked"


def record_unavailable_route(agent, provider: str, model: str) -> None:
    routes = getattr(agent, "_shared_transport_affected_routes", None)
    if not isinstance(routes, set):
        routes = set()
        agent._shared_transport_affected_routes = routes
    routes.add(f"{provider}/{model}")


def emit_unavailable_summary(agent, *, evidence: str) -> None:
    """Buffer one actionable aggregate and write route/session detail to logs."""
    if getattr(agent, "_shared_transport_summary_emitted", False):
        return
    routes = getattr(agent, "_shared_transport_affected_routes", None)
    if not isinstance(routes, set) or not routes:
        return

    agent._shared_transport_summary_emitted = True
    count = len(routes)
    # Chat gets the terse causal label. Route count, affected routes, session,
    # evidence, and remediation remain available in the diagnostic below.
    agent._buffer_status("⚠️ Tailscale down")
    logger.warning(
        "Shared transport unavailable: transport=tailscale evidence=%s "
        "affected_routes=%d routes=%s session=%s remediation=%s",
        evidence,
        count,
        sorted(routes),
        getattr(agent, "session_id", None),
        "Reconnect Tailscale or use a non-tailnet provider",
    )
