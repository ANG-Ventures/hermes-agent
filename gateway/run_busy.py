"""Busy-session queueing, slot claims, slash dispatch tables and destructive-slash confirmation
for GatewayRunner (mixin bound via the MRO).

``gateway.run`` internals are imported lazily inside method bodies (import cycle), so
``patch("gateway.run.X")`` keeps intercepting them at call time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import asyncio
import contextlib
import json
import os
import threading
import time
from agent.i18n import DEFAULT_LANGUAGE, t
from agent.session_activity import format_iteration_progress
from gateway.config import Platform
from gateway.platforms.base import EphemeralReply
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.whatsapp_identity import canonical_whatsapp_identifier
from typing import Any, Dict, List, Optional, Tuple, Union

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401
    from gateway.run_turn_runner import TurnRunner  # noqa: F401


def approval_input_words(input_key: str) -> Tuple[str, ...]:
    """Typed-reply synonyms for ``approval.inputs.<input_key>`` (comma-lists): the English words
    ALWAYS match, plus the active language's list, so a Polish pack can add "tak" without losing
    "yes". Lower-cased, de-duplicated, order preserved (English first)."""
    seen: Dict[str, None] = {}
    key = f"approval.inputs.{input_key}"
    for raw in (t(key, lang=DEFAULT_LANGUAGE), t(key)):
        if raw == key:  # catalog miss: t() echoes the key
            continue
        for word in raw.split(","):
            word = word.strip().lower()
            if word:
                seen.setdefault(word, None)
    return tuple(seen)


# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

# Metadata flag ``_inject_watch_notification`` sets on watch-match / heartbeat / watch-state wakes:
# the lowest-priority internal events, deferred or evicted before a kanban wake or user message.
WATCH_NOTIFICATION_META = "watch_notification"
_WATCH_NOTIFICATION_TYPES = frozenset({
    "watch_match", "watch_disabled", "watch_overflow_tripped", "watch_overflow_released", "heartbeat"})


def _is_watch_notification_event(event: Any) -> bool:
    return bool(getattr(event, "internal", False)) and bool(
        (getattr(event, "metadata", None) or {}).get(WATCH_NOTIFICATION_META))


def _watch_content(event: Any) -> tuple:
    return (getattr(event, "text", None), tuple(getattr(event, "media_urls", None) or ()))

# Agent attributes for steer provenance. ``_STEER_ENTRIES_ATTR`` holds one ``(text, internal)`` entry per
# gateway steer accepted into the agent's CURRENT steer buffer. It lives on the agent because the buffer
# does: a recursive follow-up reuses the cached agent while the session slot may still hold the previous
# turn's context (Prism e08366573530). Every drain of the buffer MOVES those entries into
# ``_STEER_DRAINED_ATTR`` as a ``(drained_text, entries)`` snapshot, so a leftover is classified from the
# entries it was built from, never from the live buffer, which a later steer may already have refilled
# (Prism 83626dd6bc5a, 96e62751d3d9, t_506536db r5). One per-agent lock serialises steer+record against
# the drain, so no entry can land on the wrong side of a drain.
_STEER_ENTRIES_ATTR = "_gateway_steer_entries"
_STEER_DRAINED_ATTR = "_gateway_steer_drained"
_STEER_LOCK_ATTR = "_gateway_steer_lock"
_STEER_ENTRIES_MAX = 64
# The longest a mid-turn image's vision description may hold the session's steer order (t_086da8be).
# Later follow-ups wait behind it, so this bounds how long a user's next message is held. On expiry
# the image is queued for the next turn (vision runs again there) and later follow-ups queue behind
# it. One vision attempt alone may take auxiliary.vision.timeout (120 s default), plus retries.
_BUSY_STEER_IMAGE_DEADLINE_S = 45.0
# The agent-side hook ``steer_user_row`` calls with the drained text (t_b8fbfc54); keep the name in step with
# ``agent.prompt_builder.STEER_ROW_DISPLAY_METADATA_ATTR``.
STEER_ROW_DISPLAY_METADATA_ATTR = "_steer_row_display_metadata"
_STEER_INSTALL_LOCK = threading.Lock()
# Set while steer_with_provenance runs its verb: the wrapped ``steer`` then records nothing itself.
_RECORDING = threading.local()


def _steer_buffer_empty(agent: Any) -> bool:
    """True when *agent*'s steer buffer holds nothing, i.e. every earlier entry was drained or wiped."""
    return not getattr(agent, "_pending_steer", None)


def _provenance_lock(agent: Any) -> Optional[threading.Lock]:
    """The agent's provenance lock, installing the drain snapshot wrapper on first use; None when the
    agent has no instance dict or no drain to wrap (its leftovers then always read human)."""
    try:
        attrs = agent.__dict__
    except AttributeError:
        return None
    lock = attrs.get(_STEER_LOCK_ATTR)
    if lock is not None:
        return lock
    with _STEER_INSTALL_LOCK:
        lock = attrs.get(_STEER_LOCK_ATTR)
        if lock is not None:
            return lock
        orig_drain = getattr(agent, "_drain_pending_steer", None)
        if not callable(orig_drain):
            return None
        lock = threading.Lock()

        def _drain_with_snapshot(_orig=orig_drain, _attrs=attrs, _lock=lock):
            with _lock:
                text = _orig()
                if text:
                    entries = _attrs.pop(_STEER_ENTRIES_ATTR, None) or []
                    drained = _attrs.setdefault(_STEER_DRAINED_ATTR, [])
                    drained.append((text, entries))
                    del drained[:-_STEER_ENTRIES_MAX]
            return text

        orig_steer = getattr(agent, "steer", None)

        def _steer_recorded(text, _orig=orig_steer, _attrs=attrs, _lock=lock):
            """Any producer that bypasses :func:`steer_with_provenance` (kanban comment-inject, the
            redirect re-steer, ``api_server``) is recorded as a HUMAN entry, so a stale internal
            entry can never cover its text (t_3572d4df (c))."""
            if getattr(_RECORDING, "active", False):
                return _orig(text)  # steer_with_provenance holds the lock and records the entry itself
            with _lock:
                fresh = _steer_buffer_empty(agent)
                accepted = _orig(text)
                if accepted:
                    _record_steer_entry(agent, text, internal=False, fresh=fresh)
            return accepted

        prev_requeue = attrs.get("_steer_requeue_sink")

        def _requeue_with_snapshot(text, put_back, _prev=prev_requeue, _attrs=attrs, _lock=lock):
            """A drained batch put back into the buffer carries its snapshot entries back with it, so
            the next drain classifies it like the first one would have (Prism 96e62751d3d9)."""
            with _lock:
                fresh = _steer_buffer_empty(agent)
                _prev(text, put_back) if callable(_prev) else put_back()
                drained = _attrs.get(_STEER_DRAINED_ATTR) or []
                restored = drained.pop()[1] if drained and drained[-1][0] == text else []
                if fresh or not isinstance(_attrs.get(_STEER_ENTRIES_ATTR), list):
                    _attrs[_STEER_ENTRIES_ATTR] = []
                _attrs[_STEER_ENTRIES_ATTR].extend(restored)

        def _wipe_with_entries(wipe, _attrs=attrs, _lock=lock):
            """A hard cancel wipes the buffer without a drain: drop the entries that described it
            (Prism 7c35492cc023)."""
            with _lock:
                wipe()
                _attrs.pop(_STEER_ENTRIES_ATTR, None)

        attrs["_drain_pending_steer"] = _drain_with_snapshot
        if callable(orig_steer):
            attrs["steer"] = _steer_recorded
        attrs["_steer_requeue_sink"] = _requeue_with_snapshot
        attrs["_steer_wipe_sink"] = _wipe_with_entries
        attrs[_STEER_LOCK_ATTR] = lock

        def _row_display_metadata(text, _attrs=attrs, _lock=lock):
            """The agent's steer row for drained *text* asks here for its ``display_metadata``
            (``agent.prompt_builder.steer_user_row``): the inbound record of the one message it is,
            or a merged record (t_b8fbfc54)."""
            return _drained_display_metadata(_attrs, _lock, text)

        attrs[STEER_ROW_DISPLAY_METADATA_ATTR] = _row_display_metadata
        return lock


def steer_with_provenance(agent: Any, verb: Any, text: str, *, internal: bool, event: Any) -> bool:
    """``verb(text)`` (the agent's ``steer``/``redirect``) and, when accepted, record the entry with *event*'s
    inbound record (``None`` for a steer with no platform message), which the steer row then carries."""
    from gateway import inbound_record
    record = inbound_record.display_metadata(event).get(inbound_record.KEY) if event is not None else None
    lock = _provenance_lock(agent) if agent is not None else None
    with lock if lock is not None else contextlib.nullcontext():
        fresh = _steer_buffer_empty(agent)
        _RECORDING.active = True
        try:
            accepted = bool(verb(text))
        finally:
            _RECORDING.active = False
        if accepted and lock is not None:
            _record_steer_entry(agent, text, internal=internal, fresh=fresh, record=record)
    return accepted


def _drained_display_metadata(attrs: dict, lock: Any, text: Any) -> dict:
    """``inbound_record.steer_display_metadata`` of the newest drain snapshot whose text is *text*; ``{}``
    when none is (the text was not drained through the gateway wrapper)."""
    from gateway import inbound_record
    with lock:
        for drained_text, entries in reversed(list(attrs.get(_STEER_DRAINED_ATTR) or ())):
            if drained_text == text:
                return inbound_record.steer_display_metadata(drained_text, entries)
    return {}


def leftover_steer_display_metadata(agent: Any, leftover: Optional[str]) -> dict:
    """The record a leftover steer (``result["pending_steer"]``) carries into the next user turn. Read it
    BEFORE :func:`leftover_steer_is_internal`, which retires the snapshots (t_b8fbfc54)."""
    lock = getattr(agent, "__dict__", {}).get(_STEER_LOCK_ATTR) if agent is not None else None
    if lock is None or not leftover:
        return {}
    return _drained_display_metadata(agent.__dict__, lock, leftover)


def _record_steer_entry(agent: Any, text: str, *, internal: bool, fresh: bool, record: Any = None) -> None:
    cleaned = (text or "").strip()
    if not cleaned:
        return
    attrs = agent.__dict__
    if fresh or not isinstance(attrs.get(_STEER_ENTRIES_ATTR), list):
        attrs[_STEER_ENTRIES_ATTR] = []  # a buffer wiped without a drain (hard cancel) leaves stale entries
    ledger = attrs[_STEER_ENTRIES_ATTR]
    ledger.append((cleaned, bool(internal), record if isinstance(record, dict) else None))
    del ledger[:-_STEER_ENTRIES_MAX]


def leftover_steer_is_internal(agent: Any, leftover: Optional[str]) -> bool:
    """True only when *leftover* is exactly the last text drained from *agent*'s steer buffer AND that
    drain's snapshot is built entirely from internal steer entries.

    Retires every drain snapshot of the turn whatever the verdict. Live entries are kept while the buffer
    holds text (steers still buffered for the next turn) and retired once it is empty. False when no
    snapshot matches the leftover, when any entry is human, when an entry is missing from the leftover, or when the leftover holds text no
    entry accounts for (a steer that bypassed the gateway): the failure direction is a visible turn, never
    a silenced one."""
    lock = getattr(agent, "__dict__", {}).get(_STEER_LOCK_ATTR) if agent is not None else None
    if lock is None:
        return False
    with lock:
        drained = agent.__dict__.pop(_STEER_DRAINED_ATTR, None)
        if _steer_buffer_empty(agent):
            # Nothing is buffered, so no live entry describes anything: a buffer emptied without a
            # drain (direct wipe) must not hand its entries to the next steer (Prism 43b1ce3c30ef).
            agent.__dict__.pop(_STEER_ENTRIES_ATTR, None)
    remainder = (leftover or "").strip()
    if not remainder or not drained:
        return False
    text, entries = drained[-1]
    if (text or "").strip() != remainder or not entries or not all(entry[1] for entry in entries):
        return False
    for entry_text, *_ in entries:
        if entry_text not in remainder:
            return False
        remainder = remainder.replace(entry_text, "", 1)
    return not remainder.strip()


def _strip_slot(text: str, slot: str) -> Optional[str]:
    """Remainder after ``slot`` when ``text`` starts with it as a WHOLE slot, else None.

    The ``:``-delimited slot layout is ``build_session_key``'s: an id that merely starts with another
    must never match, so a whole-slot comparison is what every caller here uses. A text that IS the
    slot yields ``""``."""
    if text == slot:
        return ""
    if text.startswith(slot + ":"):
        return text[len(slot) + 1:]
    return None


