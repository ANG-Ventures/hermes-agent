"""Redundant-notification suppression for the kanban wake/notify paths (t_4c7da648).

Ace, 2026-10-11: five ``[kanban] Task … completed`` turns reached the home chat
for cards the chat's own operator session had just closed, each answered with
"echo, nothing further". A notification about the receiver's own action carries
no information. This module is the ONE predicate both paths ask before they
spend a chat turn: the per-subscription notifier (``kanban_watchers``) and the
owner wake (``kanban_owner_wake``).

An event is redundant when ALL hold:

* ``kanban.notify_suppress.enabled`` (default on);
* its kind is in ``kanban.notify_suppress.kinds`` (default completed, archived)
  and NOT in :data:`NEVER_SUPPRESS` (a hard allowlist above config: a handback,
  a block, a crash, a triage route always wakes);
* the actor is the receiving side itself: the event's ``actor_session_id`` is
  the home / subscriber session, or its ``actor_profile`` is the home session's
  operator profile, or it was written by a configured closer (the merge pass
  landing a card) on a card the receiver owns (its home session is one of the
  receiver's sessions), or by a configured closer on a foreign card whose
  latest ``review_coverage`` APPROVE is the operator's;
* the event is within ``self_window_min`` (default 30) of the operator's last
  own action on the card: the event itself for a self actor, the APPROVE record
  for a closer on a foreign card.

Closers identify themselves by stamping ``actor_profile`` (``kanban complete
--actor <closer>``); the parenthesised summary tag is a logged last resort.

Anything unresolved (no actor, unknown operator, no APPROVE) is NOT redundant:
the filter fails open to noise, never to silence on a real handback.

A suppressed event is not dropped: the caller records a ``notify_suppressed``
task event (:func:`record_suppressed`) and the notifier routes the line to the
lifecycle channel tagged ``[suppressed:<reason>]``; only the chat turn is withheld.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import sqlite3
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

EVENT_KIND = "notify_suppressed"

DEFAULT_KINDS = ("completed", "archived")
DEFAULT_SELF_WINDOW_MIN = 30
# Identities of the review-card closers: ``actor_profile`` when stamped, else the
# ``(<closer>, …)`` tag the closer writes at the head of its completion summary
# ("Apollo merge pass (review-card closer, board default): …" carries none).
DEFAULT_CLOSERS = ("review-card closer", "land-autopilot", "merge-pass",
                   "review-merge-pass", "kanban-review-merge-pass")

# Kinds that hand a decision back. Never suppressed, whatever the config says.
NEVER_SUPPRESS = frozenset({
    "review_requested", "changes_requested", "blocked", "block_loop_detected",
    "crashed", "timed_out", "gave_up", "stalled", "skipped_nonspawnable",
})
# Owner-wake triggers that are never suppressed (the last blocker of open
# children going done is a decision for the owner, not an echo).
NEVER_SUPPRESS_TRIGGERS = frozenset({"last_blocker_done"})

# Apollo runs as the root ``default`` profile; its review records say ``apollo``.
_ROOT_OPERATOR_LABELS = frozenset({"default", "apollo"})

_SUMMARY_TAG_RE = re.compile(r"\(([^()]{1,80})\)")
_COVERAGE_RE = re.compile(r"review_coverage:\s*(\{.*)", re.S)


@dataclasses.dataclass(frozen=True)
class SuppressConfig:
    enabled: bool = True
    kinds: frozenset = frozenset(DEFAULT_KINDS)
    self_window_s: int = DEFAULT_SELF_WINDOW_MIN * 60
    closers: tuple = DEFAULT_CLOSERS
    actionable_only: bool = True


# --- S5: actionable-only wakes (t_4f6afb67) -----------------------------------
#
# 2026-10-12, two operator sessions: one red PR woke t_1ac4c83b four times (two
# of them citing a run already handled), and the operator's own request-changes
# woke the operator. Each of these is a ledger row now (a ``notify_suppressed``
# receipt, shown by ``kanban show``), not a turn. The inherited-from-main red
# rule was retired with hermes-home#8100 (a PR lands on its own checks).
# ``kanban.notify_suppress.actionable_only: false`` turns them all off.
REASON_DUPLICATE_RED = "duplicate-red"      # same (card, PR, head, red-set) already woke
REASON_STALE_HANDBACK = "stale-handback"    # the card left review before the wake ran
REASON_OWN_ECHO = "own-session-echo"        # the home session made this transition itself
REASON_AUTO_ROUTE = "auto-route"            # a closer sent the card back to its implementer itself
# Kinds the home session's own action produces; an echo of them is no news.
OWN_ECHO_KINDS = frozenset({"changes_requested", "review_requested", "blocked", "archived"})
RED_SEEN_TTL_S = 24 * 3600


def red_handback_noise(keys: Iterable[str], seen: Optional[dict],
                       card_status: Optional[str] = None) -> Optional[str]:
    """Why a red/dirty review handback is NOT actionable, or None (wake).
    ``seen``: red keys already delivered (key -> epoch); ``card_status``: the card now."""
    keys = [k for k in keys or () if k]
    if card_status is not None and card_status != "review":
        return REASON_STALE_HANDBACK
    if keys and all(k in (seen or {}) for k in keys):
        return REASON_DUPLICATE_RED
    return None


def note_red_seen(seen: dict, keys: Iterable[str], now: float) -> None:
    """Record delivered red keys; prune entries past :data:`RED_SEEN_TTL_S`."""
    for k in [k for k, ts in seen.items() if now - float(ts) > RED_SEEN_TTL_S]:
        seen.pop(k, None)
    for k in keys or ():
        if k:
            seen[k] = now


def is_own_echo(event: Any, home_session_id: Optional[str], is_home: bool) -> bool:
    """The home session's own changes_requested / review_requested / blocked / archived,
    delivered back to the home chat. A foreign subscriber still gets it."""
    actor = _s(getattr(event, "actor_session_id", None) if not isinstance(event, dict)
               else event.get("actor_session_id"))
    kind = _s(getattr(event, "kind", None) if not isinstance(event, dict) else event.get("kind"))
    return bool(is_home and actor and actor == _s(home_session_id) and kind in OWN_ECHO_KINDS)


def is_auto_route(event: Any, cfg: "SuppressConfig") -> bool:
    """A ``changes_requested`` a configured closer STAMPED (``actor_profile`` exactly a closer,
    never the prose guess): the merge pass already requeued the card to its implementer
    (``HOLD-FR``, own-diff red), so the owner has nothing to decide."""
    g = (lambda k: event.get(k)) if isinstance(event, dict) else (lambda k: getattr(event, k, None))
    actor = _s(g("actor_profile")).lower()
    return _s(g("kind")) == "changes_requested" and bool(actor) and actor in {_s(c).lower() for c in cfg.closers}


def _truthy(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "off", "no", "")
    return bool(v)


def _names(v: Any, default: Iterable[str]) -> tuple:
    if v is None:
        return tuple(default)
    if isinstance(v, str):
        v = v.split(",")
    return tuple(str(x).strip().lower() for x in v if str(x).strip())


def parse_config(kcfg: Optional[dict]) -> SuppressConfig:
    """``kanban.notify_suppress`` -> :class:`SuppressConfig`. Pure; bad values -> defaults."""
    raw = (kcfg or {}).get("notify_suppress") if isinstance(kcfg, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    try:
        window = int(float(raw.get("self_window_min", DEFAULT_SELF_WINDOW_MIN)))
    except (TypeError, ValueError):
        window = DEFAULT_SELF_WINDOW_MIN
    return SuppressConfig(
        enabled=_truthy(raw.get("enabled"), True),
        kinds=frozenset(_names(raw.get("kinds"), DEFAULT_KINDS)),
        self_window_s=max(0, window) * 60,
        closers=_names(raw.get("closers"), DEFAULT_CLOSERS),
        actionable_only=_truthy(raw.get("actionable_only"), True),
    )


def resolve_config(profile_home: Optional[Any] = None) -> SuppressConfig:
    """Live ``config.yaml`` read, same loader as ``kanban_owner_wake.resolve_settings``.

    ``profile_home``: read THAT profile's config (a multiplexing gateway serves
    several profiles from one notifier; each subscription's owner decides its own
    policy, Prism P1 dc5a880e1f16). ``None`` = the current scope's home."""
    try:
        from hermes_cli.config import load_config_readonly

        if profile_home is not None:
            from hermes_constants import reset_hermes_home_override, set_hermes_home_override

            token = set_hermes_home_override(str(profile_home))
            try:
                kcfg = (load_config_readonly() or {}).get("kanban") or {}
            finally:
                reset_hermes_home_override(token)
        else:
            kcfg = (load_config_readonly() or {}).get("kanban") or {}
    except Exception:
        kcfg = {}
    return parse_config(kcfg if isinstance(kcfg, dict) else {})


