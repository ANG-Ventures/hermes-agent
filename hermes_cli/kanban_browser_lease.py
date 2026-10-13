"""Dispatcher half of the Browser Sandboxes lease (spec v1.2 §3.8 steps 2-3 and 6, D17 with pass-3 R3).

Flag-OFF: nothing here runs unless ``fleet/browser-hosts.json`` ``browsers.mode`` is ``on`` AND the
§7 join gate has flipped ``kanban.browser_join`` (card t_42728faf / P1-6 / P1-8 own that flip). The
``needs`` shape, ``lane_of``, the six worker-facing refusal tokens and ``legacy_route`` are
t_ecfd5591's; this module only speaks to ``browserd``.

Per dispatcher tick (R3): ``mint`` at the first lease the tick needs, ``lease`` per card carrying the
token, ``revoke`` as the last step of the tick. The token lives in this object's memory only; it is
never written into any env, file or event (lint ``dispatch_token_in_placement_env``).

Every dispatcher-facing refusal maps to ONE exit shape (R3(c)): the card stays ``ready``, one
``browser_dispatch_refused {token}`` event, one page per token per hour. Never a re-route, never a
legacy fallback.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Callable, Dict, Mapping, Optional

DISPATCHER_FACING = frozenset({
    "BROWSER_DISPATCH_TOKEN_REQUIRED", "BROWSER_DISPATCH_TOKEN_LIVE", "BROWSER_DISPATCH_TOKEN_EXPIRED",
    "BROWSER_DISPATCH_PEER_UNKNOWN", "BROWSER_HOST_UNFIT", "BROWSER_SYSTEM_OFF",
})
REFUSED_EVENT = "browser_dispatch_refused"
PAGE_EVERY_S = 3600

# R1(a): the ONLY names forwarded into a placed shell (named allowlist, never ``*``). The dispatcher's own
# credential is not in it and cannot be added without failing tests/hermes_cli/test_kanban_browser_lease.py.
LEASE_ENV_NAMES = ("BROWSER_LEASE_URL", "BROWSER_LEASE_TOKEN", "BROWSER_LEASE_CLASS",
                   "BROWSER_LEASE_EXPIRES_AT", "BROWSER_RUN_TOKEN")


class BrowserdRefused(Exception):
    def __init__(self, token: str, detail: str = ""):
        super().__init__(f"{token} {detail}".strip())
        self.token, self.detail = token, detail


def _call(sock_path: str, req: dict, timeout: float = 30.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf or b"{}")


class TickLeaseClient:
    """One dispatcher tick's view of browserd. ``revoke()`` must be the tick's last step (call it in ``finally``)."""

    def __init__(self, sock_path: str, tick_id: str, *, call: Callable[[str, dict], dict] = _call):
        self.sock_path, self.tick_id, self._call = sock_path, tick_id, call
        self._token: Optional[str] = None

    def _req(self, verb: str, args: dict, auth: Optional[dict] = None) -> dict:
        resp = self._call(self.sock_path, {"verb": verb, "args": args, "auth": auth or {}})
        if not resp.get("ok"):
            raise BrowserdRefused(str(resp.get("refusal") or "BROWSER_REFUSED"), str(resp.get("detail") or ""))
        return resp

    def _ensure_token(self) -> str:
        if self._token is None:
            self._token = self._req("mint", {"tick_id": self.tick_id})["token"]
        return self._token

    def register_run(self, card: str, run_id: str) -> str:
        return self._req("register-run", {"card": card, "run_id": run_id}, {"dispatch": self._ensure_token()})["run_token"]

    def lease(self, name: str, *, klass: str, card: str, run_id: str, source_host: str) -> dict:
        """§3.8 step 2: lease BEFORE host choice; ``source_host`` is the TOOL host (R1(a))."""
        return self._req("lease", {"name": name, "class": klass, "card": card, "run": run_id,
                                   "source_host": source_host}, {"dispatch": self._ensure_token()})

    def release_any(self, lease_id: int) -> None:
        """§3.8 step 6: release on every run terminal (crash, timeout, reclaim)."""
        self._req("release", {"any": True, "lease_id": lease_id}, {"dispatch": self._ensure_token()})

    def revoke(self) -> None:
        tok, self._token = self._token, None
        if tok is not None:
            self._req("revoke", {}, {"dispatch": tok})


def lease_env(lease: Mapping[str, object], run_token: Optional[str]) -> Dict[str, str]:
    """The worker's ``BROWSER_*`` env (§3.8 step 3) from a lease reply: the allowlist and nothing else."""
    env = {
        "BROWSER_LEASE_URL": str(lease["lease_url"]),
        "BROWSER_LEASE_TOKEN": str(lease["lease_token"]),
        "BROWSER_LEASE_CLASS": str(lease["lease_class"]),
        "BROWSER_LEASE_EXPIRES_AT": str(int(float(lease["lease_expires_at"]))),
    }
    if run_token:
        env["BROWSER_RUN_TOKEN"] = run_token
    assert set(env) <= set(LEASE_ENV_NAMES)
    return env


def refusal_exit(token: str, *, last_paged: Dict[str, float], now: Optional[float] = None) -> dict:
    """R3(c): the one card exit shape for a dispatcher-facing refusal.

    Returns ``{"status": "ready", "event": (kind, payload), "page": bool}``; ``last_paged`` is the
    caller's per-token page clock (one page per token per hour). Anything not in the six is not ours
    to shape: raise, so a worker-facing token is never silently folded into this path.
    """
    if token not in DISPATCHER_FACING:
        raise ValueError(f"{token} is not a dispatcher-facing browserd refusal")
    t = time.time() if now is None else now
    page = t - last_paged.get(token, float("-inf")) >= PAGE_EVERY_S
    if page:
        last_paged[token] = t
    return {"status": "ready", "event": (REFUSED_EVENT, {"token": token}), "page": page, "reroute": False}