def _tail_has_slot(tail: str, slot: str) -> bool:
    """True when ``tail``'s FIRST slot is ``slot`` (``tail`` is ``""`` when the key ends at the
    chat id)."""
    return _strip_slot(tail, slot) is not None


def _same_chat_key_slots(
    key: str, *, prefix: str, chat_id: str, scope_id: Optional[str],
) -> Optional[Tuple[str, str]]:
    """``(chat_type, tail)`` when ``key`` names the SAME chat as ``prefix`` + ``chat_id``, else None.

    ``prefix`` is the key's fixed-shape head, ``agent:<profile>:<platform>:``. Everything after it is
    matched as TEXT, because ids may themselves contain ``:`` (Matrix ``!room:example.org``).
    ``scope_id`` is Slack's workspace slot — ``build_session_key`` emits it there alone — and a key
    without it still names the same chat; a key carrying a DIFFERENT known scope is another
    workspace's chat. ``tail`` is ``""`` when the key ends at the chat id.
    """
    if not key.startswith(prefix):
        return None
    chat_type, _, rem = key[len(prefix):].partition(":")
    candidates = (f"{scope_id}:{chat_id}", chat_id) if scope_id else (chat_id,)
    for candidate in candidates:
        tail = _strip_slot(rem, candidate)
        if tail is not None:
            return chat_type, tail
    return None


