"""Per-session turn lease — serializes the [load history → run → flush] region.

Why this exists (#64934): the gateway's busy guards are keyed by ROUTING KEY
(``_active_sessions`` in the adapter, ``_running_agents`` in the runner), but
the durable transcript is owned by SESSION_ID — and ``switch_session()`` makes
the key→id mapping many-to-one (``/resume`` of a named session from a second
chat/topic, CLI-continuity rebinding, async-delegation completion pinning,
Telegram topic-binding tip-walks). Two routing keys mapped to one session_id
run concurrent turns on two different agent objects, so no per-key guard ever
sees the collision. The two turns then interleave their flushes on one
transcript: rows persist in completion order instead of arrival order, the
identity-marker dedup over shared history dicts can swallow a row outright,
and the second turn runs on a history base that never saw the first turn's
exchange — leaving a permanent ``user;user`` alternation wedge that
``repair_message_sequence`` re-repairs on every request forever.

The lease closes that route by serializing per RESOLVED session_id: it is
acquired after session resolution is final (post ``switch_session``/tip-walk),
immediately before the transcript load, and released in the dispatch layer's
``finally`` on every exit path. Same-key messages never reach the acquisition
point while a turn runs (both routing-key guards hold them), so the lock is
uncontended everywhere except the alias-key route — where the second turn now
waits for the first turn's flush and logs one WARNING naming the session and
both routing keys (pairing with the cross-agent tripwire in
``agent/agent_runtime_helpers.note_turn_start``).

Safety properties:

- **Generation-scoped, identity-checked release.** A token records its owner
  (routing key, run generation) and release only frees the lease when that
  exact token is the current holder — a stale unwind can never release a
  newer turn's lease (the #28686 ownership lesson applied). Release is
  idempotent.
- **Fail-closed on timeout.** A timed-out waiter raises
  :class:`TurnLeaseTimeoutError` and must be rejected by the dispatch layer
  with a visible resend notice. It never runs concurrently against the
  still-held lease and therefore cannot defeat the serialization invariant.
- **Bounded registry.** The per-session lease map is size-capped; eviction
  only ever removes idle (unheld, uncontended) entries, never a live lease.

Known limits (deliberate, flagged on #64934):

- A CLI process sharing the session via CLI-continuity is outside any
  in-process lock — that pair needs a DB-level lease (separate design).
- Mid-turn compression rotation leaves a small alias window: the tip-walk can
  resolve a fresh child id while the parent-holding turn is still in flight.
  The mid-turn binding-sync sites are the right place to alias the lease in a
  follow-up.
"""

import asyncio
import inspect
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Upper bound on tracked per-session leases. Idle entries (no holder or
# pending acquire) are evicted oldest-first once the cap is reached; live
# leases are never evicted, so a burst of distinct sessions can transiently
# exceed the cap rather than break serialization.
DEFAULT_MAX_LEASES = 512

# Fallback wait (seconds) when the caller passes no positive timeout. The
# gateway carries this independently through its internal
# HERMES_TURN_LEASE_TIMEOUT bridge because lease contention is not agent
# inactivity. A caller that reaches this bound must reject the turn rather than
# run it concurrently with the holder.
DEFAULT_LEASE_WAIT = 1800.0

# Wait bound (seconds) for a waiter queued behind a STALE-generation holder —
# a turn whose run generation was already invalidated by /stop or /new but
# whose thread is still unwinding (parked in a tool call the cooperative
# interrupt has not reached yet). That holder is a zombie by construction: it
# will never produce user-visible output, so making the next message wait the
# full DEFAULT_LEASE_WAIT is pure dead time. A much shorter bound converts an
# invisible 30-minute stall into a prompt, actionable rejection.
DEFAULT_STALE_LEASE_WAIT = 90.0

# A stale-generation holder that keeps the lease longer than this emits ONE
# structured PHASE=stale_lease_holder line so the class is observable in
# gateway.log instead of silently eating the user's next turn.
STALE_HOLDER_LOG_AFTER = 60.0


def _holder_tool_name(holder: "TurnLeaseToken") -> Optional[str]:
    """Best-effort name of the tool the holder's turn is parked in.

    The dispatch layer may attach a zero-arg callable to the token
    (``tool_name_hint``) that reads the live agent's ``_current_tool``. Purely
    diagnostic: any failure yields ``None`` and the detector line says
    ``tool=unknown``.
    """
    hint = getattr(holder, "tool_name_hint", None)
    if hint is None:
        return None
    try:
        value = hint() if callable(hint) else hint
    except Exception:
        return None
    return str(value) if value else None


async def _invoke_stale_notice(callback: Callable[..., Any], **kwargs: Any) -> None:
    """Fire the stale-holder notice callback, sync or async, never raising.

    Delivering a user-visible notice must never be able to break lease
    acquisition — a failed notice degrades observability, not correctness.
    """
    try:
        result = callback(**kwargs)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.debug("stale-lease notice callback failed", exc_info=True)


class TurnLeaseTimeoutError(TimeoutError):
    """The session lease stayed held for the caller's full wait budget.

    This is a fail-closed signal: the caller did not acquire the lease and
    must not enter the transcript load/run/flush region for this turn.
    """

    def __init__(
        self,
        session_id: str,
        *,
        owner_key: str,
        generation: int,
        wait_seconds: float,
    ) -> None:
        self.session_id = session_id
        self.owner_key = owner_key
        self.generation = generation
        self.wait_seconds = wait_seconds
        super().__init__(
            f"turn lease wait timed out after {wait_seconds:.0f}s on session "
            f"{session_id} for routing key {owner_key} (gen {generation})"
        )


class TurnLeaseToken:
    """Handle returned by :meth:`SessionTurnLeaseRegistry.acquire`.

    A timeout raises :class:`TurnLeaseTimeoutError` instead of returning a
    token, so every token handed out is a held lease. ``released`` makes
    release idempotent.
    """

    __slots__ = (
        "session_id",
        "owner_key",
        "generation",
        "released",
        "tool_name_hint",
    )

    def __init__(
        self,
        session_id: str,
        owner_key: str,
        generation: int,
    ) -> None:
        self.session_id = session_id
        self.owner_key = owner_key
        self.generation = int(generation)
        self.released = False
        # Optional zero-arg callable set by the dispatch layer so the
        # PHASE=stale_lease_holder detector can name the tool this turn is
        # parked in. Diagnostic only; never read on any correctness path.
        self.tool_name_hint: Optional[Callable[[], Optional[str]]] = None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"TurnLeaseToken(session_id={self.session_id!r}, "
            f"owner_key={self.owner_key!r}, generation={self.generation}, "
            f"released={self.released})"
        )


