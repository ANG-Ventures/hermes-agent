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
  mid-turn), D6 warm-seat return included.
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
    "refusal", "auth", "unclassified",
)
# Classes the sticky writer arms (one clock: _sticky.until_epoch).
STICKY_CLASSES = frozenset({"conn", "pool_pressure", "quota_seat", "quota_model"})
# Classes that still reach apply_quota_gate / the legacy arm block. On a
# relay provider quota_seat, conn and pool_pressure skip both (§4.1 plumbing).
QUOTA_GATE_CLASSES = frozenset({"quota_model", "rate_upstream", "refusal", "unclassified"})
# §4.3 fallback_failed: quota classes only.
FALLBACK_FAILED_CLASSES = frozenset({"quota_seat", "quota_model", "rate_upstream"})

MIN = 60.0
HOUR = 3600.0
WARM_SEAT_MAX_AGE_S = 55 * MIN
FALLBACK_COLD_S = 60 * MIN
BOUND_EXPIRES_MIN_S = 60.0
ELIGIBILITY_STALE_S = 300.0
ELIGIBILITY_TIMEOUT_S = 0.5
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

_DIRECT_PIN_RE = re.compile(r"^claude-([ab])px-(\d+)$")


def direct_pin(provider: Optional[str]) -> Optional[Tuple[str, int]]:
    """``('b', 16)`` for ``claude-bpx-16``; None for relay / other providers."""
    m = _DIRECT_PIN_RE.fullmatch(str(provider or "").strip().lower())
    return (m.group(1), int(m.group(2))) if m else None


def is_direct_pin(provider: Optional[str]) -> bool:
    return direct_pin(provider) is not None


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
    if seat_ok and elig.bound_eligible and ttl_ok:
        return True, f"warm_seat: bound seat {seat} eligible", exp
    if elig.warm_rank_effective == "enforce" and elig.warm_eligible:
        return True, "warm_seat: warm eligible seat (warm_rank=enforce)", exp
    why = []
    if not seat_ok:
        why.append(f"bound_seat {elig.bound_seat!r} != last_primary_seat {seat!r}")
    if not elig.bound_eligible:
        why.append("bound seat not eligible")
    if not ttl_ok:
        why.append(f"bound_expires_in_s {exp} < 60")
    return False, "warm_seat: " + "; ".join(why), exp


def restore_allowed(state: Optional[StickyState], now: float, *, probe: bool = False,
                    live_session_id: Optional[str] = None,
                    primary_provider: str = "",
                    eligibility: Optional[EligibilityFn] = None,
                    direct_pin_benched: Optional[BenchFn] = None,
                    sticky_policy: bool = True) -> Decision:
    """§4.3: return iff (now >= until AND (warm_seat|fallback_cold|compaction)).

    ``probe=False`` is the cheap form (no network I/O): until, active,
    fallback_cold, compaction. ``probe=True`` adds warm_seat. The mid-turn
    ``fallback_failed`` branch is :func:`fallback_failed_allowed`.
    """
    if not sticky_policy or state is None or not state.active:
        return Decision(True, None, "no active sticky state")
    if now < state.until_epoch:
        return Decision(False, None, f"until: {state.until_epoch - now:.0f}s remaining")
    reasons: List[str] = []
    exp: Optional[float] = None
    if probe:
        ok, why, exp = _warm_seat(state, now, primary_provider=primary_provider or state.primary_provider,
                                  eligibility=eligibility, direct_pin_benched=direct_pin_benched)
        if ok:
            return Decision(True, "warm_seat", why, exp)
        reasons.append(why)
    last_fb = state.last_fallback_call_epoch
    if last_fb is None:
        last_fb = state.entered_at
    if last_fb is not None and now - last_fb > FALLBACK_COLD_S:
        return Decision(True, "fallback_cold", f"fallback idle {now - last_fb:.0f}s", exp)
    reasons.append("fallback_cold: last fallback call <= 60 min ago")
    if (live_session_id and state.last_fallback_session_id
            and live_session_id != state.last_fallback_session_id):
        return Decision(True, "compaction", "session_id rotated since last fallback call", exp)
    reasons.append("compaction: no session_id rotation")
    return Decision(False, None, " | ".join(reasons), exp)


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
                   sticky_policy: bool = True) -> RebuildDecision:
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
    d = restore_allowed(state, now, probe=True, live_session_id=live_session_id,
                        primary_provider=key.primary_provider, eligibility=eligibility,
                        direct_pin_benched=direct_pin_benched)
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
        "client→proxy", "proxy→anthropic", "client→bridge")
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


