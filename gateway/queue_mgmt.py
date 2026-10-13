"""Gateway ``/queue`` management: list / edit / rm / move / clear / add / run, plus the held store.

Called from both ``/queue`` entry points (busy: ``run_busy._busy_queue_command``; idle:
``run_inbound._hm_cmd_queue``). Verb parsing is shared with the CLI
(``hermes_cli/queue_verbs.py``); this module owns the gateway stores.

Stores (spec 2026-10-12_gateway-queue-management-spec.md):

* **active** — today's queue: the adapter slot ``adapter._pending_messages[key]`` (item 1, a
  run-next trigger several drains consume) + ``SessionState.conversation.queued_events``.
* **held** — ``SessionState.conversation.held_queue``. ``/stop`` moves every user item here (D14).
  No drain, rescue, or promotion path reads it; only ``/queue run`` / ``/queue clear`` /
  a session boundary empty it.

While ``held`` is non-empty, new ``/queue`` items append to it, and the verbs (list numbering, rm,
edit, move, clear) operate on the held store. The active store can still gain user items at the
same time: a plain message under ``busy_input_mode: queue`` is a normal message (D14: it runs
normally; held items keep waiting). ``/queue list`` shows those in a second, unnumbered section and
``/queue run`` restages oldest first: held, then those later active items.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from agent.i18n import t
from hermes_cli.queue_verbs import QueueOp, parse_queue_payload

logger = logging.getLogger(__name__)

_PREVIEW = 60


def is_system_event(runner: Any, event: Any) -> bool:
    """Internal wakes and synthetic /goal continuations: listed, never user-editable (I3)."""
    return bool(getattr(event, "internal", False)) or runner._is_goal_continuation_event(event)


def preview(event: Any) -> str:
    text = " ".join(str(getattr(event, "text", "") or "").split())
    tag = ""
    if getattr(event, "media_urls", None):
        tag = " [image]" if not text else " [+attachment]"
    if not text:
        return tag.strip() or "[empty]"
    return f"{text[:_PREVIEW]}{'…' if len(text) > _PREVIEW else ''}{tag}"


# --------------------------------------------------------------------------- stores

def held_list(runner: Any, key: str) -> Optional[List[Any]]:
    state = runner._peek_session_state(key)
    return getattr(state.conversation, "held_queue", None) if state else None


def active_items(runner: Any, key: str, adapter: Any) -> List[Any]:
    slot = getattr(adapter, "_pending_messages", None) if adapter is not None else None
    items: List[Any] = []
    if isinstance(slot, dict) and slot.get(key) is not None:
        items.append(slot[key])
    items.extend(runner._overflow_queue(key) or ())
    return items


def user_depth(runner: Any, key: str, adapter: Any) -> int:
    held = held_list(runner, key) or []
    active = [e for e in active_items(runner, key, adapter) if not is_system_event(runner, e)]
    return len(held) + len(active)


def rewrite_active(runner: Any, key: str, adapter: Any, items: List[Any], *, arm: Optional[bool] = True) -> bool:
    """Replace the active store with ``items`` in order; mutates the overflow list in place (I5b).
    Refuses, mutating nothing, when the adapter has no slot dict.

    ``arm`` decides whether item 1 goes into the adapter slot, which is a RUN-NEXT TRIGGER (the
    adapter's end-of-task drain dispatches it). ``True``: slot = item 1 (release paths, hold).
    ``None``: keep whatever the slot state was (management verbs: editing the queue must never start
    a turn on an idle session; Momus p4 RC-B). ``False``: everything into overflow."""
    slot = getattr(adapter, "_pending_messages", None) if adapter is not None else None
    if not isinstance(slot, dict):
        return False
    if arm is None:
        arm = slot.get(key) is not None
    overflow = runner._session_state(key).conversation.queued_events
    slot.pop(key, None)
    del overflow[:]
    if items and arm:
        slot[key] = items[0]
        overflow.extend(items[1:])
    else:
        overflow.extend(items)
    return True


def hold_user_items(runner: Any, key: str, adapter: Any) -> int:
    """D14: move every user item from the active store to ``held_queue`` (order kept); system
    events stay active exactly where they were. Returns the number of held items now."""
    # Runs inside /stop: must never raise and never lose an item. Copy into held FIRST, then strip
    # the active store; if the strip fails, roll the copy back so nothing is in both or neither.
    try:
        active = active_items(runner, key, adapter)
        users = [e for e in active if not is_system_event(runner, e)]
        if users:
            held_store = runner._session_state(key).conversation.held_queue
            before = len(held_store)
            held_store.extend(users)
            if not rewrite_active(runner, key, adapter, [e for e in active if is_system_event(runner, e)]):
                del held_store[before:]
                return before
            logger.info("queue_mgmt op=hold session=%s moved=%d held=%d", key, len(users), len(held_store))
        return len(held_list(runner, key) or [])
    except Exception:
        logger.warning("queue_mgmt hold failed for %s; queue left as-is", key, exc_info=True)
        return 0


# --------------------------------------------------------------------------- verbs

def _store_for_verbs(runner: Any, key: str, adapter: Any) -> Tuple[str, List[Any]]:
    held = held_list(runner, key)
    if held:
        return "held", list(held)
    return "active", active_items(runner, key, adapter)


def _write_store(runner: Any, key: str, adapter: Any, which: str, items: List[Any]) -> bool:
    if which == "held":
        held = runner._session_state(key).conversation.held_queue
        del held[:]
        held.extend(items)
        return True
    return rewrite_active(runner, key, adapter, items, arm=None)


def format_list(runner: Any, key: str, adapter: Any) -> str:
    which, items = _store_for_verbs(runner, key, adapter)
    if not items:
        return t("gateway.queue.empty")
    header = t("gateway.queue.held_header" if which == "held" else "gateway.queue.list_header", count=len(items))
    lines = [header]
    for i, ev in enumerate(items, 1):
        key_ = "gateway.queue.item_system" if is_system_event(runner, ev) else "gateway.queue.item"
        lines.append(t(key_, index=i, preview=preview(ev)))
    if which == "held":
        later = [e for e in active_items(runner, key, adapter) if not is_system_event(runner, e)]
        if later:
            lines.append(t("gateway.queue.active_after_held", count=len(later)))
            lines.extend(f"  · {preview(e)}" for e in later)
    lines.append(t("gateway.queue.list_footer"))
    return "\n".join(lines)


def _refuse_system(runner: Any, items: List[Any], *indices: int) -> Optional[str]:
    for idx in indices:
        if 1 <= idx <= len(items) and is_system_event(runner, items[idx - 1]):
            return t("gateway.queue.refused_system", index=idx)
    return None


def apply_management(runner: Any, op: QueueOp, key: str, adapter: Any) -> str:
    """list / rm / edit / move / clear / help. Returns the reply (always non-empty, I4)."""
    if op.kind == "list":
        return format_list(runner, key, adapter)
    if op.kind == "help":
        return t("gateway.queue.help")
    which, items = _store_for_verbs(runner, key, adapter)
    before = len(items)
    if op.kind == "clear":
        kept = [e for e in items if is_system_event(runner, e)]
        dropped = before - len(kept)
        if not _write_store(runner, key, adapter, which, kept):
            return t("gateway.queue.unavailable")
        logger.info("queue_mgmt op=clear session=%s store=%s depth_before=%d depth_after=%d", key, which, before, len(kept))
        if not dropped:
            return t("gateway.queue.empty")
        return t("gateway.queue.cleared_one" if dropped == 1 else "gateway.queue.cleared_other", count=dropped)
    idx = op.index or 0
    if not 1 <= idx <= before:
        return t("gateway.queue.item_not_found", index=idx, size=before)
    if op.kind == "rm":
        refused = _refuse_system(runner, items, idx)
        if refused:
            return refused
        removed = items.pop(idx - 1)
        if not _write_store(runner, key, adapter, which, items):
            return t("gateway.queue.unavailable")
        logger.info("queue_mgmt op=rm session=%s store=%s depth_before=%d depth_after=%d", key, which, before, len(items))
        return t("gateway.queue.removed", index=idx, preview=preview(removed))
    if op.kind == "edit":
        if not op.text:
            return t("gateway.queue.usage_edit")
        refused = _refuse_system(runner, items, idx) or refuse_command_prompt(op.text)
        if refused:
            return refused
        if which == "active" and not isinstance(getattr(adapter, "_pending_messages", None), dict):
            return t("gateway.queue.unavailable")   # check storage before mutating the shared event
        ev = items[idx - 1]
        kept_media = bool(getattr(ev, "media_urls", None))
        try:
            ev.text = op.text  # D19: text only; attachments stay
        except Exception:
            return t("gateway.queue.unavailable")
        if not _write_store(runner, key, adapter, which, items):
            return t("gateway.queue.unavailable")
        logger.info("queue_mgmt op=edit session=%s store=%s index=%d", key, which, idx)
        reply = t("gateway.queue.updated", index=idx, preview=preview(ev))
        return reply + (t("gateway.queue.attachment_kept") if kept_media else "")
    if op.kind == "move":
        if op.to is None:
            return t("gateway.queue.usage_move")
        dst = op.to
        if not 1 <= dst <= before:
            return t("gateway.queue.move_out_of_range", size=before)
        refused = _refuse_system(runner, items, idx, dst)
        if refused:
            return refused
        items.insert(dst - 1, items.pop(idx - 1))
        if not _write_store(runner, key, adapter, which, items):
            return t("gateway.queue.unavailable")
        logger.info("queue_mgmt op=move session=%s store=%s from=%d to=%d", key, which, idx, dst)
        return t("gateway.queue.moved", source=idx, destination=dst)
    return t("gateway.queue.help")


def refuse_command_prompt(text: str) -> Optional[str]:
    """A queued slash command is discarded at dequeue (run_turn safety net); refuse it up front."""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    word = stripped.split(None, 1)[0][1:].lower()
    if not word:
        return None
    try:
        from hermes_cli.commands import resolve_command
        if resolve_command(word):
            return t("gateway.queue.refused_command", command=word)
    except Exception:
        return None
    return None


def cap_reply(runner: Any, key: str, adapter: Any) -> Optional[str]:
    cap = getattr(runner, "_BUSY_QUEUE_MAX_PENDING", 32)
    if user_depth(runner, key, adapter) >= cap:
        logger.warning("queue_mgmt op=add session=%s refused: at cap (%d)", key, cap)
        return t("gateway.queue.cap_reached", cap=cap)
    return None


def append_if_held(runner: Any, key: str, event: Any) -> Optional[str]:
    """D14: while held items exist, a new /queue item joins them (FIFO), never the active queue."""
    held = held_list(runner, key)
    if not held:
        return None
    held.append(event)
    logger.info("queue_mgmt op=add store=held session=%s depth=%d", key, len(held))
    return t("gateway.queue.added_to_held", count=len(held))


MERGE_SEPARATOR = "\n\n"


def release_mode(runner: Any) -> str:
    """``display.queue_release_mode``: ``per_item`` (each released prompt is its own turn) or
    ``merged`` (every released prompt joins ONE turn). Anything else reads as ``per_item``."""
    mode = getattr(runner, "_queue_release_mode", "per_item")
    return "merged" if mode == "merged" else "per_item"


def merge_user_events(events: List[Any]) -> Any:
    """One event carrying every user prompt of ``events`` in order (follow-up spec D3): texts joined
    by a blank line, media concatenated, the FIRST item's identity/reply anchor kept. The merged
    event is the first item mutated in place, so its source/thread metadata survive."""
    head = events[0]
    texts = [str(getattr(e, "text", "") or "").strip() for e in events]
    head.text = MERGE_SEPARATOR.join(t for t in texts if t)
    for e in events[1:]:
        if getattr(e, "media_urls", None):
            head.media_urls.extend(e.media_urls)
            head.media_types.extend(getattr(e, "media_types", []) or [])
            if hasattr(head, "media_text_inlined"):
                head.media_text_inlined.extend(getattr(e, "media_text_inlined", []) or [])
        if hasattr(head, "absorb_reply_expected"):
            try:
                head.absorb_reply_expected(e)
            except Exception:
                pass
    return head


def restage_held(runner: Any, key: str, adapter: Any) -> Optional[int]:
    """Move held items back into the active store (slot = item 1) and return the user-item count
    released. The adapter's existing end-of-task / post-command drain is the ONLY dispatcher
    (spec D16/I9: single door); this function never starts a task. None = no slot storage.

    Under ``display.queue_release_mode: merged`` every released user item becomes ONE event (one
    turn, one answer); system events keep their own turns ahead of it. The armed head carries
    ``_queue_released`` so the inbound FIFO rescue never reorders the rest ahead of it (D1)."""
    held = held_list(runner, key)
    active = active_items(runner, key, adapter)
    system = [e for e in active if is_system_event(runner, e)]
    later_users = [e for e in active if not is_system_event(runner, e)]
    users = list(held or []) + later_users
    if not users:
        return 0
    released = len(users)
    if release_mode(runner) == "merged" and len(users) > 1:
        users = [merge_user_events(users)]
    for e in users:
        try:
            e._queue_released = True
        except Exception:
            pass
    if not rewrite_active(runner, key, adapter, system + users, arm=True):
        for e in users:
            try:
                e._queue_released = False
            except Exception:
                pass
        return None
    if held:
        del held[:]
    logger.info("queue_mgmt op=release session=%s released=%d mode=%s armed=%d", key, released,
                release_mode(runner), len(system) + len(users))
    return released


async def release(runner: Any, key: str, source: Any, adapter: Any, *, busy: bool) -> str:
    """``/queue run``: busy → interrupt the turn (which holds everything first), then restage; idle →
    restage. The running/command task's own drain then dispatches item 1 under a new generation."""
    if busy:
        from gateway.run import _INTERRUPT_REASON_STOP
        await runner._interrupt_and_clear_session(
            key, source, interrupt_reason=_INTERRUPT_REASON_STOP, invalidation_reason="queue_run")
    count = restage_held(runner, key, adapter)
    if count is None:
        return t("gateway.queue.unavailable")
    if not count:
        return t("gateway.queue.empty")
    return t("gateway.queue.released_interrupting" if busy else "gateway.queue.released", count=count)


def stop_reply_suffix(runner: Any, key: str, adapter: Any) -> str:
    """Appended to every /stop reply: the held-count reminder (D12), or, with
    ``display.stop_releases_queue`` on (D17), restage so /stop's own tail drain releases them.
    Part of the /stop reply: never raises (an empty suffix beats a failed /stop)."""
    try:
        held = held_list(runner, key) or []
        if not held:
            return ""
        if getattr(runner, "_stop_releases_queue", False) is True:
            count = restage_held(runner, key, adapter)
            if count:
                return "\n" + t("gateway.stop.queue_released", count=count)
        return "\n" + t("gateway.stop.queue_held", count=len(held_list(runner, key) or []))
    except Exception:
        logger.warning("queue_mgmt stop suffix failed for %s", key, exc_info=True)
        return ""


# Session boundaries (spec D21): automatic resets carry held items; user resets drop and report them.
AUTO_RESET_REASONS = frozenset({"auto_reset", "compression_exhausted_reset"})
USER_RESET_REASONS = frozenset({"session_reset", "resume"})


def note_reset_drop(runner: Any, key: str, count: int) -> None:
    """Record items a user reset dropped (reported once by the reset reply). Never raises."""
    try:
        if count > 0:
            drops = runner.__dict__.setdefault("_queue_reset_drops", {})
            drops[key] = drops.get(key, 0) + count
            logger.info("queue_mgmt op=reset_drop session=%s dropped=%d", key, count)
    except Exception:
        logger.warning("queue_mgmt reset-drop note failed for %s", key, exc_info=True)


def take_reset_drop_suffix(runner: Any, key: str) -> str:
    """Consume the recorded drop count as a reply line. Never raises."""
    try:
        count = runner.__dict__.get("_queue_reset_drops", {}).pop(key, 0)
        return ("\n" + t("gateway.queue.dropped_on_reset", count=count)) if count else ""
    except Exception:
        logger.warning("queue_mgmt reset-drop suffix failed for %s", key, exc_info=True)
        return ""


def before_scope_clear(runner: Any, key: str, reason: str) -> Optional[List[Any]]:
    """Called by ``_clear_conversation_scope`` before it resets ConversationState. Returns the held
    items to restore afterwards (automatic resets), records the drop count (user resets)."""
    # This runs inside THE conversation-boundary funnel: it must never raise, or a /new would fail
    # half-way. Any surprise (odd state shape) fails open: no carry, no count, the clear proceeds.
    try:
        state = runner._peek_session_state(key)
        if state is None:
            return None
        held_raw = getattr(state.conversation, "held_queue", None)
        held = list(held_raw) if isinstance(held_raw, list) else []
        overflow_raw = getattr(state.conversation, "queued_events", None)
        overflow = list(overflow_raw) if isinstance(overflow_raw, list) else []
        users_overflow = [e for e in overflow if not is_system_event(runner, e)]
        if reason in USER_RESET_REASONS:
            note_reset_drop(runner, key, len(held) + len(users_overflow))
            return None
        # Automatic resets have no reply target, so they must not drop anything (D5/D21): carry the
        # held items AND any active user items into the held store. An unknown reason is treated
        # the same way (carry + WARNING) so a new boundary can never drop the queue silently.
        carried = held + users_overflow
        if reason not in AUTO_RESET_REASONS and carried:
            logger.warning("queue_mgmt op=scope_clear session=%s reason=%s (unclassified) carried %d "
                           "queued item(s) into the held store", key, reason, len(carried))
        elif users_overflow:
            logger.info("queue_mgmt op=auto_reset session=%s carried %d active item(s) into held",
                        key, len(users_overflow))
        return carried or None
    except Exception:
        logger.warning("queue_mgmt before_scope_clear failed for %s (%s); clearing without carry",
                       key, reason, exc_info=True)
    return None


def classify(event: Any) -> QueueOp:
    """Parse ``/queue <args>``. A media-carrying event with no text is a prompt, not a list (I6b)."""
    args = event.get_command_args().strip()
    if not args and getattr(event, "media_urls", None):
        return QueueOp("add", text="")
    return parse_queue_payload(args)