class GatewayBusySessionMixin:
    """Busy-session queueing, slot claims, slash dispatch tables, destructive-slash confirmation."""

    def _queue_during_drain_enabled(self, busy_input_mode: Optional[str] = None) -> bool:
        # "queue"/"steer" mean messages survive a restart (queued for the new process); "interrupt" drops.
        mode = busy_input_mode or self._busy_input_mode
        return self._restart_requested and mode in {"queue", "steer"}

    def _overflow_queue(self, session_key: str):
        """The session's FIFO overflow list, or None when no session state exists yet."""
        state = self._peek_session_state(session_key)
        return state.conversation.queued_events if state else None

    def _enqueue_fifo(self, session_key: str, queued_event: "MessageEvent", adapter: Any) -> None:
        """Append a /queue event to the FIFO chain for a session."""
        pending_slot = getattr(adapter, "_pending_messages", None) if adapter is not None else None
        if pending_slot is None:
            return
        if session_key in pending_slot:
            self._session_state(session_key).conversation.queued_events.append(queued_event)
        else:
            overflow = self._session_state(session_key).conversation.queued_events
            if overflow:
                # Slot empty, older items still in overflow (a marked head was just popped to run):
                # the oldest becomes item 1 again so the new one cannot jump it (Prism e40b259b1e29).
                pending_slot[session_key] = overflow.pop(0)
                overflow.append(queued_event)
            else:
                pending_slot[session_key] = queued_event
        queued_event._gateway_accepted = True

    def _promote_queued_event(
        self, session_key: str, adapter: Any, pending_event: Optional["MessageEvent"]
    ) -> Optional["MessageEvent"]:
        """Promote the next overflow item after the slot drained.

        ``pending_event`` None → the overflow head becomes the pending event; otherwise the head is
        staged into the slot for the NEXT recursion. Returns the (possibly updated) pending_event.
        """
        overflow = self._overflow_queue(session_key)
        if not overflow:
            return pending_event
        if pending_event is None:
            return overflow.pop(0)
        if adapter is not None and hasattr(adapter, "_pending_messages"):
            adapter._pending_messages[session_key] = overflow.pop(0)
        # else: no adapter — leave the head in place so we don't silently drop it.
        return pending_event

    def _queue_depth(self, session_key: str, *, adapter: Any = None) -> int:
        """Total pending /queue items for a session — slot + overflow."""
        depth = len(self._overflow_queue(session_key) or ())
        if adapter is not None and session_key in getattr(adapter, "_pending_messages", {}):
            depth += 1
        return depth

    def _has_queued_human_followup(self, session_key: str, adapter: Any) -> bool:
        """True when the pending slot or the overflow queue holds a message from a person.

        A busy steer or redirect must not overtake such a message: it would reach the running turn
        before the earlier queued one (t_086da8be: a voice note steered in ahead of a queued
        image-only message). Internal wakes (kanban, cron, delegation completions) do not count.
        """
        head = (getattr(adapter, "_pending_messages", None) or {}).get(session_key)
        queued = [head, *(self._overflow_queue(session_key) or ())]
        return any(item is not None and not getattr(item, "internal", False) for item in queued)

    def _session_has_queued_event(self, session_key: str, adapter: Any) -> bool:
        """True when anything (a person's message or an internal wake) is queued for the session."""
        if (getattr(adapter, "_pending_messages", None) or {}).get(session_key) is not None:
            return True
        return bool(self._overflow_queue(session_key))

    @staticmethod
    def _log_busy_followup_held(session_key: str, verb: str) -> None:
        logger.info(
            "PRIORITY %s held behind queued follow-up session=%s: an earlier message from a person "
            "is still queued, so this one queues behind it to keep arrival order", verb, session_key,
        )

    def _rescue_orphaned_overflow(self, session_key: str, adapter: Any) -> Optional["MessageEvent"]:
        """Pop the oldest orphaned FIFO overflow event for an idle session (None if nothing to rescue).

        ``queued_events`` drains only at the post-turn promotion site; a busy window ending without
        it (early exit, exception/interrupt/generation-bump) orphans the overflow. On a NEW event for
        a NON-busy session the oldest orphan runs as THIS turn, the next is staged into the slot so
        arrival order holds, and the caller enqueues the incoming event behind it. The returned
        event is REMOVED from both stores, else the post-turn dequeue would run it twice.

        See #28503.
        """
        try:
            overflow = self._overflow_queue(session_key)
            if not overflow:
                return None
            pending_slot = getattr(adapter, "_pending_messages", None)
            if not isinstance(pending_slot, dict) or pending_slot.get(session_key):
                return None  # slot occupied (busy) or no slot storage — promotion owns this
            head = overflow.pop(0)
            # Keep the slot occupied so the drain promotes in order and a mid-chain arrival routes
            # to overflow instead of jumping the queue (same invariant as _promote_queued_event).
            if overflow:
                pending_slot[session_key] = overflow.pop(0)
            logger.warning(
                "Rescued orphaned FIFO overflow event for idle session "
                "%s — it was queued during a busy window but the post-turn "
                "drain never promoted it (#99882)", session_key,
            )
            if overflow:
                logger.warning(
                    "%d overflow event(s) still queued for session %s after "
                    "rescue staging (will drain via normal promotion)", len(overflow), session_key,
                )
            return head
        except Exception:
            logger.debug("FIFO overflow rescue failed for %s", session_key, exc_info=True)
            return None

    @staticmethod
    def _is_goal_continuation_event(event_or_text: Any) -> bool:
        """True for synthetic /goal turns, continuations and idle check-ins (so pause/clear can spare
        real /queue items and drop a check-in queued behind the command, Prism eb13daf988ed)."""
        from hermes_cli.goals import is_goal_synthetic_text
        return is_goal_synthetic_text(getattr(event_or_text, "text", event_or_text))

    def _clear_goal_pending_continuations(self, session_key: str, adapter: Any) -> int:
        """Remove queued synthetic /goal continuations for one session; real /queue items are kept."""
        removed = 0
        pending_slot = getattr(adapter, "_pending_messages", None) if adapter is not None else None
        if isinstance(pending_slot, dict):
            pending_event = pending_slot.get(session_key)
            if self._is_goal_continuation_event(pending_event):
                pending_slot.pop(session_key, None)
                removed += 1

        overflow = self._overflow_queue(session_key)
        if overflow:
            kept = [e for e in overflow if not self._is_goal_continuation_event(e)]
            removed += len(overflow) - len(kept)
            self._peek_session_state(session_key).conversation.queued_events = kept
        return removed

    def _goal_still_active_for_session(self, session_id: str) -> bool:
        """Best-effort fresh DB check before running a queued continuation."""
        if not session_id:
            return False
        try:
            from hermes_cli.goals import GoalManager
            return GoalManager(session_id=session_id).is_active()
        except Exception as exc:
            logger.debug("goal continuation: active-state recheck failed: %s", exc)
            return False

    def _get_max_concurrent_sessions(self) -> Optional[int]:
        """Return the configured active chat session cap, if enabled."""
        try:
            from hermes_cli.active_sessions import resolve_max_concurrent_sessions
            return resolve_max_concurrent_sessions(getattr(self, "config", None))
        except Exception:
            return None

    def _active_session_limit_message(self, session_key: str) -> Optional[str]:
        """Return a user-facing rejection when starting a new session exceeds the cap."""
        max_sessions = self._get_max_concurrent_sessions()
        if max_sessions is None or self._is_session_running(session_key):
            return None
        active_count = self._running_agent_count()
        if active_count < max_sessions:
            return None
        from hermes_cli.active_sessions import active_session_limit_message
        return active_session_limit_message(active_count, max_sessions)

    def _claim_active_session_slot(
        self, session_key: str, source: SessionSource
    ) -> tuple[Any, Optional[str]]:
        """Claim a cross-process active-session slot for a new gateway turn."""
        # Local admission is checked on the loop before reserving the pending slot.
        # This method runs wholly in a worker: never inspect mutable runner state here.
        try:
            from hermes_cli.active_sessions import try_acquire_active_session
            platform = source.platform.value if source and source.platform else "gateway"
            return try_acquire_active_session(
                session_id=session_key,
                surface=f"gateway:{platform}",
                config=getattr(self, "config", None),
                metadata={
                    "platform": platform,
                    "chat_id": getattr(source, "chat_id", "") or "",
                    "user_id": getattr(source, "user_id", "") or "",
                    # Writer identity: a leaked lease from this process is re-acquired by the next
                    # turn rather than fencing it out forever (pruning only reclaims dead PROCESSES).
                    # Writer identity for re-entrancy (#94595): if this process leaks a lease for this
                    # session (exception path skipped release), the next turn re-acquires its own entry
                    # instead of being fenced out of it forever — pruning only reclaims entries whose
                    # PROCESS died.
                    "live_session_id": str(session_key),
                },
            )
        except Exception as exc:
            logger.warning("Failed to claim active session slot: %s", exc)
            return None, None

    async def _claim_active_session_slot_off_loop(self, session_key, source):
        # Serialize displaced generations too: an older queued worker must never
        # re-enter the registry after a successor has acquired its lease.
        locks = getattr(self, "_active_session_claim_locks", None)
        if locks is None:
            locks = self._active_session_claim_locks = {}
        lock = locks.setdefault(session_key, asyncio.Lock())
        pending = getattr(self, "_active_session_claim_tasks", None)
        if pending is None:
            pending = self._active_session_claim_tasks = {}
        owner = asyncio.current_task()
        pending[owner] = session_key
        try:
            async with lock:
                return await self._await_active_session_claim(session_key, source)
        finally:
            pending.pop(owner, None)
            # Count users explicitly rather than relying on Lock's private waiters.
            if session_key not in pending.values():
                locks.pop(session_key, None)

    async def _active_session_io(self, fn, *args):
        # The registry already serializes every write at flock. Queue on the
        # loop instead of consuming a shared executor thread per lock waiter.
        lock = getattr(self, "_active_session_io_lock", None)
        if lock is None:
            lock = self._active_session_io_lock = asyncio.Lock()
        async with lock:
            return await asyncio.to_thread(fn, *args)

    async def _await_active_session_claim(self, session_key, source):
        """Keep ownership until a cancelled worker has returned its exact lease.

        Cancelling an executor await does not stop its thread. Shield and join it,
        including repeated cancellation, before unwinding the pending generation.
        """
        worker = asyncio.create_task(self._active_session_io(
            self._claim_active_session_slot, session_key, source,
        ))
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
        lease, message = worker.result()
        if cancelled:
            if lease is not None:
                cleanup = self._release_active_session_lease(lease)
                if cleanup is not None:
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            pass
                    cleanup.result()
            raise asyncio.CancelledError
        return lease, message

    def _release_active_session_lease(self, lease):
        """Detach the exact lease from turn state before scheduling durable I/O."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            lease.release()
            return None
        tasks = getattr(self, "_active_session_release_tasks", None)
        if tasks is None:
            tasks = self._active_session_release_tasks = set()
        existing = getattr(lease, "_gateway_release_task", None)
        if isinstance(existing, asyncio.Task):
            return existing
        async def release():
            try:
                await self._active_session_io(lease.release)
            except Exception:
                # Cleanup must not replace a completed reply, the turn's error,
                # or cancellation. Keep the durable failure visible in the log.
                logger.error("Failed to release active session slot", exc_info=True)

        task = loop.create_task(release())
        lease._gateway_release_task = task
        tasks.add(task)

        def finished(done):
            tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.error("Failed to release active session slot", exc_info=done.exception())

        task.add_done_callback(finished)
        return task

    async def _wait_active_session_release(self, task):
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _preserve_pending_session_claims(self):
        """Stop unstarted turns and spool their input before waiting for registry I/O."""
        snapshots = getattr(self, "_pending_session_claim_inputs", None)
        if snapshots is None:
            snapshots = self._pending_session_claim_inputs = {}
        for task, key in tuple(getattr(self, "_active_session_claim_tasks", {}).items()):
            state = self._peek_session_state(key)
            if (getattr(self, "_running_agent_tasks", {}).get(key) is task
                    and state is not None and state.turn.event is not None
                    and not getattr(task, "_claim_input_spooled", False)):
                # Retain independently of the owner's finally, which clears turn
                # state even when the first spool attempt fails or is cancelled.
                snapshots.setdefault(task, (key, state.turn.event))
            task.cancel()
        for task, (key, event) in tuple(snapshots.items()):
            if await self._preserve_followup_across_restart(key, event, None):
                task._claim_input_spooled = True
                snapshots.pop(task, None)
            elif not getattr(task, "_claim_input_spool_warned", False):
                task._claim_input_spool_warned = True
                logger.error(
                    "Pending active-session input for %s retained for shutdown retry; "
                    "not durable and may be lost if storage remains unavailable", key,
                )

    async def _drain_active_session_releases(self):
        """Do not cancel lease cleanup with ordinary background work at shutdown."""
        while tasks := tuple(getattr(self, "_active_session_release_tasks", ())):
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))

    @staticmethod
    def _agent_has_active_subagents(running_agent: Any) -> bool:
        """True when *running_agent* is driving subagents (callers demote interrupt → queue;
        ``interrupt()`` would cascade through ``_active_children``). Fail-safe False on any error."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        if running_agent is None or running_agent is _AGENT_PENDING_SENTINEL:
            return False
        children = getattr(running_agent, "_active_children", None)
        # Real collections only — a ``MagicMock()._active_children`` auto-attr must not demote.
        if not isinstance(children, (list, tuple, set)) or not children:
            return False
        lock = getattr(running_agent, "_active_children_lock", None)
        try:
            with lock if lock is not None else contextlib.nullcontext():
                return bool(children)
        except Exception:
            return False

    @staticmethod
    def _steer_active_subagents(running_agent: Any, text: str) -> int:
        """Queue *text* into every live child of *running_agent*; returns how many accepted it.

        A parent blocked inside ``delegate_task`` only drains its own steer queue after the tool
        returns, i.e. after the child finishes — so a steer aimed at a looping child would sit
        unread for the whole delegation (#112095, Telegram). Children are the parent's own
        ``_active_children`` (identity-scoped, same snapshot ``interrupt()`` fans out to)."""
        children = getattr(running_agent, "_active_children", None)
        if not isinstance(children, (list, tuple, set)) or not children:
            return 0
        lock = getattr(running_agent, "_active_children_lock", None)
        try:
            with lock if lock is not None else contextlib.nullcontext():
                snapshot = list(children)
        except Exception:
            return 0
        accepted = 0
        for child in snapshot:
            steer = getattr(child, "steer", None)
            if not callable(steer):
                continue
            try:
                accepted += bool(steer(text))
            except Exception as exc:
                logger.warning("Steer into subagent %r failed: %s", getattr(child, "_delegate_id", child), exc)
        return accepted

    def _steer_running_agent(self, running_agent: Any, text: str, *, event: Any, internal: bool = False) -> bool:
        """``running_agent.steer(text)`` plus fan-out to its active subagents (see
        :meth:`_steer_active_subagents`); True when the parent or any child queued it.

        Every accepted steer is recorded ON THE AGENT as a buffer entry with its provenance, so it
        follows the text to whichever turn drains it (see :func:`leftover_steer_is_internal`), never to
        the context the slot held at steer time (Prism e08366573530, t_506536db). *event* is the platform
        message being steered (its inbound record rides the entry to the steer row, t_b8fbfc54); pass
        ``event=None`` only for a steer no platform message produced."""
        accepted = steer_with_provenance(running_agent, running_agent.steer, text, internal=internal, event=event)
        return bool(self._steer_active_subagents(running_agent, text)) or accepted

    async def _session_has_compression_in_flight(self, session_key: str) -> bool:
        """True when a compression lock is held for this session's id (callers demote interrupt →
        queue, else a follow-up against the pre-rotation parent orphans compression siblings).
        Both blocking reads run in a worker thread so a large state.db never freezes the loop.

        Context compression is interrupt-protected (#23975) but gateway ``interrupt`` busy-input mode can
        still start a follow-up turn against the pre-rotation parent while compression is mid-flight,
        producing orphaned compression siblings (#56391).
        """
        session_store = getattr(self, "session_store", None)
        if not session_key or session_store is None:
            return False
        def _assume_active(what: str, ident) -> bool:
            logger.warning(
                "Compression in-flight check failed while reading %s %s; treating compression as "
                "active to avoid interrupting a possible parent-session rotation", what, ident, exc_info=True,
            )
            return True

        try:
            session_id = await asyncio.to_thread(
                self._lookup_session_id_under_store_lock, session_store, session_key
            )
        except (AttributeError, TypeError):
            return False
        except Exception:
            return _assume_active("session", session_key)
        session_db = getattr(self, "_session_db", None)
        if not session_id or session_db is None:
            return False
        raw_db = getattr(session_db, "_db", session_db)
        try:
            holder = await asyncio.to_thread(raw_db.get_compression_lock_holder, str(session_id))
            # Production returns Optional[str]. Reject non-strings so a MagicMock auto-attr (or any
            # unexpected truthy) cannot look like a held lock and skip hygiene.
            # See #96953.
            return isinstance(holder, str) and bool(holder)
        except (AttributeError, TypeError):
            return False
        except Exception:
            return _assume_active("lock holder for session", session_id)

    @staticmethod
    def _lookup_session_id_under_store_lock(session_store, session_key: str):
        """Sync helper run in the thread pool: read session_id under the store lock."""
        # noqa: SLF001 — intentional private access; runs off the event loop.
        with session_store._lock:  # noqa: SLF001
            session_store._ensure_loaded_locked()  # noqa: SLF001
            entry = session_store._entries.get(session_key)  # noqa: SLF001
        return getattr(entry, "session_id", None) if entry is not None else None

    # Metadata that must match for two pending events to merge into one slot.
    _SECURITY_METADATA_KEYS = (
        "hermes_plugin_id", "hermes_plugin_injection", "gateway_session_key",
        "gateway_session_id", "gateway_session_strict",
        "notification_category",
    )

    def _queue_or_replace_pending_event(self, session_key: str, event: MessageEvent) -> None:
        from gateway.platforms.base import merge_pending_message_event
        adapter = self._delivery_adapter_for(event.source)
        if not adapter:
            return
        # FIFO so each follow-up gets its own turn in arrival order (the single pending slot used to
        # be silently OVERWRITTEN). Photo bursts still merge into the head slot (album semantics).
        pending_slot = getattr(adapter, "_pending_messages", None)
        # #28503 — Previously this called ``merge_pending_message_event`` with the default
        # ``merge_text=False``, which silently OVERWROTE the single pending slot when consecutive text
        # messages arrived in ``busy_input_mode: queue``.
        existing = pending_slot.get(session_key) if isinstance(pending_slot, dict) else None
        same_security_context = existing is not None and (
            getattr(existing, "internal", False) == getattr(event, "internal", False)
            and getattr(existing, "allow_gateway_control", True)
            == getattr(event, "allow_gateway_control", True)
            and all(
                (getattr(existing, "metadata", None) or {}).get(key)
                == (getattr(event, "metadata", None) or {}).get(key)
                for key in self._SECURITY_METADATA_KEYS
            )
        )
        # Only a photo burst (PHOTO on either side, the other side TEXT or PHOTO) merges into the
        # head slot. Every other media follow-up — voice, audio, video, document — is an
        # independent message and takes its own FIFO turn like text does; merging on *any*
        # ``media_urls`` collapsed three voice notes into one turn (#114363). Telegram albums
        # (``media_group_id``, photos and videos) are already coalesced by the adapter upstream.
        merge_types = {
            getattr(existing, "message_type", None),
            getattr(event, "message_type", None),
        }
        # Merge only while the head is the newest queued message: with anything waiting in the
        # overflow behind it, the merge would move this message ahead of those (t_3ee12123).
        # At the queue cap a new slot would be refused: merging out of order beats dropping it.
        # The helper merges a TEXT/PHOTO pair only when a photo carries media; otherwise (a photo
        # whose download failed) it REPLACES the head, so that pair takes its own slot (Prism
        # bad37f463754).
        if (
            same_security_context
            and MessageType.PHOTO in merge_types
            and merge_types <= {MessageType.TEXT, MessageType.PHOTO}
            and (merge_types == {MessageType.PHOTO}
                 or getattr(existing, "media_urls", None) or getattr(event, "media_urls", None))
            and (not self._overflow_queue(session_key)
                 or self._queue_depth(session_key, adapter=adapter) >= self._BUSY_QUEUE_MAX_PENDING)
        ):
            merge_pending_message_event(
                adapter._pending_messages, session_key, event,
                merge_text=event.message_type == MessageType.TEXT,
            )
            event._gateway_accepted = True
            return

        if self._coalesce_card_wake(session_key, event, adapter):
            return

        depth = self._queue_depth(session_key, adapter=adapter)
        refused = self.__dict__.setdefault("_wake_refusal_episodes", set())
        if _is_watch_notification_event(event):
            # Watch/heartbeat notices never take the last slots: kanban wakes and user messages own
            # them. Not accepted -> the watch drain defers and folds the event (t_469a7ef8).
            if (depth >= self._BUSY_QUEUE_MAX_PENDING - self._BUSY_QUEUE_RESERVED_SLOTS
                    or self._queued_watch_notification_count(session_key, adapter) >= self._BUSY_QUEUE_MAX_WATCH):
                logger.info("Deferring watch notification for busy session %s — queue depth %d.", session_key, depth)
                return
        elif depth >= self._BUSY_QUEUE_MAX_PENDING and not self._evict_oldest_watch_notification(session_key, adapter):
            logger.warning(
                "Dropping busy-mode follow-up for session %s — pending queue at cap (%d).",
                session_key, self._BUSY_QUEUE_MAX_PENDING,
            )
            # A refused wake raises WakeNotAccepted at its sender; file it ONCE per episode so a
            # session that cannot take wakes shows as that, not as a failing cron (t_4a891379).
            if getattr(event, "internal", False) and session_key not in refused:
                refused.add(session_key)
                from gateway.wake import record_wake_refusal
                record_wake_refusal(session_key, depth, self._BUSY_QUEUE_MAX_PENDING, event)
            return
        refused.discard(session_key)   # below the cap: the refusal episode (if any) is over

        if _is_watch_notification_event(event):
            event._watch_pristine = _watch_content(event)   # what the notice held when queued
        self._enqueue_fifo(session_key, event, adapter)

    def _coalesce_card_wake(self, session_key: str, event: MessageEvent, adapter: Any) -> bool:
        """A kanban/boomerang wake for a card that already has a wake waiting in this session's
        queue REPLACES it in place: the newest text wins, with a note of how many earlier notices it
        folded. A card never holds more than one slot (t_4a891379: ~33 wakes filled the cap of 32).
        User messages and every other wake kind are never touched."""
        from gateway.wake_kind import CARD_ID_KEY, KANBAN
        meta = getattr(event, "metadata", None) or {}
        card = meta.get(CARD_ID_KEY)
        if not (getattr(event, "internal", False) and getattr(event, "wake_kind", None) == KANBAN and card):
            return False
        pending_slot = getattr(adapter, "_pending_messages", None)
        overflow = self._overflow_queue(session_key)
        held = [(pending_slot, session_key)] if isinstance(pending_slot, dict) and session_key in pending_slot else []
        held += [(overflow, i) for i in range(len(overflow or ()))]
        for store, idx in held:
            old = store[idx]
            old_meta = getattr(old, "metadata", None) or {}
            if not (getattr(old, "internal", False) and getattr(old, "wake_kind", None) == KANBAN
                    and old_meta.get(CARD_ID_KEY) == card
                    and getattr(old, "allow_gateway_control", True) == getattr(event, "allow_gateway_control", True)
                    and all(old_meta.get(k) == meta.get(k) for k in self._SECURITY_METADATA_KEYS)):
                continue
            folded = int(old_meta.get("wake_folded") or 0) + 1
            event.metadata = {**meta, "wake_folded": folded}
            event.text = (f"{event.text}\n\n[{folded} earlier notice{'s' if folded != 1 else ''} for this card "
                          "folded into this one]")
            store[idx] = event
            event._gateway_accepted = True
            logger.info("Coalesced kanban wake for %s into its queued slot in session %s (%d folded)",
                        card, session_key, folded)
            return True
        return False

    # Of _BUSY_QUEUE_MAX_PENDING, the last RESERVED slots never hold a watch notification, and at
    # most MAX_WATCH watch notifications are queued per session at once (t_469a7ef8).
    _BUSY_QUEUE_RESERVED_SLOTS = 8
    _BUSY_QUEUE_MAX_WATCH = 4

    def _queued_watch_notification_count(self, session_key: str, adapter: Any) -> int:
        head = (getattr(adapter, "_pending_messages", None) or {}).get(session_key)
        queued = [head, *(self._overflow_queue(session_key) or ())]
        return sum(1 for item in queued if item is not None and _is_watch_notification_event(item))

    def _evict_oldest_watch_notification(self, session_key: str, adapter: Any) -> bool:
        """Free one slot of a full queue by removing its oldest OVERFLOW watch notification, else a
        watch notice in the pending head slot that still holds only what it was queued with; False
        if neither. A head that absorbed a same-sender human follow-up (busy text debounce, photo
        merge) is never evicted, since that would drop the input (Prism cbbd6b0a5f80); overflow
        items are never merge targets."""
        overflow = self._overflow_queue(session_key)
        index = next((i for i, item in enumerate(overflow or ()) if _is_watch_notification_event(item)), None)
        if index is not None:
            overflow.pop(index)
        else:
            pending_slot = getattr(adapter, "_pending_messages", None)
            head = pending_slot.get(session_key) if isinstance(pending_slot, dict) else None
            if not (head is not None and _is_watch_notification_event(head)
                    and getattr(head, "_watch_pristine", None) == _watch_content(head)):
                return False
            if overflow:
                pending_slot[session_key] = overflow.pop(0)
            else:
                pending_slot.pop(session_key, None)
        logger.warning("Evicted oldest queued watch notification for session %s to admit a follow-up at cap (%d).",
                       session_key, self._BUSY_QUEUE_MAX_PENDING)
        return True

    async def _prepare_busy_steer_text(self, event: MessageEvent) -> str:
        """Steerable text for a busy follow-up, transcribing voice-message media first.

        Steer bypasses the inbound STT queue, so a media-only voice follow-up would otherwise
        silently degrade to queue mode. Uses the single out-of-band STT choke point, so STT runs at
        most once per message; on failure the caption (if any) is kept.
        """
        return (await self._busy_steer_text_and_stt_ok(event))[0]

    async def _busy_steer_text_and_stt_ok(self, event: MessageEvent) -> Tuple[str, bool]:
        """``_prepare_busy_steer_text`` plus whether EVERY voice clip was transcribed (True when
        there was no voice). Partial success reads False (Prism 404f63b197cd)."""
        text = (event.text or "").strip()
        audio_paths = self._pending_event_audio_paths(event)
        if not audio_paths:
            return text, True
        enriched_text, successful_transcripts = await self._transcribe_and_echo_pending_voice(
            event, self._delivery_adapter_for(event.source), event.source, text, log_context="Busy-steer"
        )
        if successful_transcripts:
            return (enriched_text or text).strip(), len(successful_transcripts) >= len(audio_paths)
        return text, False

    @staticmethod
    def _forget_cached_stt(event: MessageEvent) -> None:
        """Drop the STT result ``_transcribe_pending_audio_event_once`` cached on *event*, so the
        queued turn transcribes again instead of reusing a failure (Prism f095b8865489)."""
        for attr in ("_gateway_pending_stt_text", "_gateway_pending_stt_transcripts"):
            if hasattr(event, attr):
                delattr(event, attr)

    # -- Mid-turn steer of any follow-up a steer can carry: text, voice, images (t_086da8be) -----
    # Ace's ruling on #569: an image sent mid-turn interrupts the turn the way text does. A steer is
    # text, so an image is steered as the same block a queued image gets at turn start (vision
    # description + cached path, run_inbound._describe_images_as_text). Preparing that block takes
    # seconds, so steers are serialized per session in arrival order: a later follow-up waits for an
    # earlier one's preparation and can never land ahead of it.

    def _busy_steer_order_lock(self, session_key: str) -> asyncio.Lock:
        locks = getattr(self, "_busy_steer_order_locks", None)
        if locks is None:
            locks = {}
            self._busy_steer_order_locks = locks
        lock = locks.get(session_key)
        if lock is None:
            lock = locks[session_key] = asyncio.Lock()
        return lock

    def _drop_idle_busy_steer_order_lock(self, session_key: str, lock: asyncio.Lock) -> None:
        locks = getattr(self, "_busy_steer_order_locks", None) or {}
        if locks.get(session_key) is lock and not lock.locked() and not getattr(lock, "_waiters", None):
            locks.pop(session_key, None)

    def _busy_steer_image_paths(self, event: MessageEvent) -> Optional[List[str]]:
        """The image attachments of a follow-up, or None when it carries media a steer cannot carry
        (files, video, stickers, audio files): those keep queueing for the next turn."""
        from gateway.run import _event_media_is_image
        voice = set(self._pending_event_audio_paths(event))
        images: List[str] = []
        for i, path in enumerate(getattr(event, "media_urls", None) or []):
            if _event_media_is_image(event, i):
                images.append(path)
            elif path not in voice:
                return None
        return images

    async def _describe_busy_steer_images(self, event: MessageEvent, session_key: str) -> Optional[str]:
        """Vision description + cached path for each image of *event*, or None when an image was not
        downloaded (the adapter left a remote URL) or its description failed."""
        from gateway.run import _event_media_is_image
        from gateway.run_inbound import rehome_inbound_media
        async with self._async_profile_scope_for_source(event.source):
            rehome_inbound_media(event)   # same move as turn start: the routed profile's cache/
            paths = [p for i, p in enumerate(event.media_urls or []) if _event_media_is_image(event, i)]
            missing = [p for p in paths if not os.path.isfile(p)]
            if missing:
                logger.info("PRIORITY steer image not cached locally session=%s count=%d", session_key, len(missing))
                return None
            failures: List[str] = []
            block = await self._describe_images_as_text(
                event.source, session_key, "", paths, failures_out=failures,
            )
        if failures or not block:
            logger.info("PRIORITY steer image description failed session=%s count=%d", session_key, len(failures))
            return None
        return block

    async def _busy_steer_images_pinned_native(self, event: MessageEvent) -> bool:
        """True when the routed profile's config pins ``agent.image_input_mode: native`` (auto and
        text return False: auto pre-describes for a steer, since a steer cannot carry pixels)."""
        def _read() -> bool:
            try:
                from agent.image_routing import _coerce_mode, _dict_or_empty
                from hermes_cli.config import load_config
                agent_cfg = _dict_or_empty(_dict_or_empty(load_config()).get("agent"))
                return _coerce_mode(agent_cfg.get("image_input_mode")) == "native"
            except Exception:
                logger.debug("busy steer: image_input_mode read failed", exc_info=True)
                return False
        async with self._async_profile_scope_for_source(event.source):
            return await asyncio.to_thread(_read)

    async def _prepare_busy_steer_payload(self, event: MessageEvent, session_key: str) -> str:
        """The steer text for a mid-turn follow-up, or "" when it cannot be steered (caller queues).

        Plain text: the text. Voice: the transcript (#58780). Images, with or without a caption or a
        voice note: the turn-start image block, then the caption/transcript."""
        media = getattr(event, "media_urls", None) or []
        if not media:
            plain = event.message_type == MessageType.TEXT and not event.media_types
            return (event.text or "").strip() if plain else ""
        images = self._busy_steer_image_paths(event)
        if images is None:
            return ""
        if images and await self._busy_steer_images_pinned_native(event):
            # agent.image_input_mode: native is an explicit "attach pixels, never pre-describe with
            # auxiliary vision". A steer can only carry text, so the image waits for the next turn.
            logger.info("PRIORITY steer image queued: image_input_mode is native session=%s", session_key)
            return ""
        text, stt_ok = await self._busy_steer_text_and_stt_ok(event)
        if not images:
            return text   # all voice (pre-#569 behaviour, STT failure included)
        if not stt_ok:
            # Image + voice note whose STT failed: steering the picture alone would consume the
            # voice note untranscribed. Queue the whole message; its turn retries STT and vision.
            logger.info("PRIORITY steer image queued: its voice note was not transcribed session=%s", session_key)
            self._forget_cached_stt(event)
            return ""
        try:
            block = await asyncio.wait_for(
                self._describe_busy_steer_images(event, session_key), _BUSY_STEER_IMAGE_DEADLINE_S,
            )
        except asyncio.TimeoutError:
            logger.info(
                "PRIORITY steer image description exceeded %.0fs session=%s",
                _BUSY_STEER_IMAGE_DEADLINE_S, session_key,
            )
            block = None
        if block is None:
            return ""
        return self._prepend_media_prefix(block, text).strip()

    async def _steer_busy_followup(self, event: MessageEvent, session_key: str, running_agent: Any) -> str:
        """Steer *event* into the live turn, in arrival order → ``"steered"``, ``"held"`` (an earlier
        message from a person is queued, so this one must queue behind it) or ``"unsteerable"``.

        On ``"held"``/``"unsteerable"`` the caller queues the event synchronously (no await between
        the return and the queue call), so the next waiter sees it queued and holds behind it."""
        lock = self._busy_steer_order_lock(session_key)
        try:
            # Awaited = waited behind an earlier steer, or prepares an image's description (seconds,
            # up to the deadline): the turn may have ended meanwhile. A lone voice note keeps its
            # pre-#569 behaviour (STT then steer, no re-check).
            awaited = lock.locked()
            async with lock:
                if self._has_queued_human_followup(session_key, self._delivery_adapter_for(event.source)):
                    # Checked first: a held voice note or image is prepared when its own turn runs.
                    self._log_busy_followup_held(session_key, "steer")
                    return "held"
                awaited = awaited or bool(self._busy_steer_image_paths(event))
                steer_text = await self._prepare_busy_steer_payload(event, session_key)
                if steer_text and awaited and not self._busy_steer_turn_still_live(session_key, running_agent):
                    # The turn ended while this follow-up waited or was prepared. A steer now would
                    # sit in a finished agent's buffer (turn_finalizer already drained it) and
                    # surface inside some later turn; queue it as the next turn instead.
                    logger.info(
                        "PRIORITY steer target turn ended during preparation session=%s: queued "
                        "for the next turn", session_key,
                    )
                    return "unsteerable"
                steered = bool(steer_text) and self._try_agent_verb(
                    running_agent, "steer", steer_text, session_key, event=event
                )
                if steered:
                    self._fold_into_running_turn(running_agent, session_key, event)
                    return "steered"
                if self._busy_steer_image_paths(event):
                    logger.info(
                        "PRIORITY steer image queued for the next turn session=%s: later follow-ups "
                        "queue behind it to keep arrival order", session_key,
                    )
                return "unsteerable"
        finally:
            self._drop_idle_busy_steer_order_lock(session_key, lock)

    def _busy_steer_turn_still_live(self, session_key: str, running_agent: Any) -> bool:
        """True while *running_agent* still owns the session's current turn (the same test
        ``_fold_into_running_turn`` applies after a steer)."""
        state = self._peek_session_state(session_key)
        turn = state.turn if state is not None else None
        if turn is None or turn.agent is not running_agent:
            return False
        ctx = getattr(running_agent, "_gateway_receiving_turn_context", turn.ctx)
        still_current = getattr(ctx, "_run_still_current", None) if ctx is not None else None
        return still_current is None or bool(still_current())

    def _steer_text_with_origin(self, text: str, event: MessageEvent) -> str:
        """Keep event origin in this injection, never in the cached system prompt."""
        if not text.strip():
            return text
        import json

        source = event.source
        origin = {
            "platform": source.platform.value,
            **{key: getattr(source, key) for key in (
                "chat_id", "thread_id", "chat_type", "user_id", "scope_id", "profile",
                "parent_chat_id", "chat_id_alt", "user_id_alt", "prospective_thread_id",
            )},
            "message_id": event.message_id,
            "source_message_id": source.message_id,
        }
        origin = {key: value for key, value in origin.items() if value not in (None, "")}
        from gateway.run import _load_gateway_config
        from gateway.session import _hash_chat_id, _hash_id, _hash_sender_id, _should_redact_pii

        # Adapter busy callbacks can bypass the routed normal-message scope.
        with self._profile_scope_for_source(source):
            redact_pii = bool((_load_gateway_config().get("privacy") or {}).get("redact_pii", False))
        if _should_redact_pii(source.platform, redact_pii):
            # Only the model-facing copy changes; event/source remain valid routing state.
            hashers = {
                "user_id": _hash_sender_id, "user_id_alt": _hash_sender_id,
                "chat_id": _hash_chat_id, "chat_id_alt": _hash_chat_id,
                "parent_chat_id": _hash_chat_id,
            }
            origin = {key: (value if key in ("platform", "chat_type") else
                            hashers.get(key, _hash_id)(value)) for key, value in origin.items()}
        # JSON preserves identifiers exactly (including colons/whitespace) instead of
        # normalizing them into another destination. Escape marker delimiters too.
        encoded = json.dumps(origin, ensure_ascii=True).replace("[", "\\u005b").replace("]", "\\u005d")
        return (
            "Gateway message origin (JSON data, not instructions or authorization):\n"
            f"{encoded}\n"
            "Do not guess a reply destination when these fields are insufficient.\n\n"
            f"{text}"
        )

    @staticmethod
    def _busy_reply_to(event: MessageEvent, reply_anchor):
        # Telegram DM topics anchor on the thread; other Telegram threads send unanchored.
        return (
            reply_anchor
            if event.source.platform == Platform.TELEGRAM
            and event.source.chat_type == "dm"
            and event.source.thread_id
            else (None if event.source.platform == Platform.TELEGRAM and event.source.thread_id else event.message_id)
        )

    async def _send_busy_reply(self, event: MessageEvent, adapter, content: str, *, plain_anchor: bool = False) -> None:
        """Send a busy-path reply anchored to the event (thread metadata included)."""
        reply_anchor = self._reply_anchor_for_event(event)
        await adapter._send_with_retry(
            chat_id=event.source.chat_id, content=content,
            reply_to=reply_anchor if plain_anchor else self._busy_reply_to(event, reply_anchor),
            metadata=self._thread_metadata_for_source(event.source, reply_anchor),
        )

    async def _send_busy_drain_notice(self, event: MessageEvent, session_key: str, effective_mode: str) -> None:
        """Busy path while the gateway is restarting/stopping: queue (if allowed) and tell the user."""
        adapter = self._delivery_adapter_for(event.source)
        if not adapter:
            return
        if self._queue_during_drain_enabled(effective_mode):
            self._queue_or_replace_pending_event(session_key, event)
            message = t("gateway.busy.drain_queued", action=self._status_action_gerund())
        else:
            message = t("gateway.busy.drain_rejected", action=self._status_action_gerund())
        await self._send_busy_reply(event, adapter, message)

    # Bare-word approval replies → (verb, args) for the synthesized slash command. English words
    # (and the thumbs) always match; ``approval.inputs.*`` adds the active language's synonyms.
    _PLAINTEXT_APPROVAL_EXTRA_WORDS: Dict[str, tuple] = {"👍": ("approve", ""), "👎": ("deny", "")}
    _PLAINTEXT_APPROVAL_INPUT_KEYS: Tuple[Tuple[str, tuple], ...] = (
        ("approve", ("approve", "")), ("deny", ("deny", "")),
        ("always", ("approve", "always")), ("session", ("approve", "session")))

    def _plaintext_approval_words(self) -> Dict[str, tuple]:
        """Word → (verb, args) for the active language: the English list ∪ ``t("approval.inputs.<k>")``."""
        words: Dict[str, tuple] = dict(self._PLAINTEXT_APPROVAL_EXTRA_WORDS)
        for input_key, verb_args in self._PLAINTEXT_APPROVAL_INPUT_KEYS:
            for word in approval_input_words(input_key):
                words.setdefault(word, verb_args)
        return words

    async def _route_plaintext_approval_while_busy(self, event: MessageEvent, session_key: str) -> bool:
        """Route a bare "yes"/"no" to the approval handlers while a dangerous-command approval blocks.

        Returns True when the message was consumed as an approval response.
        """
        # A bare "yes" while blocked on a dangerous-command approval must reach the approval handler,
        # not queue behind a turn that can't start until it resolves (auto-deny deadlock). Gated on
        # has_blocking_approval so a conversational "yes" never fires a command.
        try:
            from tools.approval import has_blocking_approval
            # --- Approval response routing (#46866) --- When the agent is blocked waiting for a
            # dangerous-command approval, plain-text responses like "yes" or "approve" must be routed to the
            # approval handler instead of being steered/queued/interrupted. Slash forms (/approve, /deny)
            # already bypass to the runner at the base-adapter guard. This handles the bare-word forms
            # (Signal/SMS users naturally type "yes" rather than "/approve"). Gating on
            # has_blocking_approval(session_key) is the disambiguator that keeps a conversational "yes" from
            # triggering a dangerous command when no approval is actually pending (design intent — see
            # run.py "Pending exec approvals are handled by /approve and /deny" note). We reuse the
            # canonical /approve and /deny handlers rather than re-deriving the resolution + i18n messaging:
            # they resolve the waiting thread, resume typing, AND return a localized confirmation string.
            # The busy-handler path does not auto-send that return, so we deliver it ourselves (mirroring
            # the draining-case send above).
            if event.allow_gateway_control and has_blocking_approval(session_key):
                _raw_text = (event.text or "").strip().lower()
                _match = self._plaintext_approval_words().get(_raw_text)
                if _match is not None:
                    _verb, _normalized_args = _match
                    _approval_handler = (
                        self._handle_approve_command if _verb == "approve" else self._handle_deny_command
                    )
                    # Synthesize "/approve [args]" / "/deny" so the slash handlers parse modifiers via
                    # event.get_command_args(). Always a literal "/": is_command()/get_command_args()
                    # don't recognize per-platform display prefixes ("!" on Slack/Matrix).
                    event.text = f"/{_verb} {_normalized_args}".rstrip()
                    _reply = await _approval_handler(event)
                    logger.info(
                        "Approval response via plain text: session=%s verb=%s args=%r",
                        session_key, _verb, _normalized_args,
                    )
                    _adapter = self._delivery_adapter_for(event.source)
                    if _adapter and _reply:
                        _text, _eph_ttl = _adapter._unwrap_ephemeral(_reply)
                        if _text:
                            await self._send_busy_reply(event, _adapter, _text, plain_anchor=True)
                    return True
        except Exception:
            logger.warning(
                "Plain-text approval routing failed for session %s; "
                "falling through to busy handling", session_key, exc_info=True,
            )
        return False

    async def _resolve_busy_steer_or_redirect(
        self, event: MessageEvent, session_key: str, effective_mode: str, running_agent: Any
    ) -> "GatewayRunner._BusySteerOutcome":
        """Apply interrupt->queue demotions, then attempt steer (steer mode) or redirect (interrupt mode)."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        # Steer injects mid-run via running_agent.steer(), falling back to queue (nothing lost) when
        # the agent isn't running yet, lacks steer(), or the payload is empty. Interrupt is demoted
        # to queue while subagents run (interrupt() would abort them); /stop and /new still cancel all.
        demoted_for_subagents = (
            effective_mode == "interrupt" and self._agent_has_active_subagents(running_agent)
        )
        if demoted_for_subagents:
            effective_mode = self._demote_interrupt(session_key, "the running agent has active subagents (#30170)")
        demoted_for_compression = (
            effective_mode == "interrupt" and await self._session_has_compression_in_flight(session_key)
        )
        if demoted_for_compression:
            effective_mode = self._demote_interrupt(session_key, "context compression is in flight (#56391)")
        # Boot-resume protection (fork, 2026-07-10 live incident): a user message arriving seconds
        # after a gateway restart must not abort the recovery turn — that turn is replaying work the
        # restart interrupted (handoff verification, deferred deliverables). Same demotion pattern
        # as subagents/compression above; explicit /stop remains the escape hatch.
        demoted_for_startup_resume = (
            effective_mode == "interrupt" and self._session_in_startup_resume(session_key)
        )
        if demoted_for_startup_resume:
            effective_mode = self._demote_interrupt(session_key, "a boot-resume recovery turn is running")
        steered = redirected = False
        agent_live = running_agent is not None and running_agent is not _AGENT_PENDING_SENTINEL
        plain_text = (
            event.message_type == MessageType.TEXT and not event.media_urls and not event.media_types
        )
        if effective_mode == "steer":
            # Steerable: plain text, voice (#58780) and images (t_086da8be); files, video and
            # stickers queue. Arrival order is kept per session (_steer_busy_followup).
            if agent_live and hasattr(running_agent, "steer"):
                steered = await self._steer_busy_followup(event, session_key, running_agent) == "steered"
            if not steered:
                effective_mode = "queue"
        elif (
            effective_mode == "interrupt" and plain_text and agent_live
            and getattr(running_agent, "_supports_active_turn_redirect", False) is True
            and hasattr(running_agent, "redirect")
        ):
            if self._has_queued_human_followup(session_key, self._delivery_adapter_for(event.source)):
                # Not redirected: the caller queues this event behind the earlier one and interrupts.
                self._log_busy_followup_held(session_key, "redirect")
            else:
                redirected = self._redirect_active_turn(
                    running_agent, (event.text or "").strip(), session_key, event
                )
        outcome = self._BusySteerOutcome(
            effective_mode=effective_mode, demoted_for_subagents=demoted_for_subagents,
            demoted_for_compression=demoted_for_compression, steered=steered, redirected=redirected,
            demoted_for_startup_resume=demoted_for_startup_resume,
        )
        return outcome

    @staticmethod
    def _demote_interrupt(session_key: str, why: str) -> str:
        logger.info("Demoting busy_input_mode 'interrupt' to 'queue' for session %s because %s", session_key, why)
        return "queue"

    def _try_agent_verb(
        self, running_agent, verb: str, text: str, session_key: str, *, event: Optional[MessageEvent] = None
    ) -> bool:
        """Call ``running_agent.<verb>(text)`` (steer/redirect); False + warning on failure."""
        try:
            call_text = self._steer_text_with_origin(text, event) if event else text
            if verb == "steer":
                return self._steer_running_agent(running_agent, call_text, event=event,
                                                 internal=bool(getattr(event, "internal", False)))
            # A redirect may land in the steer buffer (tool phase), so it is a buffer entry too.
            return steer_with_provenance(running_agent, getattr(running_agent, verb), call_text,
                                         internal=bool(getattr(event, "internal", False)), event=event)
        except Exception as exc:
            logger.warning("Gateway %s failed for session %s: %s", verb, session_key, exc)
            return False

    def _redirect_active_turn(self, running_agent, text: str, session_key: str, event: MessageEvent) -> bool:
        """``redirect()`` the running turn onto *event* and re-anchor its delivery to that message.

        The turn's reply anchor and ledger identity were bound to the message that OPENED it, and
        the final send is bracketed against that event; after a successful redirect the answer is
        to *event*, so the reply must quote it (#115001). Both redirect entry points (busy
        interrupt mode and the priority path) go through here.
        """
        if not self._try_agent_verb(running_agent, "redirect", text, session_key, event=event):
            return False
        turn = self._fold_into_running_turn(running_agent, session_key, event)
        if turn is None:
            return True  # a newer turn already owns the slot; never re-anchor it
        anchor = self._reply_anchor_for_event(event)
        inbound_id = str(event.message_id) if event.message_id else None
        if turn.event is not None and turn.event is not event:
            turn.event.reply_anchor_override = anchor
            turn.event.ledger_message_id = inbound_id
        if turn.ctx is not None:
            turn.ctx.event_message_id = anchor
            turn.ctx.inbound_message_id = inbound_id
        return True

    def _fold_into_running_turn(self, running_agent, session_key: str, event: MessageEvent):
        """The running turn now answers *event* too (steer, redirect): if *event* was addressed to
        the bot, a bare silence marker must not end the turn. Returns the turn, or None when a newer
        turn already owns the slot."""
        turn = self._session_state(session_key).turn
        if turn.agent is not running_agent:
            return None
        ctx = getattr(running_agent, "_gateway_receiving_turn_context", turn.ctx)
        if ctx is not None and ctx._run_still_current is not None and not ctx._run_still_current():
            return None
        if turn.event is not None and turn.event is not event:
            turn.event.absorb_reply_expected(event)
        # The slot event can be the opening event of a recursive queue chain.
        # Merge into the receiving context, never copy policy back from that parent.
        if ctx is not None and ctx.reply_expected is not True and event.reply_expected is not False:
            ctx.reply_expected = event.reply_expected
        if ctx is not None and not getattr(event, "internal", False):
            from gateway.response_filters import display_kind_for_event, is_machinery_display_kind
            if not is_machinery_display_kind(display_kind_for_event(event)):
                ctx.human_engaged = True
                consumer = ctx.stream_consumer_holder[0]
                if consumer is not None and not (ctx.scheduled_heartbeat or ctx.mute_notification_reply):
                    consumer.on_human_engagement()
        return turn

    async def _interrupt_running_agent_for_busy_event(self, event: MessageEvent, adapter, running_agent) -> None:
        """Interrupt mode: abort in-flight tool calls; the agent loop exits at its next check point."""
        from gateway.run import _build_media_placeholder
        try:
            _interrupt_text = event.text
            _media_urls = getattr(event, "media_urls", None) or []
            if self._pending_event_audio_paths(event):
                _interrupt_text, _ = await self._transcribe_and_echo_pending_voice(
                    event, adapter, event.source, event.text or "", log_context="Voice-busy-interrupt",
                )
            elif not _interrupt_text and _media_urls:
                _interrupt_text = _build_media_placeholder(event)
            running_agent.interrupt(_interrupt_text)
        except Exception:
            pass  # don't let interrupt failure block the ack

    def _busy_steer_ack_enabled(self, event: MessageEvent, session_key: str) -> bool:
        # Some mobile chat setups want silent steering — keep the behavior, drop the bubble.
        from gateway.run import _load_gateway_config, _platform_config_key
        from gateway.display_config import display_scope, resolve_display_setting
        steer_ack_env = os.environ.get("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED")
        if steer_ack_env is not None:
            steer_ack_enabled = steer_ack_env.strip().lower() in {"1", "true", "yes", "on"}
        else:
            steer_ack_enabled = bool(
                resolve_display_setting(
                    _load_gateway_config(), _platform_config_key(event.source.platform),
                    "busy_steer_ack_enabled", True, **display_scope(event.source),
                )
            )
        if not steer_ack_enabled:
            logger.debug("Busy steer ack suppressed for session %s", session_key)
        return steer_ack_enabled

    @property
    def _BUSY_DEMOTED_TAIL(self) -> str:  # noqa: N802 — long-standing mixin attr name
        return t("gateway.busy.demoted_tail")

    def _compose_busy_ack_message(
        self, event: MessageEvent, now: float, _busy_state, running_agent: Any, *,
        is_steer_mode: bool, is_queue_mode: bool, is_redirect_mode: bool,
        demoted_for_subagents: bool, demoted_for_compression: bool,
        demoted_for_startup_resume: bool = False,
    ) -> str:
        from gateway.run import (
            _AGENT_PENDING_SENTINEL, _hermes_home, _load_gateway_config, _platform_config_key
        )
        from gateway.display_config import display_scope, resolve_display_setting

        # Terse by default; iteration/tool detail opts in via display.platforms.<p>.busy_ack_detail.
        status_parts = []
        busy_ack_detail_enabled = bool(
            resolve_display_setting(
                _load_gateway_config(), _platform_config_key(event.source.platform),
                "busy_ack_detail", True, **display_scope(event.source),
            )
        )
        if busy_ack_detail_enabled and running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
            try:
                summary = running_agent.get_activity_summary()
                elapsed_min = 0
                if _busy_state and _busy_state.turn.started_ts:
                    elapsed_min = int((now - _busy_state.turn.started_ts) / 60)
                if elapsed_min > 0:
                    status_parts.append(t("gateway.busy.elapsed_min", minutes=elapsed_min))
                if summary.get("max_iterations", 0):
                    status_parts.append(
                        format_iteration_progress(
                            summary.get("api_call_count", 0), summary.get("max_iterations", 0)
                        )
                    )
                if summary.get("current_tool"):
                    status_parts.append(t("gateway.busy.running_tool", tool=summary.get("current_tool")))
            except Exception:
                pass
        status_detail = (t("gateway.busy.ack_detail_wrap", detail=t("gateway.busy.ack_detail_joiner").join(status_parts))
                         if status_parts else "")
        if is_steer_mode and self._agent_has_active_subagents(running_agent):
            head = t("gateway.busy.steered_subagents_head")
            tail = t("gateway.busy.steered_subagents_tail")
        elif is_steer_mode:
            head, tail = t("gateway.busy.steered_head"), t("gateway.busy.steered_tail")
        elif is_redirect_mode:
            head, tail = t("gateway.busy.redirected_head"), t("gateway.busy.redirected_tail")
        elif is_queue_mode and demoted_for_subagents:
            # Explain the demotion: the follow-up didn't kill the subagent; /stop is the escape hatch.
            head, tail = t("gateway.busy.subagent_working_head"), self._BUSY_DEMOTED_TAIL
        elif is_queue_mode and demoted_for_startup_resume:
            # Fork: say only what is NEW here (queued behind the recovery). ONE restart message per
            # boot, and it is the boot notice (fork_ext.unclean_restart_notice) — this ack must not
            # re-announce the restart (2026-09-22 consolidation).
            head = "⏳ Still finishing the interrupted work"
            tail = " — your message is queued and folds in right after (use /stop to cancel the recovery)."
        elif is_queue_mode and demoted_for_compression:
            head, tail = t("gateway.busy.compressing_head"), self._BUSY_DEMOTED_TAIL
        elif is_queue_mode:
            head, tail = t("gateway.busy.queued_head"), t("gateway.busy.queued_tail")
        else:
            head, tail = t("gateway.busy.interrupting_head"), t("gateway.busy.interrupting_tail")
        message = t("gateway.busy.ack", head=head, detail=status_detail, tail=tail)

        # One-time onboarding hint about the queue/interrupt knob (flag persisted to config.yaml).
        try:
            from agent.onboarding import (BUSY_INPUT_FLAG, busy_input_hint_gateway, is_seen, mark_seen)
            if not is_seen(_load_gateway_config(), BUSY_INPUT_FLAG):
                _hint_mode = (
                    "steer" if is_steer_mode
                    else "queue" if is_queue_mode
                    else "redirect" if is_redirect_mode
                    else "interrupt"
                )
                message = f"{message}\n\n{busy_input_hint_gateway(_hint_mode)}"
                mark_seen(_hermes_home / "config.yaml", BUSY_INPUT_FLAG)
        except Exception as _onb_err:
            logger.debug("Failed to apply busy-input onboarding hint: %s", _onb_err)
        return message

    async def _send_busy_ack_reply(self, event: MessageEvent, adapter, message: str) -> None:
        try:
            await self._send_busy_reply(event, adapter, message)
        except Exception as e:
            logger.debug("Failed to send busy-ack: %s", e)

    async def _handle_active_session_busy_message(self, event: MessageEvent, session_key: str) -> bool:
        # Gateway wakes have no external user identity. Admit them before auth/drain/approval
        # handling, without merging their text into an already queued human message.
        if event.internal and event.allow_gateway_control:
            adapter = self._delivery_adapter_for(event.source)
            if adapter and session_key in getattr(adapter, "_pending_messages", {}):
                self._queue_or_replace_pending_event(session_key, event)
                return True
            if _is_watch_notification_event(event):   # it lands in the empty head slot: evictable (t_270034fa)
                event._watch_pristine = _watch_content(event)
            return False  # base adapter queues silently behind the active turn

        # Same authorization gate as the cold path, else unauthorized users in shared threads
        # inject messages into a session they don't own.
        from gateway.run import _AGENT_PENDING_SENTINEL
        # See #17775. A primary transport can route a turn into a secondary
        # profile, so authorize in the stamped transport scope.
        if not self._is_user_authorized_for_source(event.source):
            logger.warning(
                "Dropping message from unauthorized user in active session: "
                "user=%s (%s), platform=%s, session=%s", event.source.user_id, event.source.user_name,
                event.source.platform.value if event.source.platform else "unknown", session_key,
            )
            return True  # handled (silently dropped); do not fall through
        # A steered or queued follow-up never reaches _hm_admit_event, so the budget is charged here.
        if not self._admit_bot_message_for_source(event.source):
            return True
        event._bot_loop_admitted = True

        effective_mode = self._effective_busy_input_mode(event.source)
        if self._draining:  # gateway restarting/stopping
            await self._send_busy_drain_notice(event, session_key, effective_mode)
            return True
        if await self._route_plaintext_approval_while_busy(event, session_key):
            return True
        adapter = self._delivery_adapter_for(event.source)
        if not adapter:
            return False  # let default path handle it
        # Internal synthetic events (delegation / background completions) must never interrupt or
        # steer; they surface as a NEW turn when idle. Plugin events carry untrusted payload text, so
        # queue them through the FIFO (security metadata kept apart).
        if getattr(event, "internal", False):
            self._queue_or_replace_pending_event(session_key, event)
            return True
        if (
            event.message_type == MessageType.TEXT
            and self._effective_busy_text_mode(event.source) == "queue"
            and effective_mode != "steer"
        ):
            return False

        _busy_state = self._peek_session_state(session_key)
        running_agent = _busy_state.turn.agent if _busy_state else None
        _steer = await self._resolve_busy_steer_or_redirect(event, session_key, effective_mode, running_agent)
        effective_mode, redirected = _steer.effective_mode, _steer.redirected
        # Queue as the next turn — skipped after a successful steer/redirect (the text is already in
        # the run and must NOT replay). FIFO gives each text its own turn (raw merge would join them).
        if not _steer.steered and not redirected:
            self._queue_or_replace_pending_event(session_key, event)
        # Store the message so it's processed as the next turn after the current run finishes (or is
        # interrupted). Skip this for a successful steer — the text already landed inside the run and must
        # NOT also be replayed as a next-turn user message. Route through _queue_or_replace_pending_event
        # (the same FIFO infrastructure used by busy queue-mode and /queue) rather than a raw
        # merge_pending_message_event(merge_text=True). The raw merge newline-joins consecutive TEXT
        # follow-ups into a SINGLE pending turn, destroying message boundaries — so two separate user
        # messages sent while the agent was busy (interrupt mode, or a steer that fell back to queue)
        # arrived as one mashed-together turn (#43066 sub-bug 2). The FIFO path gives each text its own turn
        # in arrival order while still preserving photo-burst / album merge semantics for media.
        is_queue_mode = effective_mode == "queue"
        is_steer_mode = effective_mode == "steer"
        is_redirect_mode = effective_mode == "interrupt" and redirected
        if (
            effective_mode == "interrupt" and not redirected
            and running_agent and running_agent is not _AGENT_PENDING_SENTINEL
        ):
            await self._interrupt_running_agent_for_busy_event(event, adapter, running_agent)

        # Disabled ack: still process input. Checked before debounce so an undelivered ack never
        # stamps the "last ack" timestamp.
        if os.environ.get("HERMES_GATEWAY_BUSY_ACK_ENABLED", "true").lower() != "true":
            logger.debug("Busy ack suppressed for session %s", session_key)
            return True  # input still processed, just no ack sent

        # Debounce (30s) before the config-heavy display lookup.
        now = time.time()
        if now - (_busy_state.turn.busy_ack_ts if _busy_state else 0) < 30:
            return True  # interrupt sent (if not queue), ack already delivered recently

        if is_steer_mode and not self._busy_steer_ack_enabled(event, session_key):
            return True

        self._session_state(session_key).turn.busy_ack_ts = now

        message = self._compose_busy_ack_message(
            event, now, _busy_state, running_agent, is_steer_mode=is_steer_mode,
            is_queue_mode=is_queue_mode, is_redirect_mode=is_redirect_mode,
            demoted_for_subagents=_steer.demoted_for_subagents,
            demoted_for_compression=_steer.demoted_for_compression,
            demoted_for_startup_resume=getattr(_steer, "demoted_for_startup_resume", False),
        )
        await self._send_busy_ack_reply(event, adapter, message)
        return True

    # Slash name → handler method is ``_handle_<name>_command`` (``-`` → ``_``) except these.
    _COMMAND_HANDLER_ALIASES = {"bg": "_handle_background_command", "sethome": "_handle_set_home_command"}
    # Ordinary slash handlers shared by idle and busy dispatch.
    _PLAIN_COMMANDS = (
        "status", "context", "restart", "approve", "deny", "pause", "agents", "bg", "btw",
        "kanban", "subgoal", "heartbeat", "busy", "yolo", "verbose", "footer", "help",
        "commands", "profile", "login", "update", "version",
        "overview",   # fork-only (t_66ffcd4f): handler in gateway/slash_commands.py
    )
    # Dispatched only on the idle path (busy dispatch has its own allowlist).
    _IDLE_COMMANDS = (
        "topic", "whoami", "platform", "stop", "reasoning", "memory", "skills", "fast",
        "approvals", "model", "codex-runtime", "personality", "suggestions", "save", "retry",
        "sethome", "compress", "usage", "topup", "insights", "reload-mcp", "reload-skills",
        "bundles", "debug", "title", "resume", "sessions", "branch", "rollback", "diff", "goal",
        "loop", "refine", "review", "voice",
        # fork-only session commands (handlers in gateway/slash_commands.py)
        "redo", "merge", "resume-handoff",
    )

    def _command_handler_table(self, names) -> Dict[str, Any]:
        return {
            name: getattr(
                self, self._COMMAND_HANDLER_ALIASES.get(name, f"_handle_{name.replace('-', '_')}_command"),
            )
            for name in names
        }

    def _gateway_plain_command_handlers(self):
        """Return ordinary slash handlers shared by idle and busy dispatch."""
        return self._command_handler_table(self._PLAIN_COMMANDS)

    async def _send_command_ack(self, source, text: str, label: str) -> None:
        """Best-effort acknowledgment for a slash command that falls through to agent processing."""
        try:
            adapter = self._delivery_adapter_for(source)
            if adapter:
                await adapter.send(
                    str(source.chat_id), text, metadata=self._thread_metadata_for_source(source)
                )
        except Exception:
            logger.debug("%s ack send failed", label, exc_info=True)

    def _gateway_idle_command_handlers(self):
        """Slash handlers dispatched only on the idle path (busy dispatch has its own allowlist)."""
        return self._command_handler_table(self._IDLE_COMMANDS)

    # busy_handler key (hermes_cli/commands.py CommandDef) → mid-run variant ``_busy_<key>_command``.
    _BUSY_SPECIAL_HANDLERS: Dict[str, str] = {
        k: f"_busy_{k}_command" for k in ("start", "stop", "new", "queue", "steer", "egress", "goal", "loop")
    }

    def _busy_slash_would_reject(self, cmd_def) -> bool:
        """True when ``_dispatch_busy_slash_command`` would answer ``cmd_def`` with reject text rather
        than running it mid-turn (no special busy handler, no dispatch policy)."""
        if getattr(cmd_def, "busy_handler", None) in self._BUSY_SPECIAL_HANDLERS:
            return False
        return getattr(cmd_def, "busy_policy", "reject") not in ("dispatch", "interrupt_then_dispatch")

    def _busy_turn_is_stopping(self, quick_key: str) -> bool:
        state = self._peek_session_state(quick_key)
        return bool(state is not None and getattr(state.turn, "stopping", False))

    async def _dispatch_busy_slash_command(self, event: MessageEvent, cmd_def, quick_key: str, source):
        """Dispatch a recognized slash command while an agent is running.

        Order: ``busy_handler`` (mid-run variant) → ``busy_policy == "dispatch"`` (normal handler)
        → catch-all reject text. Rejecting is required rather than falling through to
        interrupt + discard: commands like /model, /reasoning, /voice, /insights, /title,
        /resume, /retry, /undo, /compress, /usage, /reload-mcp, /sethome, /reset (all
        registered as Discord slash commands) would interrupt the agent AND get silently
        discarded by the slash-command safety net, producing a zero-char response.
        See #5057, #6252, #10370.

        1. ``busy_handler`` — special mid-run variant (e.g. /goal's control-verb whitelist, /queue's FIFO
        enqueue, /model's custom reject text). 2. 3. See #5057, #6252, #10370.
        """
        name = cmd_def.name
        policy = getattr(cmd_def, "busy_policy", "reject")
        handler_key = getattr(cmd_def, "busy_handler", None)
        if handler_key:
            special = self._BUSY_SPECIAL_HANDLERS.get(handler_key)
            if special is not None:
                return await getattr(self, special)(event, quick_key, source)
        if self._busy_turn_is_stopping(quick_key) and self._busy_slash_would_reject(cmd_def):
            # The user already stopped this turn; "wait or /stop first" would be false. Reached only
            # when the stop outlived _hm_wait_for_stop_to_settle's bound.
            return t("gateway.busy.stop_settling", command=name)
        if handler_key:
            reject_key = self._BUSY_REJECT_TEXT.get(handler_key)
            if reject_key is not None:
                return t(reject_key)
        if policy in ("dispatch", "interrupt_then_dispatch"):
            plain = self._gateway_plain_command_handlers().get(name)
            if plain is not None:
                async with self._async_profile_scope_for_source(source):
                    return await plain(event)
            logger.warning(
                "busy_policy=%s for /%s has no mid-run handler — "
                "falling back to busy-reject", policy, name,
            )

        return t("gateway.busy.slash_rejected", command=name)

    async def _handle_pause_command(self, event: MessageEvent):
        """`/pause [reason]` engages the global emergency stop; `/pause off` lifts it (the estop gate
        lets slash commands through while paused so messaging-only operators are never locked out)."""
        from agent import estop
        args = (event.get_command_args() or "").strip()
        if args.lower() in {"off", "resume", "stop", "disengage"}:
            if estop.disengage():
                return t("gateway.pause.resumed")
            return t("gateway.pause.not_paused")
        state = estop.get_state()
        if state is not None and not args:
            suffix = t("gateway.pause.reason_suffix", reason=state.get("reason")) if state.get("reason") else ""
            return t("gateway.pause.already_paused", suffix=suffix)
        estop.engage(reason=args or None)
        suffix = t("gateway.pause.reason_suffix", reason=args) if args else ""
        return t("gateway.pause.paused", suffix=suffix)

    async def _busy_start_command(self, event: MessageEvent, quick_key: str, source):
        # Telegram's /start is a platform ping (bot launch/deep-link), not a user command.
        logger.info("Ignoring /start platform ping for active session %s", quick_key)
        return ""

    async def _busy_egress_command(self, event: MessageEvent, quick_key: str, source):
        from hermes_cli.proxy_cli import format_status_text
        return format_status_text()

    async def _busy_stop_command(self, event: MessageEvent, quick_key: str, source):
        # Hard-kill: a soft interrupt can't reach a truly hung executor thread.
        from gateway.run import _INTERRUPT_REASON_STOP
        await self._interrupt_and_clear_session(
            quick_key, source, interrupt_reason=_INTERRUPT_REASON_STOP, invalidation_reason="stop_command",
            **({"invoke_timing": event.invoke_timing} if isinstance(getattr(event, "invoke_timing", None), dict) else {}),
        )
        logger.info("STOP for session %s — agent interrupted, session lock released", quick_key)
        suffix = ""
        try:  # never let the queue reminder break /stop
            from gateway import queue_mgmt
            suffix = queue_mgmt.stop_reply_suffix(self, quick_key, self._delivery_adapter_for(source))
        except Exception:
            logger.warning("queue suffix skipped on /stop for %s", quick_key, exc_info=True)
        return EphemeralReply(t("gateway.stop.stopped") + suffix)

    async def _busy_new_command(self, event: MessageEvent, quick_key: str, source):
        # /reset and /new bypass the running-agent guard (else they'd queue as user text and replay
        # into the same broken history); clear pending messages so the old text doesn't replay.
        from gateway.run import _INTERRUPT_REASON_RESET
        # Interrupt the agent first, then clear the adapter's pending queue so the stale "/reset" text
        # doesn't get re-processed as a user message after the interrupt completes. See #2170.
        await self._interrupt_and_clear_session(
            quick_key, source, interrupt_reason=_INTERRUPT_REASON_RESET, invalidation_reason="new_command",
        )
        return await self._handle_reset_command(event)

    async def _busy_queue_command(self, event: MessageEvent, quick_key: str, source):
        # Each /queue is its own full agent turn, run FIFO after the current run; never merged.
        # Management verbs (list/edit/rm/move/clear/run/help) are parsed by the shared CLI+gateway
        # parser and handled in gateway/queue_mgmt.py (spec 2026-10-12).
        from gateway import queue_mgmt
        adapter = self._delivery_adapter_for(source)
        op = queue_mgmt.classify(event)
        if op.kind == "run":
            return await queue_mgmt.release(self, quick_key, source, adapter, busy=True)
        if op.is_management:
            return queue_mgmt.apply_management(self, op, quick_key, adapter)
        queued_text = op.text
        # A /queue carrying media or reply context is valid with no prompt text (image caption).
        has_media = bool(getattr(event, "media_urls", None))
        if not queued_text and not has_media:
            return t("gateway.queue.usage")
        refused = queue_mgmt.refuse_command_prompt(queued_text) or queue_mgmt.cap_reply(self, quick_key, adapter)
        if refused:
            return refused
        if adapter:
            _queued_event = MessageEvent(
                text=queued_text, message_type=event.message_type if has_media else MessageType.TEXT,
                source=event.source, raw_message=event.raw_message, message_id=event.message_id,
                media_urls=list(getattr(event, "media_urls", []) or []),
                media_types=list(getattr(event, "media_types", []) or []),
                media_text_inlined=list(getattr(event, "media_text_inlined", []) or []),
                reply_to_message_id=event.reply_to_message_id, reply_to_text=event.reply_to_text,
                reply_to_author_id=event.reply_to_author_id,
                reply_to_author_name=event.reply_to_author_name,
                reply_to_is_own_message=event.reply_to_is_own_message, auto_skill=event.auto_skill,
                channel_prompt=event.channel_prompt, channel_context=event.channel_context,
                internal=event.internal, timestamp=event.timestamp,
                wake_kind=getattr(event, "wake_kind", None),
            )
            self._enqueue_fifo(quick_key, _queued_event, adapter)
            queue_mgmt.log_items(self, "add", quick_key, [_queued_event])
        depth = self._queue_depth(quick_key, adapter=adapter)
        return (t("gateway.queue.queued") + (t("gateway.queue.queued_depth", depth=depth) if depth > 1 else "")
                + queue_mgmt.held_note(self, quick_key))

    async def _busy_steer_command(self, event: MessageEvent, quick_key: str, source):
        # /steer lands BETWEEN tool-call iterations of the same run (appended to the last tool
        # result) — no interrupt, no new user turn, no role-alternation violation.
        from gateway.run import _AGENT_PENDING_SENTINEL
        steer_text = event.get_command_args().strip()
        if not steer_text:
            return t("gateway.steer.usage")
        _steer_state = self._peek_session_state(quick_key)
        running_agent = _steer_state.turn.agent if _steer_state else None

        def _queue_fallback(reply: str) -> str:
            # Turn-boundary fallback: queue the steer text as its own follow-up turn.
            adapter = self._delivery_adapter_for(source)
            if adapter:
                self._enqueue_fifo(quick_key, MessageEvent(
                    text=steer_text, message_type=MessageType.TEXT, source=event.source,
                    message_id=event.message_id, channel_prompt=event.channel_prompt,
                    channel_context=event.channel_context,
                ), adapter)
            return reply

        if running_agent is _AGENT_PENDING_SENTINEL:
            return _queue_fallback(t("gateway.steer.queued_starting"))
        if not running_agent or not hasattr(running_agent, "steer"):
            return _queue_fallback(t("gateway.steer.queued_no_agent"))
        try:
            accepted = self._steer_running_agent(running_agent, self._steer_text_with_origin(steer_text, event),
                                                 event=event)
        except Exception as exc:
            logger.warning("Steer failed for session %s: %s", quick_key, exc)
            return t("gateway.steer.failed", error=exc)
        if not accepted:
            # The payload is not empty (checked above): the turn has returned and closed steering
            # (t_2b16f0e6), so queue it as the next turn instead of dropping it (Prism ed7e5f5d5936).
            # A queued slash command would run as a command at dequeue: refuse it (Prism 028df965e393).
            from gateway import queue_mgmt
            return queue_mgmt.refuse_command_prompt(steer_text) or _queue_fallback(t("gateway.queue.queued"))
        self._fold_into_running_turn(running_agent, quick_key, event)
        preview = steer_text[:60] + ("..." if len(steer_text) > 60 else "")
        target = (t("gateway.steer.target_subagents") if self._agent_has_active_subagents(running_agent)
                  else t("gateway.steer.target_run"))
        return t("gateway.steer.queued", target=target, preview=preview)

    async def _busy_goal_command(self, event: MessageEvent, quick_key: str, source):
        # Control verbs are safe mid-run (state only); setting new goal text is rejected so we don't
        # race a second continuation against the current turn. wait/gate take an argument.
        from hermes_cli.goal_command import is_goal_control

        if is_goal_control(event.get_command_args() or ""):
            return await self._handle_goal_command(event)
        return t("gateway.busy.reject_goal")

    async def _busy_loop_command(self, event: MessageEvent, quick_key: str, source):
        # Mirrors /goal: control verbs are safe mid-run; a new loop is rejected.
        _loop_arg = (event.get_command_args() or "").strip().lower()
        if not _loop_arg or _loop_arg in {"status", "pause", "resume", "stop", "clear", "cancel", "help", "--help", "-h"}:
            return await self._handle_loop_command(event)
        return t("gateway.busy.reject_loop")

    def _check_slash_access(self, source: SessionSource, canonical_cmd: str) -> Optional[str]:
        """Denial message if ``source`` cannot run ``canonical_cmd``, else None (both dispatch paths
        use it so an in-flight agent can't bypass admin gating; no ``allow_admin_from`` → None)."""
        from gateway.slash_access import policy_for_runner_source
        if not canonical_cmd:
            return None
        policy = policy_for_runner_source(self, source)
        if not policy.enabled or policy.can_run(source.user_id, canonical_cmd):
            return None
        logger.info(
            "Slash command /%s denied for %s:%s (not admin, not in user_allowed_commands)",
            canonical_cmd, source.platform.value if source.platform else "?", source.user_id,
        )
        allowed_preview = sorted(policy.user_allowed_commands)
        if allowed_preview:
            suffix = (
                t("gateway.unauthorized.admin_only_can_run") + ", ".join(f"/{c}" for c in allowed_preview[:12])
                + ("…" if len(allowed_preview) > 12 else "") + t("gateway.unauthorized.admin_only_whoami_hint")
            )
        else:
            suffix = t("gateway.unauthorized.admin_only_none")
        return t("gateway.unauthorized.admin_only", command=canonical_cmd, suffix=suffix)

    def _same_chat_runs(self, source: SessionSource, own_key: str) -> List[Tuple[str, str, str]]:
        """``(key, chat_type, tail)`` for every OTHER running turn in the caller's chat (``tail`` is
        the key text after the chat id, ``""`` when the key ends there).

        The namespace comes from ``own_key`` — the session store's own answer, so a named-profile
        stop matches that profile's runs and never a literal. ``_snapshot_running_agents`` already
        drops the pending sentinel (a session still being set up has no agent). Callers gate on
        authorization; ``own_key`` is excluded. Both tiers share one call.
        """
        chat_id = str(getattr(source, "chat_id", None) or "")
        if not chat_id:
            return []
        if source.chat_type == "dm" and source.platform == Platform.WHATSAPP:
            # Match the same text build_session_key keyed: WhatsApp DM chat ids are canonicalised
            # there, so a raw JID/LID alias would never line up with the stored key.
            chat_id = canonical_whatsapp_identifier(chat_id) or chat_id
        namespace = ":".join(own_key.split(":", 2)[:2])
        prefix = f"{namespace}:{source.platform.value}:"
        scope_id = str(getattr(source, "scope_id", None) or "") or None
        runs = []
        for key in self._snapshot_running_agents():
            if key == own_key:
                continue
            parsed = _same_chat_key_slots(key, prefix=prefix, chat_id=chat_id, scope_id=scope_id)
            if parsed is not None:
                runs.append((key, parsed[0], parsed[1]))
        return runs

    def _sibling_thread_run_keys(
        self, source: SessionSource, runs: List[Tuple[str, str, str]],
    ) -> List[str]:
        """Keys from ``runs`` belonging to OTHER participants in the caller's own thread (per-user
        thread mode keys are ``...:{thread_id}:{user_id}``, so another user's run is invisible to the
        caller's own ``/stop``). Callers still gate on authz."""
        thread_id = str(getattr(source, "thread_id", None) or "")
        chat_type = getattr(source, "chat_type", None) or ""
        if not thread_id or not chat_type:
            return []
        return [
            key
            for key, key_chat_type, tail in runs
            if key_chat_type == chat_type and _tail_has_slot(tail, thread_id)
        ]

    def _chat_scoped_run_keys(
        self, source: SessionSource, runs: List[Tuple[str, str, str]],
    ) -> List[str]:
        """Keys from ``runs`` for ANY session of the same chat, whatever the chat_type/thread/
        participant slots. Two supported shapes make a /stop key miss a run in the same chat (found
        via Slack's native stop button, gateway-gateway#286): a top-level channel turn keys
        ``channel`` while an in-thread /stop normalizes to ``thread``, and rolling-DM configs key
        without the thread slot the stop carries. "/stop" means "stop what's running in THIS chat",
        which is also what lets a human stop a peer's per-sender group run (see ``_same_chat_runs``).

        A stop sent from INSIDE a thread only reaches runs whose own thread slot is that thread (or
        that carry no thread slot at all — the rolling-DM shape). Anything else in the channel is a
        different conversation: another reply thread, or a peer's top-level run. Callers gate on
        authz.
        """
        thread_id = str(getattr(source, "thread_id", None) or "")
        return [
            key
            for key, _key_chat_type, tail in runs
            if not thread_id or not tail or _tail_has_slot(tail, thread_id)
        ]

    def _is_stale_restart_redelivery(self, event: MessageEvent) -> bool:
        """True if this /restart is a Telegram re-delivery we already handled.

        The previous gateway wrote ``.restart_last_processed.json`` (platform + update_id). A
        /restart with update_id <= that value is a redelivery when this process booted from that
        restart; otherwise the marker must be < 5 minutes old. Telegram only (numeric ordering).
        """
        from gateway.run import _hermes_home
        if event is None or event.source is None or event.platform_update_id is None:
            return False
        try:
            if event.source.platform.value != "telegram":
                return False
        except Exception:
            return False

        try:
            marker_path = _hermes_home / ".restart_last_processed.json"
            if not marker_path.exists():
                # Missing marker: a redelivered /restart would otherwise re-restart forever. Suppress
                # ONLY when this process booted from a chat /restart AND is within a short post-boot
                # window; consume the flag one-shot so a later legitimate /restart is honored.
                if (
                    # Belt-and-suspenders for when the dedup marker goes missing (manually cleaned up, or
                    # the previous cycle's write failed). Without a marker the update_id comparison below
                    # can't run, so a redelivered /restart would sail through and re-restart the gateway —
                    # an infinite loop (issue #18528).
                    getattr(self, "_booted_from_restart", False)
                    and time.time() - getattr(self, "_startup_time", 0.0) < 60
                ):
                    self._booted_from_restart = False
                    return True
                return False
            data = json.loads(marker_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return False

        recorded_uid = data.get("update_id")
        if (
            data.get("platform") != "telegram"
            or not isinstance(recorded_uid, int)
            or event.platform_update_id > recorded_uid
        ):
            return False

        # A service-managed restart can outlast the 5-minute trust window; consume the boot
        # signal one-shot.
        if getattr(self, "_booted_from_restart", False):
            self._booted_from_restart = False
            return True

        # Staleness guard: an old marker (crash recovery) must not swallow a fresh /restart.
        requested_at = data.get("requested_at")
        return not (isinstance(requested_at, (int, float)) and time.time() - requested_at > 300)

    async def _handle_suggestions_command(self, event: MessageEvent) -> str:
        """/suggestions via the shared handler (origin = event source so jobs deliver back here)."""
        from gateway.run import _command_origin_for_source
        try:
            from hermes_cli.suggestions_cmd import handle_suggestions_command
            return handle_suggestions_command(
                (event.get_command_args() or "").strip(),
                origin=_command_origin_for_source(event.source), surface="gateway",
            )
        except Exception as e:
            logger.debug("suggestions command failed: %s", e)
            return t("gateway.suggestions.failed", error=e)

    async def _handle_blueprint_command(self, event: MessageEvent):
        """/blueprint via the shared handler (origin = event source so jobs deliver back here)."""
        from gateway.run import _command_origin_for_source
        try:
            from hermes_cli.blueprint_cmd import handle_blueprint_command
            return handle_blueprint_command(
                (event.get_command_args() or "").strip(),
                origin=_command_origin_for_source(event.source), surface="gateway",
            )
        except Exception as e:
            logger.debug("blueprint command failed: %s", e)
            from hermes_cli.blueprint_cmd import BlueprintCommandResult
            return BlueprintCommandResult(t("gateway.blueprint.failed", error=e))

    async def _maybe_confirm_destructive_slash(
        self, *, event: MessageEvent, command: str, title: str, detail: str, execute
    ) -> Union[str, "EphemeralReply", None]:
        """Gate a destructive session slash command (/new, /reset, /undo).

        ``execute()`` (async → str | EphemeralReply) runs immediately when
        ``approvals.destructive_slash_confirm`` is off; otherwise via ``_request_slash_confirm``:
        ``once`` runs it, ``always`` persists the opt-out then runs it, ``cancel`` skips it.
        """
        confirm_required = True
        try:
            approvals = self._read_user_config().get("approvals")
            if isinstance(approvals, dict):
                confirm_required = bool(approvals.get("destructive_slash_confirm", True))
        except Exception:
            pass
        if not confirm_required:
            return await execute()

        session_key = self._session_key_for_source(event.source)

        async def _on_confirm(choice: str):
            # Via the class, not ``self``: tests drive this gate on a bare SimpleNamespace runner.
            return await GatewayBusySessionMixin._run_confirmed_destructive_slash(
                choice, command, execute, session_key
            )

        _p = self._typed_command_prefix_for(event.source.platform)
        prompt_message = t("gateway.confirm.destructive_prompt", command=command, detail=detail, prefix=_p)
        return await self._request_slash_confirm(
            event=event, command=command, title=title, message=prompt_message, handler=_on_confirm
        )

    @staticmethod
    def _destructive_optout_note(persisted: bool) -> str:
        """Note appended after an "always" confirm. ``persisted=False``: the user did approve this
        run, so the action still goes ahead, but the preference did not stick and the prompt will be
        back next time — say so rather than promising an opt-out that was never written."""
        return t("gateway.confirm.destructive_optout" if persisted else "gateway.confirm.destructive_optout_save_failed")

    @staticmethod
    async def _run_confirmed_destructive_slash(choice: str, command: str, execute, session_key: str):
        """Confirm-callback body: ``cancel`` → message; ``always`` persists the opt-out, then runs."""
        if choice == "cancel":
            return t("gateway.confirm.cancelled", command=command)
        persisted = False
        if choice == "always":
            try:
                from cli import save_config_value
                # save_config_value swallows its own errors and reports the outcome in the return
                # value, so the try block alone says nothing about whether the write landed.
                persisted = bool(save_config_value("approvals.destructive_slash_confirm", False))
                if persisted:
                    logger.info("User opted out of destructive slash confirm (session=%s)", session_key)
                else:
                    logger.warning(
                        "Could not persist destructive_slash_confirm=false "
                        "(session=%s); config.yaml is not writable", session_key,
                    )
            except Exception as exc:
                logger.warning("Failed to persist destructive_slash_confirm=false: %s", exc)
        result = await execute()
        # Only plain-string results get the note: it would mangle an EphemeralReply.
        if choice == "always" and isinstance(result, str):
            return result + GatewayBusySessionMixin._destructive_optout_note(persisted)
        return result

    async def _request_slash_confirm(
        self, *, event: MessageEvent, command: str, title: str, message: str, handler
    ) -> Optional[str]:
        """Ask the user to confirm a slash command; ``handler(choice)`` runs on "once"/"always"/
        "cancel" and its return is sent as a message. Returns None if buttons rendered, else the
        text-fallback message (which IS the ack)."""
        from tools import slash_confirm as _slash_confirm_mod
        source = event.source
        session_key = self._session_key_for_source(source)
        # object.__new__ test runners lack the counter; fall back to a local one.
        counter = getattr(self, "_slash_confirm_counter", None)
        if counter is None:
            import itertools as _itertools
            counter = self._slash_confirm_counter = _itertools.count(1)
        confirm_id = f"{next(counter)}"

        # Register FIRST so a fast button click cannot race the send_slash_confirm return.
        _slash_confirm_mod.register(session_key, confirm_id, command, handler)

        adapter = self._delivery_adapter_for(source)
        metadata = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))

        if adapter is not None:
            try:
                button_result = await adapter.send_slash_confirm(
                    chat_id=source.chat_id, title=title, message=message, session_key=session_key,
                    confirm_id=confirm_id, metadata=metadata,
                )
                if button_result and getattr(button_result, "success", False):
                    return None  # buttons rendered — no redundant text ack
                # P5(b): distinguish a connector egress DECLINE from a lane
                # failure. On a decline the connector refused this destination,
                # so returning `message` as the direct reply would deliver the
                # very content it refused, as text, to the same chat. Suppress
                # the fallback and tear down the registration — no card
                # rendered, so a later reply must not be captured as an answer
                # to an invisible prompt.
                #
                # Classify the STRUCTURED response (see _approval_send_outcome):
                # a code-only decline has no marker colon in its rendered text,
                # and an ambiguous result must not be treated as a definite
                # refusal.
                from gateway.relay.egress import declined_send

                _confirm_err = getattr(button_result, "error", None)
                if declined_send(button_result):
                    logger.warning(
                        "slash-confirm DECLINED by the connector's egress "
                        "guard for %s on %s — suppressing the text fallback: %s",
                        command, source.platform, _confirm_err,
                    )
                    _slash_confirm_mod.clear(session_key)
                    return None
            except Exception as exc:
                logger.debug("send_slash_confirm failed for %s on %s: %s", command, source.platform, exc)
        # Text fallback — the prompt message itself is the direct reply.
        return message

    def _read_user_config(self) -> Dict[str, Any]:
        """Raw config.yaml for gate lookups that must see on-disk changes without a restart."""
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:
            return {}
        return cfg if isinstance(cfg, dict) else {}
