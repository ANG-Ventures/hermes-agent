"""Conversation prefix-stability invariant (prompt-cache correctness guard).

A prefix-cached provider only serves a cache hit when the leading bytes of a
request are identical to an earlier request's. Within one conversation the
harness must therefore keep the system prompt, the tool schemas and every
already-sent message byte-stable between consecutive requests; only the tail
may grow (the last message of the previous request may still change, new
messages append). The one sanctioned rewrite is context compaction, which the
compressor tags so it is EXCLUDED here rather than allowlisted.

This module is a read-only observer over the outbound request dict: it
produces a fingerprint (hashes + byte sizes, never text), compares two
fingerprints, and renders/dispatches the once-per-session #alerts page.
Persistence lives in ``store.record_prefix_check``; the wiring from the
transport chokepoint lives in ``agent.chat_completion_helpers`` →
``plugins.blackbox.record_api_call``. Nothing here alters a request.

HOT-PATH CONTRACT (same as ``sentinel``): nothing here may raise into, block
or slow the request path. The ledger row is already durable before the guard
runs; delivery happens on a daemon thread the caller never joins.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

CARD_REF = "t_c07124ab"

SEGMENTS = ("system", "tools", "messages")
KIND_MUTATION = "mutation"   # a historical index changed in place (the cache-collapsing class)
KIND_SHRINK = "shrink"       # history got shorter without a tagged compaction (undo/clear/trim)

_SYSTEM_ROLES = ("system", "developer")


def _strip_cache_control(node: Any) -> Any:
    """Copy ``node`` without ``cache_control`` markers.

    Breakpoint markers move as the conversation grows and are not part of
    the cached content, so they must not count as a byte change.
    """
    if isinstance(node, dict):
        return {
            k: _strip_cache_control(v) for k, v in node.items() if k != "cache_control"
        }
    if isinstance(node, (list, tuple)):
        return [_strip_cache_control(v) for v in node]
    return node


def _canonical_bytes(obj: Any) -> bytes:
    return json.dumps(
        _strip_cache_control(obj), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    ).encode("utf-8", "surrogatepass")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _entry(obj: Any) -> list:
    data = _canonical_bytes(obj)
    return [_digest(data), len(data)]


def fingerprint_request(api_kwargs: Any) -> dict[str, Any] | None:
    """Hash the three cache-relevant segments of an outbound request.

    Accepts the native request dict of any API mode: Anthropic
    (``system``/``messages``/``tools``), OpenAI chat (leading system or
    developer messages inside ``messages``) and Responses (``instructions``/
    ``input``). Returns ``None`` when the shape carries no message list, so
    the caller records nothing rather than a misleading fingerprint.

    Shape: ``{"system": [hash, nbytes] | None, "tools": [hash, nbytes] | None,
    "messages": [[hash, nbytes], ...], "checkpoint": hash | None}``. Hashes
    are sha256 prefixes of the canonical JSON with ``cache_control``
    stripped; no text is retained. ``checkpoint`` is the hash of the newest
    Responses ``type: "compaction"`` item (server-side native compaction),
    so a rewrite caused by a NEW checkpoint is recognisable as a compaction.
    """
    if not isinstance(api_kwargs, dict):
        return None
    messages = api_kwargs.get("messages")
    if messages is None:
        messages = api_kwargs.get("input")
    if not isinstance(messages, list):
        return None
    system = api_kwargs.get("system")
    if system is None:
        system = api_kwargs.get("instructions")
    history = list(messages)
    if system is None:
        leading = []
        while history and isinstance(history[0], dict) and history[0].get("role") in _SYSTEM_ROLES:
            leading.append(history.pop(0))
        system = leading or None
    tools = api_kwargs.get("tools")
    checkpoint = None
    for item in history:
        if isinstance(item, dict) and item.get("type") == "compaction":
            checkpoint = _entry(item)[0]
    return {
        "system": _entry(system) if system is not None else None,
        "tools": _entry(tools) if tools else None,
        "messages": [_entry(m) for m in history],
        "checkpoint": checkpoint,
    }


def native_checkpoint_changed(prev: dict[str, Any], cur: dict[str, Any]) -> bool:
    """True when the current request carries a native compaction checkpoint
    the previous one did not (a server-side compaction landed in between)."""
    return bool(cur.get("checkpoint")) and cur.get("checkpoint") != prev.get("checkpoint")


def history_hash(fingerprint: dict[str, Any]) -> str:
    """One hash over every message hash, for offline audit joins."""
    return _digest("|".join(h for h, _n in fingerprint.get("messages") or []).encode())


def compare(prev: dict[str, Any], cur: dict[str, Any]) -> list[dict[str, Any]]:
    """Diff two fingerprints of consecutive same-session requests.

    Returns one record per violated segment (empty list = invariant holds):
    ``{"segment", "kind", "first_divergent_index", "bytes_before", "bytes_after"}``.
    For ``messages`` every index below ``len(prev) - 1`` must be identical
    (the previous request's LAST message is allowed to change, later ones
    append). A shorter history is reported as ``shrink`` — a rewrite, but not
    the in-place toggle class that collapses cache reads mid-conversation.
    """
    out: list[dict[str, Any]] = []
    for segment in ("system", "tools"):
        a, b = prev.get(segment), cur.get(segment)
        if a == b:
            continue
        out.append({
            "segment": segment, "kind": KIND_MUTATION, "first_divergent_index": None,
            "bytes_before": a[1] if a else 0, "bytes_after": b[1] if b else 0,
        })
    p = prev.get("messages") or []
    c = cur.get("messages") or []
    stable = max(len(p) - 1, 0)
    if len(c) < stable:
        out.append({
            "segment": "messages", "kind": KIND_SHRINK, "first_divergent_index": len(c),
            "bytes_before": sum(n for _h, n in p), "bytes_after": sum(n for _h, n in c),
        })
        return out
    for i in range(stable):
        if p[i][0] != c[i][0]:
            out.append({
                "segment": "messages", "kind": KIND_MUTATION, "first_divergent_index": i,
                "bytes_before": p[i][1], "bytes_after": c[i][1],
            })
            break
    return out


def _expiry(value: Any) -> float | None:
    """Parse an allowlist ``expires`` (ISO date/datetime or epoch) to epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return 0.0  # unparseable expiry never matches: fail closed
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def allowlist_reason(
    entries: Any, *, segment: str, session_key: str | None, now: float | None = None,
) -> str | None:
    """Return the reason of the first live allowlist entry covering this event.

    ``entries`` is ``blackbox.prefix_guard_allowlist`` from config.yaml: a list
    of ``{segment, reason, expires, session_prefix?}`` dicts. Every entry
    needs a reason AND an expiry; an entry without either is ignored, so a
    permanent silent exemption cannot be configured.
    """
    if not isinstance(entries, list):
        return None
    now = time.time() if now is None else now
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        reason = str(entry.get("reason") or "").strip()
        expires = _expiry(entry.get("expires"))
        if not reason or expires is None or expires <= now:
            continue
        if str(entry.get("segment") or "").strip() not in ("", "*", segment):
            continue
        prefix = str(entry.get("session_prefix") or "")
        if prefix and not str(session_key or "").startswith(prefix):
            continue
        return reason
    return None


