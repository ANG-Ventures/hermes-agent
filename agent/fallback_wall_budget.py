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

Cross-session dedupe (t_78b5a5bb). On 2026-10-28 17:00-18:00 PT five Discord sessions
on one gateway each posted their own "still waiting" line every 120 s: 47 identical
lines, all from ONE slow window on claude-alr/claude-fable-5-1. One session cannot see
its siblings, so this module keeps a process-local registry of armed waits
(``_REGISTRY``, guarded by ``_LOCK``). When 2+ sessions are slow (past their own
notice interval) on the SAME lane (provider, model, base_url), a session's notice
becomes ``⏳ N sessions waiting on <lane> ...`` once (never "provider slow": the
2026-10-28 window was our uplink, not the provider), and later
intervals of that wait post nothing until the condition changes (lane switch, the cut
fired, back to solo). At 3+ slow sessions one line goes to #logs (never #alerts), and
one more when the lane has no slow waiter left; at most one such pair per lane per
30 min. ``fallback_wall_budget.cross_session_dedupe: false`` turns all of it off.

Model with no capacity (Ace 2026-11-13 20:50 PT). ``⏳ 12 sessions waiting on
claude-dtlr/claude-fable-5-2 (oldest 2429s); this turn 120s, cut at 600s`` read like a
normal queue while the Fable weekly allowance was used up on 23 of 25 seats: the relay
kept retrying drained seats and every turn sat out the full 600 s. On a pool relay the
notice now names the relay's own capacity answer (``agent.fork_ext.relay_capacity``),
and once a wait passes :data:`EXHAUSTED_PROBE_AFTER_S` with the relay stating the model
has no capacity (allowance used up on every seat, or no seat eligible) the attempt is
cut at once as ``RelayModelExhausted`` (a relay ``quota_model``), once per lane, when a
usable lane is ahead. The 600 s budget is unchanged for everything else.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_S = 600.0
DEFAULT_NOTICE_S = 120.0
#: A wait this old asks the relay whether it can serve the model at all (cached, so a
#: healthy call that answers sooner never probes).
EXHAUSTED_PROBE_AFTER_S = 10.0

# --- cross-session registry (t_78b5a5bb) -------------------------------------------
#: Waiters at or above this many on one lane open a #logs slow window.
WINDOW_OPEN_SESSIONS = 3
#: At most one open/close pair per lane in this many seconds.
WINDOW_RATE_LIMIT_S = 30 * 60
#: A registered wait not polled for this long (and not inside a declared sleep) is a
#: turn that ended without a disarm (an exception, a turn-start reset): not a waiter.
STALE_S = 60.0
#: Statuses counted in the window's close line.
_WINDOW_STATUSES = frozenset({502, 529})

_LOCK = threading.Lock()
#: id(state dict) -> {"agent", "st", "session"}. The entry is live only while
#: ``agent._wall_budget is st``, so a turn reset that bypasses ``disarm`` cannot leave
#: a phantom waiter behind.
_REGISTRY: dict = {}
#: lane key -> window record (open flag, last open time, stats of the slow episode).
_LANES: dict = {}


def _default_post_logs(text: str) -> None:
    """One line to #logs through the gateway's notify.py sender, off the caller's thread
    (the caller is a stream monitor). Same sender the kanban #logs lines use."""
    def _send() -> None:
        try:
            from gateway import kanban_watchers as _kw

            script = _kw._alert_notify_script()  # noqa: SLF001
            if script is None:
                logger.info("wall budget: notify.py not found; #logs line not sent: %s", text)
                return
            _kw._notify_send(script, text, ["--target", _kw._needs_input_channels()[1]])  # noqa: SLF001
        except Exception:
            logger.debug("wall budget: #logs line not sent: %s", text, exc_info=True)

    threading.Thread(target=_send, name="wall-budget-logs", daemon=True).start()


#: Seam (tests): ``(text) -> None`` posting one #logs line.
post_logs: Callable[[str], None] = _default_post_logs


def clear_registry() -> None:
    """Forget every registered wait and lane window (tests)."""
    with _LOCK:
        _REGISTRY.clear()
        _LANES.clear()


