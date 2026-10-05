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
import json
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

TRIGGER_CLASSES = (
    "conn", "pool_pressure", "quota_model", "quota_seat", "rate_upstream",
    "refusal", "auth", "provider_invalid_response", "unclassified",
)
# A billed response the loop rejected (empty content / invalid shape), named
# from the floor evidence ``stash_response_failure`` stashed (t_d35beb85).
INVALID_RESPONSE_CLASS = "provider_invalid_response"
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
    # "Third-party apps now draw from your extra usage" (HTTP 400) is filed
    # under auth on purpose: like a revoked token it is the ACCOUNT refusing
    # this route, with no reset window to wait for. quota_seat/quota_model
    # would arm the sticky quota clock and render "seat quota exhausted",
    # both false. Its own backoff is fallback_policy.same_error_backoff.
    ("auth", ("oauth access token has been revoked", "token has been revoked",
              "invalid x-api-key", "authentication_error", "invalid bearer",
              "unauthorized", "third-party apps now draw from",
              # CLIProxyAPI (cpa) 503 when it holds no credential for the
              # requested upstream ("auth_unavailable: no auth available
              # (providers=kimi, model=kimi-k3)"). Not capacity: nothing to
              # wait out until a credential is loaded (t_ac76e76f).
              "auth_unavailable", "no auth available")),
    # Relay pool-wide model exhaustion (sent as 503) — pool-wide ONLY.
    ("quota_model", ("no eligible sub for the requested model",
                     "this model's budget is capped")),
    # Operator drain is deliberate capacity withholding, not quota.
    ("pool_pressure", ("drained", "draining-for-deploy")),
    # Bridge interactive (TUI) session demotion, HTTP 409 tui_history_diverged
    # (claude-bpx bridge/src/tuiRunner.js). The bridge withdrew this
    # conversation's resident session; it is sticky until the bridge's idle TTL
    # reaps it, so the route is unavailable for this session, not a bad request
    # and not quota (t_693aa2e5: 145 rows/7d rendered "unclassified error").
    ("pool_pressure", ("replayed history no longer matches",)),
    # claude-pool box capacity / interactive-session startup (503s, t_0ff05041).
    # Ahead of conn so "startup deadline" never drifts into a timeout needle.
    ("pool_pressure", ("no free interactive session slot",
                       "cli children concurrently",
                       "did not become ready before the startup deadline")),
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
    # "You're out of extra usage" (HTTP 400, t_f00f05bd) is this seat's
    # overage wall, not a malformed request; the third-party-apps 400 above
    # matches auth first and never reaches this row.
    ("quota_seat", ("fable limit", "session limit", "5-hour limit",
                    "5 hour limit", "weekly limit", "usage limit",
                    "opus limit", "hit your limit", "reached your",
                    "out of extra usage")),
    ("rate_upstream", ("exceed your account's rate limit",
                       "exceeded your account's rate limit",
                       "rate limit", "rate_limit", "too many requests")),
)

# Relay-synthetic error bodies -> the hop the relay states for them under
# error-class-v2 (claude-pool ``_SYNTHETIC_CLASS``). A lane that does not
# negotiate v2 (claude-dtlr*, claude-btpr) gets the same bytes WITHOUT the
# ``x-relay-error-hop`` header; the body alone names the hop (t_6eddafcd: a
# dlr 504 "upstream attempt timed out" rendered "(hop unknown)"). Exact
# ``error`` strings only: they are the relay's own answers, never upstream text.
RELAY_SYNTHETIC_HOP: Dict[str, str] = {
    "pool at capacity": "relay",
    "upstream connect timed out": "relay->bridge",
    "upstream attempt timed out": "relay->bridge",
    "pool deadline exceeded": "relay->bridge",
    "upstream unreachable on every box": "relay->bridge",
    "upstream unreachable": "relay->bridge",
    "client cancelled": "relay",
    "pool dispatch error": "relay",
    "upstream capacity unavailable for the requested model": "relay",
    "overflow_exhausted": "relay",
    "no eligible sub": "relay",
    "confirm probe failed": "relay->bridge",
    "no box could serve /v1/models": "relay->bridge",
}
_RELAY_JSON_RE = re.compile(r'\{\s*"error"\s*:\s*"([^"]{1,120})"\s*\}')


def relay_synthetic_error(body: Any, text: Optional[str] = None) -> Optional[str]:
    """The relay-synthetic ``error`` string of a failed call, else None.

    Reads ``body["error"]`` (a string: the relay's own shape) first, then the
    exception text when it carries the relay's JSON (``HTTP 504:
    {"error":"upstream attempt timed out"}``). Never raises."""
    try:
        cand = None
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            cand = body["error"]
        if cand is None and text:
            m = _RELAY_JSON_RE.search(str(text))
            if m:
                cand = m.group(1)
        if cand is None:
            return None
        cand = cand.strip().lower()
        return cand if cand in RELAY_SYNTHETIC_HOP else None
    except Exception:  # noqa: BLE001
        return None


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
    # Last resort, same as the runtime classifier (error_classifier routes an
    # otherwise-unrecognized 403 to FailoverReason.auth) (C7 k80).
    if http_status in (401, 403):
        return "auth"
    return "unclassified"


