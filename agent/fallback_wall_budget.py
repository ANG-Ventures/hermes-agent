"""Per-turn wall budget across the fallback chain while no byte has arrived (t_452c6ead).

The chain is walked lane by lane, and each lane keeps its own clocks (relay
``per_attempt_timeout_s`` 420 s, a reasoning-model stale floor of 600-840 s). On
2026-10-11 01:59-02:34 a turn walked alr -> dtlr -> btpr -> bpr on the SAME model
and the user got zero bytes for 34 minutes: two dtlr seats stalled 420 s each,
then btpr/bpr ate 429s and a 460 s dead seat (t_f2ddf6d1, defect 3).

This module adds one clock that spans the lanes. It is armed when an API call
starts waiting and disarmed by the first real stream event (a keepalive ping does
not count) or a completed response. While armed:

* every ``fallback.still_waiting_notice_seconds`` (120) the chat gets a
  "still waiting" line naming how many lanes were tried and what is left ahead;
* once ``fallback.no_first_byte_budget_seconds`` (600) is spent, the current
  attempt is cut (once per wait, and only when a usable lane is still ahead),
  and the native chain walker takes the NEXT lane in ``fallback_providers``
  order, whatever model it serves.

No same-model skip (Ace ruling 2026-10-12 01:43 + 02:43 PT, t_778185f3). The
first version also skipped every remaining lane on the model that stalled and
only cut when a lane on a DIFFERENT model was ahead. On 2026-10-11 23:26 that
sent a 378k-token fable turn from claude-alr straight to cpa/gpt-6.1-sol,
skipping claude-dtlr, claude-btpr and claude-bpr, which were healthy and next in
the chain. Ace: every lane in the chain is tried first; use the native stale
timer and the chain. The skip is gone because the 34-minute cascade it was
written against is now guarded where it happened: claude-bpx#505 (60 s
first-event cut on a dead TUI), claude-pool#244/#247/#248 (read-timeout bench +
valve) and claude-pool#249 (tui gate).

The 600 s default sits above every lane's healthy p99 (alr 100 s, dtlr TUI
p90 168 s / max 275 s on 2026-10-11): it ends dead air, not slow turns.

``0`` disables either knob. State lives on the agent as ``_wall_budget``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_S = 600.0
DEFAULT_NOTICE_S = 120.0


class FallbackWallBudgetExceeded(RuntimeError):
    """The attempt was cut because the turn's no-first-byte budget was spent.

    Classified ``pool_stalled`` (non-retryable, fall back): re-sending to the same
    lane re-enters the stall the budget just measured. Carries the cut evidence the
    fallback banner renders (``fallback_events`` class ``no_first_byte_budget``):
    ``lane`` (the provider cut), ``elapsed_s`` (the budget clock at the cut) and
    ``pool_headers`` (the relay attribution headers seen before the cut, ``{}`` when
    the cut came before any header)."""

    def __init__(self, message: str = "", *, lane: Optional[str] = None,
                 elapsed_s: Optional[float] = None,
                 pool_headers: Optional[dict] = None) -> None:
        super().__init__(message)
        self.lane = lane
        self.elapsed_s = elapsed_s
        self.pool_headers = dict(pool_headers or {})


def _knobs() -> tuple[float, float]:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = (load_config_readonly() or {}).get("fallback") or {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}

    def _num(key: str, default: float) -> float:
        value = cfg.get(key, default)
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return default

    return (_num("no_first_byte_budget_seconds", DEFAULT_BUDGET_S),
            _num("still_waiting_notice_seconds", DEFAULT_NOTICE_S))


def _state(agent: Any) -> Optional[dict]:
    st = getattr(agent, "_wall_budget", None)
    return st if isinstance(st, dict) else None


def reset(agent: Any) -> None:
    """Turn start: nothing is armed."""
    agent._wall_budget = None


def arm(agent: Any, now: Optional[float] = None) -> None:
    """An API call is about to wait. Keeps an already-armed clock: a fallback
    restart is the same wait continuing on another lane."""
    if _state(agent) is not None:
        return
    budget_s, notice_s = _knobs()
    agent._wall_budget = {
        "started": time.monotonic() if now is None else now,
        "budget_s": budget_s, "notice_s": notice_s,
        "lanes": 1, "notices": 0, "spent_logged": False,
    }


def disarm(agent: Any) -> None:
    """A real byte (or a full response) arrived: the wait is over."""
    if getattr(agent, "_wall_budget", None) is not None:
        agent._wall_budget = None


def note_lane_switch(agent: Any) -> None:
    st = _state(agent)
    if st is not None:
        st["lanes"] += 1


def note_excused_wait(agent: Any, seconds: float) -> None:
    """Credit the clock for a wait the upstream ASKED for, so it is not read as silence.

    2026-10-11 16:37 PT: a relay deploy drain refused every request for 126 s with
    ``503 draining-for-deploy`` + ``Retry-After``; the harness waited in place as
    designed and this clock then posted "still waiting for a first reply: 124s" into a
    healthy session. A drain (or any explicit retry-after hold) is the relay telling us
    to wait, not the provider failing to answer. Moves ``started`` forward by the slept
    seconds so ``elapsed``/``spent``/``notice_due`` all see only genuine silence.
    No-op when nothing is armed."""
    st = _state(agent)
    if st is None:
        return
    try:
        credit = float(seconds)
    except (TypeError, ValueError):
        return
    if credit > 0:
        st["started"] += credit
        st["excused_s"] = st.get("excused_s", 0.0) + credit


# t_b0a117bd: the words a failed attempt is named by in the notice.
_STATUS_WORDS = {429: "rate limited", 500: "server error", 502: "bad gateway",
                 503: "unavailable", 504: "gateway timeout", 529: "overloaded"}
_SOCKET_WORDS = {"conn_reset": "connection reset", "read_timeout": "read timeout",
                 "connect_timeout": "connect timeout", "connect_refused": "connection refused",
                 "remote_protocol": "stream dropped", "read_error": "stream dropped",
                 "connect_error": "connection failed", "pool_timeout": "pool timeout",
                 "tls": "TLS error", "dns": "DNS failure"}
# Where a failure happened, by the hop the relay stated (``x-relay-error-hop``,
# fallback_policy.normalize_hop). Apollo 2026-10-28 20:2x: the 17:00-20:00 stall
# was our uplink (relay_send p90 80-117 s, upstream_ttfb p90 13-19 s) and the 502s
# were ECONNRESET on our path; the notice must never blame the provider for that.
_HOP_WHERE = {"relay": "at our relay", "relay→bridge": "between relay and box",
              "bridge→anthropic": "between box and provider"}


def _failure_phrase(code: Optional[int], socket: Optional[str], exc_name: Optional[str],
                    hop: Optional[str], relay_class: Optional[str], relay_error: Optional[str],
                    text: str, lane: str) -> Optional[str]:
    """One failed attempt in plain words, naming the stage it failed at.

    A transport fault (reset, stream drop, connect/read timeout, 408, relay class
    ``conn``) is a connection fault on our path, never "Anthropic 502". The
    provider is named only for a status the relay marks as the provider's answer
    (hop ``bridge->upstream`` and not class ``conn``)."""
    t = (text or "").lower()
    transport = (code == 408 or (code is None and bool(socket or exc_name))
                 or (relay_class or "").lower() == "conn"
                 or "econnreset" in t or "connection reset" in t)
    if transport:
        if socket:
            what = _SOCKET_WORDS.get(socket, socket.replace("_", " "))
        elif "econnreset" in t or "reset" in t:
            what = "connection reset"
        elif relay_error and "timed out" in relay_error:
            what = "timed out"
        elif relay_error and "unreachable" in relay_error:
            what = "connection failed"
        elif code == 408:
            what = "request timed out"
        else:
            what = "connection fault"
        where = _HOP_WHERE.get(hop or "", "")
        # No stated hop: name the fault, not a place (a direct lane has no relay).
        return f"{what} {where}".rstrip() + (f" ({code})" if code else "")
    if code is None:
        return None
    words = f"{code} {_STATUS_WORDS.get(code, 'error')}"
    if hop == "bridge→anthropic":
        who = "Anthropic" if lane.startswith("claude-") else "provider"
        return f"{who} {words}"
    if hop == "relay":
        return f"{words} at our relay" + (f" ({relay_error})" if relay_error else "")
    if hop in _HOP_WHERE:
        return f"{words} {_HOP_WHERE[hop]}"
    # No relay attribution: say the status, never who sent it.
    return f"HTTP {words}"


def note_upstream_failure(agent: Any, *, status: Any = None, socket: Optional[str] = None,
                          exc_name: Optional[str] = None, box: Optional[str] = None,
                          hop: Optional[str] = None, relay_class: Optional[str] = None,
                          relay_error: Optional[str] = None, text: Any = None) -> None:
    """An attempt of the armed wait failed (status, reset, stream drop).

    Ace 2026-10-28 17:58 PT: 47 bare "still waiting for a first reply: 120s, 1 lane tried"
    lines in one hour while the same waits had seen failures. The notice now names the
    last failure and the stage it failed at, how many times that same failure repeated
    in this wait, and the box when the relay said it. No-op when nothing is armed."""
    st = _state(agent)
    if st is None:
        return
    try:
        code = int(status) if status is not None else None
    except (TypeError, ValueError):
        code = None
    if code is not None and code < 400:
        code = None   # a 200 head that then dropped is a drop, not a status
    phrase = _failure_phrase(code, socket, exc_name, hop, relay_class, relay_error,
                             str(text or ""), str(getattr(agent, "provider", "") or ""))
    if not phrase:
        return
    counts = st.setdefault("failure_counts", {})
    counts[phrase] = int(counts.get(phrase, 0)) + 1
    st["last_failure"] = phrase
    st["last_box"] = (str(box).strip() or None) if box else None


def _failure_text(st: dict) -> str:
    """``last failure: connection reset between relay and box (502) ×3 on sub-vps-5``,
    or "" when nothing failed. The count is how often THAT failure repeated."""
    last = st.get("last_failure")
    if not last:
        return ""
    n = int((st.get("failure_counts") or {}).get(last, 0))
    count = f" ×{n}" if n > 1 else ""
    box = f" on {st['last_box']}" if st.get("last_box") else ""
    return f"last failure: {last}{count}{box}"


def elapsed(agent: Any, now: Optional[float] = None) -> Optional[float]:
    st = _state(agent)
    if st is None:
        return None
    return (time.monotonic() if now is None else now) - st["started"]


def spent(agent: Any, now: Optional[float] = None) -> bool:
    st = _state(agent)
    if st is None or st["budget_s"] <= 0:
        return False
    return elapsed(agent, now) >= st["budget_s"]


def _remaining_entries(agent: Any) -> list:
    chain = getattr(agent, "_fallback_chain", None) or []
    idx = int(getattr(agent, "_fallback_index", 0) or 0)
    return [fb for fb in chain[idx:] if isinstance(fb, dict)]


def _viable(agent: Any, fb: dict) -> bool:
    """The walker's side-effect-free filters: an entry it would skip anyway is no
    reason to cut the attempt (Prism a54a5cb796b0)."""
    provider = str(fb.get("provider") or "").strip().lower()
    model = str(fb.get("model") or "").strip()
    if not provider or not model:
        return False
    key = (provider, model, str(fb.get("base_url") or "").strip().rstrip("/"))
    if key in (getattr(agent, "_unavailable_fallback_keys", None) or ()):
        return False
    if provider in (getattr(agent, "_quota_gate_skipped_providers", None) or ()):
        return False
    try:
        from agent.fallback_cooldown import _is_entitlement_rejected
        if _is_entitlement_rejected(agent, provider, model):
            return False
        from agent.fallback_capability import lane_incapable_shape, stamped_request_shape
        if lane_incapable_shape(provider, stamped_request_shape(agent)):
            return False
    except Exception:
        logger.debug("wall budget: viability probe failed for %s/%s", provider, model, exc_info=True)
    return True


def _lane_ahead(agent: Any) -> bool:
    """A usable lane is still ahead in the chain, on any model."""
    return any(_viable(agent, fb) for fb in _remaining_entries(agent))


def should_cut_attempt(agent: Any, now: Optional[float] = None) -> bool:
    """Cut the in-flight attempt: budget spent and a lane is still ahead to walk to.
    Fires once per wait, so the lane the cut moves to gets its own clocks instead of
    an instant cut. With nothing ahead the native per-lane timers keep the attempt."""
    st = _state(agent)
    if st is None or st.get("fired") or not spent(agent, now) or not _lane_ahead(agent):
        return False
    st["fired"] = True
    return True


def notice_due(agent: Any, now: Optional[float] = None) -> Optional[str]:
    """The next "still waiting" line when its interval has passed, else None (one per interval)."""
    st = _state(agent)
    if st is None or st["notice_s"] <= 0:
        return None
    waited = elapsed(agent, now) or 0.0
    due = int(waited // st["notice_s"])
    if due <= st["notices"]:
        return None
    st["notices"] = due
    lanes = st["lanes"]
    route = f"{getattr(agent, 'provider', '') or '?'}/{getattr(agent, 'model', '') or '?'}"
    # t_94a2b0e4: the lane holds a recorded error (a 429/503 and its Retry-After
    # sleep): say what we are waiting on, never "turn in progress / buffering".
    from agent.fallback_cause import wait_notice
    waiting = wait_notice(agent)
    if waiting:
        return f"{waiting}; {int(waited)}s elapsed{_ahead_suffix(agent)}"
    tried = f", {lanes} lanes tried" if lanes != 1 else ""
    failed = _failure_text(st)
    if failed:
        # t_b0a117bd: this wait has seen a failure on an earlier attempt; name it
        # and the stage it failed at instead of a bare lane count (wins over the buffering wording too:
        # a lane that just failed is not a healthy turn in progress).
        return (f"⏳ still waiting for a first reply: {int(waited)}s on {route}{tried}; "
                f"{failed}, retrying{_ahead_suffix(agent)}")
    if _buffering_lane(agent):
        return (f"⏳ turn in progress on {route} (buffering lane, first byte not visible until "
                f"the turn ends): {int(waited)}s elapsed; long outputs run 2–4 min; "
                f"cut at {int(st['budget_s'])}s{_ahead_suffix(agent)}")
    return (f"⏳ still waiting for a first reply: {int(waited)}s, "
            f"{lanes} lane{'s' if lanes != 1 else ''} tried (now on {route})"
            f"{_cut_suffix(agent, st, waited) or _ahead_suffix(agent)}")


def _cut_suffix(agent: Any, st: dict, waited: float) -> str:
    """t_b0a117bd D2: genuine silence says what happens next, in the same line:
    ``; cut to next lane at 600s``. Empty when the budget is off, the cut already
    fired or is past, or no usable lane is ahead (the caller then keeps the
    "nothing left" suffix). Never lists the chain (Ace 2026-10-16)."""
    budget = st.get("budget_s") or 0
    if budget <= 0 or st.get("fired") or waited >= budget or not _lane_ahead(agent):
        return ""
    return f"; cut to next lane at {int(budget)}s"


def _buffering_lane(agent: Any) -> bool:
    """The claude pool relays (alr/dtlr/btpr/bpr faces) buffer the whole upstream body,
    so no byte is visible here until the turn ends (t_e35032e2): a long healthy turn
    reads as "no first reply". Wording only; the clock and the cut are unchanged.
    t_8d6b8e30: a relay that says ``x-relay-streaming: 1`` streams after its commit
    point, so its wait IS a real no-first-byte wait and keeps the plain wording."""
    from agent.fork_ext.relay_headers import _POOL_CAPABILITY_PROVIDERS, relay_streams
    if (getattr(agent, "provider", "") or "").strip().lower() not in _POOL_CAPABILITY_PROVIDERS:
        return False
    return not relay_streams(getattr(agent, "base_url", "") or "")


def _ahead_suffix(agent: Any) -> str:
    """What the notice says about the chain. Ace (2026-10-16 00:29, after three 600 s
    notices in one thread): "omit the fallback chain, I know what it is". The chain is
    config he set; repeating it on every notice is noise. The ONE state worth a word is
    when there is nothing left to fall to, because then the cut ends the turn instead of
    moving it. So: empty string normally, ``; nothing left in the fallback chain`` when
    ``_remaining_entries`` is empty. ``_ahead_text`` stays for the cut banner/tests."""
    return "" if _remaining_entries(agent) else "; nothing left in the fallback chain"


def _ahead_text(agent: Any) -> str:
    """The rest of the chain, consecutive lanes grouped by model:
    ``ahead: claude-btpr, claude-bpr [claude-fable-5-1] + cpa [gpt-6.1-sol]``."""
    groups: list[tuple[str, list[str]]] = []
    for fb in _remaining_entries(agent):
        model = str(fb.get("model") or "?").strip() or "?"
        provider = str(fb.get("provider") or "?").strip() or "?"
        if groups and groups[-1][0] == model:
            groups[-1][1].append(provider)
        else:
            groups.append((model, [provider]))
    if not groups:
        return "nothing left in the fallback chain"
    return "ahead: " + " + ".join(f"{', '.join(p)} [{m}]" for m, p in groups)
