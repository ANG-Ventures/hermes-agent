"""Fail-fast rail preflight for kanban workers that need the operator's own browser or screen.

A spilled kanban worker (``KANBAN_WORKER_PLACEMENT``) runs in the dispatcher's process but its
terminal, and with it the browser placement, lives on a pool host. A loopback CDP endpoint there
is not the operator's signed-in browser: on one pool host a different Brave answers
``127.0.0.1:9222`` with a valid ``/json/version``, so liveness alone is not proof. The placement is
the primary signal; the 2 s ``/json/version`` probe only covers an unplaced worker whose endpoint
is down. The error names the fix (pin the card) instead of surfacing as generic connection errors
20 minutes in (t_db491366).

Only the dispatcher-owned worker is affected: interactive sessions, cron and delegate children
never see these errors. Every emitted error is also recorded so the worker loop can park the
card ``needs_input`` with the message verbatim (``take_rail_absent``).
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from typing import List, Optional

RAIL_ABSENT_SUFFIX = "_RAIL_ABSENT"
_PROBE_TIMEOUT_S = 2.0
_PROBE_OK_TTL_S = 30.0

_lock = threading.Lock()
_emitted: List[str] = []
_probe_ok: dict = {}


def rail_absent_message(rail: str, host: str) -> str:
    """The one sentence a worker sees; ``rail`` is ``browser`` or ``screen``."""
    what = "browser session" if rail == "browser" else "screen session"
    needs = "logged-in browser" if rail == "browser" else "logged-in screen session"
    return (
        f"{rail.upper()}{RAIL_ABSENT_SUFFIX}: no {what} on {host}. This card needs Ace's {needs}, "
        "which lives only on the Mac Studio. Add the line `host: studio` to the card body (or "
        "`requires: browser-session` once the affinity spec lands) and re-run; do not retry here."
    )


def _owned_task() -> str:
    from agent.delegation_context import owned_kanban_task
    return owned_kanban_task()


def _placed_host() -> Optional[str]:
    """Pool host this worker was placed on, or None for a worker on the dispatcher host."""
    from hermes_cli import kanban_worker_hosts as kwh
    return kwh.BOOT_PLACED_HOST or kwh._placed_host(os.environ.get(kwh.PLACEMENT_ENV))


def _endpoint_answers(cdp_url: str) -> bool:
    """``/json/version`` answers within 2 s. A success is cached briefly so a live endpoint
    costs one probe per 30 s, not one per browser command."""
    from agent.proxy_bypass import loopback_request_kwargs
    raw = cdp_url.strip()
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        scheme = "https" if scheme.lower() in ("https", "wss") else "http"
    else:
        scheme, rest = "http", raw
    hostport = rest.split("/", 1)[0]
    version_url = f"{scheme}://{hostport}/json/version"
    now = time.monotonic()
    with _lock:
        if now - _probe_ok.get(version_url, -_PROBE_OK_TTL_S) < _PROBE_OK_TTL_S:
            return True
    try:
        import requests
        resp = requests.get(version_url, timeout=_PROBE_TIMEOUT_S, **loopback_request_kwargs(version_url))
        ok = resp.status_code == 200
    except Exception:
        ok = False
    if ok:
        with _lock:
            _probe_ok[version_url] = now
    return ok


def _emit(message: str) -> str:
    with _lock:
        _emitted.append(message)
    return message


def browser_rail_error(cdp_url: str, *, local_browser: bool) -> Optional[str]:
    """``BROWSER_RAIL_ABSENT`` message, or None to proceed.

    ``cdp_url`` is the configured (unresolved) CDP override; ``local_browser`` is True when no
    override and no cloud provider is set (the agent's own Chromium, placed with the terminal).
    """
    if not _owned_task():
        return None
    from agent.proxy_bypass import is_loopback_host, split_host_port
    loopback = bool(cdp_url) and is_loopback_host(split_host_port(cdp_url)[0])
    if not (loopback or local_browser):
        return None  # cloud provider or an explicit remote CDP endpoint: reachable from anywhere
    placed = _placed_host()
    if placed:
        return _emit(rail_absent_message("browser", placed))
    if loopback and not _endpoint_answers(cdp_url):
        return _emit(rail_absent_message("browser", socket.gethostname()))
    return None


def screen_rail_error() -> Optional[str]:
    """``SCREEN_RAIL_ABSENT`` message for computer_use, or None. The operator's screen is the
    macOS dispatcher host: a placed worker or a non-macOS host has none."""
    if not _owned_task():
        return None
    placed = _placed_host()
    if placed:
        return _emit(rail_absent_message("screen", placed))
    if sys.platform != "darwin":
        return _emit(rail_absent_message("screen", socket.gethostname()))
    return None


def take_rail_absent() -> Optional[str]:
    """Pop the first rail-absent message emitted since the last call (None when there is none)."""
    with _lock:
        if not _emitted:
            return None
        first = _emitted[0]
        _emitted.clear()
        return first