def classify_trigger(*, text: Optional[str] = None,
                     http_status: Optional[int] = None,
                     headers: Any = None,
                     body: Any = None,
                     exc_name: Optional[str] = None,
                     reason: Optional[str] = None,
                     floor: Any = None) -> Tuple[str, str]:
    """Return ``(trigger_class, class_source)``.

    class_source is ``relay_header`` | ``relay_stream`` | ``text`` |
    ``floor``. ``floor`` is the evidence of a billed response the loop
    rejected (:func:`stash_response_failure`): with no http/exc/text class it
    names the failure ``provider_invalid_response`` instead of ``unclassified``.
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
    cls = classify_text(text, http_status=http_status, exc_name=exc_name,
                        reason=reason)
    if (cls == "unclassified" and isinstance(floor, dict) and floor.get("site")
            and not text and http_status is None and not exc_name):
        return INVALID_RESPONSE_CLASS, "floor"
    return cls, "text"


def floor_err_hash(floor: Any) -> Optional[str]:
    """err_hash of a rejected-response floor: hash(site, stop_reason,
    content_blocks), so a repeat of the same empty shape is recognisable
    (t_d35beb85). None without a floor site."""
    if not isinstance(floor, dict) or not floor.get("site"):
        return None
    key = f"{floor.get('site')}|{floor.get('stop_reason')}|{floor.get('content_blocks')}"
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:10]


def pending_err_hash(pending: Any) -> Optional[str]:
    """err_hash of a stashed failing call: the text hash, else the floor hash."""
    if not isinstance(pending, dict):
        return None
    if pending.get("text"):
        return err_hash(pending.get("text"))
    return floor_err_hash(pending.get("floor"))


def empty_tool_use_floor(response: Any) -> bool:
    """The retry-in-place shape (t_d35beb85): a parsed (HTTP 200) response
    whose ``stop_reason`` is ``tool_use`` and whose ``content`` list is EMPTY.
    Measured 17x in 24 h on claude-alr across 6 seats: a transient empty body
    from the same model, so one same-route retry (warm cache) beats a
    cross-provider failover (cold cache). Never raises."""
    try:
        if response is None:
            return False
        content = getattr(response, "content", None)
        return (getattr(response, "stop_reason", None) == "tool_use"
                and isinstance(content, list) and len(content) == 0)
    except Exception:  # noqa: BLE001
        return False


RELAY_PROVIDERS = frozenset(("claude-apr", "claude-alr", "claude-alrs", "claude-alrf", "claude-dalrs", "claude-dalrf", "claude-bpr"))


def relay_gave_up_empty(response: Any) -> bool:
    """True when the pooled relay already retried this empty-content 200 and
    gave up (``x-pool-empty-content-retried: gave_up``, claude-pool #193): it
    re-sent the request on the same seat once, then on one OTHER seat, and
    both came back empty. A harness same-route retry would re-enter the same
    relay ladder (2 more billed calls on the same affinity seat); fail over
    instead (t_9d411670: 0/13 different-seat or cross-provider retries
    repeated the empty, vs 12/22 same-seat retries). Never raises."""
    try:
        ph = _lower_headers(getattr(response, "pool_headers", None))
        return (ph.get("x-pool-empty-content-retried") or "").strip().lower() == "gave_up"
    except Exception:  # noqa: BLE001
        return False


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
            exc_name=pending.get("exc"), floor=pending.get("floor"))
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


def pending_floor_repeat(agent: Any) -> bool:
    """True when the stashed failing call is a same-shape repeat of a
    rejected response after a same-route retry (t_d35beb85). The failover then
    skips entries on the failing provider: a provider-wide fault is never
    answered with a model swap on that provider (2026-09-30). Peeked, not
    consumed. Never raises."""
    try:
        pending = getattr(agent, "_pending_fallback_error", None)
        if not isinstance(pending, dict):
            return False
        if time.monotonic() - float(pending.get("at") or 0) > _PENDING_MAX_AGE_S:
            return False
        return bool((pending.get("floor") or {}).get("repeat"))
    except Exception:  # noqa: BLE001
        return False


def _scrub(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text

        # Persisted error preview: a non-navigation sink, so URL query credentials
        # and user:pass@ userinfo are redacted too (Backfill C3).
        return redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:  # noqa: BLE001
        return ""


# Dead-letter ledger for unclassified fallback riders (t_a716610d).
DEAD_LETTER_BODY_MAX = 400
DEAD_LETTER_REL = ("state", "fallback-unclassified.jsonl")
# Memory bound on the kept raw body. Scrubbing runs on ALL of it before the
# 400-char cut, so only a secret starting in the first 400 chars and ending
# past 64 KiB could be split; an unterminated key block is cut anyway
# (:func:`_cut_unterminated_key`). Error bodies are small; 64 KiB covers an
# HTML error page head.
_DL_RAW_MAX = 65536
_DL_HEADER_PREFIXES = ("x-relay-", "retry-after", "anthropic-ratelimit-")
_KEY_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)


def _dead_letter_headers(headers: Any) -> Dict[str, str]:
    return {k: v for k, v in _lower_headers(headers).items()
            if k.startswith(_DL_HEADER_PREFIXES)}


def _raw_body_text(response: Any, body: Any, msg: str) -> Any:
    """The failing response body: parsed JSON (dict/list) when there is any,
    so the scrubber sees decoded strings (``https:\\/\\/u:p@h`` on the wire
    hides URL credentials from it), else the wire text, else the message.
    Never raises."""
    text = None
    try:
        text = getattr(response, "text", None) if response is not None else None
    except Exception:  # noqa: BLE001 - an unread stream raises here
        text = None
    if isinstance(text, str) and text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                return parsed
            if isinstance(parsed, str):  # a top-level JSON string: decoded too
                return parsed[:_DL_RAW_MAX]
        except Exception:  # noqa: BLE001
            pass
    if isinstance(body, (dict, list)):
        return body
    if isinstance(text, str) and text:
        return text[:_DL_RAW_MAX]
    if isinstance(body, str) and body:
        return body[:_DL_RAW_MAX]
    return str(msg or "")[:_DL_RAW_MAX]


def _cut_unterminated_key(text: str) -> str:
    """Fail closed on a key block the redactor could not match whole."""
    m = _KEY_BEGIN_RE.search(text)
    return text if m is None else text[:m.start()] + "[REDACTED PRIVATE KEY]"


def _scrub_dead_letter(text: str) -> str:
    """``_scrub`` plus the repo's leak-corpus catalog (LCM sensitive patterns,
    ``all``): the core redactor misses ``op://`` refs, cookies and
    ``password: x`` lines, and this sink keeps raw bodies. Scrub the WHOLE
    string, cut later. Fails CLOSED ("")."""
    try:
        from types import SimpleNamespace

        from plugins.context_engine.lcm.ingest_protection import redact_sensitive_text

        out = _scrub(text)
        if text and not out:
            return ""
        out = redact_sensitive_text(out, SimpleNamespace(
            sensitive_patterns_enabled=True, sensitive_patterns=["all"])) or ""
        return _cut_unterminated_key(out)
    except Exception:  # noqa: BLE001
        return ""


def _scrub_leaves(obj: Any, depth: int = 0) -> Any:
    """Scrub every decoded string (keys too) in a parsed JSON body."""
    if depth > 20:
        return "[depth]"
    if isinstance(obj, str):
        return _scrub_dead_letter(obj)
    if isinstance(obj, dict):
        return {_scrub_dead_letter(str(k)): _scrub_leaves(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub_leaves(v, depth + 1) for v in obj]
    return obj


def _dead_letter_body(raw: Any) -> str:
    """Scrubbed body, cut to DEAD_LETTER_BODY_MAX only after scrubbing."""
    if isinstance(raw, (dict, list)):
        # Per leaf, never redact_sensitive_text over json.dumps output (it can
        # eat JSON syntax; tests/agent/test_redact_json_leaf.py). _scrub_leaves
        # runs both scrubbers on every string; redact_sensitive_json adds the
        # key rule ("password": "..." masked whole). Fails CLOSED ("").
        try:
            from agent.redact import redact_sensitive_json

            leaves = redact_sensitive_json(
                _scrub_leaves(raw), force=True, redact_url_credentials=True)
            serialized = json.dumps(leaves, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            return ""
        return _cut_unterminated_key(serialized)[:DEAD_LETTER_BODY_MAX]
    return _scrub_dead_letter(str(raw or ""))[:DEAD_LETTER_BODY_MAX]


def dead_letter_path() -> Any:
    from pathlib import Path
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()).joinpath(*DEAD_LETTER_REL)


def note_unclassified(row: Optional[Dict[str, Any]], rendered: str,
                      floors: Any, *, path: Any = None) -> bool:
    """Append ONE JSON line to the dead-letter ledger for an announce rendered
    from a floor branch (``unclassified error`` / ``(hop unknown, sub
    unknown)`` / the generic ``connection issue`` head). The raw evidence
    (relay/retry/ratelimit headers, scrubbed 400-char body) is what the next
    ``_TEXT_TABLE`` row gets written from. Never raises; never touches the
    rendered text. Returns True when a line was written."""
    try:
        floors = [str(f) for f in (floors or ()) if f]
        if not floors:
            return False
        row = row if isinstance(row, dict) else {}
        ev = row.get("_dead_letter") if isinstance(row.get("_dead_letter"), dict) else {}
        rec = {
            "ts": time.time(),
            "session": row.get("session_id"),
            "provider": row.get("from_provider"),
            "model": row.get("from_model"),
            "to_provider": row.get("to_provider"),
            "to_model": row.get("to_model"),
            "http_status": row.get("http_status"),
            "exc_name": ev.get("exc"),
            "reason": row.get("reason"),
            "trigger_class": row.get("trigger_class"),
            "class_source": row.get("class_source"),
            "err_hash": row.get("err_hash"),
            "floors": floors,
            "headers": {str(k): _scrub_dead_letter(str(v))
                        for k, v in (ev.get("headers") or {}).items()},
            "body": _dead_letter_body(ev.get("body")),
            "rendered": _scrub_dead_letter(str(rendered or "")),
            # t_b2e9ef12 (additive): what the classifier had no name for.
            "socket_cause": ev.get("socket_cause"),
            "exc_chain": [str(n) for n in (ev.get("exc_chain") or ())],
            # host:port by construction (_endpoint); scrubbed anyway (defence in depth).
            "endpoint": (_scrub_dead_letter(str(ev["endpoint"])) or None)
            if ev.get("endpoint") else None,
            "elapsed_s": ev.get("elapsed_s"),
            "floor": {str(k): (_scrub_dead_letter(v) if isinstance(v, str) else v)
                      for k, v in (ev.get("floor") or {}).items()},
        }
        rec["cause"] = dead_letter_cause(rec)
        target = path or dead_letter_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001
        logger.debug("fallback dead-letter write failed", exc_info=True)
        return False


# Socket-level cause of a no-status failure (t_b2e9ef12). The SDK raises a
# generic ``APIConnectionError``/``APITimeoutError``; the cause sits in the
# ``__cause__`` chain (httpx -> httpcore -> builtins). Checked in priority
# order over the WHOLE chain: a connect timeout's innermost link is a bare
# ``TimeoutError``, so innermost-wins would misname it a read timeout.
_SOCKET_CAUSES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("dns", ("gaierror", "herror")),
    ("connect_refused", ("ConnectionRefusedError",)),
    ("connect_timeout", ("ConnectTimeout",)),
    ("pool_timeout", ("PoolTimeout",)),
    ("tls", ("SSLError", "SSLCertVerificationError", "SSLZeroReturnError")),
    ("conn_reset", ("ConnectionResetError", "BrokenPipeError", "ConnectionAbortedError")),
    ("remote_protocol", ("RemoteProtocolError", "IncompleteRead", "RemoteDisconnected",
                         "LocalProtocolError")),
    ("read_timeout", ("ReadTimeout", "WriteTimeout", "APITimeoutError", "TimeoutError",
                      "timeout")),
    ("read_error", ("ReadError", "WriteError")),
    ("connect_error", ("ConnectError", "APIConnectionError", "ConnectionError")),
)
_EXC_CHAIN_MAX = 8


def exc_chain(exc: Any) -> list:
    """Class names along ``__cause__``/``__context__``, outermost first.
    Bounded and cycle-safe. Never raises."""
    out: list = []
    seen: set = set()
    try:
        while exc is not None and id(exc) not in seen and len(out) < _EXC_CHAIN_MAX:
            seen.add(id(exc))
            out.append(type(exc).__name__)
            exc = exc.__cause__ or exc.__context__
    except Exception:  # noqa: BLE001
        pass
    return out


def socket_cause(exc: Any) -> Optional[str]:
    """Named socket cause of ``exc`` (``connect_refused`` / ``read_timeout``
    / ``dns`` ...), or None when no link in its chain is a transport error
    (an HTTP status error). Never raises."""
    names = set(exc_chain(exc))
    for cause, members in _SOCKET_CAUSES:
        if names.intersection(members):
            return cause
    return None


def _attempt_seats(raw: Any) -> Optional[list]:
    """``x-pool-empty-content-attempts: sub-vps-18,sub-vps-18,sub-vps-23`` ->
    the seat list (at most 8 short tokens), else None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    seats = [t.strip() for t in raw.split(",") if t.strip()]
    seats = [t for t in seats if re.fullmatch(r"[A-Za-z0-9_.:-]{1,40}", t)][:8]
    return seats or None


