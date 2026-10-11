"""Fail-fast rail preflight for kanban workers that need the operator's own browser or screen.

A spilled kanban worker (``KANBAN_WORKER_PLACEMENT``) runs in the dispatcher's process, but its
terminal and its browser/desktop placement live on a pool host. A loopback CDP endpoint there is
not the operator's signed-in browser: on one pool host a different Brave answers
``127.0.0.1:9222`` with a valid ``/json/version``, so liveness alone is not proof. Placement is
the primary signal. The 2 s probe only covers an unplaced worker whose loopback endpoint is dead.
The error names the fix (pin the card) instead of surfacing as generic connection errors 20
minutes in (t_db491366).

Scope: only the dispatcher-owned worker (``owned_kanban_task``); interactive sessions, in-process
cron and delegate children never see these errors. A configured LOOPBACK CDP override is the
operator's browser and is refused up front off the dispatcher host. The agent's own Chromium is
not an operator rail: a placed worker keeps it wherever the pool host's sandbox can host it, and
only when that placement fails is the failure reported as this rail (``placed_browser_rail_error``).
The screen rail is the dispatcher host's own screen, so only a placed worker lacks it; a
Linux/Windows dispatcher with its own (Bot Desktop) screen is left alone. Every emitted error is
recorded under the owning task, so that worker's turn loop, and only it, parks the card
``needs_input`` with the message verbatim (``take_rail_absent``).
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Dict, Optional, Tuple

RAIL_ABSENT_SUFFIX = "_RAIL_ABSENT"
_PROBE_TIMEOUT_S = 2.0
_PROBE_OK_TTL_S = 30.0

_lock = threading.Lock()
_emitted: Dict[str, str] = {}  # owning task id -> first unconsumed rail message
_probe_ok: Dict[str, float] = {}


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


def _probe_target(cdp_url: str) -> Tuple[str, object]:
    """What proves the endpoint is up, with ``browser_tool_cdp._resolve_cdp_override``'s
    semantics: ``("tcp", (host, port))`` for a WebSocket URL with a path (full
    ``/devtools/browser/...`` or a proxy route, which need no HTTP discovery), else
    ``("http", <discovery root>/json/version)`` keeping any path prefix."""
    from agent.proxy_bypass import split_host_port
    raw = cdp_url.strip()
    lowered = raw.lower()
    is_ws = lowered.startswith(("ws://", "wss://"))
    if "/devtools/browser/" in lowered or (is_ws and "/" in raw.split("://", 1)[1].rstrip("/")):
        host, port = split_host_port(raw)
        return "tcp", (host, port or (443 if lowered.startswith(("wss://", "https://")) else 80))
    if is_ws:
        discovery = ("https://" if lowered.startswith("wss://") else "http://") + raw.split("://", 1)[1]
    else:
        discovery = raw if "://" in raw else "http://" + raw
    if discovery.lower().endswith("/json/version"):
        return "http", discovery
    return "http", discovery.rstrip("/") + "/json/version"


def _endpoint_answers(cdp_url: str) -> bool:
    """The endpoint answers within 2 s. A success is cached briefly, so a live endpoint costs
    one probe per 30 s, not one per browser command."""
    kind, target = _probe_target(cdp_url)
    key = f"{kind}:{target}"
    now = time.monotonic()
    with _lock:
        if now - _probe_ok.get(key, -_PROBE_OK_TTL_S) < _PROBE_OK_TTL_S:
            return True
    try:
        if kind == "tcp":
            with socket.create_connection(target, timeout=_PROBE_TIMEOUT_S):  # type: ignore[arg-type]
                ok = True
        else:
            import requests
            from agent.proxy_bypass import loopback_request_kwargs
            ok = requests.get(target, timeout=_PROBE_TIMEOUT_S,
                              **loopback_request_kwargs(str(target))).status_code == 200
    except Exception:
        ok = False
    if ok:
        with _lock:
            _probe_ok[key] = now
    return ok


def _emit(task: str, message: str) -> str:
    with _lock:
        _emitted.setdefault(task, message)
    return message


def browser_rail_error(cdp_url: str) -> Optional[str]:
    """``BROWSER_RAIL_ABSENT`` for a configured loopback CDP override (the operator's browser),
    or None to proceed. ``cdp_url`` is the configured, unresolved override."""
    task = _owned_task()
    if not task or not cdp_url:
        return None
    from agent.proxy_bypass import is_loopback_host, split_host_port
    if not is_loopback_host(split_host_port(cdp_url)[0]):
        return None  # a remote CDP endpoint is reachable from any host
    placed = _placed_host()
    if placed:
        return _emit(task, rail_absent_message("browser", placed))
    if not _endpoint_answers(cdp_url):
        return _emit(task, rail_absent_message("browser", socket.gethostname()))
    return None


def placed_browser_rail_error() -> Optional[str]:
    """The agent's own browser could not be placed on this worker's pool host (no desktop stack
    in its sandbox): report it as the browser rail, since the only browser left is the Studio's.
    None for anything but a placed, dispatcher-owned worker."""
    task = _owned_task()
    placed = _placed_host() if task else None
    return _emit(task, rail_absent_message("browser", placed)) if placed else None


def screen_rail_error() -> Optional[str]:
    """``SCREEN_RAIL_ABSENT`` for computer_use, or None. A placed worker's desktop would be the
    pool host's, never the operator's screen; an unplaced worker uses its own host's screen."""
    task = _owned_task()
    placed = _placed_host() if task else None
    return _emit(task, rail_absent_message("screen", placed)) if placed else None


def take_rail_absent() -> Optional[str]:
    """Pop the pending rail message of the task THIS execution owns (None otherwise). Delegate
    children, in-process cron and interactive turns own no task and never drain it."""
    task = _owned_task()
    if not task:
        return None
    with _lock:
        return _emitted.pop(task, None)