def _dedupe_on() -> bool:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = (load_config_readonly() or {}).get("fallback_wall_budget") or {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        return True
    return cfg.get("cross_session_dedupe", True) is not False


def _lane_key(agent: Any) -> tuple:
    return (str(getattr(agent, "provider", "") or "").strip().lower(),
            str(getattr(agent, "model", "") or "").strip(),
            str(getattr(agent, "base_url", "") or "").strip().rstrip("/"))


def _lane_label(lane: tuple) -> str:
    return f"{lane[0] or '?'}/{lane[1] or '?'}"


def _session_key(agent: Any) -> str:
    return str(getattr(agent, "session_id", None) or f"agent:{id(agent)}")


def _register(agent: Any, st: dict, now: float) -> None:
    st["seen"] = now
    with _LOCK:
        _REGISTRY[id(st)] = {"agent": agent, "st": st, "session": _session_key(agent)}
        # A waiter that vanished without a disarm (turn reset, exception) is pruned
        # lazily; settle any open window whose last waiter went that way.
        lines = [ln for lane, rec in list(_LANES.items()) if rec["open"]
                 for ln in _evaluate_locked(lane, now)]
    _send_logs(lines)


def _unregister(agent: Any, st: dict, now: float) -> list:
    """Drop the wait; returns the #logs lines its end makes due (send outside the lock)."""
    with _LOCK:
        _REGISTRY.pop(id(st), None)
        lane = _lane_key(agent)
        rec = _LANES.get(lane)
        if rec is not None:
            rec["peak"] = max(rec["peak"], now - st["started"])
        return _evaluate_locked(lane, now)


def _live_waiters_locked(lane: tuple, now: float) -> list:
    """(session, waited, slow) for every live registered wait on ``lane``; prunes the dead."""
    out = []
    for key, ent in list(_REGISTRY.items()):
        agent, st = ent["agent"], ent["st"]
        if getattr(agent, "_wall_budget", None) is not st:
            del _REGISTRY[key]
            continue
        if now - max(st.get("seen", 0.0), st.get("quiet_until", 0.0)) > STALE_S:
            del _REGISTRY[key]
            continue
        if _lane_key(agent) != lane:
            continue
        waited = now - st["started"]
        notice_s = st.get("notice_s") or 0.0
        out.append((ent["session"], waited, notice_s > 0 and waited >= notice_s))
    return out


def _slow_sessions(waiters: list) -> dict:
    """session -> longest wait, slow waits only (one session = one count)."""
    slow: dict = {}
    for session, waited, is_slow in waiters:
        if is_slow:
            slow[session] = max(waited, slow.get(session, 0.0))
    return slow


def _evaluate_locked(lane: tuple, now: float) -> list:
    """Advance ``lane``'s #logs window; returns the lines to send."""
    waiters = _live_waiters_locked(lane, now)
    slow = _slow_sessions(waiters)
    rec = _LANES.get(lane)
    if rec is None:
        if not slow:
            return []
        rec = _LANES[lane] = {"open": False, "last_open": None, "peak": 0.0,
                              "errors": 0, "sessions": set()}
    if slow:
        rec["sessions"].update(slow)
        rec["peak"] = max(rec["peak"], max(slow.values()))
    lines = []
    if rec["open"]:
        if not slow:
            lines.append(f"slow window over: lane {_lane_label(lane)}, "
                         f"{len(rec['sessions'])} sessions, peak wait {int(rec['peak'])}s, "
                         f"502/529 ×{rec['errors']}")
            rec.update(open=False, peak=0.0, errors=0, sessions=set())
        return lines
    if len(slow) >= WINDOW_OPEN_SESSIONS and (
            rec["last_open"] is None or now - rec["last_open"] >= WINDOW_RATE_LIMIT_S):
        rec.update(open=True, last_open=now)
        lines.append(f"⏳ slow window open: lane {_lane_label(lane)}, "
                     f"{len(slow)} sessions waiting (oldest {int(max(slow.values()))}s)")
    elif not waiters:
        rec.update(peak=0.0, errors=0, sessions=set())  # episode over, nothing was opened
    return lines


def _send_logs(lines: list) -> None:
    for line in lines:
        try:
            post_logs(line)
        except Exception:
            logger.debug("wall budget: #logs line failed: %s", line, exc_info=True)


def note_status(agent: Any, status_code: Any) -> None:
    """A provider error on the agent's current lane: counted in the window's close line
    (502/529 only). No-op with dedupe off or nothing armed. Never raises."""
    st = _state(agent)
    if st is None or not st.get("dedupe"):
        return
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return
    if code not in _WINDOW_STATUSES:
        return
    lane = _lane_key(agent)
    with _LOCK:
        rec = _LANES.setdefault(lane, {"open": False, "last_open": None, "peak": 0.0,
                                       "errors": 0, "sessions": set()})
        rec["errors"] += 1


def note_quiet(agent: Any, seconds: float, now: Optional[float] = None) -> None:
    """The wait is about to sleep ``seconds`` without polling (a retry backoff): keep it a
    live waiter for the sleep instead of reading the silence as a finished turn."""
    st = _state(agent)
    if st is None:
        return
    try:
        secs = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return
    st["quiet_until"] = (time.monotonic() if now is None else now) + secs


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


class RelayModelExhausted(FallbackWallBudgetExceeded):
    """The attempt was cut early: the relay states it has no capacity for the model
    (every Fable seat's allowance used up, or no seat eligible). Same recovery as the
    budget cut (fall back now), but classified as the relay's ``quota_model`` so the
    fallback banner names the quota and the sticky policy holds the session off the
    model until the relay says it can serve it again. ``relay_headers`` carries the
    relay's capacity answer in its refusal-header form (``x-pool-fable-allowance``)."""

    def __init__(self, message: str = "", *, capacity: Optional[dict] = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        from agent.fork_ext.relay_capacity import relay_headers
        self.capacity = dict(capacity or {})
        self.relay_headers = relay_headers(self.capacity)


def cut_exception(agent: Any, model: Any, **evidence: Any) -> FallbackWallBudgetExceeded:
    """The exception for the cut :func:`should_cut_attempt` just ordered."""
    st = _state(agent) or {}
    cap = st.pop("cut_capacity", None)
    if cap is not None:
        from agent.fork_ext.relay_capacity import capacity_phrase
        why = capacity_phrase(cap) or "no capacity"
        return RelayModelExhausted(
            f"relay has no capacity for {model or 'the model'} ({why}); moving to the next lane",
            capacity=cap, **evidence)
    return FallbackWallBudgetExceeded(
        f"no first byte from {model or 'the provider'} within the turn's fallback wall budget "
        "(fallback.no_first_byte_budget_seconds); moving to the next lane", **evidence)


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
    started = time.monotonic() if now is None else now
    agent._wall_budget = {
        "started": started,
        "budget_s": budget_s, "notice_s": notice_s,
        "lanes": 1, "notices": 0, "spent_logged": False,
        "dedupe": _dedupe_on(),
    }
    if agent._wall_budget["dedupe"]:
        _register(agent, agent._wall_budget, started)


def disarm(agent: Any, now: Optional[float] = None) -> None:
    """A real byte (or a full response) arrived: the wait is over."""
    st = getattr(agent, "_wall_budget", None)
    if st is not None:
        agent._wall_budget = None
        if isinstance(st, dict) and st.get("dedupe"):
            _send_logs(_unregister(agent, st, time.monotonic() if now is None else now))


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
    if _is_quota_refusal(code, relay_class, text):
        # t_c5cddd09: this wait IS on quota, on THIS route. The clock survives a
        # fallback; the refusal does not speak for the next lane.
        st.setdefault("quota_refused", set()).add(_route_key(agent))
    phrase = _failure_phrase(code, socket, exc_name, hop, relay_class, relay_error,
                             str(text or ""), str(getattr(agent, "provider", "") or ""))
    if not phrase:
        return
    counts = st.setdefault("failure_counts", {})
    counts[phrase] = int(counts.get(phrase, 0)) + 1
    st["last_failure"] = phrase
    st["last_box"] = (str(box).strip() or None) if box else None


_QUOTA_CLASSES = ("quota_model", "quota_seat")


def _is_quota_refusal(code: Optional[int], relay_class: Optional[str], text: Any) -> bool:
    """The relay's stated class decides (t_d1de29dd, Prism #600 4dab164d36f9): a plain
    rate-limit 429 is not a quota refusal. An unclassed 429 counts only when its text
    reads as quota (the same text table the ledger uses)."""
    rc = (relay_class or "").strip().lower()
    if rc:
        return rc in _QUOTA_CLASSES
    if code != 429:
        return False
    try:
        from agent.fallback_events import classify_text
        return classify_text(str(text or ""), http_status=code) in _QUOTA_CLASSES
    except Exception:
        return False


def _route_key(agent: Any) -> tuple[str, str]:
    """The lane and model a refusal belongs to."""
    return (str(getattr(agent, "provider", "") or "").strip().lower(),
            str(getattr(agent, "model", "") or "").strip().lower())


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
    if model in (getattr(agent, "_pool_exhausted_models", None) or ()):
        from agent.relay_lanes import is_relay_backed
        if is_relay_backed(provider):
            return False
    from agent.context_ceilings import request_tokens, too_large_for
    if too_large_for(provider, model, fb.get("base_url"), request_tokens(agent)):
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


def _capacity(agent: Any) -> Optional[dict]:
    """The relay's capacity answer for the agent's current pool lane, else None."""
    try:
        from agent.fork_ext.relay_headers import _POOL_CAPABILITY_PROVIDERS
        if (getattr(agent, "provider", "") or "").strip().lower() not in _POOL_CAPABILITY_PROVIDERS:
            return None
        from agent.fork_ext.relay_capacity import model_capacity
        return model_capacity(getattr(agent, "base_url", "") or "", getattr(agent, "model", "") or "")
    except Exception:
        logger.debug("wall budget: relay capacity probe failed", exc_info=True)
        return None


def _exhausted_cut_due(agent: Any, st: dict, now: Optional[float]) -> bool:
    """The relay says this lane cannot serve its model and a usable lane is ahead: cut
    now instead of sitting out the budget. Once per lane per wait."""
    if (st.get("budget_s") or 0) <= 0 or (elapsed(agent, now) or 0.0) < EXHAUSTED_PROBE_AFTER_S:
        return False
    lane = _lane_key(agent)
    done = st.setdefault("exhausted_cut_lanes", [])
    if lane in done:
        return False
    from agent.fork_ext.relay_capacity import exhausted
    cap = _capacity(agent)
    if not exhausted(cap) or not _lane_ahead(agent):
        return False
    done.append(lane)
    st["cut_capacity"] = cap
    logger.warning("Relay reports no capacity for %s/%s (%s); cutting the attempt to fall back now.",
                   lane[0], lane[1], cap)
    return True


def should_cut_attempt(agent: Any, now: Optional[float] = None) -> bool:
    """Cut the in-flight attempt: budget spent and a lane is still ahead to walk to.
    Fires once per wait, so the lane the cut moves to gets its own clocks instead of
    an instant cut. With nothing ahead the native per-lane timers keep the attempt.
    Also cuts early, once per lane, when the relay states the model has no capacity."""
    st = _state(agent)
    if st is None:
        return False
    if _exhausted_cut_due(agent, st, now):
        return True
    if st.get("fired") or not spent(agent, now) or not _lane_ahead(agent):
        return False
    st.pop("cut_capacity", None)
    st["fired"] = True
    return True


def notice_due(agent: Any, now: Optional[float] = None) -> Optional[str]:
    """The next "still waiting" line when its interval has passed, else None (one per interval)."""
    st = _state(agent)
    if st is None or st["notice_s"] <= 0:
        return None
    now = time.monotonic() if now is None else now
    if st.get("dedupe"):
        st["seen"] = now
    waited = elapsed(agent, now) or 0.0
    due = int(waited // st["notice_s"])
    if due <= st["notices"]:
        return None
    st["notices"] = due
    if st.get("dedupe"):
        shared = _shared_notice(agent, st, waited, now)
        if shared is not None:
            return shared or None
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
    capacity = _capacity_stage(agent)   # reads /inflight only under capacity pressure
    if capacity:
        # 2026-11-13: never read like a normal queue when the relay says the model is
        # (nearly) out of capacity; say so, with the relay's own counts.
        return (f"⏳ waiting on {route}: {capacity}; {int(waited)}s elapsed{tried}"
                f"{'; ' + failed if failed else ''}"
                f"{_cut_suffix(agent, st, waited) or _ahead_suffix(agent)}")
    if failed:
        # t_b0a117bd: this wait has seen a failure on an earlier attempt; name it
        # and the stage it failed at instead of a bare lane count (wins over the buffering wording too:
        # a lane that just failed is not a healthy turn in progress).
        return (f"⏳ still waiting for a first reply: {int(waited)}s on {route}{tried}; "
                f"{failed}, retrying{_ahead_suffix(agent)}")
    stage = _relay_stage_phrase(agent)
    if stage:
        return (f"⏳ {stage}: {int(waited)}s elapsed; cut at {int(st['budget_s'])}s"
                f"{_ahead_suffix(agent)}")
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


def _allowance_phrase(info: dict, *, shared: bool) -> Optional[str]:
    """Render only producer-stamped counts; unknown eligibility is not zero."""
    allowance = info.get("allowance")
    model = info.get("model")
    if not isinstance(allowance, dict) or not isinstance(model, str) or not model:
        return None
    total, exhausted, eligible = (allowance.get(k) for k in
                                  ("seats_total", "seats_exhausted", "seats_eligible"))
    if (any(type(n) is not int for n in (total, exhausted, eligible))
            or not 0 < exhausted <= total or not 0 <= eligible <= total - exhausted):
        return None
    text = f"{model} weekly cap{',' if shared else ':'} {eligible} of {total} seats left"
    if not shared:
        inflight, seat = allowance.get("seat_inflight"), info.get("seat")
        if type(inflight) is not int or inflight < 0 or not isinstance(seat, str) or not seat:
            return None
        text += f", {inflight} turns on {seat}"
    reset = allowance.get("earliest_reset")
    if isinstance(reset, str):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from hermes_time import safe_strftime
        try:
            dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                clock = safe_strftime(dt.astimezone(ZoneInfo("America/Los_Angeles")), "%H:%M")
                text += f"{',' if shared else ' ·'} resets {clock} PT"
        except (ValueError, OverflowError):
            pass  # A missing reset must not discard the measured seat counts.
    return text


def _inflight(agent: Any) -> Optional[dict]:
    """The relay's ``/inflight`` answer for this session (one GET, <= 300 ms), else None."""
    from agent.fork_ext.relay_headers import _POOL_CAPABILITY_PROVIDERS, fetch_relay_stage
    if (getattr(agent, "provider", "") or "").strip().lower() not in _POOL_CAPABILITY_PROVIDERS:
        return None
    return fetch_relay_stage(getattr(agent, "base_url", "") or "",
                             getattr(agent, "session_id", None))


def _relay_retrying(info: Optional[dict]) -> bool:
    """The relay's current attempt started well after the request did: it already
    gave up on at least one seat for this request."""
    if not isinstance(info, dict):
        return False
    req, att = info.get("request_ms"), info.get("attempt_ms")
    ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
             for v in (req, att))
    return bool(ok and req - att >= 1000)


def _capacity_stage(agent: Any) -> Optional[str]:
    """``relay retrying seats (Fable allowance used up on 23/25 seats, 2 eligible, resets
    Sun 10:59)`` when the relay states capacity pressure on the lane's model, else None.
    A producer-stamped per-seat allowance on ``/inflight`` keeps its own wording."""
    from agent.fork_ext.relay_capacity import capacity_phrase, exhausted
    cap = _capacity(agent)
    text = capacity_phrase(cap)
    if not text:
        return None
    # t_c5cddd09: seats used up elsewhere in the pool are a pool fact, not why THIS
    # call waits. Name the allowance only when no seat can serve the model or this
    # wait saw a quota refusal; otherwise the notice says the stage it is on.
    refused = (_state(agent) or {}).get("quota_refused") or ()
    if not (exhausted(cap) or _route_key(agent) in refused):
        return None
    info = _inflight(agent)
    if isinstance(info, dict) and _allowance_phrase(info, shared=False):
        return None
    return f"relay retrying seats ({text})" if _relay_retrying(info) else text


def _relay_stage_phrase(agent: Any, *, shared: bool = False) -> Optional[str]:
    """Which hop a pool-relay wait is on, in plain words (t_9a635fbd), else None.

    2026-10-28 17:00-20:00 PT the relay's upload to the box (our uplink) ran 80-117 s
    p90 while the provider's ttfb stayed 13-19 s, and the notice could not say so.
    The relay's ``GET /inflight`` is read once per notice (<= 300 ms). Stages that
    say nothing new (streaming, a buffered body read, unknown) keep today's wording."""
    info = _inflight(agent)
    if not info:
        return None
    seat = info.get("seat") if isinstance(info.get("seat"), str) and info.get("seat") else None
    since_ms = info.get("since_ms")
    # json.loads accepts NaN/Infinity; int() of those raises in the monitor thread.
    in_stage = (f" for {int(since_ms // 1000)}s"
                if isinstance(since_ms, (int, float)) and not isinstance(since_ms, bool)
                and math.isfinite(since_ms) else "")
    stage = info.get("stage")
    allowance = _allowance_phrase(info, shared=shared)
    # Shared notices keep their existing recorded-cause wording without enrichment.
    if shared and not allowance:
        return None
    if stage == "relay_send":
        return f"request still uploading to {seat or 'the pool seat'} (our uplink){in_stage}"
    if allowance:
        return allowance
    if stage in ("box_headers", "upstream_ttfb"):
        return f"{seat or 'the pool seat'} connected, provider has not sent a first token{in_stage}"
    if stage == "queued":
        return f"queued on {seat}{in_stage}" if seat else f"queued in the relay pool{in_stage}"
    return None


def _shared_notice(agent: Any, st: dict, waited: float, now: float) -> Optional[str]:
    """The cross-session notice (t_78b5a5bb): the shared line, ``""`` (post nothing: the
    shared line of this wait already stands), or None (solo: the per-session wording)."""
    lane = _lane_key(agent)
    with _LOCK:
        if id(st) not in _REGISTRY:  # pruned during a long unpolled stretch: rejoin
            _REGISTRY[id(st)] = {"agent": agent, "st": st, "session": _session_key(agent)}
        slow = _slow_sessions(_live_waiters_locked(lane, now))
        lines = _evaluate_locked(lane, now)
    _send_logs(lines)
    if len(slow) < 2:
        st["shared"] = None  # back to solo: the next shared episode posts again
        return None
    key = (lane, bool(st.get("fired")))
    if st.get("shared") == key:
        return ""
    st["shared"] = key
    stage = (_capacity_stage(agent) or _slow_stage(agent)
             or _relay_stage_phrase(agent, shared=True))
    return (f"⏳ {len(slow)} sessions waiting on {_lane_label(lane)} "
            f"(oldest {int(max(slow.values()))}s){': ' + stage if stage else ''}; "
            f"this turn {int(waited)}s, cut at {int(st['budget_s'])}s{_ahead_suffix(agent)}")


def _slow_stage(agent: Any) -> str:
    """What is slow, only when the lane has RECORDED it (``fallback_cause``), else "".
    Never "provider slow": 2026-10-28's shared window was our uplink (Apollo 20:20,
    relay_send p90 1 s -> 80-117 s, upstream ttfb flat), not the provider. The stage
    wording itself is t_b0a117bd's; this line only carries it."""
    try:
        from agent import fallback_cause as _fbc
        cause = _fbc.peek(agent)
        if cause is None or cause.lane != (str(getattr(agent, "provider", "") or "") or None):
            return ""
        return str(_fbc.cause_text(agent, cause) or "").strip()
    except Exception:
        logger.debug("wall budget: slow-stage lookup failed", exc_info=True)
        return ""


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