# --- identity helpers --------------------------------------------------------


def _s(v: Any) -> str:
    return str(v or "").strip()


def _operator_label(name: Any) -> str:
    v = _s(name).lower()
    if v.startswith("human:"):
        v = v[len("human:"):]
    return "default" if v in _ROOT_OPERATOR_LABELS else v


def same_operator(a: Any, b: Any) -> bool:
    """``default`` / ``apollo`` / ``human:apollo`` name one operator."""
    la, lb = _operator_label(a), _operator_label(b)
    return bool(la) and la == lb


def closer_identity(event: Any) -> str:
    """Who closed the card: ``actor_profile`` when stamped, else the first
    parenthesised tag of the completion summary (``review-card closer``)."""
    actor = _s(getattr(event, "actor_profile", None))
    if actor:
        return actor.lower()
    payload = getattr(event, "payload", None) or {}
    summary = _s(payload.get("summary") if isinstance(payload, dict) else "")
    head = summary.splitlines()[0][:160] if summary else ""
    m = _SUMMARY_TAG_RE.search(head)
    if not m:
        return ""
    ident = m.group(1).split(",")[0].strip().lower()
    # Last resort, and visible: a closer that did not stamp ``actor_profile``
    # (``kanban complete --actor <closer>``) is guessed from prose, which read
    # "(CI gate, …)" as the closer on 2026-10-12 and delivered the echo.
    logger.info("kanban notify-suppress: closer identity from summary prose: %r (event %s, actor_profile unstamped)",
                ident, getattr(event, "id", None))
    return ident


