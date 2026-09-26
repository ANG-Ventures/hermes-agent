"""Fallback-events ledger: one row per harness model/provider route change.

Spec: ~/.hermes/plans/2026-09-25_same-family-fallback-cache-spec.md, Phase 1
(§4.1 trigger classes, §4.7 instrumentation). The ledger is the data source
for the fallback-cache report (`scripts/fallback-cache-report.py`); it does
NOT change any routing, cooldown or restore decision.

Three pieces:

* ``classify_trigger`` — a TOTAL function from the failing call's evidence to
  one of the §4.1 classes. The relay's stated class wins (header
  ``x-relay-error-class`` pre-stream, ``relay_error_class`` in the SSE error
  JSON after HTTP 200); ``upstream_passthrough`` and non-relay providers use
  the text table; novel text is ``unclassified``, never ``quota_model``.
  The status code is evidence of last resort because the relay lies with it
  today (connect timeout sent as 429, pool-wide model cap sent as 503).
* ``stash_api_error`` / ``clear_pending`` — the consume-once evidence slot
  the API-error site fills and a successful call clears, so a reason-less
  failover site still records WHAT failed.
* ``record`` — best-effort insert via the Blackbox plugin (I3: a telemetry
  failure never breaks a turn).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

TRIGGER_CLASSES = (
    "conn", "pool_pressure", "quota_model", "quota_seat", "rate_upstream",
    "refusal", "auth", "unclassified",
)
# Relay-stated classes (spec D2). `upstream_passthrough` defers to the text
# table; anything else unknown is `unclassified`.
_RELAY_CLASSES = frozenset(
    ("conn", "pool_pressure", "quota_model", "quota_seat", "rate_upstream", "auth")
)

ERR_HEAD_MAX = 160
_PENDING_MAX_AGE_S = 900.0

# Ordered text table (§4.1). First match wins; each row is (class, needles).
# Order matters where one message could match two rows: the relay's own
# unreachable / capacity wording is checked before generic "rate limit".
_TEXT_TABLE: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("refusal", ("content_policy", "content policy", "safety refusal")),
    ("auth", ("oauth access token has been revoked", "token has been revoked",
              "invalid x-api-key", "authentication_error", "invalid bearer",
              "unauthorized")),
    # Relay pool-wide model exhaustion (sent as 503) — pool-wide ONLY.
    ("quota_model", ("no eligible sub for the requested model",
                     "this model's budget is capped")),
    # Operator drain is deliberate capacity withholding, not quota.
    ("pool_pressure", ("drained",)),
    # Pool-wide exhaustion for every model is still pool-wide quota.
    ("quota_model", ("no eligible sub",)),
    ("conn", ("upstream connect timed out", "upstream unreachable",
              "upstream attempt timed out", "pool deadline exceeded",
              "connection error", "connection reset", "connection refused",
              "incompleteread", "incomplete read", "remote end closed",
              "server disconnected", "read timed out", "readtimeout",
              "readerror", "apiconnectionerror", "apitimeouterror",
              "timed out")),
    ("pool_pressure", ("pool at capacity", "burn-in", "burn in",
                       "per-minute ceiling", "newly-activated",
                       "share ceiling", "overloaded", "capacity",
                       "overflow_exhausted")),
    ("quota_seat", ("fable limit", "session limit", "5-hour limit",
                    "5 hour limit", "weekly limit", "usage limit",
                    "opus limit", "hit your limit", "reached your")),
    ("rate_upstream", ("exceed your account's rate limit",
                       "exceeded your account's rate limit",
                       "rate limit", "rate_limit", "too many requests")),
)

_CONN_EXC_NAMES = frozenset((
    "APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout",
    "ReadError", "ReadTimeout", "RemoteProtocolError", "IncompleteRead",
    "ConnectionError", "ConnectionResetError", "TimeoutError",
))


def _norm(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[0-9a-f]{8,}", "H", t)
    t = re.sub(r"\d+", "N", t)
    return re.sub(r"\s+", " ", t).strip()


def err_hash(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return hashlib.sha1(_norm(text).encode("utf-8", "replace")).hexdigest()[:10]


def _lower_headers(headers: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        items = headers.items() if headers is not None else ()
        for k, v in items:
            out[str(k).lower()] = str(v)
    except Exception:  # noqa: BLE001
        pass
    return out


def classify_text(text: Optional[str], *, http_status: Optional[int] = None,
                  exc_name: Optional[str] = None,
                  reason: Optional[str] = None) -> str:
    """§4.1 text table. Total: always returns a member of TRIGGER_CLASSES."""
    if reason == "content_policy_blocked":
        return "refusal"
    t = (text or "").lower()
    for cls, needles in _TEXT_TABLE:
        if any(n in t for n in needles):
            return cls
    if exc_name in _CONN_EXC_NAMES:
        return "conn"
    if http_status == 401:
        return "auth"
    return "unclassified"


def classify_trigger(*, text: Optional[str] = None,
                     http_status: Optional[int] = None,
                     headers: Any = None,
                     body: Any = None,
                     exc_name: Optional[str] = None,
                     reason: Optional[str] = None) -> Tuple[str, str]:
    """Return ``(trigger_class, class_source)``.

    class_source is ``relay_header`` | ``relay_stream`` | ``text``.
    """
    h = _lower_headers(headers)
    rc = (h.get("x-relay-error-class") or "").strip().lower()
    if rc:
        if rc in _RELAY_CLASSES:
            return rc, "relay_header"
        if rc != "upstream_passthrough":
            return "unclassified", "relay_header"
    else:
        stream_rc = None
        if isinstance(body, dict):
            stream_rc = body.get("relay_error_class")
            if stream_rc is None and isinstance(body.get("error"), dict):
                stream_rc = body["error"].get("relay_error_class")
        if isinstance(stream_rc, str) and stream_rc.strip():
            s = stream_rc.strip().lower()
            if s in _RELAY_CLASSES:
                return s, "relay_stream"
            if s != "upstream_passthrough":
                return "unclassified", "relay_stream"
    return classify_text(text, http_status=http_status, exc_name=exc_name,
                         reason=reason), "text"


RELAY_PROVIDERS = frozenset(("claude-apr", "claude-bpr"))


def relay_error_class(error: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(class, source)`` the relay stated on ``error``, else ``(None, None)``.

    Reads the ``x-relay-error-class`` response header first (pre-stream, and
    also present on a buffered stream error), then ``relay_error_class`` in the
    error body: top level (Anthropic SDK: the whole SSE event) or inside
    ``error`` (OpenAI SDK raises with ``body=data["error"]``, the inner object).
    Unknown values come back verbatim; callers map them. Never raises.
    """
    try:
        response = getattr(error, "response", None)
        h = _lower_headers(getattr(response, "headers", None))
        rc = (h.get("x-relay-error-class") or "").strip().lower()
        if rc:
            return rc, "relay_header"
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            s = body.get("relay_error_class")
            if s is None and isinstance(body.get("error"), dict):
                s = body["error"].get("relay_error_class")
            if isinstance(s, str) and s.strip():
                return s.strip().lower(), "relay_stream"
    except Exception:  # noqa: BLE001
        pass
    return None, None