class _SessionLease:
    __slots__ = (
        "lock",
        "holder",
        "acquired_at",
        "last_used",
        "pending_acquires",
        "stale_logged",
    )

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.holder: Optional[TurnLeaseToken] = None
        self.acquired_at = 0.0
        self.last_used = time.time()
        self.pending_acquires = 0
        # One-shot latch for the PHASE=stale_lease_holder detector line, so a
        # long zombie drain logs once rather than once per poll.
        self.stale_logged = False

    @property
    def idle(self) -> bool:
        """True when this lease can be evicted: nobody holds or awaits it."""
        return (
            self.holder is None
            and not self.lock.locked()
            and self.pending_acquires == 0
        )


class SessionTurnLeaseRegistry:
    """Asyncio lease per resolved session_id serializing transcript turns.

    Process-local and single-event-loop by design — the same visibility scope
    as the routing-key guards it extends. All methods must be called from the
    gateway's event loop.
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_LEASES,
        *,
        is_generation_current: Optional[Callable[[str, int], bool]] = None,
        stale_wait: float = DEFAULT_STALE_LEASE_WAIT,
    ) -> None:
        self._leases: Dict[str, _SessionLease] = {}
        self._max_entries = max(1, int(max_entries))
        # Predicate supplied by the gateway runner
        # (``GatewayRunner._is_session_run_current``) answering "is this
        # holder's (routing key, generation) still the session's current
        # run?". False means the holder was already /stop'd or /new'd and is
        # merely draining — a ZOMBIE, not a live alias-key turn. Optional so
        # the registry stays usable standalone (tests, embedders): with no
        # predicate every holder is treated as live, i.e. the pre-fix
        # behavior.
        self._is_generation_current = is_generation_current
        self._stale_wait = (
            float(stale_wait) if stale_wait and stale_wait > 0
            else DEFAULT_STALE_LEASE_WAIT
        )

    def __len__(self) -> int:
        return len(self._leases)

    def _get_or_create(self, session_id: str) -> _SessionLease:
        lease = self._leases.get(session_id)
        if lease is None:
            self._evict_idle()
            lease = _SessionLease()
            self._leases[session_id] = lease
        lease.last_used = time.time()
        return lease

    def _evict_idle(self) -> None:
        """Drop oldest idle entries so a new lease fits under the cap.

        Never evicts a held or contended lease — correctness beats the cap.
        """
        overflow = len(self._leases) - self._max_entries + 1
        if overflow <= 0:
            return
        idle_ids = sorted(
            (sid for sid, lease in self._leases.items() if lease.idle),
            key=lambda sid: self._leases[sid].last_used,
        )
        for sid in idle_ids[:overflow]:
            self._leases.pop(sid, None)

    def _holder_is_stale(self, holder: Optional[TurnLeaseToken]) -> bool:
        """True when ``holder``'s run generation is no longer current.

        A stale holder is a turn whose generation was invalidated by /stop or
        /new while its thread was still parked in a tool call — the zombie
        case. Fail-open: with no predicate, or if the predicate raises, treat
        the holder as LIVE so a predicate bug can never shorten the wait for a
        genuinely-running turn.
        """
        predicate = self._is_generation_current
        if holder is None or predicate is None:
            return False
        try:
            return not bool(predicate(holder.owner_key, holder.generation))
        except Exception:
            logger.debug("turn lease currency predicate failed", exc_info=True)
            return False

    def _log_stale_holder(
        self,
        session_id: str,
        lease: _SessionLease,
        holder: TurnLeaseToken,
        held: float,
    ) -> None:
        """Emit the one-shot structured detector line for a zombie holder.

        Machine-greppable by design: the class was invisible in gateway.log
        for 16 minutes during the 2026-09-20 incident because the only signal
        was a WARNING whose text blamed alias routing keys.
        """
        if lease.stale_logged or held < STALE_HOLDER_LOG_AFTER:
            return
        lease.stale_logged = True
        logger.warning(
            "PHASE=stale_lease_holder session=%s key=%s gen=%s held=%.0fs tool=%s",
            session_id,
            holder.owner_key,
            holder.generation,
            held,
            _holder_tool_name(holder) or "unknown",
        )

    async def acquire(
        self,
        session_id: str,
        *,
        owner_key: str,
        generation: int,
        timeout: Optional[float] = None,
        on_stale_holder: Optional[Callable[..., Any]] = None,
    ) -> Optional[TurnLeaseToken]:
        """Acquire the turn lease for ``session_id``, waiting if held.

        Returns a held :class:`TurnLeaseToken`. Raises
        :class:`TurnLeaseTimeoutError` when the wait budget expires; the caller
        must reject rather than enter the serialized region. Returns ``None``
        for a falsy ``session_id``.

        Two kinds of contention are distinguished (#64934 / the 2026-09-20
        zombie-lease incident):

        * **Alias-key contention** — the holder's generation is still current,
          so a genuinely-live turn on a second routing key is running. Wait the
          full ``timeout``; this is the case the lease was built for.
        * **Stale holder** — the holder's generation was already invalidated
          (/stop, /new) and its thread is merely draining a tool call. The
          holder will never emit user-visible output, so waiting the full
          budget strands the user's next message in silence. Wait only
          ``stale_wait``, log the HONEST reason (same routing key, stopped
          generation, still draining — NOT two aliased keys), fire
          ``on_stale_holder`` so the dispatch layer can send an immediate
          user-visible "your message is queued" notice, and emit the
          ``PHASE=stale_lease_holder`` detector line once past
          ``STALE_HOLDER_LOG_AFTER``.
        """
        if not session_id:
            return None
        wait = float(timeout) if timeout and timeout > 0 else DEFAULT_LEASE_WAIT
        token = TurnLeaseToken(session_id, owner_key, int(generation))
        lease = self._get_or_create(session_id)

        if lease.lock.locked():
            holder = lease.holder
            held = time.time() - lease.acquired_at if lease.acquired_at else -1.0
            stale = self._holder_is_stale(holder)
            if stale and holder is not None:
                # Bound the wait to the stale budget — never EXTEND a caller's
                # own shorter timeout.
                wait = min(wait, self._stale_wait)
                logger.warning(
                    "turn lease held by a STALE turn on session %s: routing "
                    "key %s (gen %s) is waiting behind routing key %s "
                    "(gen %s, held %.0fs) whose generation was already "
                    "invalidated — that turn was stopped and is still "
                    "draining a tool call, so this is NOT alias-key "
                    "contention; waiting at most %.0fs before rejecting",
                    session_id,
                    owner_key,
                    generation,
                    holder.owner_key,
                    holder.generation,
                    held,
                    wait,
                )
                self._log_stale_holder(session_id, lease, holder, held)
                if on_stale_holder is not None:
                    await _invoke_stale_notice(
                        on_stale_holder,
                        session_id=session_id,
                        owner_key=holder.owner_key,
                        generation=holder.generation,
                        held_seconds=held,
                        wait_seconds=wait,
                    )
            else:
                logger.warning(
                    "turn lease contention on session %s: routing key %s (gen %s) "
                    "waiting behind in-flight turn held by routing key %s (gen %s, "
                    "held %.0fs) — two routing keys are mapped to one session_id "
                    "(#64934); serializing this turn behind the previous turn's "
                    "flush",
                    session_id,
                    owner_key,
                    generation,
                    holder.owner_key if holder else "?",
                    holder.generation if holder else "?",
                    held,
                )

        # Lock.release() wakes a waiter while leaving the lock momentarily
        # unlocked. Track every in-progress acquire across that handoff so
        # eviction cannot orphan the old lock and create a second lock for the
        # same session. Count even apparently-uncontended acquires: wait_for()
        # may schedule them before the underlying lock coroutine runs.
        lease.pending_acquires += 1
        try:
            await asyncio.wait_for(lease.lock.acquire(), timeout=wait)
        except asyncio.TimeoutError:
            holder = lease.holder
            logger.error(
                "turn lease wait timed out after %.0fs on session %s "
                "(waiter: routing key %s gen %s; holder: routing key %s "
                "gen %s) — failing closed: refusing to run this turn "
                "UNSERIALIZED against the still-held lease",
                wait,
                session_id,
                owner_key,
                generation,
                holder.owner_key if holder else "?",
                holder.generation if holder else "?",
            )
            raise TurnLeaseTimeoutError(
                session_id,
                owner_key=owner_key,
                generation=generation,
                wait_seconds=wait,
            ) from None
        finally:
            lease.pending_acquires -= 1

        # The lock is held and there is no await before holder publication, so
        # the lease cannot become evictable after the pending count is cleared.
        lease.holder = token
        lease.acquired_at = time.time()
        lease.last_used = lease.acquired_at
        return token

    def rebind(self, token: Optional[TurnLeaseToken], new_session_id: str) -> bool:
        """Alias a HELD lease onto ``new_session_id`` after mid-turn rotation.

        Compression can rotate the durable session_id while a turn is in
        flight (session-hygiene pre-compression, in-agent compression). The
        turn's flush then targets the NEW id — so the serialization boundary
        must follow it, or an alias routing key resolving the new id (e.g. a
        topic tip-walk landing on the fresh child) could start a concurrent
        turn the lease never sees. This closes the rotation-alias window
        flagged on #64934.

        Mechanism: the SAME ``_SessionLease`` object is registered under the
        new id (the old mapping stays until it goes idle and is evicted), so
        acquirers on either id serialize against one lock — no lock state is
        moved, no asyncio internals are touched. Only the current holder can
        rebind (identity-checked like release), and the token follows to the
        new id so release frees the shared object.

        Edge: if the new id already has a live lease of its own (another
        turn is running on the target session), the two serialization
        domains cannot be merged mid-wait — log loudly and keep the token on
        the old id. Fail-open, never deadlock: a holder cannot wait mid-turn.
        """
        if (
            token is None
            or token.released
            or not new_session_id
            or new_session_id == token.session_id
        ):
            return False
        lease = self._leases.get(token.session_id)
        if lease is None or lease.holder is not token:
            return False

        existing = self._leases.get(new_session_id)
        if existing is not None and existing is not lease and not existing.idle:
            holder = existing.holder
            logger.warning(
                "turn lease rebind blocked: session %s rotated to %s mid-turn "
                "(holder: routing key %s gen %s) but the target session's "
                "lease is already live (holder: routing key %s gen %s) — "
                "keeping the lease on the old id; transcript writes on %s "
                "may interleave (#64934 rotation-alias edge)",
                token.session_id,
                new_session_id,
                token.owner_key,
                token.generation,
                holder.owner_key if holder else "?",
                holder.generation if holder else "?",
                new_session_id,
            )
            return False

        self._leases[new_session_id] = lease
        lease.last_used = time.time()
        token.session_id = new_session_id
        return True

    def release(self, token: Optional[TurnLeaseToken]) -> bool:
        """Release ``token``'s lease. Idempotent; ownership-checked.

        Returns True only when this exact token was the current holder and
        the lock was freed. A re-release or a stale token whose slot has
        since been granted to a newer turn are both safe no-ops — a stale
        unwind can never release a newer turn's lease.
        """
        if token is None or token.released:
            return False
        token.released = True
        lease = self._leases.get(token.session_id)
        if lease is None:
            return False
        if lease.holder is not token:
            logger.debug(
                "turn lease release skipped on session %s: token (key %s "
                "gen %s) is not the current holder",
                token.session_id,
                token.owner_key,
                token.generation,
            )
            return False
        lease.holder = None
        lease.acquired_at = 0.0
        lease.last_used = time.time()
        # Re-arm the detector: a LATER zombie on this session must log again.
        lease.stale_logged = False
        if lease.lock.locked():
            lease.lock.release()
        return True