def is_closer(identity: str, closers: Iterable[str]) -> bool:
    ident = _s(identity).lower()
    if not ident:
        return False
    for c in closers:
        c = _s(c).lower()
        if c and (ident == c or ident.startswith(c + "/") or ident.startswith(c + ":")
                  or ident.startswith(c + " ")):
            return True
    return False


def parse_approve(body: str) -> Optional[dict]:
    """``{"reviewer": …}`` when a comment body carries a ``review_coverage`` APPROVE record."""
    m = _COVERAGE_RE.search(body or "")
    if not m:
        return None
    try:
        rec = json.JSONDecoder().raw_decode(m.group(1))[0]
    except ValueError:
        return None
    if not isinstance(rec, dict) or _s(rec.get("verdict")).upper() != "APPROVE":
        return None
    return rec


def latest_approve(conn: sqlite3.Connection, task_id: str, before_ts: Optional[int] = None) -> Optional[dict]:
    """Newest ``review_coverage`` APPROVE on the card: ``{"by": <operator label>, "ts": <epoch>}``.
    ``by`` is the record's ``reviewer`` field, else the comment author."""
    rows = conn.execute(
        "SELECT author, body, created_at FROM task_comments WHERE task_id = ? "
        "AND body LIKE '%review_coverage%' ORDER BY id DESC LIMIT 20", (task_id,),
    ).fetchall()
    for author, body, ts in rows:
        if before_ts is not None and int(ts) > int(before_ts):
            continue
        rec = parse_approve(str(body or ""))
        if rec is None:
            continue
        return {"by": _s(rec.get("reviewer")) or _s(author), "ts": int(ts)}
    return None


def card_context(conn: sqlite3.Connection, task_id: str, home_session_id: Optional[str],
                 operator_profile: Optional[str], event_ts: Optional[int] = None) -> dict:
    """The card-side facts :func:`is_redundant` needs, read once per card."""
    return {
        "session_id": _s(home_session_id),
        "operator_profile": _s(operator_profile),
        "latest_approve": latest_approve(conn, task_id, event_ts),
    }


# --- the predicate -----------------------------------------------------------