def pending_trigger_class(agent: Any) -> Optional[str]:
    """Peek (do NOT consume) the §4.1 class of the stashed failing call, so
    the failover arm/gate can branch on class before the ledger row consumes
    the slot. None when nothing fresh is stashed. Never raises."""
    try:
        pending = getattr(agent, "_pending_fallback_error", None)
        if not isinstance(pending, dict):
            return None
        if time.monotonic() - float(pending.get("at") or 0) > _PENDING_MAX_AGE_S:
            return None
        cls, _src = classify_trigger(
            text=pending.get("text"), http_status=pending.get("status"),
            headers=pending.get("headers"), body=pending.get("body"),
            exc_name=pending.get("exc"))
        return cls
    except Exception:  # noqa: BLE001
        return None


def quota_seat_on_relay(agent: Any) -> bool:
    """True when the failing call is a seat-level quota (``quota_seat``) on a
    pool relay provider. Such a limit is the relay's to rotate around: the
    harness neither benches the model (``_rate_limited_until``) nor runs
    ``apply_quota_gate`` for it (spec §4.1 / Phase 1b). Direct pins
    (claude-bpx-N / claude-apx-N) are excluded: there the seat IS the provider."""
    provider = (getattr(agent, "provider", "") or "").strip().lower()
    return provider in RELAY_PROVIDERS and pending_trigger_class(agent) == "quota_seat"