def _cause_phrase(row: Mapping[str, Any]) -> str:
    cls = row.get("trigger_class") or "unclassified"
    t = str(row.get("err_head") or row.get("err_text") or "").lower()
    if cls == "conn":
        if "reset" in t:
            return "connection reset"
        if "connect" in t and "time" in t:
            return "connect timeout"
        if "incomplete" in t:
            return "incomplete read"
        if "timed out" in t or "timeout" in t:
            return "read timeout"
        return "connection error"
    if cls == "pool_pressure":
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
    return {
        "rate_upstream": "account rate limit",
        "refusal": "content policy refusal",
        "auth": "OAuth revoked (401)",
    }.get(cls, "unclassified error")


def _seat_token(row: Mapping[str, Any], seat_names: bool) -> str:
    seat = row.get("seat")
    if (row.get("trigger_class") == "quota_model" and not is_direct_pin(row.get("from_provider"))
            and (not seat or row.get("pool_wide"))):
        return "all subs"
    if not seat or seat == "unknown":
        return "sub ?"
    return str(seat) if seat_names else "a sub"


def _hop_segment(hop: Optional[str], seat: str, status: Any) -> str:
    st = status if status else "error"
    return {
        "client→relay": f"to the relay for {seat}",
        "relay": f"at the relay on {seat}",
        "relay→bridge": f"to {seat} bridge",
        "bridge→anthropic": f"(Anthropic {st}) on {seat}",
        "client→proxy": f"to {seat} proxy",
        "proxy→anthropic": f"(Anthropic {st}) via {seat} proxy",
        "client→bridge": f"to {seat} bridge (direct)",
    }.get(hop or "", f"hop ? on {seat}")


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
                       tz: Optional[_dt.tzinfo] = None) -> str:
    """Four mandatory fields: cause, hop, sub, count+window (§4.8).

    Built from the Phase 1 ``fallback_events`` row dict. Unknown hop/sub
    render as the literal ``hop ?`` / ``sub ?``, never omitted or guessed.
    """
    prefix, window = _count_window(row, tz)
    seat = _seat_token(row, seat_names)
    hop = normalize_hop(row.get("hop"))
    return f"{prefix}{_cause_phrase(row)} {_hop_segment(hop, seat, row.get('http_status'))}, {window}"


def head_label_override(row: Mapping[str, Any]) -> Optional[str]:
    """The one head-label byte change (§4.8): relay-sourced conn/pool_pressure
    name the real cause instead of the status-derived ``rate limit``. When
    this returns a label, ``_quota_window_suffix`` is not applied."""
    cls = row.get("trigger_class")
    relay_sourced = (row.get("class_source") in ("relay_header", "relay_stream")
                     or bool(row.get("relay_synthetic")))
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
    seat = row.get("seat") or "sub ?"
    if seat != "sub ?" and not seat_names:
        seat = "a sub"
    dwell = f"after {_mins(row.get('dwell_s'))} / {int(row.get('dwell_turns') or 0)} turns on {_model_short(row.get('from_model'))}"
    since = row.get("since_primary_call_s")
    warm = row.get("expected_warm")
    expect = "expected warm" if warm else "expected cold"
    if branch == "warm_seat":
        return f"primary eligible on {seat}, last call {_mins(since)} ago ({expect}), {dwell}"
    if branch == "fallback_cold":
        return (f"fallback idle {_mins(row.get('fallback_idle_s'))}; both caches cold, one full "
                f"cache write ({expect}) on {seat}, {dwell}")
    if branch == "compaction":
        return (f"compaction rewrote the prefix; both caches cold, one full cache write "
                f"({expect}) on {seat}, {dwell}")
    if branch == "fallback_failed":
        return (f"fallback failed ({row.get('trigger_class') or 'quota'}), primary eligible on "
                f"{seat} ({expect}), {dwell}")
    return f"branch ? on {seat} ({expect}), {dwell}"


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
    }