def _request_ids(raw: Any) -> Optional[list]:
    """``x-pool-empty-content-request-ids: req_a,req_b,req_c`` -> the upstream
    request ids of each billed empty attempt (at most 8), else None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    ids = [t.strip() for t in raw.split(",") if t.strip()]
    ids = [t for t in ids if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", t)][:8]
    return ids or None


def _prompt_tokens(usage: Any) -> Optional[int]:
    """Billed prompt size of one response: input + cache read + cache write
    (Anthropic usage fields), else None when none of them is an int."""
    if usage is None:
        return None
    parts = [getattr(usage, f, None) for f in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    parts = [p for p in parts if isinstance(p, int) and not isinstance(p, bool)]
    return sum(parts) if parts else None


def stash_response_failure(agent: Any, site: str, response: Any = None, *,
                           detail: Optional[str] = None,
                           elapsed_s: Optional[float] = None,
                           repeat: bool = False) -> None:
    """Evidence for a reason-less floor failover off a BILLED response the
    loop rejected (invalid shape, empty content). t_b2e9ef12: the 2026-10-01
    11:25 alr fallback was a relay 200 rejected here, and the dead-letter
    row was all nulls because nothing was stashed.

    It sets no ``text``/``status``/``headers``/``exc``; the ``floor`` alone
    names the class (``provider_invalid_response``), the err_hash
    (:func:`floor_err_hash`) and the seat (``served_by``) the rider renders
    (t_d35beb85). ``repeat`` marks a same-shape repeat after a same-route
    retry: the failover then skips entries on the failing provider.
    Never raises."""
    try:
        ph = _lower_headers(getattr(response, "pool_headers", None))
        usage = getattr(response, "usage", None)
        content = getattr(response, "content", None)
        floor = {
            "site": str(site),
            # Scrub the WHOLE detail before the cut: a cut that lands between a
            # URL password and its '@' would hide it from the redactor.
            "detail": (_scrub_dead_letter(str(detail))[:200] or None) if detail else None,
            "stop_reason": getattr(response, "stop_reason", None)
            if response is not None else None,
            "content_blocks": len(content) if isinstance(content, list) else None,
            "output_tokens": getattr(usage, "output_tokens", None)
            if usage is not None else None,
            "route_id": ph.get("x-pool-route-id"),
            "served_by": ph.get("x-pool-served-by"),
            "repeat": True if repeat else None,
            # t_6eddafcd: the relay's own empty-content ladder (claude-pool
            # #193): "gave_up" / "1" and, when sent, the seats it tried in order.
            "relay_retry": (ph.get("x-pool-empty-content-retried") or "").strip().lower() or None,
            "relay_attempts": _attempt_seats(ph.get("x-pool-empty-content-attempts")),
            # t_c706fd1e: each billed empty attempt's upstream request id
            # (claude-pool x-pool-empty-content-request-ids) and the prompt size.
            "relay_request_ids": _request_ids(ph.get("x-pool-empty-content-request-ids")),
            "prompt_tokens": _prompt_tokens(usage),
        }
        agent._pending_fallback_error = {
            "at": time.monotonic(),
            "status": None,
            "text": None,
            "headers": {},
            "body": None,
            "exc": None,
            "endpoint": None,
            "elapsed_s": _round_s(elapsed_s),
            "floor": {k: v for k, v in floor.items() if v is not None},
            "dl_headers": {},
            "dl_body": None,
        }
        logger.warning(
            "provider response rejected by the loop: site=%s stop_reason=%s "
            "content_blocks=%s output_tokens=%s route_id=%s served_by=%s elapsed_s=%s",
            floor["site"], floor["stop_reason"], floor["content_blocks"],
            floor["output_tokens"], floor["route_id"], floor["served_by"],
            _round_s(elapsed_s))
    except Exception:  # noqa: BLE001
        logger.debug("fallback ledger: response-failure stash failed", exc_info=True)


INVALID_RETRY_OUTCOMES = ("retry_ok", "retry_same", "retry_other", "retry_error", "fallback",
                          "relay_gave_up")
# Same-route retry backoff for the empty tool_use shape (card: <= 2 s; short
# enough to land inside the relay's affinity window so the cache is warm).
INVALID_RETRY_BACKOFF_S = 1.5


def record_invalid_response(agent: Any, floor: Any, outcome: str, *,
                            response: Any = None, path: Any = None) -> bool:
    """Append ONE structured ``invalid_response`` line per occurrence to
    ``$HERMES_HOME/state/model-route-changes.log`` (t_d35beb85), e.g.::

        2026-10-02T08:33:30 invalid_response class=provider_invalid_response
            provider=claude-alr model=claude-fable-5-1 served_by=sub-vps-2
            route_id=9ed7... stop_reason=tool_use content_blocks=0
            output_tokens=462 err_hash=... retry_outcome=retry_ok cache_read=231000

    (one line; ``key=value`` tokens, values never contain spaces). The
    ``failover|recovery`` parsers of this sink match on the second token and
    skip it. ``outcome`` is one of :data:`INVALID_RETRY_OUTCOMES`;
    ``cache_read`` is the retry's cache read when it came back valid (warm
    retry ≈ prompt). Never raises; returns True when a line was written."""
    try:
        import os

        fl = floor if isinstance(floor, dict) else {}
        fields = [
            ("class", INVALID_RESPONSE_CLASS),
            ("provider", getattr(agent, "provider", None)),
            ("model", getattr(agent, "model", None)),
            ("served_by", fl.get("served_by")),
            ("route_id", fl.get("route_id")),
            ("site", fl.get("site")),
            ("stop_reason", fl.get("stop_reason")),
            ("content_blocks", fl.get("content_blocks")),
            ("output_tokens", fl.get("output_tokens")),
            ("err_hash", floor_err_hash(fl)),
            ("session", getattr(agent, "session_id", None)),
            ("retry_outcome", outcome if outcome in INVALID_RETRY_OUTCOMES else "fallback"),
        ]
        usage = getattr(response, "usage", None) if response is not None else None
        cr = getattr(usage, "cache_read_input_tokens", None) if usage is not None else None
        if isinstance(cr, int):
            fields.append(("cache_read", cr))

        def _tok(v: Any) -> str:
            return re.sub(r"\s+", "_", str(v)) if v not in (None, "") else "-"

        line = (time.strftime("%Y-%m-%dT%H:%M:%S") + " invalid_response "
                + " ".join(f"{k}={_tok(v)}" for k, v in fields))
        if path is None:
            home = os.environ.get("HERMES_HOME") or os.path.join(
                os.path.expanduser("~"), ".hermes")
            path = os.path.join(home, "state", "model-route-changes.log")
        os.makedirs(os.path.dirname(str(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        logger.warning("provider_invalid_response: %s", line.split(" ", 2)[2])
        return True
    except Exception:  # noqa: BLE001
        logger.debug("invalid_response count row failed", exc_info=True)
        return False


def _round_s(v: Any) -> Optional[float]:
    return round(float(v), 2) if isinstance(v, (int, float)) and v >= 0 else None


def dead_letter_cause(rec: Dict[str, Any]) -> str:
    """The NAME a dead-letter row files under: the socket cause, else the
    floor site, else the relay-stated / text class, else ``http_<status>``,
    else ``no_evidence``. Total; never ``unclassified``."""
    if rec.get("socket_cause"):
        return str(rec["socket_cause"])
    floor = rec.get("floor") if isinstance(rec.get("floor"), dict) else {}
    if floor.get("site"):
        return str(floor["site"])
    rc = (rec.get("headers") or {}).get("x-relay-error-class")
    if rc:
        return f"relay_{rc}"
    tc = rec.get("trigger_class")
    if tc and tc != "unclassified":
        return str(tc)
    if rec.get("http_status") is not None:
        return f"http_{rec['http_status']}"
    if rec.get("exc_name"):
        return f"exc_{rec['exc_name']}"
    return "no_evidence"


def stash_api_error(agent: Any, api_error: BaseException,
                    status_code: Optional[int],
                    error_context: Optional[Dict[str, Any]] = None,
                    *, elapsed_s: Optional[float] = None) -> None:
    """Remember the latest failing call's evidence for the next failover.

    Never raises. The raw message is kept in memory only; what reaches disk
    is the class, a hash, and (for non-2xx error JSON) a scrubbed 160-char
    head.
    """
    try:
        response = getattr(api_error, "response", None)
        headers = _lower_headers(getattr(response, "headers", None))
        body = getattr(api_error, "body", None)
        if not isinstance(body, dict) and response is not None:
            # OpenAI SDK passes ``data.get("error", data)``: a relay's
            # {"error": "<token>"} arrives as the bare string. Recover the
            # JSON dict so the rider keeps the error head (err_head).
            try:
                _json = response.json()
                if isinstance(_json, dict):
                    body = _json
            except Exception:  # noqa: BLE001
                pass
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
                        if k in ("x-relay-error-class", "x-relay-error-hop",
                                 "x-relay-seat", "x-pool-served-by",
                                 "x-pool-unreachable",
                                 "x-pool-route-id", "retry-after",
                                 "x-relay-eligible", "x-pool-other-eligible",
                                 "x-ratelimit-limit", "x-ratelimit-remaining",
                                 "x-ratelimit-reset")},
            "body": body if isinstance(body, dict) else None,
            "exc": type(api_error).__name__,
            "endpoint": _endpoint(api_error),
            # t_b2e9ef12: the socket cause the SDK wrapper hides, the chain it
            # came from, and how long the call ran. Dead-letter only.
            "socket_cause": socket_cause(api_error),
            "exc_chain": exc_chain(api_error),
            "elapsed_s": _round_s(elapsed_s),
            # Dead-letter evidence (t_a716610d), in memory until a floor rider
            # renders; scrubbed by note_unclassified before it reaches disk.
            "dl_headers": _dead_letter_headers(headers),
            "dl_body": _raw_body_text(response, body, msg),
        }
        pend = agent._pending_fallback_error
        if pend["status"] is None:
            # t_b2e9ef12: a no-status failure used to log nothing parseable.
            logger.warning(
                "provider call failed without HTTP status: exc=%s socket_cause=%s "
                "endpoint=%s elapsed_s=%s chain=%s",
                pend["exc"], pend["socket_cause"], pend["endpoint"],
                pend["elapsed_s"], ">".join(pend["exc_chain"]))
    except Exception:  # noqa: BLE001
        logger.debug("fallback ledger: stash failed", exc_info=True)


def _endpoint(api_error: BaseException) -> Optional[str]:
    """``host:port`` the failing call was sent to (the request URL), or None.
    t_21bba7dc: a connection drop on a relay lane names the relay it hit."""
    try:
        req = getattr(api_error, "request", None)
        if req is None:
            req = getattr(getattr(api_error, "response", None), "request", None)
        url = getattr(req, "url", None)
        host = getattr(url, "host", None)
        if not host:
            return None
        port = getattr(url, "port", None)
        return f"{host}:{port}" if port else str(host)
    except Exception:  # noqa: BLE001
        return None


RELAY_PROBE_TIMEOUT_S = 0.3


def probe_listener(addr: Optional[str], timeout: float = RELAY_PROBE_TIMEOUT_S) -> Optional[bool]:
    """TCP-connect ``host:port``: True = something is listening, False = not,
    None = no address to probe. Never raises."""
    if not addr or ":" not in addr:
        return None
    try:
        import socket

        host, _, port = addr.rpartition(":")
        with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def _note_relay_conn(row: Dict[str, Any], pending: Optional[Dict[str, Any]]) -> None:
    """t_21bba7dc: a conn failover on a relay lane with no hop/seat evidence
    never reached a seat. Record the address it hit and whether the listener
    is back now (in memory for the notice only; not ledger columns)."""
    try:
        from agent.fallback_policy import relay_conn_without_evidence

        if not relay_conn_without_evidence(row):
            return
        addr = (pending or {}).get("endpoint")
        if addr and not row.get("relay_addr"):
            row["relay_addr"] = addr
        if "relay_up" not in row:
            up = probe_listener(row.get("relay_addr"))
            row["relay_up"] = up
            if up:
                row["relay_up_ts"] = time.time()
    except Exception:  # noqa: BLE001
        logger.debug("fallback ledger: relay conn note failed", exc_info=True)


_SEAT_UNSTATED = frozenset(("", "unknown", "none"))


def relay_hop_seat(headers: Any, body: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(hop, seat)`` the relay stated for a failed call (error-class-v2).

    ``x-relay-error-hop`` / ``x-relay-seat`` first, then ``relay_error_hop`` /
    ``relay_seat`` in the SSE error body (top level or inside ``error``, same
    shapes as :func:`relay_error_class`). The hop comes back in the notice
    enum (``fallback_policy.normalize_hop``); an unstated seat (the relay
    sends ``none`` when it has none) is None. Never raises.
    """
    hop = seat = None
    try:
        h = _lower_headers(headers)
        raw_hop, raw_seat = h.get("x-relay-error-hop"), h.get("x-relay-seat")
        if isinstance(body, dict):
            inner = body.get("error") if isinstance(body.get("error"), dict) else {}
            if not raw_hop:
                raw_hop = body.get("relay_error_hop") or inner.get("relay_error_hop")
            if not raw_seat:
                raw_seat = body.get("relay_seat") or inner.get("relay_seat")
        if isinstance(raw_hop, str) and raw_hop.strip():
            from agent.fallback_policy import normalize_hop

            hop = normalize_hop(raw_hop)
        if isinstance(raw_seat, str) and raw_seat.strip().lower() not in _SEAT_UNSTATED:
            seat = raw_seat.strip()
    except Exception:  # noqa: BLE001
        return None, None
    return hop, seat


