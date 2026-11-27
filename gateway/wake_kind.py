"""Wake kinds: the PRODUCER names a wake; the progress header never guesses it from prose (t_17ba87a6).

An internal wake's progress bubbles open with one header line (``🔔 <label>``, i18n key
``gateway.progress.wake_head``). The label used to be parsed out of the wake TEXT, so a /goal
check-in whose goal mentioned a card id rendered as ``🔔 wake · internal · t_c676b4f3`` and read as a
kanban wake. Now every producer stamps ``MessageEvent.wake_kind`` (or ``deliver_wake(wake_kind=)``)
and puts its detail in event metadata: ``wake_sub`` (sub-kind), ``wake_name`` (caller / route name)
and ``card_id`` (kanban only). ``tests/gateway/test_wake_kind_producer_contract.py`` fails any
``deliver_wake(`` / ``MessageEvent(..., internal=...)`` in gateway/ that omits ``wake_kind``.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

KANBAN = "kanban"
GOAL = "goal"
LOOP = "loop"
DELEGATION = "delegation"
PROCESS = "process"
RESTART = "restart"
WATCHER = "watcher"
HEARTBEAT = "heartbeat"
WEBHOOK = "webhook"
HANDOFF = "handoff"
PLUGIN = "plugin"
STEER = "steer"
RECALL = "recall"

#: Header label per kind (Ace 2026-11-08 17:53/18:05 PT). Keep them short: the sub-kind, caller name
#: and card follow as `` · `` parts.
WAKE_KIND_LABELS = {
    KANBAN: "kanban wake",
    GOAL: "goal wake",
    LOOP: "loop wake",
    DELEGATION: "delegation done",
    PROCESS: "process done",
    RESTART: "restart resume",
    WATCHER: "watcher wake",
    HEARTBEAT: "heartbeat",
    WEBHOOK: "webhook",
    HANDOFF: "handoff",
    PLUGIN: "plugin wake",
    STEER: "steer",
    RECALL: "message recalled",
}

WAKE_KIND_KEY = "wake_kind"
WAKE_SUB_KEY = "wake_sub"
WAKE_NAME_KEY = "wake_name"
CARD_ID_KEY = "card_id"
#: Header parts after the label, in render order.
DETAIL_KEYS = (WAKE_SUB_KEY, WAKE_NAME_KEY, CARD_ID_KEY)


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:60] or None


def wake_meta(*, sub: Any = None, name: Any = None, card_id: Any = None) -> dict:
    """Event-metadata detail for a wake (only the keys that have a value)."""
    out = {}
    for key, value in ((WAKE_SUB_KEY, sub), (WAKE_NAME_KEY, name), (CARD_ID_KEY, card_id)):
        value = _clean(value)
        if value:
            out[key] = value
    return out


def kanban_wake_sub(kinds: Iterable[Any] = (), triggers: Iterable[Any] = ()) -> str:
    """Sub-kind of a kanban wake from the board event kinds / owner-wake triggers it carries."""
    seen = {str(k) for k in (*kinds, *triggers) if k}
    if "review_deadman" in seen:
        return "deadman"
    if any(k.startswith("boomerang") for k in seen):
        return "boomerang"
    if "review_requested" in seen:
        return "review"
    if seen & {"blocked", "needs_input"}:
        return "needs-input"
    return "handback"


def single_card(cards: Iterable[Any]) -> Optional[str]:
    """The card id when a wake is about exactly one card, else ``None`` (no misleading id)."""
    ids = {str(c) for c in cards if c}
    return next(iter(ids)) if len(ids) == 1 else None


_CARD_ID_RE = re.compile(r"\At_[0-9a-f]{8}\Z")
_TEXT_TAG_RE = re.compile(r"\A\s*\[([A-Za-z][A-Za-z0-9 _-]{0,39})\]")


def control_wake_identity(params: dict, text: str) -> tuple:
    """``(wake_kind, metadata detail)`` for an EXTERNAL control-socket wake (a script/cron watcher).

    The caller names itself: ``wake_name`` (or ``caller``), else the wake text's own leading bracket
    tag; an optional ``wake_kind`` from the vocabulary (default ``watcher``), ``wake_sub`` and a
    ``card_id`` (``t_xxxxxxxx`` only; a card is never read out of the text).
    """
    kind = str(params.get(WAKE_KIND_KEY) or "").strip().lower()
    if kind not in WAKE_KIND_LABELS:
        kind = WATCHER
    name = params.get(WAKE_NAME_KEY) or params.get("caller")
    if not name:
        tag = _TEXT_TAG_RE.match(text or "")
        name = tag.group(1) if tag else None
    card = str(params.get(CARD_ID_KEY) or "").strip()
    return kind, wake_meta(sub=params.get(WAKE_SUB_KEY), name=name,
                           card_id=card if _CARD_ID_RE.match(card) else None)


def wake_display_metadata(event: Any) -> dict:
    """The wake identity a turn's display metadata carries for its progress header.

    Only self-injected turns have one: an internal event's ``wake_kind`` plus its metadata detail,
    or ``heartbeat`` for the scheduled heartbeat prompt (non-internal by design). Human turns: ``{}``.
    """
    kind = getattr(event, "wake_kind", None)
    if not kind and getattr(event, "_heartbeat_session_id", None):
        kind = HEARTBEAT
    if not kind or not (getattr(event, "internal", False) or kind == HEARTBEAT):
        return {}
    meta = getattr(event, "metadata", None) or {}
    out = {WAKE_KIND_KEY: str(kind)}
    for key in DETAIL_KEYS:
        value = _clean(meta.get(key))
        if value:
            out[key] = value
    return out


def wake_label(metadata: Optional[dict]) -> Optional[str]:
    """``<kind label> · <sub> · <name> · <card>`` from producer metadata, or ``None`` if unlabeled."""
    md = metadata or {}
    label = WAKE_KIND_LABELS.get(md.get(WAKE_KIND_KEY)) if isinstance(md.get(WAKE_KIND_KEY), str) else None
    if label is None:
        return None
    parts = [label] + [v for v in (_clean(md.get(k)) for k in DETAIL_KEYS) if v]
    return " · ".join(parts)
