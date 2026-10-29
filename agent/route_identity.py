"""Route identity and the per-call route-change note (spec 2026-10-29 model-change-notes v3).

One definition of "what is serving": ``RouteIdentity = (model, effort, speed_tier)``. Provider,
pool, seat and relay are deliberately NOT part of it; a same-model hop between pools is not a
change the model needs to hear about.

``Known`` is what the model was last told: ``{intended, serving, observed_tier,
pending_correction}``. On every API call (every rung of a fallback ladder) the loop builds
``Now = {intended, serving_requested}`` for the rung about to be tried; when it differs from
``Known`` one short note rides THAT request only. On success the note row is persisted to the
transcript first, then ``Known := Now`` is written to the session sink (gateway ``SessionEntry``).
A crash between the two re-sends the note once; the reverse order would lose it.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ROUTE_NOTE_DISPLAY_KIND = "route_note"
ROUTE_NOTE_OPENER = "[Route note:"
MAX_NOTE_CHARS = 200
MAX_NOTES_PER_TURN = 2

STANDARD, FAST, ULTRA = "standard", "fast", "ultra"
# The ONE vendor-name table (request words, OpenAI ``service_tier`` values, Anthropic ``speed``).
_TIER_TABLE = {
    "priority": FAST, "fast": FAST, "on": FAST,
    "ultrafast": ULTRA, "ultra": ULTRA, "ultra-fast": ULTRA, "ultra_fast": ULTRA,
}


def normalize_tier(raw: Any) -> str:
    """``auto``/``default``/absent/unknown -> ``standard``; vendor names map only here."""
    return _TIER_TABLE.get(str(raw or "").strip().lower(), STANDARD)


@dataclass(frozen=True)
class RouteIdentity:
    model: str
    effort: Optional[str]
    speed_tier: str = STANDARD

    def to_dict(self) -> Dict[str, Any]:
        return {"model": self.model, "effort": self.effort, "speed_tier": self.speed_tier}

    @classmethod
    def from_dict(cls, data: Any) -> Optional["RouteIdentity"]:
        if not isinstance(data, dict) or not isinstance(data.get("model"), str) or not data["model"]:
            return None
        effort = data.get("effort")
        return cls(data["model"], effort if isinstance(effort, str) and effort else None,
                   normalize_tier(data.get("speed_tier")))

    def compact(self) -> str:
        return f"{self.model}@{self.effort or '-'}/{self.speed_tier}"


@dataclass(frozen=True)
class Known:
    intended: RouteIdentity
    serving: RouteIdentity
    observed_tier: Optional[Tuple[str, str]] = None  # (requested, reported)
    pending_correction: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intended": self.intended.to_dict(), "serving": self.serving.to_dict(),
            "observed_tier": ({"requested": self.observed_tier[0], "reported": self.observed_tier[1]}
                              if self.observed_tier else None),
            "pending_correction": self.pending_correction,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["Known"]:
        if not isinstance(data, dict):
            return None
        intended = RouteIdentity.from_dict(data.get("intended"))
        serving = RouteIdentity.from_dict(data.get("serving"))
        if intended is None or serving is None:
            return None
        obs = data.get("observed_tier")
        observed = ((normalize_tier(obs.get("requested")), normalize_tier(obs.get("reported")))
                    if isinstance(obs, dict) else None)
        pending = data.get("pending_correction")
        return cls(intended, serving, observed, pending if isinstance(pending, str) and pending else None)


@dataclass(frozen=True)
class Now:
    intended: RouteIdentity
    serving: RouteIdentity  # REQUESTED serving identity for the rung about to be tried


def effective_tier(known: Optional[Known], requested_tier: str) -> str:
    """The provider's last reported tier for this exact request, else the request itself."""
    obs = known.observed_tier if known else None
    return obs[1] if obs and requested_tier == obs[0] else requested_tier


# --- reason enum (closed; raw provider text never enters a note) -------------------------------
_REASONS = ("rate limit", "weekly cap", "quota", "provider overloaded", "connection dropped",
            "safety refusal", "relay deploying", "unavailable")