def is_redundant(event: Any, card: Optional[dict], subscription: Optional[dict],
                 cfg: SuppressConfig, *, trigger: Optional[str] = None) -> tuple[bool, str]:
    """``(redundant, reason)`` for one event about to wake the receiving side.

    ``event`` has ``kind``, ``payload``, ``created_at``, ``actor_profile``,
    ``actor_session_id`` (``kanban_db.Event`` or any object / dict with those).
    ``card`` is :func:`card_context`'s dict (``session_id`` = the home session,
    ``operator_profile`` = the profile that owns it, ``latest_approve``).
    ``subscription`` may add ``session_ids`` (sessions the receiving chat owns);
    ``trigger`` is the owner-wake trigger, if any.
    """
    def g(obj: Any, key: str) -> Any:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    if not cfg.enabled:
        return False, "disabled"
    kind = _s(g(event, "kind")).lower()
    if kind in NEVER_SUPPRESS or (trigger and trigger in NEVER_SUPPRESS_TRIGGERS):
        return False, "protected-kind"
    if kind not in cfg.kinds:
        return False, "kind-not-configured"
    card = card or {}
    sub = subscription or {}
    # The notifier's own origin match (kanban_watchers.self_caused_event_ids):
    # the actor session provably lives in the subscriber's chat.
    if g(event, "id") in set(sub.get("self_event_ids") or ()):
        return True, "self-session"
    # Card-side evidence (home session, its operator, its APPROVE) only counts
    # for the receiver that IS the home: a foreign subscriber (another operator's
    # chat subscribed to this card) is not told by the home's own close, and
    # must still get its line (Prism P1 03f8b8156055). Default True keeps the
    # owner-wake path, whose receiver is the home by construction.
    if not sub.get("is_home", True):
        return False, "foreign-subscriber"
    receiving = {_s(card.get("session_id"))}
    receiving.update(_s(x) for x in (sub.get("session_ids") or ()))
    receiving.discard("")
    operator = _s(card.get("operator_profile"))
    actor_sid = _s(g(event, "actor_session_id"))
    actor_profile = _s(g(event, "actor_profile"))
    try:
        event_ts = int(g(event, "created_at") or 0)
    except (TypeError, ValueError):
        event_ts = 0

    if actor_sid and actor_sid in receiving:
        return True, "self-session"
    if actor_profile and operator and same_operator(actor_profile, operator):
        return True, "self-operator"
    if not operator:
        return False, "unknown-operator"
    ident = closer_identity(event)
    if ident and is_closer(ident, cfg.closers):
        # A trusted closer acting on a card the receiver OWNS (its home session
        # is one of the receiver's sessions) is self-operator by construction:
        # the lander only closes what the operator's lane handed it. The
        # APPROVE-in-window test gates closers on cards the receiver does NOT
        # own (second miss, t_fb95da9b 2026-10-12 14:10: the PR landed under the
        # lander before the attended read, so no APPROVE existed, and the echo
        # was delivered).
        #
        # The shortcut has no window and no APPROVE, so it trusts ONLY a STAMPED
        # ``actor_profile`` that is EXACTLY a configured closer: never the
        # summary-prose guess, never the prefix match (Prism #1933 r1: an
        # unstamped "(merge-pass follow-up) ..." summary would otherwise silence
        # the owner). Prose/prefix identities keep the APPROVE-gated path below.
        home_sid = _s(card.get("session_id"))
        owned = bool(home_sid) and (
            home_sid in {_s(x) for x in (sub.get("session_ids") or ())}
            or sub.get("is_home") is True   # the notifier's explicit "this subscription IS the home chat"
        )
        if owned and actor_profile.lower() in {_s(c).lower() for c in cfg.closers}:
            return True, "closer-on-owned-card"
        approve = card.get("latest_approve") or None
        if not approve or not same_operator(approve.get("by"), operator):
            return False, "closer-without-operator-approve"
        try:
            age = event_ts - int(approve.get("ts") or 0)
        except (TypeError, ValueError):
            return False, "closer-approve-unaged"
        if age < 0 or age > cfg.self_window_s:
            return False, "closer-approve-outside-window"
        return True, "closer-after-operator-approve"
    return False, "other-actor"


# --- the receipt ---------------------------------------------------------------


def record_suppressed(board: Optional[str], task_id: str, event: Any, reason: str,
                      path: str) -> bool:
    """Append one ``notify_suppressed`` task event for ``event`` (idempotent per
    event id + path). Blocking; worker thread only. Never raises."""
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        event_id = int(getattr(event, "id", 0) or 0)
        payload = {
            "event_id": event_id, "kind": _s(getattr(event, "kind", "")), "reason": reason, "path": path,
            "actor": _s(getattr(event, "actor_profile", None)) or closer_identity(event) or None,
            "actor_session_id": _s(getattr(event, "actor_session_id", None)) or None,
        }
        conn = kbc.connect(board=board)
        try:
            with kbc.write_txn(conn):
                dup = conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? AND payload LIKE ? LIMIT 1",
                    (task_id, EVENT_KIND, f'%"event_id": {event_id},%"path": "{path}"%'),
                ).fetchone()
                if dup is None:
                    kb._append_event(conn, task_id, EVENT_KIND, payload)  # noqa: SLF001 -- the one event writer
        finally:
            conn.close()
        logger.info("kanban notify-suppress: withheld %s event %s for %s (%s, %s)",
                    payload["kind"], event_id, task_id, reason, path)
        return True
    except Exception as exc:
        logger.warning("kanban notify-suppress: could not record receipt for %s: %s", task_id, exc)
        return False