def _scrub(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception:  # noqa: BLE001
        return ""


def stash_api_error(agent: Any, api_error: BaseException,
                    status_code: Optional[int],
                    error_context: Optional[Dict[str, Any]] = None) -> None:
    """Remember the latest failing call's evidence for the next failover.

    Never raises. The raw message is kept in memory only; what reaches disk
    is the class, a hash, and (for non-2xx error JSON) a scrubbed 160-char
    head.
    """
    try:
        response = getattr(api_error, "response", None)
        headers = _lower_headers(getattr(response, "headers", None))
        body = getattr(api_error, "body", None)
        msg = ""
        if isinstance(error_context, dict):
            msg = str(error_context.get("message") or "")
        if not msg:
            msg = str(api_error)
        agent._pending_fallback_error = {
            "at": time.monotonic(),
            "status": status_code if isinstance(status_code, int) else None,
            "text": msg[:2000],
            "headers": {k: v for k, v in headers.items()
                        if k in ("x-relay-error-class", "x-pool-unreachable",
                                 "x-pool-route-id", "retry-after")},
            "body": body if isinstance(body, dict) else None,
            "exc": type(api_error).__name__,
        }
    except Exception:  # noqa: BLE001
        logger.debug("fallback ledger: stash failed", exc_info=True)


def clear_pending(agent: Any) -> None:
    try:
        agent._pending_fallback_error = None
    except Exception:  # noqa: BLE001
        pass


def _consume_pending(agent: Any) -> Optional[Dict[str, Any]]:
    pending = getattr(agent, "_pending_fallback_error", None)
    try:
        agent._pending_fallback_error = None
    except Exception:  # noqa: BLE001
        pass
    if not isinstance(pending, dict):
        return None
    if time.monotonic() - float(pending.get("at") or 0) > _PENDING_MAX_AGE_S:
        return None
    return pending


def _reason_value(reason: Any) -> Optional[str]:
    if reason is None:
        return None
    return str(getattr(reason, "value", reason))


def build_row(agent: Any, kind: str, *, from_provider: Any, from_model: Any,
              to_provider: Any, to_model: Any, reason: Any = None,
              error_context: Optional[Dict[str, Any]] = None,
              cooldown_s: Optional[float] = None,
              consume: bool = True,
              extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Assemble one ledger row (pure except for consuming the pending slot).

    ``extra`` carries the Phase 2 policy fields (``sticky_until_epoch``,
    ``return_branch``, ``dwell_s``, ``notice_text``, …); its keys win."""
    pending = _consume_pending(agent) if consume else None
    status = pending.get("status") if pending else None
    headers = pending.get("headers") if pending else {}
    body = pending.get("body") if pending else None
    text = (pending.get("text") if pending else None) or (
        str((error_context or {}).get("message") or "") or None)
    reason_s = _reason_value(reason)
    if kind == "failover":
        trigger_class, class_source = classify_trigger(
            text=text, http_status=status, headers=headers, body=body,
            exc_name=pending.get("exc") if pending else None, reason=reason_s)
    else:
        trigger_class, class_source = None, None
    err_head = None
    if kind == "failover" and text and isinstance(status, int) and status >= 400 \
            and isinstance(body, dict):
        err_head = _scrub(text)[:ERR_HEAD_MAX] or None
    turn_id = str(getattr(agent, "_current_turn_id", "") or "") or None
    session_id = str(getattr(agent, "session_id", "") or "") or None
    if session_id is None and turn_id:
        session_id = turn_id.split(":", 1)[0]
    seq = None
    try:
        counters = getattr(agent, "_api_call_seq_by_turn", None)
        if isinstance(counters, dict) and turn_id in counters:
            seq = int(counters[turn_id])
    except Exception:  # noqa: BLE001
        seq = None
    return {
        "seq": seq,
        "ts": time.time(),
        "session_id": session_id,
        "turn_id": turn_id,
        "from_provider": str(from_provider or "") or None,
        "from_model": str(from_model or "") or None,
        "to_provider": str(to_provider or "") or None,
        "to_model": str(to_model or "") or None,
        "kind": kind,
        "reason": reason_s,
        "trigger_class": trigger_class,
        "class_source": class_source,
        "http_status": status,
        "relay_synthetic": 1 if (headers or {}).get("x-pool-unreachable") else 0,
        "route_id": (headers or {}).get("x-pool-route-id"),
        "err_hash": err_hash(text) if kind == "failover" else None,
        "err_head": err_head,
        "cooldown_s": float(cooldown_s) if isinstance(cooldown_s, (int, float)) else None,
        # Phase 2 (sticky policy) fills this through ``extra``.
        "sticky_until_epoch": None,
        **{k: v for k, v in (extra or {}).items() if k not in ("kind",)},
    }


def write_row(row: Dict[str, Any]) -> None:
    """Best-effort insert of an already-built row (I3). Never raises."""
    try:
        from plugins.blackbox import record_fallback_event

        record_fallback_event(row)
    except Exception:  # noqa: BLE001
        logger.warning("fallback ledger write failed", exc_info=True)


def record(agent: Any, kind: str, **kwargs: Any) -> None:
    """Best-effort ledger write (I3). Never raises."""
    try:
        row = build_row(agent, kind, **kwargs)
    except Exception:  # noqa: BLE001
        logger.warning("fallback ledger row build failed", exc_info=True)
        return
    write_row(row)


def record_restore_refused(agent: Any, why: str,
                           extra: Optional[Dict[str, Any]] = None) -> None:
    """One `restore_refused` row per fallback episode (not per turn)."""
    try:
        if getattr(agent, "_fallback_restore_refused_logged", False):
            return
        agent._fallback_restore_refused_logged = True
        rt = getattr(agent, "_primary_runtime", None) or {}
        record(agent, "restore_refused",
               from_provider=getattr(agent, "provider", None),
               from_model=getattr(agent, "model", None),
               to_provider=rt.get("provider"), to_model=rt.get("model"),
               reason=why, consume=False, extra=extra)
    except Exception:  # noqa: BLE001
        logger.debug("restore_refused ledger write failed", exc_info=True)