_REASON_MAP = {
    "rate_limit": "rate limit", "upstream_rate_limit": "rate limit", "rate_upstream": "rate limit",
    "pool_exhausted": "weekly cap", "quota_seat": "weekly cap",
    "billing": "quota", "extra_usage_only": "quota", "model_entitlement": "quota", "quota_model": "quota",
    "overloaded": "provider overloaded", "server_error": "provider overloaded",
    "pool_stalled": "provider overloaded", "session_busy": "provider overloaded",
    "pool_pressure": "provider overloaded",
    "timeout": "connection dropped", "stream_parse": "connection dropped",
    "decode_error": "connection dropped", "conn": "connection dropped",
    "content_policy_blocked": "safety refusal", "refusal": "safety refusal",
    "relay_draining": "relay deploying",
}


def reason_label(failover_reason: Any) -> str:
    """Map a ``FailoverReason`` (or its value / a fallback_events class) onto the closed enum."""
    key = str(getattr(failover_reason, "value", failover_reason) or "").strip().lower()
    return _REASON_MAP.get(key, "unavailable")


# --- renderer ----------------------------------------------------------------------------------
def model_label(model: str) -> str:
    try:
        from hermes_cli.model_switch import format_model_for_display
        model = format_model_for_display(model) or model
    except Exception:
        pass
    model = model.rsplit("/", 1)[-1]
    return model[len("claude-"):] if model.startswith("claude-") and len(model) > 7 else model


def _delta(old: RouteIdentity, new: RouteIdentity) -> List[str]:
    parts = []
    if old.model != new.model:
        parts.append(f"model {model_label(old.model)} → {model_label(new.model)}")
    if old.effort != new.effort:
        parts.append(f"effort {old.effort or 'default'} → {new.effort or 'default'}")
    if old.speed_tier != new.speed_tier:
        parts.append(f"speed {old.speed_tier} → {new.speed_tier}")
    return parts


def _wrap(body: str, correction: Optional[str] = None) -> str:
    """One note ≤ ``MAX_NOTE_CHARS`` (E10). A pending tier ``correction`` body is reserved first:
    when the two do not fit, the change body is cut, never the correction, because committing
    the call clears ``pending_correction`` on the strength of this note having carried it."""
    if correction:
        budget = MAX_NOTE_CHARS - len(f"{ROUTE_NOTE_OPENER} ; {correction}.]")
        if len(body) > budget:
            body = body[:max(budget, 0)].rstrip()
        body = f"{body}; {correction}" if body else correction
    note = f"{ROUTE_NOTE_OPENER} {body}.]"
    return note if len(note) <= MAX_NOTE_CHARS else note[:MAX_NOTE_CHARS - 2] + ".]"


def correction_note(requested: str, reported: str) -> str:
    return _wrap(f"speed {requested} → {reported} (provider served {reported})")


def _body(note: Optional[str]) -> str:
    return note[len(ROUTE_NOTE_OPENER):].strip().rstrip("]").rstrip(".") if note else ""


def _describe(ident: RouteIdentity, other: RouteIdentity) -> str:
    """``ident`` by the fields where it differs from ``other`` (model first)."""
    parts = []
    if ident.model != other.model:
        parts.append(model_label(ident.model))
    if ident.effort != other.effort:
        parts.append(f"effort {ident.effort or 'default'}")
    if ident.speed_tier != other.speed_tier:
        parts.append(f"speed {ident.speed_tier}")
    return " ".join(parts) or model_label(ident.model)