def pool_eligible(headers: Any) -> Optional[int]:
    """Eligible seat count the relay stated with a failed call
    (``x-pool-other-eligible``: seats other than the one that failed, sent on
    every relay error; else v2 ``x-relay-eligible``), else None. ``?`` or junk
    -> None. Never raises."""
    h = _lower_headers(headers)
    v = (h.get("x-pool-other-eligible") or "").strip()
    if v.isdigit():
        return int(v)
    # v2 counts every eligible seat, the failing one included: only 0 proves
    # there was no other.
    v = (h.get("x-relay-eligible") or "").strip()
    return 0 if v == "0" else None


def served_by_seat(headers: Any) -> Optional[str]:
    """Seat in ``x-pool-served-by`` (claude-pool emits it on EVERY response,
    upstream 4xx passthrough included); None when absent or ``none``/``unknown``."""
    s = _lower_headers(headers).get("x-pool-served-by", "").strip()
    return s if s and s.lower() not in _SEAT_UNSTATED else None


PROVIDER_MESSAGE_MAX = 120


def _error_obj(body: Any) -> Dict[str, Any]:
    """The vendor error object: OpenAI SDK passes the inner ``error`` dict as
    ``body``; other SDKs pass the whole ``{"error": {...}}`` envelope."""
    if not isinstance(body, dict):
        return {}
    inner = body.get("error")
    return inner if isinstance(inner, dict) else body


