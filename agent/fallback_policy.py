"""Harness fallback policy (same-family fallback spec rev12, §4.1-4.3, §4.8).

Pure policy core. Hot-path wiring (``try_activate_fallback``,
``restore_primary_runtime``, the gateway pre-run rebuild site and
``_emit_fallback_announce``) calls into this module; nothing here mutates an
agent's client or provider.

* §4.1 class -> cooldown table and the per-class doubling formula.
* §4.2 sticky writer: arms only when the failing (provider, model) IS the
  primary (pass-1 B1), keeps ``n_c`` hysteresis, the ``fallback_failed``
  anti-loop (``ff_disabled_until``), fallback-side write-through fields.
* §4.3 the single return gate :func:`restore_allowed` (``warm_seat``,
  ``fallback_cold``, ``compaction`` after ``until``; ``fallback_failed``
  mid-turn), D6 warm-seat return included, plus the warm-seat spec §4.4 gate
  (:func:`warm_gate`: return-now half on enforce, refusal half + hard cap on
  shadow/enforce in the ``warm_refusal_ab_pct`` session arm).
* The rebuild decision (:func:`decide_rebuild`) behind
  ``resume_sticky_fallback`` and the target-entry lookup.
* §4.8 cause and recovery riders (:func:`format_cause_rider`,
  :func:`format_recovery_rider`) and the head-label override.

Every read of sticky state goes through ``fallback_sticky_store.get`` (the
single accessor).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import logging
import random
import re
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from agent import fallback_sticky_store as _store_mod
from agent.fallback_sticky_store import StickyKey, StickyState, StickyStore, StoreUnreadable

logger = logging.getLogger(__name__)

# ── §4.1 vocabulary (identical to Phase 1 fallback_events.TRIGGER_CLASSES) ──
TRIGGER_CLASSES = (
    "conn", "pool_pressure", "quota_model", "quota_seat", "rate_upstream",
    "refusal", "auth", "provider_invalid_response", "lane_incapable", "unclassified",
)
LANE_INCAPABLE_CLASS = "lane_incapable"
INVALID_RESPONSE_CLASS = "provider_invalid_response"
# Classes the sticky writer arms (one clock: _sticky.until_epoch).
STICKY_CLASSES = frozenset({"conn", "pool_pressure", "quota_seat", "quota_model"})
# Classes that still reach apply_quota_gate / the legacy arm block. On a
# relay provider quota_seat, conn and pool_pressure skip both (§4.1 plumbing).
QUOTA_GATE_CLASSES = frozenset({"quota_model", "rate_upstream", "refusal", "unclassified"})
# §4.3 fallback_failed: quota classes only.
FALLBACK_FAILED_CLASSES = frozenset({"quota_seat", "quota_model", "rate_upstream"})
# Transient causes (connection drop / timeout). Their cooldown is a retry
# delay, not a real quota window: a restart never resumes one, and on a
# primary with no seat signal (not relay, not direct pin) the episode returns
# on the ``transient`` branch once ``until`` passes (t_b2e9bb23: a host network
# blip pinned an OpenRouter-primary session to its fallback for 90 min because
# warm_seat is unreachable for a non-relay primary and an active user never
# lets the fallback go cold). Relay primaries keep the warm-seat gate.
TRANSIENT_CLASSES = frozenset({"conn"})
TRANSIENT_BRANCH = "transient"
USER_ROUTE_BRANCH = "user_route"

MIN = 60.0
HOUR = 3600.0
WARM_SEAT_MAX_AGE_S = 55 * MIN
FALLBACK_COLD_S = 60 * MIN
BOUND_EXPIRES_MIN_S = 60.0
ELIGIBILITY_STALE_S = 300.0
ELIGIBILITY_TIMEOUT_S = 0.5
WARM_REFUSAL_AB_PCT_DEFAULT = 50.0
N_C_WINDOW_S = HOUR          # consecutive failures within 1h of the previous restore
N_C_RESET_AFTER_S = 30 * MIN  # primary served successfully for 30 min
LEGACY_DEFAULT_COOLDOWN_S = 60.0
WINDOW_CEILING_7D_S = 7 * 24 * HOUR
WINDOW_CEILING_DEFAULT_S = 6 * HOUR


class ClassCooldown(NamedTuple):
    base_s: Optional[float]   # None -> derived from retry_after at the call site
    ceiling_s: float


# §4.1 "On fallback: cooldown" column. refusal / rate_upstream / auth /
# unclassified are absent on purpose: they keep today's formula and the shared
# 60 s default in _primary_cooldown_seconds (B3).
COOLDOWN_TABLE: Dict[str, ClassCooldown] = {
    "conn": ClassCooldown(120.0, 30 * MIN),
    "pool_pressure": ClassCooldown(None, 5 * MIN),   # base = Retry-After (2 s if absent)
    "quota_seat": ClassCooldown(None, 30 * MIN),     # base = max(60 s, retry_after)
    "quota_model": ClassCooldown(6 * HOUR, 24 * HOUR),
}

_DIRECT_PIN_RE = re.compile(r"^claude-([abc])px-(\d+)$")


def direct_pin(provider: Optional[str]) -> Optional[Tuple[str, int]]:
    """``('b', 16)`` for ``claude-bpx-16`` (``a``/``b``/``c`` = apx/bpx/cpx);
    None for relay / other providers."""
    m = _DIRECT_PIN_RE.fullmatch(str(provider or "").strip().lower())
    return (m.group(1), int(m.group(2))) if m else None


def is_direct_pin(provider: Optional[str]) -> bool:
    return direct_pin(provider) is not None


RELAY_PROVIDERS = frozenset(("claude-apr", "claude-alr", "claude-alrs", "claude-alrf", "claude-dalrs", "claude-dalrf", "claude-bpr"))


def has_seat_signal(provider: Optional[str]) -> bool:
    """Whether ``warm_seat`` can ever be evaluated for this primary: a relay
    lane (``/eligibility``) or a direct pin (seat == provider)."""
    p = str(provider or "").strip().lower()
    return p in RELAY_PROVIDERS or is_direct_pin(p)


# ── direct-pin seat + hop (§4.8; t_246ce7d6) ──────────────────────────────
# A direct pin carries no x-relay headers (#1260), but both fields are knowable
# locally: the seat is the box the pin names, the hop follows from whether the
# pin's own server answered (status -> it relayed Anthropic's result) or the
# client never got a response (connect / timeout).

_PIN_ROLE = {"a": "proxy", "b": "bridge", "c": "cli"}
_PIN_SERVICE_RE = re.compile(r"claude-([ab])px-(\d+)(?:\.service)?$")
_SUB_KEY_RE = re.compile(r"local|sub-vps-\d+")
_HOSTS_REL = ("fleet", "hosts.json")
_hosts_cache: Dict[str, Any] = {"key": None, "map": {}}


def _hosts_json_path() -> Optional[str]:
    """Nearest HERMES_HOME ancestor holding ``fleet/hosts.json`` (a profile
    home sits under the fleet root)."""
    import os
    from pathlib import Path

    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:  # noqa: BLE001
        return None
    for cand in (home, *home.parents):
        f = cand.joinpath(*_HOSTS_REL)
        if f.is_file():
            return os.fspath(f)
    return None


def _pin_seat_map(path: Optional[str] = None) -> Dict[Tuple[str, int], str]:
    """``{('b', 21): 'sub-vps-21', ('a', 0): 'local', ...}`` from the deploy
    registry's service units. Aliases normalise ``claude-sub-N`` -> ``sub-vps-N``
    (the registry's own boundary rule). Cached on (path, mtime); {} on error."""
    import os

    path = path or _hosts_json_path()
    if not path:
        return {}
    try:
        key = (path, os.stat(path).st_mtime_ns)
        if _hosts_cache["key"] == key:
            return _hosts_cache["map"]
        with open(path, encoding="utf-8-sig") as fh:
            hosts = json.load(fh).get("hosts") or []
        out: Dict[Tuple[str, int], str] = {}
        for h in hosts:
            alias = str((h or {}).get("alias") or "").strip()
            if not alias:
                continue
            alias = re.sub(r"^claude-sub-(\d+)$", r"sub-vps-\1", alias)
            # Only subscription identities (``local`` = Sub #0, ``sub-vps-N``):
            # a deploy-only host (ace-ai-lan) also runs *-0 units but serves
            # no subscription.
            if not _SUB_KEY_RE.fullmatch(alias):
                continue
            for unit in ((h.get("services") or {}).values()):
                m = _PIN_SERVICE_RE.search(str(unit or "").split(":")[-1])
                if m:
                    out[(m.group(1), int(m.group(2)))] = alias
        _hosts_cache.update(key=key, map=out)
        return out
    except Exception:  # noqa: BLE001
        logger.debug("hosts.json seat map unreadable", exc_info=True)
        return {}


def pin_seat(provider: Optional[str], *, hosts_path: Optional[str] = None) -> Optional[str]:
    """Seat (sub name) a direct pin serves from: hosts.json mapping of the
    pin's service unit; ``sub-vps-N`` only when the registry has no entry.
    cpx-N runs the CLI on box N, so it shares the bpx-N box."""
    pin = direct_pin(provider)
    if pin is None:
        return None
    lane, n = pin
    seats = _pin_seat_map(hosts_path)
    seat = seats.get(("b" if lane == "c" else lane, n)) or seats.get(("b", n))
    return seat or f"sub-vps-{n}"


_CONN_EXC = frozenset((
    "APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout",
    "ReadError", "ReadTimeout", "RemoteProtocolError", "IncompleteRead",
    "ConnectionError", "ConnectionResetError", "TimeoutError",
))


def pin_hop(provider: Optional[str], *, http_status: Any = None,
            trigger_class: Optional[str] = None,
            exc_name: Optional[str] = None) -> Optional[str]:
    """§4.8 hop inference rules 3/4 for a direct pin: a status response is
    the pin relaying Anthropic's result (``bridge→anthropic`` …); no response
    on a connection-class failure is ``client→bridge`` …; else None."""
    pin = direct_pin(provider)
    if pin is None:
        return None
    role = _PIN_ROLE[pin[0]]
    if isinstance(http_status, int) and http_status > 0:
        return f"{role}→anthropic"
    if trigger_class == "conn" or exc_name in _CONN_EXC:
        return f"client→{role}"
    return None


def fill_pin_evidence(row: Mapping[str, Any], *, exc_name: Optional[str] = None,
                      hosts_path: Optional[str] = None) -> Dict[str, Any]:
    """Copy of ``row`` with ``seat`` / ``hop`` filled for a direct-pin
    ``from_provider`` when they are missing. Pooled rows pass through."""
    out = dict(row)
    prov = out.get("from_provider")
    if not is_direct_pin(prov):
        return out
    if not out.get("seat") or out.get("seat") == "unknown":
        out["seat"] = pin_seat(prov, hosts_path=hosts_path)
    if not normalize_hop(out.get("hop")):
        hop = pin_hop(prov, http_status=out.get("http_status"),
                      trigger_class=out.get("trigger_class"), exc_name=exc_name)
        if hop:
            out["hop"] = hop
    return out


def _norm_pm(provider: Any, model: Any) -> Tuple[str, str]:
    return (str(provider or "").strip().lower(), str(model or "").strip())


# ── stated reset / window parsing ─────────────────────────────────────────

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def quota_window_from_text(text: Optional[str]) -> Optional[str]:
    t = (text or "").lower()
    if any(k in t for k in ("weekly", "7-day", "7 day", "7d")):
        return "7d"
    if any(k in t for k in ("5-hour", "5 hour", "5h", "session limit")):
        return "5h"
    return None


def parse_stated_reset_s(text: Optional[str], now: float,
                         tz: Optional[_dt.tzinfo] = None) -> Optional[float]:
    """Seconds until the reset stated in a cap text (``resets …``), or None.

    bpx does not forward unified-quota headers, so a direct-pin cap's reset is
    parsed from its text. Unparseable -> None (the 6h -> 24h fallback applies).
    """
    t = (text or "").lower()
    m = re.search(r"resets?\s+in\s+(?:(\d+)\s*d(?:ays?)?)?\s*(?:(\d+)\s*h(?:ours?|rs?)?)?\s*"
                  r"(?:(\d+)\s*m(?:in(?:ute)?s?)?)?", t)
    if m and any(m.groups()):
        d, h, mi = (int(g) if g else 0 for g in m.groups())
        secs = d * 86400 + h * 3600 + mi * 60
        return float(secs) if secs > 0 else None
    m = re.search(r"resets?\s+(?:at\s+)?(\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?(?:z|[+-]\d{2}:?\d{2})?)", t)
    if m:
        raw = m.group(1).upper().replace(" ", "T").replace("Z", "+00:00")
        try:
            when = _dt.datetime.fromisoformat(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=tz or _dt.timezone.utc)
            secs = when.timestamp() - now
            return secs if secs > 0 else None
        except ValueError:
            pass
    m = re.search(r"resets?\s+(?:on\s+)?([a-z]{3})[a-z]*\.?\s+(\d{1,2})"
                  r"(?:(?:,|\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm))?", t)
    if m and m.group(1) in _MONTHS:
        zone = tz or _dt.datetime.now().astimezone().tzinfo
        ref = _dt.datetime.fromtimestamp(now, zone)
        hour = int(m.group(3)) % 12 if m.group(3) else 0
        if m.group(5) == "pm":
            hour += 12
        minute = int(m.group(4)) if m.group(4) else 0
        try:
            when = ref.replace(month=_MONTHS[m.group(1)], day=int(m.group(2)),
                               hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if when.timestamp() <= now:
            when = when.replace(year=when.year + 1)
        return when.timestamp() - now
    return None


def stated_reset_from_context(error_context: Optional[Mapping[str, Any]],
                              now: float) -> Optional[float]:
    """``reset_at`` from ``extract_api_error_context`` -> seconds, or None."""
    if not isinstance(error_context, Mapping):
        return None
    raw = error_context.get("reset_at")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    secs = val - now if val > 1_000_000_000 else val
    return secs if secs > 0 else None


def lane_class(trigger_class: str, provider: Optional[str],
               text: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Apply the direct-pin rule: return ``(class, quota_window)``.

    On a direct pin (apx-N / bpx-N) the seat IS the provider, so a seat-level
    limit is ``quota_model`` with the stated window (7d for an identified
    weekly limit), never the 30-min ``quota_seat`` re-probe.
    """
    window = quota_window_from_text(text)
    if trigger_class == "quota_seat" and is_direct_pin(provider):
        return "quota_model", window
    return trigger_class, window


# ── §4.1 cooldown math ────────────────────────────────────────────────────

def compute_cooldown_s(cls: str, n: int, *,
                       stated_reset_s: Optional[float] = None,
                       retry_after_s: Optional[float] = None,
                       window: Optional[str] = None,
                       jitter: float = 1.0) -> Optional[float]:
    """Per-class cooldown in seconds; None for classes that keep today's formula.

    ``until = now + (min(stated_reset, window_ceiling) if known else
    min(base_c * 2**n_c, ceiling_c)) * U(1.0, 1.1)``. A stated reset is used
    exactly (plus jitter), never doubled.
    """
    if cls not in COOLDOWN_TABLE:
        return None
    n = max(0, int(n))
    if cls == "quota_model" and stated_reset_s is not None and stated_reset_s > 0:
        ceiling = WINDOW_CEILING_7D_S if window == "7d" else WINDOW_CEILING_DEFAULT_S
        return min(float(stated_reset_s), ceiling) * jitter
    row = COOLDOWN_TABLE[cls]
    if cls == "pool_pressure":
        base = float(retry_after_s) if retry_after_s and retry_after_s > 0 else 2.0
    elif cls == "quota_seat":
        base = max(60.0, float(retry_after_s or 0.0))
    else:
        base = float(row.base_s or 0.0)
    return min(base * (2 ** n), row.ceiling_s) * jitter


def default_jitter(rng: Callable[[], float] = random.random) -> float:
    return 1.0 + 0.1 * rng()


def next_n(prev: Optional[StickyState], cls: str, now: float) -> int:
    """``n_c``: consecutive same-class failures within 1h of the previous restore."""
    if prev is None or prev.cls != cls:
        return 0
    if prev.active:
        return prev.n + 1
    if prev.returned_at is not None and now - prev.returned_at <= N_C_WINDOW_S:
        return prev.n + 1
    return 0


def should_arm(failing: Tuple[Any, Any], primary: Tuple[Any, Any]) -> bool:
    """Guard fix (pass-1 B1): arm/extend/escalate only when the FAILING
    (provider, model) equals the primary's. A 429 on the bpr Opus fallback
    after bpr Fable is not a primary failure."""
    f, p = _norm_pm(*failing), _norm_pm(*primary)
    return bool(p[0]) and f == p


def skips_quota_gate(cls: str) -> bool:
    return cls not in QUOTA_GATE_CLASSES


def _load(store: StickyStore, key: StickyKey) -> Optional[StickyState]:
    try:
        return _store_mod.get(key, store)
    except StoreUnreadable:
        return None


def arm_sticky(store: StickyStore, key: StickyKey, *, cls: str, now: float,
               failing: Tuple[Any, Any], fallback: Tuple[Any, Any] = ("", ""),
               fallback_index: Optional[int] = None,
               stated_reset_s: Optional[float] = None,
               retry_after_s: Optional[float] = None,
               window: Optional[str] = None,
               jitter: Optional[float] = None) -> Optional[StickyState]:
    """The sticky writer (§4.2). Returns the new state, or None when it does
    not arm (non-sticky class, or the failing route is not the primary)."""
    if cls not in STICKY_CLASSES:
        return None
    if not should_arm(failing, (key.primary_provider, key.primary_model)):
        return None
    prev = _load(store, key)
    n = next_n(prev, cls, now)
    cooldown = compute_cooldown_s(cls, n, stated_reset_s=stated_reset_s,
                                  retry_after_s=retry_after_s, window=window,
                                  jitter=default_jitter() if jitter is None else jitter)
    state = dataclasses.replace(prev) if prev is not None else StickyState()
    was_active = bool(prev and prev.active)
    state.primary_provider, state.primary_model = key.primary_provider, key.primary_model
    fb_p, fb_m = _norm_pm(*fallback)
    if fb_p:
        state.fallback_provider, state.fallback_model = fb_p, fb_m
        state.fallback_index = fallback_index
    until = now + float(cooldown or 0.0)
    # §4.3 anti-loop: a fallback_failed return that fails again with the
    # same class disables fallback_failed until the re-armed until.
    if (prev is not None and not prev.active and prev.return_branch == "fallback_failed"
            and prev.cls == cls):
        state.ff_disabled_until = until
    state.cls = cls
    state.n = n
    state.until_epoch = max(until, prev.until_epoch) if (was_active and prev) else until
    state.active = True
    state.last_failure_epoch = now
    if not was_active:
        state.entered_at = now
        state.turns_on_fallback = 0
        state.last_fallback_call_epoch = None
        state.last_fallback_session_id = None
    store.put(key, state, now)
    return state


def note_fallback_success(store: StickyStore, key: StickyKey, now: float,
                          session_id: Optional[str]) -> Optional[StickyState]:
    """Write-through of the fallback-side fields on every fallback success."""
    state = _load(store, key)
    if state is None or not state.active:
        return state
    state.last_fallback_call_epoch = now
    state.last_fallback_session_id = str(session_id or "") or None
    state.turns_on_fallback += 1
    store.put(key, state, now)
    return state


def note_compaction(store: StickyStore, key: StickyKey, now: float) -> Optional[StickyState]:
    """§4.3 ``compaction`` input: stamp ``last_compaction_epoch`` on the
    active episode. Called after every completed compaction, in place or
    rotating (in-place mode never rotates ``session_id``). No-op without an
    active episode."""
    state = _load(store, key)
    if state is None or not state.active:
        return state
    state.last_compaction_epoch = now
    store.put(key, state, now)
    return state


def seat_from_response(provider: Optional[str],
                       headers: Optional[Mapping[str, str]]) -> Optional[str]:
    """D6 seat: ``x-pool-served-by`` on a pooled response; the provider name
    (which carries N) on a direct pin; ``unknown``/missing -> None."""
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    served = (h.get("x-pool-served-by") or "").strip()
    if served and served.lower() != "unknown":
        return served
    if is_direct_pin(provider):
        return str(provider).strip().lower()
    return None


def note_primary_success(store: StickyStore, key: StickyKey, now: float, *,
                         provider: Optional[str],
                         headers: Optional[Mapping[str, str]] = None) -> StickyState:
    """Record ``last_primary_call_epoch`` / ``last_primary_seat`` (§4.3 State)
    and reset ``n_c`` after 30 min of restored primary service."""
    state = _load(store, key) or StickyState(primary_provider=key.primary_provider,
                                             primary_model=key.primary_model)
    state.last_primary_call_epoch = now
    seat = seat_from_response(provider, headers)
    if seat is not None:
        state.last_primary_seat = seat
    if (not state.active and state.returned_at is not None
            and now - state.returned_at >= N_C_RESET_AFTER_S):
        state.n = 0
    store.put(key, state, now)
    return state


def record_return(store: StickyStore, key: StickyKey, now: float,
                  branch: str) -> Optional[StickyState]:
    """ANY return: active=false, returned_at=now; until and n are kept."""
    state = _load(store, key)
    if state is None:
        return None
    state.active = False
    state.returned_at = now
    state.return_branch = branch
    store.put(key, state, now)
    return state


# ── /eligibility (Phase 1b relay endpoint) ────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Eligibility:
    bound_seat: Optional[str]
    bound_eligible: bool
    model_eligible: bool
    bound_expires_in_s: Optional[float]
    bound_idle_s: Optional[float]
    bound_expiry: Optional[str]
    snapshot_age_s: Optional[float]
    instance_id: str
    warm_eligible: bool = False
    warm_rank_effective: Optional[str] = None
    warm_seat: Optional[str] = None
    warm_age_s: Optional[float] = None
    warm_window_s: Optional[float] = None
    warm_refusal: Optional[str] = None
    # Free child slots on the bound / warm seat's box (claude-pool BoxCapacity,
    # t_90d3bd12). None = unknown, stale or an older relay: today's behaviour.
    bound_box_free: Optional[int] = None
    warm_box_free: Optional[int] = None
    # t_bbe0023c: warm holders the relay dropped for an active model cap
    # (``warm_skip: "model_capped"``). The relay only lists copies still inside
    # their window, so a non-empty list = "warm copy exists, not eligible".
    warm_skip: Optional[str] = None
    warm_skipped: Tuple[str, ...] = ()


def _opt_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _opt_float(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse_eligibility(obj: Any) -> Optional[Eligibility]:
    """None (fail closed) on malformed, missing ``instance_id`` or stale > 300 s."""
    if not isinstance(obj, Mapping) or not obj.get("instance_id"):
        return None
    age = _opt_float(obj.get("snapshot_age_s"))
    if age is not None and age > ELIGIBILITY_STALE_S:
        return None
    seat = obj.get("bound_seat")
    return Eligibility(
        bound_seat=str(seat) if seat else None,
        bound_eligible=bool(obj.get("bound_eligible")),
        model_eligible=bool(obj.get("model_eligible")),
        bound_expires_in_s=_opt_float(obj.get("bound_expires_in_s")),
        bound_idle_s=_opt_float(obj.get("bound_idle_s")),
        bound_expiry=obj.get("bound_expiry"),
        snapshot_age_s=age,
        instance_id=str(obj.get("instance_id")),
        warm_eligible=bool(obj.get("warm_eligible")),
        warm_rank_effective=obj.get("warm_rank_effective"),
        warm_seat=str(obj.get("warm_seat")) if obj.get("warm_seat") else None,
        warm_age_s=_opt_float(obj.get("warm_age_s")),
        warm_window_s=_opt_float(obj.get("warm_window_s")),
        warm_refusal=obj.get("warm_refusal"),
        bound_box_free=_opt_int(obj.get("bound_box_free")),
        warm_box_free=_opt_int(obj.get("warm_box_free")),
        warm_skip=str(obj.get("warm_skip")) if obj.get("warm_skip") else None,
        warm_skipped=tuple(str(x) for x in (obj.get("warm_skipped") or ())
                           if x) if isinstance(obj.get("warm_skipped"), (list, tuple)) else (),
    )


def eligibility_url(base_url: str, *, model: str, session: Optional[str] = None,
                    user: Optional[str] = None) -> str:
    """scheme+host+port of the primary's base_url + ``/eligibility``. Pass the
    same affinity input the request carries: ``session=`` (apr header) or
    ``user=`` (bpr body)."""
    parts = urllib.parse.urlsplit(base_url)
    q: Dict[str, str] = {"model": model}
    if session:
        q["session"] = session
    if user:
        q["user"] = user
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/eligibility",
                                    urllib.parse.urlencode(q), ""))


def fetch_eligibility(base_url: str, *, model: str, session: Optional[str] = None,
                      user: Optional[str] = None,
                      timeout: float = ELIGIBILITY_TIMEOUT_S,
                      opener: Callable[..., Any] = urllib.request.urlopen) -> Optional[Eligibility]:
    """One GET, 500 ms timeout. Unreachable / error -> None (fail closed)."""
    try:
        with opener(eligibility_url(base_url, model=model, session=session, user=user),
                    timeout=timeout) as resp:
            return parse_eligibility(json.loads(resp.read().decode("utf-8")))
    except Exception as exc:  # noqa: BLE001
        logger.debug("eligibility probe failed: %s", exc)
        return None


class TurnEligibility:
    """At most one ``/eligibility`` call per turn, cached for that turn."""

    def __init__(self, fetch: Callable[[], Optional[Eligibility]]) -> None:
        self._fetch = fetch
        self._turn: Any = object()
        self._value: Optional[Eligibility] = None
        self.calls = 0

    def get(self, turn_id: Any) -> Optional[Eligibility]:
        if turn_id != self._turn:
            self._turn = turn_id
            self.calls += 1
            self._value = self._fetch()
        return self._value


# ── §4.3 the single return gate ───────────────────────────────────────────

class Decision(NamedTuple):
    allowed: bool
    branch: Optional[str]
    reason: str
    gate_bound_expires_in_s: Optional[float] = None
    warm: Optional[Dict[str, Any]] = None   # warm-seat §4.4 poll snapshot (ledger rows)


EligibilityFn = Callable[[], Optional[Eligibility]]
BenchFn = Callable[[], bool]


def _warm_seat(state: StickyState, now: float, *, primary_provider: str,
               eligibility: Optional[EligibilityFn],
               direct_pin_benched: Optional[BenchFn]) -> Tuple[bool, str, Optional[float]]:
    lpc = state.last_primary_call_epoch
    if lpc is None or now - lpc >= WARM_SEAT_MAX_AGE_S:
        return False, "warm_seat: last primary call >= 55 min (or never)", None
    if is_direct_pin(primary_provider):
        benched = bool(direct_pin_benched()) if direct_pin_benched else False
        if benched:
            return False, "warm_seat: direct pin benched (sticky/auth/quota)", None
        return True, "warm_seat: direct pin, age < 55 min, not benched", None
    elig = eligibility() if eligibility else None
    if elig is None:
        return False, "warm_seat: /eligibility unreachable or stale", None
    exp = elig.bound_expires_in_s
    seat = state.last_primary_seat
    seat_ok = bool(seat) and seat != "unknown" and elig.bound_seat == seat
    # bound_expires_in_s is null when the relay's AffinityMap has no time
    # expiry (bound_expiry="none", claude-pool since t_4bbac8a8): a binding
    # that still reports bound_seat == last_primary_seat is live.
    ttl_ok = exp is None or exp >= BOUND_EXPIRES_MIN_S
    if elig.bound_box_free == 0:
        # Ace 09-27 04:38: return to the warm seat "unless that box is too
        # full and contended". Stay sticky; the next boundary re-checks.
        return False, BOX_FULL_REASON, exp
    if seat_ok and elig.bound_eligible and ttl_ok:
        return True, f"warm_seat: bound seat {seat} eligible", exp
    why = []
    if not seat_ok:
        why.append(f"bound_seat {elig.bound_seat!r} != last_primary_seat {seat!r}")
    if not elig.bound_eligible:
        why.append("bound seat not eligible")
    if not ttl_ok:
        why.append(f"bound_expires_in_s {exp} < 60")
    return False, "warm_seat: " + "; ".join(why), exp


BOX_FULL_REASON = "warm_seat: bound box full"


def _box_full(elig: Optional[Eligibility], verdict: Optional[str]) -> bool:
    """A warm return would land on a box with no free child slot: the bound
    box, or (``return_now``: the relay's warm pick routes there) the warm
    seat's box. Unknown (None) never refuses."""
    if elig is None:
        return False
    return elig.bound_box_free == 0 or (verdict == "return_now" and elig.warm_box_free == 0)


def _box_fields(elig: Eligibility) -> Dict[str, Any]:
    return {"bound_box_free": elig.bound_box_free, "warm_box_free": elig.warm_box_free}


def warm_refusal_arm(session_key: Optional[str], pct: Any = WARM_REFUSAL_AB_PCT_DEFAULT) -> bool:
    """Warm-seat spec §5 P3 A/B: sha1(session_key) % 100 < pct -> refusal arm
    (the relay's ``warm_ab_enforce_arm`` hash, harness-side knob)."""
    if not session_key:
        return False
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return False
    return int(hashlib.sha1(session_key.encode("utf-8")).hexdigest(), 16) % 100 < pct


def _warm_snapshot(elig: Eligibility, arm: bool, verdict: Optional[str]) -> Dict[str, Any]:
    return {"warm_rank_effective": elig.warm_rank_effective, "warm_refusal": elig.warm_refusal,
            "warm_seat": elig.warm_seat, "warm_age_s": elig.warm_age_s,
            "warm_window_s": elig.warm_window_s, "warm_eligible": elig.warm_eligible,
            "warm_refusal_arm": arm, "warm_gate": verdict, **_box_fields(elig)}


def warm_gate(elig: Optional[Eligibility], *, refusal_arm: bool) -> Optional[str]:
    """Warm-seat spec §4.4 verdict at a restore boundary, keyed only on
    ``warm_rank_effective`` and ``warm_refusal``:

    * ``"return_now"`` - enforce, ``warm_eligible`` and age < window;
    * ``"refuse"``     - refusal half (shadow|enforce, ``warm_refusal`` on,
      session in the A/B arm): a warm copy exists but no warm seat is eligible.
      A copy the relay dropped for a model cap (``warm_skipped``) counts as
      "warm copy exists, not eligible" (t_e001a935, fallback spec D6);
    * ``"cap"``        - refusal half, but the primary's warm copy has expired
      or has no entry. Telemetry only: :func:`restore_allowed` no longer
      returns early on it (Ace 2026-09-25 19:35 "do not bother returning
      early"; D6 rejects the warm-seat spec P3 hard cap, Apollo t_e001a935);
    * ``None``         - the fallback spec rule unchanged (relay unreachable, no
      warm fields, ``off``, ``warm_refusal=off``, control arm, or shadow with a
      warm eligible seat).
    """
    if elig is None:
        return None
    mode = elig.warm_rank_effective
    if mode not in ("shadow", "enforce"):
        return None
    age, win = elig.warm_age_s, elig.warm_window_s
    has_copy = ((age is not None and win is not None and age < win)
                or (elig.warm_skip is not None and bool(elig.warm_skipped)))
    if mode == "enforce" and elig.warm_eligible and has_copy:
        return "return_now"
    if elig.warm_refusal == "off" or not refusal_arm:
        return None
    if not has_copy:
        return "cap"
    return None if elig.warm_eligible else "refuse"


def _compacted_since_fallback(state: StickyState, live_session_id: Optional[str]) -> bool:
    """§4.3 compaction input: an in-place compaction marker newer than the last
    fallback call, or a session_id rotated since it (rotating compaction)."""
    last_fb = state.last_fallback_call_epoch
    if last_fb is None:
        last_fb = state.entered_at
    lc = state.last_compaction_epoch
    if lc is not None and last_fb is not None and lc > last_fb:
        return True
    return bool(live_session_id and state.last_fallback_session_id
                and live_session_id != state.last_fallback_session_id)


def restore_allowed(state: Optional[StickyState], now: float, *, probe: bool = False,
                    live_session_id: Optional[str] = None,
                    primary_provider: str = "",
                    eligibility: Optional[EligibilityFn] = None,
                    direct_pin_benched: Optional[BenchFn] = None,
                    sticky_policy: bool = True,
                    refusal_arm: bool = False) -> Decision:
    """§4.3: return iff (now >= until AND (warm_seat|fallback_cold|compaction)).

    ``probe=False`` is the cheap form (no network I/O): until, active,
    fallback_cold, compaction. ``probe=True`` adds warm_seat and the warm-seat
    spec §4.4 gate (:func:`warm_gate`; relay primaries only). The mid-turn
    ``fallback_failed`` branch is :func:`fallback_failed_allowed` and never
    consults the warm gate (a failing fallback is never stranded).
    """
    if not sticky_policy or state is None or not state.active:
        return Decision(True, None, "no active sticky state")
    if now < state.until_epoch:
        return Decision(False, None, f"until: {state.until_epoch - now:.0f}s remaining")
    if (state.cls in TRANSIENT_CLASSES
            and not has_seat_signal(primary_provider or state.primary_provider)):
        # No warm_seat signal can ever exist for this primary (not a relay,
        # not a direct pin), so without this branch an active user is held on
        # the fallback until it idles 60 min (t_b2e9bb23, measured 90 min).
        return Decision(True, TRANSIENT_BRANCH,
                        f"transient cause ({state.cls}); cooldown elapsed, "
                        "primary has no seat signal")
    reasons: List[str] = []
    exp: Optional[float] = None
    verdict: Optional[str] = None
    warm: Optional[Dict[str, Any]] = None
    box_full = False
    # A compaction since the last fallback call rewrote the conversation, so the
    # primary seat's cached copy no longer matches what will be sent: a return now
    # is cold whatever /eligibility says about the seat. `compaction` therefore
    # outranks `warm_seat` (t_2b064101 rig, 2026-09-27: branch=warm_seat with
    # cache_read 2449/8399 on the return call). The probe still runs below so the
    # ledger keeps the warm/box snapshot.
    compacted = _compacted_since_fallback(state, live_session_id)
    if probe:
        provider = primary_provider or state.primary_provider
        if eligibility is not None:
            _once: List[Optional[Eligibility]] = []
            _fetch = eligibility

            def eligibility() -> Optional[Eligibility]:  # one poll per gate call
                if not _once:
                    _once.append(_fetch())
                return _once[0]

        if eligibility is not None and not is_direct_pin(provider):
            elig = eligibility()
            verdict = warm_gate(elig, refusal_arm=refusal_arm)
            if elig is not None and elig.warm_rank_effective in ("shadow", "enforce"):
                warm = _warm_snapshot(elig, refusal_arm, verdict)
            if _box_full(elig, verdict):
                box_full = True
                warm = {**(warm or {}), **_box_fields(elig)}
            if verdict == "return_now" and not box_full and not compacted:
                return Decision(True, "warm_seat",
                                f"warm_seat: warm eligible seat {elig.warm_seat} "
                                f"(warm_rank=enforce, age {elig.warm_age_s}s < {elig.warm_window_s}s)",
                                elig.bound_expires_in_s, warm)
        if box_full:
            reasons.append(BOX_FULL_REASON)
            exp = elig.bound_expires_in_s
        elif verdict != "refuse" and not compacted:
            ok, why, exp = _warm_seat(state, now, primary_provider=provider,
                                      eligibility=eligibility, direct_pin_benched=direct_pin_benched)
            if ok:
                return Decision(True, "warm_seat", why, exp, warm)
            reasons.append(why)
        # verdict == "cap" (primary warm copy expired/absent) does NOT return
        # here: the fallback copy may still be warm, and D6 waits for
        # fallback_cold/compaction below (t_e001a935; no cap_expiry branch).
    last_fb = state.last_fallback_call_epoch
    if last_fb is None:
        last_fb = state.entered_at
    if last_fb is not None and now - last_fb > FALLBACK_COLD_S:
        return Decision(True, "fallback_cold", f"fallback idle {now - last_fb:.0f}s", exp, warm)
    reasons.append("fallback_cold: last fallback call <= 60 min ago")
    lc = state.last_compaction_epoch
    if lc is not None and last_fb is not None and lc > last_fb:
        return Decision(True, "compaction", "compaction ran since last fallback call", exp, warm)
    if (live_session_id and state.last_fallback_session_id
            and live_session_id != state.last_fallback_session_id):
        return Decision(True, "compaction", "session_id rotated since last fallback call", exp, warm)
    reasons.append("compaction: none since last fallback call")
    if verdict == "refuse":
        return Decision(False, None, "no_warm_primary_seat", exp, warm)
    if box_full:
        return Decision(False, None, BOX_FULL_REASON, exp, warm)
    return Decision(False, None, " | ".join(reasons), exp, warm)


def fallback_failed_allowed(state: Optional[StickyState], failed_class: str, now: float, *,
                            primary_provider: str = "",
                            eligibility: Optional[EligibilityFn] = None,
                            direct_pin_benched: Optional[BenchFn] = None,
                            sticky_policy: bool = True) -> Decision:
    """§4.3 ``fallback_failed``: the FALLBACK just failed with a quota class
    and the primary is eligible -> return instead of walking the chain.
    Ignores ``until``; blocked by ``ff_disabled_until`` (anti-loop)."""
    if not sticky_policy or state is None or not state.active:
        return Decision(False, None, "fallback_failed: no active sticky state")
    if failed_class not in FALLBACK_FAILED_CLASSES:
        return Decision(False, None, f"fallback_failed: class {failed_class} is not a quota class")
    if now < (state.ff_disabled_until or 0.0):
        return Decision(False, None, "fallback_failed: disabled (anti-loop) until re-armed until")
    provider = primary_provider or state.primary_provider
    if is_direct_pin(provider):
        if direct_pin_benched and direct_pin_benched():
            return Decision(False, None, "fallback_failed: direct-pin primary benched")
        return Decision(True, "fallback_failed", "direct-pin primary not benched")
    elig = eligibility() if eligibility else None
    if elig is None:
        return Decision(False, None, "fallback_failed: /eligibility unreachable or stale")
    if not (elig.model_eligible or elig.bound_eligible):
        return Decision(False, None, "fallback_failed: primary not eligible")
    return Decision(True, "fallback_failed", "primary eligible", elig.bound_expires_in_s)


def direct_pin_benched(store: StickyStore, key: StickyKey, now: float, *,
                       token_fp: Optional[str] = None,
                       registry_exhausted: Optional[BenchFn] = None) -> bool:
    """Direct-pin eligibility: an active sticky, an auth mark or a quota bench."""
    state = _load(store, key)
    if state is not None and state.active and now < state.until_epoch:
        return True
    if token_fp and store.auth_marked(token_fp, now):
        return True
    return bool(registry_exhausted and registry_exhausted())


# ── §4.2 rebuild decision + resume target ─────────────────────────────────

class RebuildDecision(NamedTuple):
    action: str            # primary | resume | return | store_unreadable
    state: Optional[StickyState]
    decision: Optional[Decision]


def decide_rebuild(store: StickyStore, key: StickyKey, now: float, *,
                   live_session_id: Optional[str],
                   eligibility: Optional[EligibilityFn] = None,
                   direct_pin_benched: Optional[BenchFn] = None,
                   sticky_policy: bool = True,
                   refusal_arm: bool = False) -> RebuildDecision:
    """The single construction-time decision (gateway pre-run site)."""
    if not sticky_policy:
        return RebuildDecision("primary", None, None)
    try:
        state = _store_mod.get(key, store)
    except StoreUnreadable:
        logger.warning("sticky store unreadable at rebuild; starting on primary "
                       "(lineage root %s)", key.lineage_root)
        return RebuildDecision("store_unreadable", None, None)
    if state is None or not state.active:
        return RebuildDecision("primary", state, None)
    if state.cls in TRANSIENT_CLASSES:
        # A restart never resumes a transient episode, even inside its
        # cooldown: the rebuilt session starts on the primary and the normal
        # per-turn failover decides again.
        d = Decision(True, TRANSIENT_BRANCH,
                     f"transient cause ({state.cls}); restart resumes on primary")
        return RebuildDecision("return", record_return(store, key, now, TRANSIENT_BRANCH), d)
    d = restore_allowed(state, now, probe=True, live_session_id=live_session_id,
                        primary_provider=key.primary_provider, eligibility=eligibility,
                        direct_pin_benched=direct_pin_benched, refusal_arm=refusal_arm)
    if d.allowed:
        return RebuildDecision("return", record_return(store, key, now, d.branch or ""), d)
    return RebuildDecision("resume", state, d)


def resume_target_index(chain: Sequence[Mapping[str, Any]],
                        state: StickyState) -> Tuple[int, bool]:
    """Chain index of the stored fallback entry; ``(0, True)`` when it is no
    longer in the chain (config reload -> walk from the head, recorded)."""
    want = _norm_pm(state.fallback_provider, state.fallback_model)
    for i, fb in enumerate(chain or ()):
        if _norm_pm(fb.get("provider"), fb.get("model")) == want:
            return i, False
    return 0, True


def token_fingerprint(bearer: Optional[str]) -> str:
    import hashlib

    return hashlib.sha256(str(bearer or "").encode("utf-8")).hexdigest()[:12] if bearer else ""


# ── §4.8 notice riders ────────────────────────────────────────────────────

HOPS = ("client→relay", "relay", "relay→bridge", "bridge→anthropic",
        "client→proxy", "proxy→anthropic", "client→bridge",
        "client→cli", "cli→anthropic")
# Genuinely unknown fields (pooled lane, no relay attribution). Rendered as
# words, never a bare "?" (t_246ce7d6: "hop ? on sub ?" was unreadable).
HOP_UNKNOWN = "hop unknown"
SUB_UNKNOWN = "sub unknown"
_RELAY_HOP_ASCII = {"relay": "relay", "relay->bridge": "relay→bridge",
                    "bridge->upstream": "bridge→anthropic"}


def normalize_hop(raw: Optional[str]) -> Optional[str]:
    """Relay ``x-relay-error-hop`` ASCII values -> the notice enum."""
    if not raw:
        return None
    raw = str(raw).strip()
    if raw in HOPS:
        return raw
    return _RELAY_HOP_ASCII.get(raw.lower())


# claude-pool deploy-drain 503 (t_4349cf26): decided AT the relay, no seat.
RELAY_DRAIN_CAUSE = "relay draining for deploy"
# Anthropic 400 "Third-party apps now draw from your extra usage" (t_7f2ced0d).
THIRD_PARTY_CAUSE = "plan billing refused (extra usage only)"
# Anthropic 400 "You're out of extra usage" (t_f00f05bd).
EXTRA_USAGE_EXHAUSTED_CAUSE = "extra usage exhausted"
# Bridge 409 tui_history_diverged (t_693aa2e5): the bridge demoted this
# conversation's interactive session; its reason follows in parentheses.
SESSION_DEMOTED_CAUSE = "interactive session demoted"
_DEMOTE_REASON_RE = re.compile(r"has been demoted \(([^)]+)")


def _session_demoted_cause(t: str) -> str:
    m = _DEMOTE_REASON_RE.search(t)
    reason = m.group(1).strip() if m else ""
    return f"{SESSION_DEMOTED_CAUSE} ({reason})" if reason else SESSION_DEMOTED_CAUSE


# §4.8 renderer floors (t_a716610d): the branches that render when the harness
# did NOT classify the failure. Each one is a dead-letter detection point
# (``fallback_events.note_unclassified``); the rendered text stays as is.
UNCLASSIFIED_CAUSE = "unclassified error"
FLOOR_CAUSE = "unclassified_cause"
FLOOR_HOP_SUB = "hop_sub_unknown"
FLOOR_HEAD = "generic_head"
_HOP_SUB_UNKNOWN_SEG = f"({HOP_UNKNOWN}, {SUB_UNKNOWN})"


# Relay 504s that arrive BEFORE the first byte (t_6eddafcd): the relay buffers
# the whole upstream body, so "upstream attempt timed out" means the seat
# never answered within the relay's per-attempt budget. Not a client read
# timeout and not a mid-stream stall.
SEAT_TIMEOUT_RELAY_ERROR = "upstream attempt timed out"
SEAT_TIMEOUT_CAUSE = "seat timed out"
RELAY_DEADLINE_CAUSE = "relay deadline exceeded"
POOL_NO_OTHER_SEAT = "pool had no other seat"
_CLIENT_READ_TIMEOUT_EXC = frozenset(("ReadTimeout", "APITimeoutError"))


def _elapsed_text(seconds: Any) -> Optional[str]:
    """``7m00s`` / ``42s`` for a positive duration, else None."""
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def _seat_timeout_body(row: Mapping[str, Any], seat: str) -> str:
    """``sub-vps-18 did not answer in 7m00s (relay deadline)``."""
    who = "the seat" if seat == SUB_UNKNOWN else seat
    took = _elapsed_text(row.get("elapsed_s"))
    if took:
        return f"{who} did not answer in {took} (relay deadline)"
    return f"{who} did not answer before the relay deadline"


def _pool_context(row: Mapping[str, Any]) -> str:
    """`` · pool had no other seat`` when the relay stated zero eligible seats
    besides the failing one (``x-pool-other-eligible: 0`` / v2
    ``x-relay-eligible: 0``): the failure was lane-wide, not one seat."""
    return f" · {POOL_NO_OTHER_SEAT}" if row.get("pool_eligible") == 0 else ""


# claude-pool box-level refusals (t_0ff05041): no session slot / CLI-child cap
# on the relay box, and an interactive session that missed its startup deadline.
BOX_CAPACITY_CAUSE = "relay box at session capacity"
BOX_STARTUP_CAUSE = "relay session startup timed out"

# Our bridge/relay refused the request SHAPE for the lane (t_1ed37625). The
# banner names OUR hop as the source: these codes are never Anthropic's.
LANE_INCAPABLE_CAUSE = "lane cannot serve this request shape"
LANE_INCAPABLE_NOT_ANTHROPIC = "ours, not Anthropic"


def _lane_incapable_cause(row: Mapping[str, Any]) -> str:
    """``lane cannot serve tools`` / ``... images`` from the body's machine code
    (``row["lane_code"]``); the generic shape wording without one."""
    from agent.fallback_capability import LANE_INCAPABLE_CODES

    code = str(row.get("lane_code") or "").strip().lower()
    if code == "mode_not_allowed":
        return "lane closed to this delivery mode"
    shape = LANE_INCAPABLE_CODES.get(code) if code else None
    return f"lane cannot serve {shape}" if shape else LANE_INCAPABLE_CAUSE


def _lane_incapable_body(row: Mapping[str, Any], seat: str) -> str:
    """``lane cannot serve tools — 400 tui_tools_unsupported at the bridge (ours,
    not Anthropic) on sub-vps-24``. The relay's own ``mode_not_allowed`` is
    ``at the relay``; a bridge ``tui_*`` code is ``at the bridge``."""
    code = str(row.get("lane_code") or "").strip().lower()
    st = row.get("http_status") or "error"
    if not code:
        # No machine code on the row: the hop is whatever the relay stated (or
        # unknown), rendered by the shared segment like every other class.
        return f"{_lane_incapable_cause(row)} {_hop_segment(normalize_hop(row.get('hop')), seat, st)}"
    where = "at the relay" if code == "mode_not_allowed" else "at the bridge"
    seat_seg = f" on {seat}" if seat != SUB_UNKNOWN else f" ({SUB_UNKNOWN})"
    return (f"{_lane_incapable_cause(row)} — {st} {code} {where} "
            f"({LANE_INCAPABLE_NOT_ANTHROPIC}){seat_seg}")


def invalid_response_cause(row: Mapping[str, Any]) -> str:
    """``empty response (stop_reason=tool_use, 0 content blocks, 462 out)``
    from a rejected billed response's floor evidence (t_d35beb85)."""
    fl = row.get("floor") if isinstance(row.get("floor"), Mapping) else {}
    blocks = fl.get("content_blocks")
    parts = []
    if fl.get("stop_reason"):
        parts.append(f"stop_reason={fl['stop_reason']}")
    if isinstance(blocks, int):
        parts.append(f"{blocks} content block{'' if blocks == 1 else 's'}")
    if isinstance(fl.get("output_tokens"), int):
        parts.append(f"{fl['output_tokens']} out")
    if not parts and fl.get("detail"):
        parts.append(str(fl["detail"])[:80])
    head = "empty response" if blocks == 0 else "invalid response"
    return f"{head} ({', '.join(parts)})" if parts else head


def _relay_empty_chain(row: Mapping[str, Any], seat_names: bool) -> Optional[str]:
    """``empty reply from Anthropic ×3 (sub-vps-18, sub-vps-18, sub-vps-23) —
    relay retried, gave up`` when the pooled relay already ran its own
    empty-content ladder (``x-pool-empty-content-retried: gave_up``,
    claude-pool #193) and gave up. None otherwise (t_6eddafcd)."""
    fl = row.get("floor") if isinstance(row.get("floor"), Mapping) else {}
    if fl.get("relay_retry") != "gave_up":
        return None
    seats = [str(x) for x in (fl.get("relay_attempts") or ()) if x]
    if seats:
        names = ", ".join(seats) if seat_names else ", ".join("a sub" for _ in seats)
        return f"empty reply from Anthropic ×{len(seats)} ({names}) — relay retried, gave up"
    seat = row.get("seat") or fl.get("served_by")
    where = f" (last on {seat if seat_names else 'a sub'})" if seat and seat != "unknown" else ""
    return f"empty reply from Anthropic{where} — relay retried, gave up"


def invalid_response_chat_cause(row: Mapping[str, Any]) -> str:
    """Chat wording of a rejected billed response: ``empty reply`` for the
    0-block shape. The raw ``stop_reason=…, 0 content blocks, N out`` stays in
    the route-changes log and the ledger (t_6eddafcd)."""
    fl = row.get("floor") if isinstance(row.get("floor"), Mapping) else {}
    if fl.get("content_blocks") == 0:
        return "empty reply"
    return invalid_response_cause(row)


def _invalid_response_body(row: Mapping[str, Any], seat_names: bool) -> str:
    """``<cause> · hop=relay-200 · sub=<seat>``: the relay ANSWERED 200 (the
    fault is box/upstream side, not the relay hop) and named the seat in
    ``x-pool-served-by``. Never ``(hop unknown, sub unknown)``."""
    fl = row.get("floor") if isinstance(row.get("floor"), Mapping) else {}
    prov = str(row.get("from_provider") or "").strip().lower()
    if prov.startswith("custom:"):
        prov = prov[len("custom:"):]
    relayed = prov in RELAY_PROVIDERS or bool(fl.get("served_by") or fl.get("route_id"))
    hop = "relay-200" if relayed else "200"
    seat = row.get("seat") or fl.get("served_by")
    sub = (str(seat) if seat_names else "a sub") if seat and seat != "unknown" else "unknown"
    chain = _relay_empty_chain(row, seat_names)
    if chain:
        return chain
    return f"{invalid_response_chat_cause(row)} · hop={hop} · sub={sub}"


def _cause_phrase(row: Mapping[str, Any]) -> str:
    cls = row.get("trigger_class") or "unclassified"
    if cls == INVALID_RESPONSE_CLASS:
        return invalid_response_cause(row)
    if cls == LANE_INCAPABLE_CLASS:
        return _lane_incapable_cause(row)
    t = str(row.get("err_head") or row.get("err_text") or "").lower()
    if cls == "conn":
        if "reset" in t:
            return "connection reset"
        if "connect" in t and "time" in t:
            return "connect timeout"
        if "incomplete" in t:
            return "incomplete read"
        if row.get("relay_error") == SEAT_TIMEOUT_RELAY_ERROR:
            return SEAT_TIMEOUT_CAUSE
        if row.get("relay_error") == "pool deadline exceeded":
            return RELAY_DEADLINE_CAUSE
        if "timed out" in t or "timeout" in t:
            # t_6eddafcd: "read timeout" only when the CLIENT's read timed out.
            if (row.get("socket_cause") == "read_timeout"
                    or row.get("exc_name") in _CLIENT_READ_TIMEOUT_EXC):
                return "read timeout"
            return "timed out"
        return "connection error"
    if cls == "pool_pressure":
        if "replayed history no longer matches" in t:
            return _session_demoted_cause(t)
        if "draining-for-deploy" in t or ("drain" in t and "deploy" in t):
            return RELAY_DRAIN_CAUSE
        if "startup deadline" in t:
            return BOX_STARTUP_CAUSE
        if "interactive session slot" in t or "cli children" in t:
            return BOX_CAPACITY_CAUSE
        if "burn" in t:
            return "burn-in ceiling"
        if "newly-activated" in t or "young" in t:
            return "young-seat ceiling"
        if "capacity" in t:
            return "pool at capacity"
        if "overloaded" in t or row.get("http_status") == 529:
            return "upstream overloaded"
        if "drain" in t:
            return "seat drained"
        return "relay busy"
    if cls in ("quota_model", "quota_seat"):
        if "out of extra usage" in t:
            return EXTRA_USAGE_EXHAUSTED_CAUSE
        if "budget" in t and "capped" in t:
            return "model budget capped"
        if "no eligible sub" in t:
            return "no eligible sub for the model"
        weekly = quota_window_from_text(t) == "7d"
        if "fable" in t:
            return "Fable weekly limit" if weekly else "Fable limit"
        if quota_window_from_text(t) == "5h":
            return "5h session limit"
        if weekly:
            return "weekly limit"
        return "model quota exhausted" if cls == "quota_model" else "seat quota exhausted"
    if cls == "auth" and "third-party apps" in t:
        return THIRD_PARTY_CAUSE
    return {
        "rate_upstream": "account rate limit",
        "refusal": "content policy refusal",
        "auth": "OAuth revoked (401)",
    }.get(cls, UNCLASSIFIED_CAUSE)


def _seat_token(row: Mapping[str, Any], seat_names: bool) -> str:
    seat = row.get("seat")
    if (row.get("trigger_class") == "quota_model" and not is_direct_pin(row.get("from_provider"))
            and (not seat or row.get("pool_wide"))):
        return "all subs"
    if not seat or seat == "unknown":
        return SUB_UNKNOWN
    return str(seat) if seat_names else "a sub"


def _hop_segment(hop: Optional[str], seat: str, status: Any) -> str:
    st = status if status else "error"
    if seat == SUB_UNKNOWN:
        known = {
            "client→relay": "to the relay",
            "relay": "at the relay",
            "relay→bridge": "to the bridge",
            "bridge→anthropic": f"(Anthropic {st}) from the bridge",
            "client→proxy": "to the proxy",
            "proxy→anthropic": f"(Anthropic {st}) via the proxy",
            "client→bridge": "to the bridge (direct)",
            "client→cli": "to the CLI (direct)",
            "cli→anthropic": f"(Anthropic {st}) via the CLI",
        }.get(hop or "")
        return f"{known} ({SUB_UNKNOWN})" if known else _HOP_SUB_UNKNOWN_SEG
    return {
        "client→relay": f"to the relay for {seat}",
        "relay": f"at the relay on {seat}",
        "relay→bridge": f"to {seat} bridge",
        "bridge→anthropic": f"(Anthropic {st}) on {seat}",
        "client→proxy": f"to {seat} proxy",
        "proxy→anthropic": f"(Anthropic {st}) via {seat} proxy",
        "client→bridge": f"to {seat} bridge (direct)",
        "client→cli": f"to {seat} CLI (direct)",
        "cli→anthropic": f"(Anthropic {st}) via {seat} CLI",
    }.get(hop or "", f"on {seat} ({HOP_UNKNOWN})")


def _hms(ts: float, tz: Optional[_dt.tzinfo]) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(float(ts), tz)


def _count_window(row: Mapping[str, Any], tz: Optional[_dt.tzinfo]) -> Tuple[str, str]:
    attempts = int(row.get("attempts") or 1)
    first = row.get("first_err_ts") or row.get("ts") or row.get("last_err_ts")
    last = row.get("last_err_ts") or first
    prefix = f"{attempts}x " if attempts > 1 else ""
    if first is None:
        return prefix, "time ?"
    a = _hms(first, tz)
    if attempts <= 1 or last is None:
        return prefix, a.strftime("%H:%M:%S")
    b = _hms(last, tz)
    if b.strftime("%H:%M") == a.strftime("%H:%M"):
        return prefix, f"{a.strftime('%H:%M:%S')}-{b.strftime('%S')}"
    return prefix, f"{a.strftime('%H:%M:%S')}-{b.strftime('%H:%M:%S')}"


def format_cause_rider(row: Mapping[str, Any], *, seat_names: bool = True,
                       tz: Optional[_dt.tzinfo] = None,
                       floors: Optional[List[str]] = None) -> str:
    """Four mandatory fields: cause, hop, sub, count+window (§4.8).

    Built from the Phase 1 ``fallback_events`` row dict. A direct pin's
    missing seat/hop are derived locally (:func:`fill_pin_evidence`); a
    genuinely unknown hop/sub renders as ``hop unknown`` / ``sub unknown``,
    never omitted or guessed. ``floors``, when given, is extended with the
    floor branches the cause rendered from (``FLOOR_CAUSE``: the cause fell to
    ``unclassified error``; ``FLOOR_HOP_SUB``: ``(hop unknown, sub unknown)``).
    """
    row = fill_pin_evidence(row)
    body, hit = _cause_body(row, seat_names, tz)
    if floors is not None:
        floors.extend(hit)
    return body + same_error_rider(row, seat_names=seat_names, tz=tz)


def cause_rider_with_floors(row: Mapping[str, Any], *, seat_names: bool = True,
                            tz: Optional[_dt.tzinfo] = None) -> Tuple[str, Tuple[str, ...]]:
    """:func:`format_cause_rider` text plus the floor branches it rendered from."""
    hit: List[str] = []
    text = format_cause_rider(row, seat_names=seat_names, tz=tz, floors=hit)
    return text, tuple(hit)


def _cause_body(row: Mapping[str, Any], seat_names: bool,
                tz: Optional[_dt.tzinfo]) -> Tuple[str, Tuple[str, ...]]:
    prefix, window = _count_window(row, tz)
    _fl = row.get("floor") if isinstance(row.get("floor"), Mapping) else {}
    if row.get("trigger_class") == INVALID_RESPONSE_CLASS and (
            _fl.get("served_by") or not _plain_provider(row)):
        # A pool seat answered the rejected 200 (x-pool-served-by): name it,
        # whatever the provider label (t_d35beb85).
        return f"{prefix}{_invalid_response_body(row, seat_names)}, {window}", ()
    if _plain_provider(row):
        # The banner ends after the vendor's words when there are any (Ace,
        # 2026-09-27: no relay legs, no seats, nothing after the cause).
        cause = _provider_cause(row)
        floors = (FLOOR_CAUSE,) if cause.startswith(UNCLASSIFIED_CAUSE) else ()
        text = f"{prefix}{cause}" if row.get("provider_message") else f"{prefix}{cause}, {window}"
        return text, floors
    if relay_conn_without_evidence(row):
        return f"{prefix}{_relay_conn_cause(row, tz)}, {window}", ()
    seat = _seat_token(row, seat_names)
    if row.get("trigger_class") == LANE_INCAPABLE_CLASS:
        # The relay's hop header says bridge->upstream for a bridge 400 it
        # passed through; the body code says the bridge/relay itself refused
        # the shape. Name our hop, never "(Anthropic 400)" (t_1ed37625).
        return f"{prefix}{_lane_incapable_body(row, seat)}, {window}", ()
    hop = normalize_hop(row.get("hop"))
    cause = _cause_phrase(row)
    if _is_pool_wide_relay_busy(row, hop, cause):
        if cause == RELAY_DRAIN_CAUSE:
            return f"{prefix}{cause} (at the relay), {window}", ()
        return f"{prefix}relay busy: all subs at capacity (at the relay), {window}", ()
    if cause == SEAT_TIMEOUT_CAUSE:
        return f"{prefix}{_seat_timeout_body(row, seat)}{_pool_context(row)}, {window}", ()
    seg = _hop_segment(hop, seat, row.get("http_status"))
    floors = tuple(f for f, hit in ((FLOOR_CAUSE, cause == UNCLASSIFIED_CAUSE),
                                    (FLOOR_HOP_SUB, seg == _HOP_SUB_UNKNOWN_SEG)) if hit)
    return f"{prefix}{cause} {seg}{_pool_context(row)}, {window}", floors


# ── same-error re-failover backoff (t_7f2ced0d) ───────────────────────────
# A primary return whose first call fails with the SAME normalized error
# (``fallback_events.err_hash``) as the failover that preceded it learned
# nothing new and paid a cold cache write on the fallback. Measured
# 2026-09-30, session 20260929_140138_45a33cbb: apr/fable -> bpr/opus at
# 14:01:40, 14:19:43 and 14:29:58, each one 2-3 s after a turn-boundary
# return, same 400 on sub-vps-15 each time.
#
# Re-failover counts as "the same" when it lands within this many seconds of
# the return. Measured return -> re-failover gaps were 2-3 s (pre-stream 400);
# 60 s also covers a slow first byte without catching a genuinely new episode.
SAME_ERR_RETURN_WINDOW_S = 60.0
# First backoff. Today's recovery cadence is one try per user turn: measured
# returns came 18 min (14:01 -> 14:19) and 10 min (14:19 -> 14:29) apart, so
# 20 min doubles the shortest observed window and skips at least one return.
SAME_ERR_BACKOFF_BASE_S = 20 * MIN
# Ceiling. The same 400 on the same session was seen 09-29 14:02 and again
# 09-30 13:41-14:29, so this condition outlives any doubling; 4 h (the
# legacy unattributed-backoff ceiling in try_activate_fallback) still probes
# a few times a day so a fixed seat or a relay rebind is found the same day.
SAME_ERR_BACKOFF_CEILING_S = 4 * HOUR


def same_error_backoff(prev: Optional[Mapping[str, Any]], *, err_hash: Optional[str],
                       now: float, last_return_ts: Optional[float],
                       seat: Optional[str] = None,
                       route: Tuple[Any, Any] = ("", "")) -> Dict[str, Any]:
    """Next same-error episode for a failover of ``route`` with ``err_hash``.

    ``prev`` is the episode the previous PRIMARY failover left (``{}``/None if
    none). Returns ``{route, err_hash, ts, seat, repeats, backoff_s, prev_ts,
    prev_seat}``: ``backoff_s`` is None unless this failover repeats ``prev``'s
    route AND hash within :data:`SAME_ERR_RETURN_WINDOW_S` of a primary
    return, in which case it is ``min(BASE * 2**(repeats-1), CEILING)`` and
    ``prev_ts`` / ``prev_seat`` name the earlier failure for the rider. Pure.
    """
    prev = dict(prev or {})
    route_l = list(_norm_pm(*route))
    repeat = bool(
        err_hash and prev.get("err_hash") == err_hash
        and list(prev.get("route") or ()) == route_l
        and last_return_ts is not None
        and isinstance(prev.get("ts"), (int, float)) and prev["ts"] <= last_return_ts
        and 0.0 <= now - float(last_return_ts) <= SAME_ERR_RETURN_WINDOW_S)
    repeats = int(prev.get("repeats") or 0) + 1 if repeat else 0
    backoff = (min(SAME_ERR_BACKOFF_BASE_S * (2 ** (repeats - 1)), SAME_ERR_BACKOFF_CEILING_S)
               if repeat else None)
    return {
        "route": route_l, "err_hash": err_hash, "ts": now,
        "seat": (seat or prev.get("seat")) if repeat else seat,
        "repeats": repeats, "backoff_s": backoff,
        "prev_ts": prev.get("ts") if repeat else None,
        "prev_seat": prev.get("seat") if repeat else None,
    }


def _dur(s: float) -> str:
    s = int(round(s))
    if s < HOUR:
        return f"{max(1, s // 60)}m"
    h, m = divmod(s // 60, 60)
    return f"{h}h{m:02d}m" if m else f"{h}h"


def same_error_rider(row: Mapping[str, Any], *, seat_names: bool = True,
                     tz: Optional[_dt.tzinfo] = None) -> str:
    """``; same error as 14:19:43 on sub-vps-15; backing off to 40m`` when the
    row carries a same-error backoff, else ''."""
    backoff, prev_ts = row.get("same_err_backoff_s"), row.get("same_err_prev_ts")
    if not isinstance(backoff, (int, float)) or not isinstance(prev_ts, (int, float)):
        return ""
    seat = row.get("same_err_prev_seat") or row.get("seat")
    where = f" on {seat if seat_names else 'a sub'}" if seat and seat != "unknown" else ""
    return (f"; same error as {_hms(prev_ts, tz).strftime('%H:%M:%S')}{where}; "
            f"backing off to {_dur(backoff)}")


_VENDOR_NAMES = {"openrouter": "OpenRouter", "openai-codex": "OpenAI", "openai": "OpenAI",
                 "xai": "xAI", "anthropic": "Anthropic", "nous": "Nous Portal",
                 "gemini": "Google", "google": "Google", "deepseek": "DeepSeek"}


def _plain_provider(row: Mapping[str, Any]) -> bool:
    """True for a failover FROM a provider with no relay/hop/sub concept
    (openrouter, openai-codex, xai, ...). "hop" and "sub" are relay-pool
    vocabulary for the claude-* lanes; elsewhere "(hop unknown, sub unknown)"
    is noise (t_a8dc8b21). A row without ``from_provider``, or with any relay
    evidence (hop/seat), keeps the relay rider."""
    prov = str(row.get("from_provider") or "").strip().lower()
    if prov.startswith("custom:"):  # relay lanes can be recorded as custom:claude-apr/-apx-N
        prov = prov[len("custom:"):]
    if not prov or prov.startswith("claude-"):
        return False
    return not (row.get("hop") or row.get("seat"))


_LOOPBACK_HOSTS = ("127.", "localhost", "::1", "[::1]")
# Failures before the relay answered. "incomplete read" / "read timeout" can
# be a mid-stream drop after the relay picked a seat, so they keep the rider.
_PRE_ANSWER_CONN = ("connection error", "connection reset", "connect timeout")


def relay_conn_without_evidence(row: Mapping[str, Any]) -> bool:
    """A pooled relay lane's connection failure with no relay evidence (no
    hop, seat, relay header or HTTP status): the relay never answered, so there is no
    hop or sub to name (t_21bba7dc: "connection error (hop unknown, sub
    unknown)" while relay-autodeploy restarted the local relay)."""
    if row.get("trigger_class") != "conn":
        return False
    prov = str(row.get("from_provider") or "").strip().lower()
    if prov.startswith("custom:"):
        prov = prov[len("custom:"):]
    if not prov.startswith("claude-") or is_direct_pin(prov):
        return False
    if normalize_hop(row.get("hop")) or (row.get("seat") and row.get("seat") != "unknown"):
        return False
    if row.get("class_source") in ("relay_header", "relay_stream") or row.get("relay_synthetic"):
        return False
    if row.get("http_status") is not None:  # an HTTP status means the relay answered
        return False
    return _cause_phrase(row) in _PRE_ANSWER_CONN


def _relay_conn_cause(row: Mapping[str, Any], tz: Optional[_dt.tzinfo]) -> str:
    addr = str(row.get("relay_addr") or "").strip()
    if not addr:
        name = "the relay"
    elif addr.lower().startswith(_LOOPBACK_HOSTS):
        name = f"local relay {addr}"
    else:
        name = f"relay {addr}"
    if _cause_phrase(row) == "connect timeout":
        text = f"{name} did not accept the connection"
    else:
        text = f"{name} dropped the connection before answering"
    up = row.get("relay_up")
    if up and row.get("relay_up_ts") is not None:
        text += f"; relay back up {_hms(row['relay_up_ts'], tz).strftime('%H:%M:%S')}"
    elif up is False:
        text += "; relay not reachable"
    return text


def _provider_cause(row: Mapping[str, Any]) -> str:
    """Cause for a non-relay provider: whose limit tripped, then the vendor's
    own words. "account rate limit" only when the vendor says "account"."""
    prov = str(row.get("from_provider") or "").strip().lower()
    vendor = _VENDOR_NAMES.get(prov, prov)
    scope = row.get("provider_scope")
    msg = row.get("provider_message")
    status = row.get("http_status")
    text = str(msg or row.get("err_head") or "").lower()
    cls = row.get("trigger_class")
    if scope == "credits":
        cause = f"out of {vendor} credits"
    elif scope == "byok":
        cause = "rate limit on the upstream provider's own key (BYOK), not your account or credits"
    elif scope == "upstream":
        cause = f"rate limit at {vendor}'s upstream provider, not your account"
    elif scope == "platform":
        cause = f"{vendor} rate limit on your key"
    elif cls == "rate_upstream":
        cause = "account rate limit" if "account" in text else "rate limit"
    else:
        cause = _cause_phrase(row)
    if msg:
        head = f"{vendor} {status}" if status else vendor
        cause += f'; {head} said "{msg}"'
    return cause


def _is_pool_wide_relay_busy(row: Mapping[str, Any], hop: Optional[str], cause: str) -> bool:
    """A pooled lane's pool_pressure refusal that names no seat was decided AT the
    relay before any seat was chosen ("pool at capacity"): there is no seat to name
    and the hop is known. t_e17de574: rendered "relay busy (hop unknown, sub
    unknown)" through a 9-minute tailnet outage, which read as missing data."""
    return (row.get("trigger_class") == "pool_pressure"
            and not row.get("seat") and hop in (None, "relay")
            and cause in ("relay busy", "pool at capacity", RELAY_DRAIN_CAUSE)
            and not is_direct_pin(row.get("from_provider")))


def head_label_override(row: Mapping[str, Any]) -> Optional[str]:
    """The one head-label byte change (§4.8): relay-sourced conn/pool_pressure
    name the real cause instead of the status-derived ``rate limit``. When
    this returns a label, ``_quota_window_suffix`` is not applied."""
    cls = row.get("trigger_class")
    relay_sourced = (row.get("class_source") in ("relay_header", "relay_stream")
                     or bool(row.get("relay_synthetic")))
    if cls == INVALID_RESPONSE_CLASS:
        # t_6eddafcd: say what came back, and that the relay already retried.
        fl = row.get("floor") if isinstance(row.get("floor"), Mapping) else {}
        if fl.get("content_blocks") == 0:
            if fl.get("relay_retry") == "gave_up":
                n = len(fl.get("relay_attempts") or ())
                return f"empty reply, retried ×{n}" if n else "empty reply, relay retried"
            return "empty reply"
        return None
    if cls == "conn" and _cause_phrase(row) == SEAT_TIMEOUT_CAUSE:
        return SEAT_TIMEOUT_CAUSE
    if cls == "conn" and _cause_phrase(row) == RELAY_DEADLINE_CAUSE:
        return "relay deadline"
    if cls == "pool_pressure" and _cause_phrase(row) == RELAY_DRAIN_CAUSE:
        return "relay deploying"
    if cls == "pool_pressure" and _cause_phrase(row).startswith(SESSION_DEMOTED_CAUSE):
        return "session demoted"
    if not relay_sourced:
        return None
    return {"conn": "connection issue", "pool_pressure": "relay busy"}.get(cls)


def _model_short(model: Optional[str]) -> str:
    m = str(model or "").lower()
    for fam in ("fable", "opus", "sonnet", "haiku"):
        if fam in m:
            return fam.capitalize()
    return str(model or "fallback")


def _mins(seconds: Optional[float]) -> str:
    return "?" if seconds is None else f"{max(0, int(round(float(seconds) / 60)))}m"


def format_recovery_rider(row: Mapping[str, Any], *, seat_names: bool = True) -> str:
    """Recovery rider: return_branch, seat, dwell/turns and the cache
    EXPECTATION (never a claim; the outcome is back-filled, not announced)."""
    branch = row.get("return_branch")
    # "sub" is relay-pool vocabulary. A primary with no seat concept (cpa/Kimi,
    # openai-codex, openrouter, xai ...) gets NO seat clause at all; a relay
    # primary whose seat is genuinely unknown says so in words (t_246ce7d6:
    # never a bare "?"). Ace 2026-09-28: "sub ?" on a Kimi return was confusing.
    seat = row.get("seat")
    if seat and not seat_names:
        seat = "a sub"
    if seat:
        on_seat = f" on {seat}"
    elif _plain_provider({"from_provider": row.get("to_provider")}):
        on_seat = ""
    else:
        on_seat = " on sub unknown"
    dwell = f"after {_mins(row.get('dwell_s'))} / {int(row.get('dwell_turns') or 0)} turns on {_model_short(row.get('from_model'))}"
    since = row.get("since_primary_call_s")
    warm = row.get("expected_warm")
    expect = "expected warm" if warm else "expected cold"
    if branch == "warm_seat":
        return f"primary eligible{on_seat}, last call {_mins(since)} ago ({expect}), {dwell}"
    if branch == "fallback_cold":
        return (f"fallback idle {_mins(row.get('fallback_idle_s'))}; both caches cold, one full "
                f"cache write ({expect}){on_seat}, {dwell}")
    if branch == "compaction":
        return (f"compaction rewrote the prefix; both caches cold, one full cache write "
                f"({expect}){on_seat}, {dwell}")
    if branch == "cap_expiry":
        return (f"primary warm copy expired; one full cache write ({expect}){on_seat}, {dwell}")
    if branch == "fallback_failed":
        return (f"fallback failed ({row.get('trigger_class') or 'quota'}), primary eligible"
                f"{on_seat} ({expect}), {dwell}")
    if branch == TRANSIENT_BRANCH:
        return f"cause was transient (connection); retrying primary ({expect}), {dwell}"
    if branch == USER_ROUTE_BRANCH:
        return f"cleared by /model ({expect}), {dwell}"
    return f"branch ?{on_seat} ({expect}), {dwell}"


def recovery_row(state: StickyState, decision: Decision, now: float, *,
                 live_session_id: Optional[str] = None) -> Dict[str, Any]:
    """Ledger-row dict for a return (feeds both the ledger and the rider)."""
    since = None if state.last_primary_call_epoch is None else now - state.last_primary_call_epoch
    last_fb = state.last_fallback_call_epoch or state.entered_at
    return {
        "kind": "recovery",
        "from_provider": state.fallback_provider, "from_model": state.fallback_model,
        "to_provider": state.primary_provider, "to_model": state.primary_model,
        "return_branch": decision.branch,
        "seat": state.last_primary_seat,
        "since_primary_call_s": since,
        "expected_warm": bool(since is not None and since < WARM_SEAT_MAX_AGE_S
                              and decision.branch in ("warm_seat", "fallback_failed")),
        "fallback_idle_s": None if last_fb is None else now - last_fb,
        "dwell_s": None if state.entered_at is None else now - state.entered_at,
        "dwell_turns": state.turns_on_fallback,
        "sticky_until_epoch": state.until_epoch,
        "trigger_class": state.cls,
        "gate_bound_expires_in_s": decision.gate_bound_expires_in_s,
        "session_id": live_session_id,
        **(decision.warm or {}),
    }