def render_note(known: Optional[Known], now: Now, reason: str = "unavailable") -> Optional[str]:
    """The note for ``now`` against ``known``; ``None`` when nothing the model knows changed."""
    if reason not in _REASONS:
        reason = "unavailable"
    if known is None:  # no baseline: the session starts on its intended route
        known = Known(now.intended, now.intended)
    pending = known.pending_correction
    serving = replace(now.serving, speed_tier=effective_tier(known, now.serving.speed_tier))
    # A pending correction owns the tier axis: comparing it again would produce a reversed note.
    tier_covered = pending is not None
    base = replace(known.serving, speed_tier=serving.speed_tier) if tier_covered else known.serving
    changes = _delta(base, serving)
    body = None
    if now.intended != known.intended:
        if serving != now.intended:  # R2: what the user asked for AND what actually serves
            body = (f"the user selected {_describe(now.intended, serving)}; it is unavailable ({reason}): "
                    f"you are running on {_describe(serving, now.intended)}")
        elif serving.model != known.serving.model:
            body = "; ".join([f"the user switched to {model_label(serving.model)}"] + changes[1:])
        elif changes:
            body = "; ".join(changes)
        else:  # E7: the pin moved onto the route already serving
            body = f"the user selected {_describe(now.intended, known.intended)}"
    elif changes:
        if serving == now.intended:  # R4
            body = (f"{model_label(serving.model)} restored" if serving.model != known.serving.model
                    else "; ".join(changes) + " (restored)")
        elif serving.model != known.serving.model:  # R1
            body = "; ".join([f"fell back to {model_label(serving.model)} ({reason})"] + changes[1:])
        else:
            body = "; ".join(changes)
    correction = _body(pending) or None
    if body is None and correction is None:
        return None
    return _wrap(body or "", correction)


# --- runtime reads (post-swap agent, never config defaults) ------------------------------------
def _effort(cfg: Any) -> Optional[str]:
    from agent.chat_completion_helpers import _effort_label
    return _effort_label(cfg) or None


def compute_now(agent: Any) -> Optional[Now]:
    model = str(getattr(agent, "model", "") or "")
    if not model:
        return None
    rt = getattr(agent, "_primary_runtime", None)
    rt = rt if isinstance(rt, dict) else {}
    overrides = getattr(agent, "request_overrides", None) or {}
    serving = RouteIdentity(model, _effort(getattr(agent, "reasoning_config", None)),
                            normalize_tier(overrides.get("service_tier") or overrides.get("speed")))
    if "reasoning_config" in rt:
        intended_effort = _effort(rt["reasoning_config"])
    elif getattr(agent, "_fallback_activated", False) is True:
        intended_effort = getattr(agent, "_route_primary_effort", serving.effort)
    else:  # on the primary: what it serves IS the intended effort; remember it for a later fallback
        intended_effort = agent._route_primary_effort = serving.effort
    intended = RouteIdentity(str(rt.get("model") or model), intended_effort,
                             normalize_tier(getattr(agent, "service_tier", None)))
    return Now(intended, serving)


def _current_reason(agent: Any) -> str:
    ev = getattr(agent, "_last_fallback_event", None)
    return reason_label(ev.get("reason")) if isinstance(ev, dict) else "unavailable"


def route_notes_enabled() -> bool:
    try:
        from hermes_cli.config import read_raw_config_readonly
        model_cfg = (read_raw_config_readonly() or {}).get("model")
        return bool(model_cfg.get("route_notes", True)) if isinstance(model_cfg, dict) else True
    except Exception:
        return True


def seed_known(agent: Any, data: Any, sink: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """Bind the session's persisted ``Known`` and its writer onto the agent (gateway, per turn)."""
    agent._route_known = Known.from_dict(data)
    agent._route_known_sink = sink


# --- audit sink --------------------------------------------------------------------------------
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUPS = 5


def _audit(agent: Any, call_id: Any, frm: Optional[RouteIdentity], to: RouteIdentity, reason: str) -> None:
    try:
        from hermes_constants import get_hermes_home
        state_dir = os.path.join(str(get_hermes_home()), "state")
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, "model-route-changes.log")
        if os.path.exists(path) and os.path.getsize(path) >= _LOG_MAX_BYTES:
            for i in range(_LOG_BACKUPS - 1, 0, -1):
                if os.path.exists(f"{path}.{i}"):
                    os.replace(f"{path}.{i}", f"{path}.{i + 1}")
            os.replace(path, f"{path}.1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} route_note session={agent.session_id or '-'} "
                     f"call={call_id} from={frm.compact() if frm else '-'} to={to.compact()} "
                     f"reason={reason.replace(' ', '_')}\n")
    except Exception:
        logger.debug("route-note audit write failed", exc_info=True)


# --- per-call hook -----------------------------------------------------------------------------
@dataclass
class PendingCall:
    now: Now
    note: Optional[str]
    base_api_messages: Any
    reason: str