def provider_error_detail(provider: Any, body: Any, headers: Any = None,
                          http_status: Optional[int] = None) -> Tuple[Optional[str], Optional[str]]:
    """``(message, scope)`` a non-relay provider stated for a failed call.

    ``message`` is the vendor's own ``error.message``, scrubbed and cut to
    :data:`PROVIDER_MESSAGE_MAX` chars. ``scope`` says WHOSE limit tripped on
    an OpenRouter 429 (t_a8dc8b21): ``byok`` (the upstream provider's own
    key, "custom key used"), ``upstream`` (``metadata.provider_name`` /
    ``provider_code`` set), ``platform`` (OpenRouter's own limiter:
    ``metadata.error_type=rate_limit_exceeded`` or X-RateLimit headers), or
    ``credits`` for an OpenRouter 402. None when the body says nothing.
    Never raises.
    """
    try:
        err = _error_obj(body)
        raw = err.get("message")
        msg = None
        if isinstance(raw, str) and raw.strip():
            msg = _scrub(" ".join(raw.split()))
            if len(msg) > PROVIDER_MESSAGE_MAX:
                msg = msg[:PROVIDER_MESSAGE_MAX - 1].rstrip() + "…"
            msg = msg or None
        scope = None
        if str(provider or "").strip().lower() == "openrouter":
            meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
            h = _lower_headers(headers)
            low = raw.lower() if isinstance(raw, str) else ""
            if http_status == 402:
                scope = "credits"
            elif http_status == 429 or "rate limit" in low:
                if "custom key" in low or meta.get("is_byok") is True:
                    scope = "byok"
                elif meta.get("provider_name") or meta.get("provider_code"):
                    scope = "upstream"
                elif (meta.get("error_type") == "rate_limit_exceeded"
                      or any(k.startswith("x-ratelimit-") for k in h)):
                    scope = "platform"
        return msg, scope
    except Exception:  # noqa: BLE001
        return None, None


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


