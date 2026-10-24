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
operator's browser: the browser_* tools (whose agent-browser runs on the pool host under
placement) are refused up front off the dispatcher host, while ``browser_cdp`` (which connects from
the dispatcher process itself) only gets the liveness probe. The agent's own Chromium and the
computer_use desktop are not operator rails: a placed worker keeps both wherever the pool host can
host them, and only when that placement fails is the failure reported as this rail
(``placed_browser_rail_error`` / ``screen_rail_error``). Exception, Layer 1b (t_f813a42f): while
``browser-session`` is ENFORCED, a browser_* call on a placed host that lacks it is refused up
front (``browser_capability_error``), so the card re-routes to the Studio instead of failing
slowly in a pool host's CDP. Only a rail error returned by an active
model tool call is recorded (background cleanup, ``background_cleanup``, never is), under the owning task, so that worker's
turn loop, and only it, parks the card ``needs_input`` with the message verbatim
(``take_rail_absent``).
"""

from __future__ import annotations

import os
import socket
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Dict, Iterator, Optional, Tuple

RAIL_ABSENT_SUFFIX = "_RAIL_ABSENT"
_PROBE_TIMEOUT_S = 2.0
_PROBE_OK_TTL_S = 30.0

_lock = threading.Lock()
_emitted: Dict[str, str] = {}  # owning task id -> first unconsumed rail message
_probe_ok: Dict[str, float] = {}
_in_cleanup: ContextVar[bool] = ContextVar("rail_preflight_in_cleanup", default=False)


@contextmanager
def background_cleanup() -> Iterator[None]:
    """Mark browser session teardown (janitor / ``close``): a rail failure inside it is returned to
    the caller but never recorded, so it cannot park the card. Every other browser command runs on
    behalf of a model tool call (browser_*, vault, browser-use) and records its rail failure however
    that tool's adapter reformats or drops the error."""
    token = _in_cleanup.set(True)
    try:
        yield
    finally:
        _in_cleanup.reset(token)


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
    """The card this context may act for. A background-review fork runs in the worker's
    process with the parent's task, but its reads are housekeeping, not the card's work:
    it never queues (or drains) a rail/ABSENT line for the parent run (t_ecfd5591)."""
    from tools.skill_provenance import is_background_review
    if is_background_review():
        return ""
    from agent.delegation_context import owned_kanban_task
    return owned_kanban_task()


def _placed_host() -> Optional[str]:
    """Pool host this worker was placed on, or None for a worker on the dispatcher host."""
    from hermes_cli import kanban_worker_hosts as kwh
    return kwh.BOOT_PLACED_HOST or kwh._placed_host(os.environ.get(kwh.PLACEMENT_ENV))


def _probe_target(cdp_url: str) -> Tuple[str, object]:
    """What proves the endpoint is up, using the resolver's own discovery decision
    (``browser_tool_cdp.cdp_version_url``): ``("http", <discovery>/json/version)`` when the resolver
    would discover, else ``("tcp", (host, port))`` for an endpoint it passes through unchanged."""
    from agent.proxy_bypass import split_host_port
    from tools.browser_tool_cdp import cdp_version_url
    raw = cdp_url.strip()
    version_url = cdp_version_url(raw if "://" in raw else "http://" + raw)
    if version_url is not None:
        return "http", version_url
    host, port = split_host_port(raw)
    return "tcp", (host, port or (443 if raw.lower().startswith(("wss://", "https://")) else 80))


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


_RAIL_CAP = {"BROWSER": "browser-session", "SCREEN": "screen"}


def _emit(task: str, message: str) -> str:
    """Record ``message`` for the owning worker's turn loop to act on (park or re-route).

    ``KANBAN_PLACEMENT_CAPS`` (F5, t_ecfd5591): for a PLACED worker (spawned onto a pool
    host) whose rail capability is not ``enforce``, the tool call is still refused (the
    operator's browser/screen is not on this host either way) but nothing is recorded, so
    a warn/off capability never blocks or re-routes the card. An unplaced worker always
    records and parks, as in #1876: the dispatcher sets the caps env on every spawn (``off``
    when the capability layer is off), so the env alone must not switch #1876 off
    (#1963 Prism r2).
    """
    from hermes_cli import kanban_placement_failfast as pff
    from hermes_cli import kanban_worker_hosts as kwh
    placed = kwh.BOOT_PLACED_HOST or kwh._placed_host(os.environ.get(kwh.PLACEMENT_ENV))
    modes = pff.caps_modes() if kwh.is_pool_host(placed) else {}
    cap = _RAIL_CAP.get(message.split("_", 1)[0])
    if modes and cap and not pff.enforced(cap, modes):
        pff.note_gate_warn(cap, message, modes)
        return message
    with _lock:
        _emitted.setdefault(task, message)
    return message


def record_absent(message: str) -> Optional[str]:
    """Record a ``<CAP>_ABSENT`` line from another gate (the mid-run skill_view gate) for the
    owning worker's turn loop; None outside a dispatcher-owned worker."""
    task = _owned_task()
    if not task:
        return None
    with _lock:
        _emitted.setdefault(task, message)
    return message


def browser_rail_error(cdp_url: str, *, host_side: bool = False) -> Optional[str]:
    """``BROWSER_RAIL_ABSENT`` for a configured loopback CDP override (the operator's browser),
    or None to proceed. ``cdp_url`` is the configured, unresolved override. ``host_side``: the
    caller connects from this (dispatcher) process, not from the pool host, so placement does
    not move the endpoint and only liveness counts."""
    task = _owned_task()
    if not task or not cdp_url:
        return None
    from agent.proxy_bypass import is_loopback_host, split_host_port
    if not is_loopback_host(split_host_port(cdp_url)[0]):
        return None  # a remote CDP endpoint is reachable from any host
    placed = None if host_side else _placed_host()
    if placed:
        return _emit(task, rail_absent_message("browser", placed))
    if not _endpoint_answers(cdp_url):
        return _emit(task, rail_absent_message("browser", socket.gethostname()))
    return None


def browser_capability_error() -> Optional[str]:
    """Layer 1b (D1b-undeclared, t_f813a42f): a browser_* call from a placed worker whose host
    lacks an ENFORCED ``browser-session`` gets the placement ABSENT line at once, recorded like
    the mid-run skill_view gate, whether or not the card declared the need. Without it the call
    passes every declared-need gate and fails slowly inside CDP on the pool host."""
    from hermes_cli.kanban_placement_failfast import capability_gate
    absent = capability_gate("browser-session")
    if absent is not None:
        record_absent(absent)
    return absent


def placed_browser_rail_error() -> Optional[str]:
    """The agent's own browser could not be placed on this worker's pool host (no desktop stack
    in its sandbox): report it as the browser rail, since the only browser left is the Studio's.
    None for anything but a placed, dispatcher-owned worker. Never recorded for parking from a
    background cleanup ``close`` (``background_cleanup``)."""
    task = _owned_task()
    placed = _placed_host() if task else None
    if not placed:
        return None
    message = rail_absent_message("browser", placed)
    return message if _in_cleanup.get() else _emit(task, message)


def screen_rail_error() -> Optional[str]:
    """``SCREEN_RAIL_ABSENT`` for a placed worker whose computer_use desktop could not be brought up
    on its pool host, or None. Call it only after that failure: a working pool-host desktop (Bot
    Desktop in the ssh sandbox) is the worker's own and stays usable."""
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