def _participates(agent: Any) -> bool:
    # Background-review forks and auxiliary agents neither read nor write Known (E9).
    return not getattr(agent, "_persist_disabled", False)


def _turn_state(agent: Any, turn_id: Any) -> Dict[str, Any]:
    st = getattr(agent, "_route_notes_turn", None)
    if not isinstance(st, dict) or st.get("turn_id") != turn_id:
        st = {"turn_id": turn_id, "enabled": route_notes_enabled(), "notes": 0}
        agent._route_notes_turn = st
    return st


def _with_note(api_messages: List[Dict[str, Any]], note: str) -> Optional[List[Dict[str, Any]]]:
    """A request copy carrying ``note``: prepended to a trailing user row, or a standalone user row
    after a trailing tool result. ``None`` when the tail cannot take one without breaking alternation."""
    if not api_messages:
        return None
    tail = api_messages[-1]
    role = tail.get("role") if isinstance(tail, dict) else None
    if role == "tool":
        return list(api_messages) + [{"role": "user", "content": note}]
    if role != "user":
        return None
    new_tail = dict(tail)
    content = tail.get("content")
    if isinstance(content, str):
        new_tail["content"] = f"{note}\n\n{content}"
    elif isinstance(content, list) and content and isinstance(content[0], dict) and content[0].get("type") == "text":
        # Same bytes the persisted ``api_content`` replays as (cache decoration is re-applied later).
        new_tail["content"] = [dict(content[0], text=f"{note}\n\n{content[0].get('text', '')}")] + list(content[1:])
    elif isinstance(content, list):
        new_tail["content"] = [{"type": "text", "text": note}] + list(content)
    else:
        return None
    return list(api_messages[:-1]) + [new_tail]


def prepare_call(agent: Any, s: Any) -> Optional[PendingCall]:
    """Compute ``Now`` for the rung about to be tried and attach the note to ``s.api_messages``.
    Fail-open: a bookkeeping error never costs the request."""
    try:
        return _prepare_call(agent, s)
    except Exception:
        logger.warning("route-note prepare failed", exc_info=True)
        return None


def _prepare_call(agent: Any, s: Any) -> Optional[PendingCall]:
    if not _participates(agent):
        return None
    now = compute_now(agent)
    if now is None:
        return None
    st = _turn_state(agent, getattr(s, "turn_id", None))
    known = getattr(agent, "_route_known", None)
    reason = _current_reason(agent)
    note = render_note(known, now, reason) if st["enabled"] and st["notes"] < MAX_NOTES_PER_TURN else None
    base = s.api_messages
    if note:
        noted = _with_note(base, note)
        if noted is None:
            note = None
        else:
            s.api_messages = noted
    return PendingCall(now, note, base, reason)


def _persist_note(agent: Any, s: Any, note: str) -> None:
    """Transcript write (step 1): the note becomes part of the durable history, in the same shape
    the request carried, before ``Known`` advances."""
    messages = s.messages
    tail = messages[-1] if messages else None
    if isinstance(tail, dict) and tail.get("role") == "tool":
        messages.append({"role": "user", "content": note, "display_kind": ROUTE_NOTE_DISPLAY_KIND})
    elif isinstance(tail, dict) and tail.get("role") == "user":
        content = tail.get("content")
        if isinstance(content, str):
            from agent.turn_context import compose_user_api_content
            base = tail.get("api_content")
            if not (isinstance(base, str) and base):
                base = None
                if messages.index(tail) == getattr(s, "current_turn_user_idx", None):
                    base = compose_user_api_content(content, getattr(s, "_ext_prefetch_cache", "") or "",
                                                    getattr(s, "_plugin_user_context", "") or "")
                base = base or content
            api_content = f"{note}\n\n{base.strip()}"
            from agent.session_persistence import _persist_lock, durable_user_row_content
            durable, api_content = durable_user_row_content(agent, tail, content, api_content)
            tail["api_content"] = api_content
            row_id = tail.get("_row_id") or tail.get("_db_persisted_row_id")
            db = getattr(agent, "_session_db", None)
            if db is not None and isinstance(row_id, int):
                with _persist_lock(agent):
                    db.set_message_api_content(agent.session_id, row_id, durable, api_content)
        elif isinstance(content, list):
            tail["content"] = [{"type": "text", "text": note}] + list(content)
    if getattr(agent, "_session_db", None) is not None:
        agent._flush_messages_to_session_db(messages, getattr(s, "conversation_history", None))