_ROUTE_KEYS = frozenset({"kind", "session_id", "from_provider", "from_model",
                         "to_provider", "to_model"})


def policy_fields(row: Any) -> Optional[Dict[str, Any]]:
    """A ``fp.recovery_row`` minus its route/identity keys, for ``extra``.

    The policy row's from-route is ``state.fallback_*`` (the route the sticky
    episode armed on), which is stale after a fallback#1->#2 walk (B1 skips the
    re-arm). The caller's live served route must win (t_abad4e80)."""
    if not isinstance(row, dict):
        return None
    return {k: v for k, v in row.items() if k not in _ROUTE_KEYS}


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
            exc_name=pending.get("exc") if pending else None, reason=reason_s,
            floor=pending.get("floor") if pending else None)
        # Record the class the policy acted on: on a direct pin the seat IS
        # the provider, so a seat quota is quota_model (fp.lane_class; the
        # sticky writer already arms with it). Without this the ledger said
        # quota_seat while the cooldown was quota_model's (t_246ce7d6).
        try:
            from agent.fallback_policy import lane_class

            trigger_class = lane_class(trigger_class, str(from_provider or ""), text)[0]
        except Exception:  # noqa: BLE001
            pass
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
    row = {
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
        "err_hash": (floor_err_hash((pending or {}).get("floor"))
                     if trigger_class == INVALID_RESPONSE_CLASS
                     else err_hash(text) if kind == "failover" else None),
        "err_head": err_head,
        "cooldown_s": float(cooldown_s) if isinstance(cooldown_s, (int, float)) else None,
        # Phase 2 (sticky policy) fills this through ``extra``.
        "sticky_until_epoch": None,
        **{k: v for k, v in (extra or {}).items() if k not in ("kind",)},
    }
    if kind == "failover":
        # §4.8: a pooled relay states hop/seat in its error-class-v2 headers
        # (or SSE error fields). Recorded values (``extra``) win.
        # t_a8dc8b21: a non-relay provider's own error text and, for
        # OpenRouter, whose limit tripped. In memory for the notice only
        # (not ledger columns); the rendered notice_text carries it.
        p_msg, p_scope = provider_error_detail(from_provider, body, headers, status)
        if p_msg and not row.get("provider_message"):
            row["provider_message"] = p_msg
        if p_scope and not row.get("provider_scope"):
            row["provider_scope"] = p_scope
        r_hop, r_seat = relay_hop_seat(headers, body)
        synthetic = relay_synthetic_error(body, text)
        if not r_hop and synthetic:
            # No error-class-v2 hop header (the lane did not negotiate v2):
            # the relay-synthetic body names the hop (t_6eddafcd).
            from agent.fallback_policy import normalize_hop

            r_hop = normalize_hop(RELAY_SYNTHETIC_HOP[synthetic])
        if synthetic and not row.get("relay_error"):
            row["relay_error"] = synthetic
        if r_hop and not row.get("hop"):
            row["hop"] = r_hop
        elig = pool_eligible(headers)
        if elig is not None and "pool_eligible" not in row:
            row["pool_eligible"] = elig
        if pending and pending.get("elapsed_s") is not None:
            row.setdefault("elapsed_s", pending.get("elapsed_s"))
        if not r_seat:
            # A pooled relay names the seat that answered an upstream 4xx
            # passthrough only in x-pool-served-by (no error-class-v2 headers).
            r_seat = served_by_seat(headers)
        if r_seat and (not row.get("seat") or row.get("seat") == "unknown"):
            row["seat"] = r_seat
        # §4.8: a direct pin's seat and hop are knowable locally (no relay
        # headers by design, #1260). Pooled rows are left as they are.
        try:
            from agent.fallback_policy import fill_pin_evidence

            row = fill_pin_evidence(row, exc_name=pending.get("exc") if pending else None)
        except Exception:  # noqa: BLE001
            logger.debug("fallback ledger: pin seat/hop fill failed", exc_info=True)
        _note_relay_conn(row, pending)
        # t_d35beb85: a rejected billed response names its seat and route
        # from the floor (x-pool-served-by / x-pool-route-id on the 200).
        if trigger_class == INVALID_RESPONSE_CLASS:
            fl = dict((pending or {}).get("floor") or {})
            row["floor"] = fl
            if fl.get("served_by") and (not row.get("seat") or row.get("seat") == "unknown"):
                row["seat"] = fl["served_by"]
            if fl.get("route_id") and not row.get("route_id"):
                row["route_id"] = fl["route_id"]
            # t_c706fd1e: ledger columns, so the billed ladder is queryable.
            if fl.get("relay_request_ids") and not row.get("request_ids"):
                row["request_ids"] = ",".join(fl["relay_request_ids"])
            if isinstance(fl.get("prompt_tokens"), int):
                row.setdefault("prompt_tokens", fl["prompt_tokens"])
        # t_b2e9ef12: name what a no-status / rejected-response call died of.
        if pending:
            row.setdefault("exc_name", pending.get("exc"))
            row.setdefault("socket_cause", pending.get("socket_cause"))
            row.setdefault("floor_site", (pending.get("floor") or {}).get("site"))
        # Raw evidence for the dead-letter ledger (t_a716610d). Not a column.
        row["_dead_letter"] = {
            "exc": pending.get("exc") if pending else None,
            "headers": dict(pending.get("dl_headers") or {}) if pending else {},
            "body": pending.get("dl_body") if pending else None,
            "socket_cause": pending.get("socket_cause") if pending else None,
            "exc_chain": list(pending.get("exc_chain") or ()) if pending else [],
            "endpoint": pending.get("endpoint") if pending else None,
            "elapsed_s": pending.get("elapsed_s") if pending else None,
            "floor": dict(pending.get("floor") or {}) if pending else {},
        }
    return row


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
