"""What a claude-pool relay says about one model's capacity (Ace 2026-11-13 20:50 PT).

The wait notice said ``⏳ 12 sessions waiting on claude-dtlr/claude-fable-5-2 (oldest
2429s); this turn 120s, cut at 600s`` while the Fable weekly allowance was used up on
23 of 25 seats. It read like a normal queue, and the harness sat out the full 600 s
budget on a model the relay could not serve, then "recovered" back to it next turn.

This module reads the relay's own answer, on loopback only:

* ``GET /health`` -> ``fable_allowance {exhausted, of, earliest_reset}`` (Fable models);
* ``GET /eligibility?model=<m>`` -> ``eligible_count`` / ``model_eligible``.

One read per (relay, model) per :data:`CACHE_TTL_S`, shared by every session on the
process (12 waiting sessions must not mean 12 probes per poll); callers that arrive
while that read is in flight wait for it instead of starting their own. Redirects are
never followed (the loopback check covers only the URL built here). Any failure is
``None``: unknown is never read as "no capacity".
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

PROBE_TIMEOUT_S = 0.3
CACHE_TTL_S = 20.0
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_LOCK = threading.Lock()
#: (origin, model) -> (fetched_at_monotonic, capacity dict or None)
_CACHE: dict = {}
#: (origin, model) -> Event set when the one in-flight probe for that key has stored
#: its answer; concurrent callers wait on it instead of probing too.
_INFLIGHT: dict = {}
#: Longest a caller waits on another caller's probe (two GETs at PROBE_TIMEOUT_S).
_INFLIGHT_WAIT_S = 2 * PROBE_TIMEOUT_S + 0.5


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed: the loopback check covers only the URL we built,
    so a 3xx to another host (e.g. a metadata endpoint) would escape it. A refused
    redirect surfaces as an HTTPError, i.e. unknown capacity."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _http_get_json(url: str, timeout: float) -> Optional[dict]:
    try:
        # Loopback only: never through an env proxy, never across a redirect.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirect())
        with opener.open(url, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


#: Seam (tests): ``(url, timeout) -> dict | None``.
fetch_json: Callable[[str, float], Optional[dict]] = _http_get_json


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def is_fable(model: Any) -> bool:
    return "fable" in str(model or "").lower()


def _origin(base_url: Any) -> Optional[str]:
    try:
        u = urlsplit(str(base_url or ""))
        if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in _LOOPBACK_HOSTS:
            return None
        return f"{u.scheme}://{u.netloc}"
    except Exception:
        return None


def _int(v: Any) -> Optional[int]:
    return v if type(v) is int and v >= 0 else None


def _read(origin: str, model: str) -> Optional[dict]:
    cap: dict = {"model": model, "exhausted": None, "of": None,
                 "earliest_reset": None, "eligible": None}
    if is_fable(model):
        health = fetch_json(f"{origin}/health", PROBE_TIMEOUT_S) or {}
        fa = health.get("fable_allowance")
        if isinstance(fa, dict):
            n, m = _int(fa.get("exhausted")), _int(fa.get("of"))
            if n is not None and m and n <= m:
                cap["exhausted"], cap["of"] = n, m
                reset = fa.get("earliest_reset")
                cap["earliest_reset"] = reset if isinstance(reset, str) and reset else None
    elig = fetch_json(f"{origin}/eligibility?" + urllib.parse.urlencode({"model": model}),
                      PROBE_TIMEOUT_S)
    if isinstance(elig, dict) and elig.get("model") in (None, model):
        count = _int(elig.get("eligible_count"))
        if count is None and isinstance(elig.get("eligible"), list):
            count = len(elig["eligible"])
        if count is None and elig.get("model_eligible") is False:
            count = 0
        cap["eligible"] = count
    if cap["of"] is None and cap["eligible"] is None:
        return None
    return cap


def model_capacity(base_url: Any, model: Any, *, now: Optional[float] = None) -> Optional[dict]:
    """``{model, exhausted, of, earliest_reset, eligible}`` for ``model`` on the relay at
    ``base_url`` (any field may be None), else None (not a loopback relay, unreachable,
    nothing stated). Cached per (relay, model) for :data:`CACHE_TTL_S`."""
    model = str(model or "").strip()
    origin = _origin(base_url)
    if not model or origin is None:
        return None
    now = time.monotonic() if now is None else now
    key = (origin, model)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL_S:
            return hit[1]
        done = _INFLIGHT.get(key)
        leader = done is None
        if leader:
            done = _INFLIGHT[key] = threading.Event()
    if not leader:
        # Another caller is probing this key: share its answer (unknown if it is late).
        done.wait(_INFLIGHT_WAIT_S)
        with _LOCK:
            hit = _CACHE.get(key)
        return hit[1] if hit is not None and hit[0] >= now - CACHE_TTL_S else None
    cap = None
    try:
        cap = _read(origin, model)
    finally:
        with _LOCK:
            _CACHE[key] = (now, cap)
            if _INFLIGHT.get(key) is done:
                del _INFLIGHT[key]
        done.set()
    return cap


def exhausted(cap: Optional[dict]) -> bool:
    """The relay says it cannot serve the model: every Fable seat's allowance is used
    up, or no seat is eligible for it. Unknown is never exhausted."""
    if not isinstance(cap, dict):
        return False
    n, m = cap.get("exhausted"), cap.get("of")
    if type(n) is int and type(m) is int and m > 0 and n >= m:
        return True
    return cap.get("eligible") == 0


def under_pressure(cap: Optional[dict]) -> bool:
    """Worth naming in a wait notice: some Fable seats are used up, or none is eligible."""
    if not isinstance(cap, dict):
        return False
    n = cap.get("exhausted")
    return exhausted(cap) or (type(n) is int and n > 0)


def reset_label(reset: Any) -> Optional[str]:
    """``Sun 10:59`` (Pacific) for an ISO timestamp, else None."""
    if not isinstance(reset, str) or not reset:
        return None
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from hermes_time import safe_strftime

        dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return safe_strftime(dt.astimezone(ZoneInfo("America/Los_Angeles")), "%a %H:%M")
    except (ValueError, OverflowError, TypeError):
        return None


def capacity_phrase(cap: Optional[dict]) -> Optional[str]:
    """Plain words for a model under capacity pressure, else None.

    ``Fable allowance used up on 23/25 seats, 2 eligible, resets Sun 10:59`` /
    ``Fable allowance used up on all 25 seats, resets Sun 10:59`` /
    ``no seat can serve claude-opus-5-5``."""
    if not isinstance(cap, dict) or not under_pressure(cap):
        return None
    n, m, eligible = cap.get("exhausted"), cap.get("of"), cap.get("eligible")
    reset = reset_label(cap.get("earliest_reset"))
    if type(n) is int and type(m) is int and m > 0 and n > 0:
        text = (f"Fable allowance used up on all {m} seats" if n >= m
                else f"Fable allowance used up on {n}/{m} seats")
        if n < m and type(eligible) is int:
            text += ", no seat eligible" if eligible == 0 else f", {eligible} eligible"
    else:
        text = f"no seat can serve {cap.get('model') or 'the model'}"
    if reset:
        text += f", resets {reset}"
    return text


def relay_headers(cap: Optional[dict]) -> dict:
    """The Fable allowance as the relay's own refusal header (``x-pool-fable-allowance``)
    so the fallback banner renders it the way it renders a relay refusal."""
    if not isinstance(cap, dict):
        return {}
    out: dict = {}
    n, m = cap.get("exhausted"), cap.get("of")
    if type(n) is int and type(m) is int and m > 0:
        out["x-pool-fable-allowance"] = f"{n}/{m}; earliest_reset={cap.get('earliest_reset') or 'none'}"
    return out