def _observed_tier(agent: Any, known_obs: Optional[Tuple[str, str]], requested: str) -> Optional[Tuple[str, str]]:
    served = getattr(agent, "_served_service_tier", None)
    if not isinstance(served, str) or not served.strip():
        return None
    try:
        from hermes_cli.fast_mode_contracts import SERVED_TIER_ECHO_UNRELIABLE_PROVIDERS
        if str(getattr(agent, "provider", "") or "").strip().lower() in SERVED_TIER_ECHO_UNRELIABLE_PROVIDERS:
            return None
    except Exception:
        pass
    reported = normalize_tier(served)
    if reported == requested or known_obs == (requested, reported):
        return None
    return (requested, reported)


def abandon_call(s: Any, call: Optional[PendingCall]) -> None:
    """The rung failed or was retried: the note leaves with the request (§5 ephemeral)."""
    if call is not None:
        s.api_messages = call.base_api_messages


def call_succeeded(s: Any, action: str) -> bool:
    """``check_api_response`` accepted the response and armed no restart for this iteration."""
    retry = getattr(s, "_retry", None)
    return action == "break" and getattr(s, "response", None) is not None and not any(
        getattr(retry, f, False) for f in ("restart_with_compressed_messages", "restart_with_length_continuation",
                                            "restart_with_rebuilt_messages", "restart_with_redirected_messages"))


def commit_call_safely(agent: Any, s: Any, call: PendingCall) -> None:
    """``commit_call`` for the live loop: a failure after the transcript write leaves ``Known``
    unadvanced (the note is re-sent once), and never surfaces as an API error."""
    try:
        commit_call(agent, s, call)
    except Exception:
        abandon_call(s, call)
        logger.warning("route-note commit failed; the note will be re-sent", exc_info=True)
    return None


def commit_call(agent: Any, s: Any, call: PendingCall) -> None:
    """The rung succeeded: transcript first, then ``Known``. Any exception between the two leaves
    ``Known`` unadvanced, so the next call re-sends the note (one duplicate at most)."""
    s.api_messages = call.base_api_messages
    known: Optional[Known] = getattr(agent, "_route_known", None)
    st = _turn_state(agent, getattr(s, "turn_id", None))
    enabled = st["enabled"]
    now = call.now
    if call.note is None and enabled and render_note(known, now, call.reason) is not None:
        return  # capped this turn (E12) or no slot: keep Known so a later call carries the net change
    requested = now.serving.speed_tier
    obs = known.observed_tier if known else None
    if obs and obs[0] != requested:
        obs = None  # the requested tier changed: the next response re-establishes it (§6.5)
    if call.note is not None:
        _persist_note(agent, s, call.note)  # step 1: transcript
        st["notes"] += 1
    # A sent note carried any pending correction; the kill switch drops it unsent.
    pending = None if (call.note is not None or not enabled) else (known.pending_correction if known else None)
    serving = replace(now.serving, speed_tier=effective_tier(Known(now.intended, now.serving, obs), requested))
    new_obs = _observed_tier(agent, obs, requested)
    if new_obs is not None:
        obs = new_obs
        if enabled:
            # §6.2: record the correction; the served tier is not advanced until it is delivered.
            pending, serving = correction_note(*new_obs), replace(serving, speed_tier=requested)
        else:
            serving = replace(serving, speed_tier=new_obs[1])
    new_known = Known(now.intended, serving, obs, pending)
    if call.note is not None:
        _audit(agent, getattr(s, "api_request_id", None), known.serving if known else None,
               now.serving, call.reason)
    sink = getattr(agent, "_route_known_sink", None)
    if callable(sink):
        sink(new_known.to_dict())  # step 2: SessionEntry
    agent._route_known = new_known