# ---------------------------------------------------------------------------
# Alerting — once per session, on the transition recorded by the store.
# ---------------------------------------------------------------------------

def _describe(v: dict[str, Any]) -> str:
    where = v["segment"]
    if v["segment"] == "messages" and v.get("first_divergent_index") is not None:
        where = f"messages[{v['first_divergent_index']}]"
    return (
        f"{where} ({v['kind']}) {int(v.get('bytes_before') or 0):,}"
        f" → {int(v.get('bytes_after') or 0):,} bytes"
    )


def render_alert(
    *, profile: str, provider: str, model: str, session_key: str, turn_id: str,
    seq: int, violations: list[dict[str, Any]], messages_before: int,
    messages_after: int, cache_read_before: int | None, cache_read_after: int | None,
    suppressed: int = 0,
) -> str:
    """The #alerts message body. Pure, so it is directly assertable."""
    lines = [
        "🧬 Prompt-cache prefix mutation (history rewritten mid-session)",
        f"• Profile: {profile or '(unknown)'} · lane: {provider or '(empty)'}/{model or '(empty)'}",
        f"• Session: {session_key} · turn {turn_id[:32]} · call #{seq}",
    ]
    for v in violations:
        lines.append(f"• Changed: {_describe(v)}")
    lines.append(f"• History: {messages_before} → {messages_after} messages")
    if cache_read_before is not None or cache_read_after is not None:
        before = f"{cache_read_before:,}" if cache_read_before is not None else "?"
        after = f"{cache_read_after:,}" if cache_read_after is not None else "?"
        lines.append(f"• Cache read tokens: {before} → {after}")
    if suppressed:
        lines.append(f"• {suppressed} more session(s) hit this since the last page")
    lines.append(f"• One page per session · rows in blackbox prefix_mutations · card {CARD_REF}")
    return "\n".join(lines)


def send_alert(body: str) -> bool:
    """Deliver ``body`` to Discord #alerts via notify.py. Never raises."""
    try:
        from plugins.blackbox.sentinel import ALERTS_CHANNEL_ID, _notify_script

        script = _notify_script()
        if script is None:
            logger.warning("blackbox prefix guard: no notify.py found; alert skipped")
            return False
        proc = subprocess.run(
            [sys.executable, str(script), "--send", body,
             "--channel", "discord", "--target", ALERTS_CHANNEL_ID],
            check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30,
        )
        return proc.returncode == 0
    except Exception:
        logger.warning("blackbox prefix guard: alert delivery failed", exc_info=True)
        return False


def dispatch_alert(
    body: str, alert_fn: Callable[[str], bool] | None = None,
) -> threading.Thread | None:
    """Fire the page on a daemon thread. Fire-and-forget; the request path
    never joins it (the handle is returned for tests only)."""
    fn = alert_fn or send_alert

    def _run() -> None:
        try:
            fn(body)
        except Exception:
            logger.warning("blackbox prefix guard: alert thread failed", exc_info=True)

    try:
        thread = threading.Thread(target=_run, name="blackbox-prefix-guard", daemon=True)
        thread.start()
        return thread
    except Exception:
        logger.warning("blackbox prefix guard: could not start alert thread", exc_info=True)
        return None
