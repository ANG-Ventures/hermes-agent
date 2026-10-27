"""Goal/heartbeat continuation, post-turn hooks and loop-wakeup watcher methods for GatewayRunner.

Split out of ``gateway/run.py``; bound onto ``GatewayRunner`` via the MRO.
``gateway.run`` internals are imported lazily inside method bodies (import cycle),
so ``patch("gateway.run.X")`` keeps intercepting them at call time.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import logging
import time
from contextlib import nullcontext, suppress
from typing import TYPE_CHECKING, Any, Optional

from gateway.platforms.event import MessageEvent, MessageType

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401
    from gateway.run_turn_runner import TurnRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

# True while the post-turn hooks run as the adapter's post-delivery callback: that slot has already
# been popped, so a /goal status notice is sent directly (registering would fire on the NEXT turn).
_AFTER_DELIVERY: contextvars.ContextVar[bool] = contextvars.ContextVar("post_turn_after_delivery", default=False)


class GatewayGoalsMixin:
    """Goal/heartbeat continuation, post-turn hooks and loop-wakeup watcher methods for GatewayRunner."""

    # ── /goal — persistent cross-turn goals (Ralph-style loop) ──────────
    def _goals_config(self) -> dict:
        """The top-level ``goals`` config block. GatewayRunner.config is a GatewayConfig dataclass, so
        the block is only reachable via hermes_cli.config.load_config()."""
        try:
            goals_cfg = (
                (self.config or {}).get("goals", {})
                if isinstance(self.config, dict)
                else getattr(self.config, "goals", {}) or {}
            )
            if not goals_cfg:
                from hermes_cli.config import load_config

                goals_cfg = (load_config() or {}).get("goals") or {}
            return goals_cfg if isinstance(goals_cfg, dict) else {}
        except Exception:
            return {}

    def _goal_max_turns_from_config(self) -> int:
        """Configured /goal turn budget (default 20)."""
        try:
            return int(self._goals_config().get("max_turns", 20) or 20)
        except Exception:
            return 20

    def _goal_int_setting(self, key: str, default: int) -> int:
        """``goals.<key>`` as a non-negative int; ``default`` when unset or malformed (0 is honored)."""
        try:
            value = self._goals_config().get(key, default)
            return max(0, int(default if value is None else value))
        except Exception:
            return default

    async def _warm_goals_session_db(self, label: str) -> None:
        """Warm the goals SessionDB cache off-loop (best-effort): a cold cache runs the state.db
        init on the loop thread and freezes the loop. The executor hop keeps the profile home
        override alive under multiplex; a failed warm-up is a bounded stall, never a crash."""
        try:
            from hermes_cli.goals import _get_session_db as _warm_goals_db

            await self._run_in_executor_with_context(_warm_goals_db)
        except Exception as exc:
            logger.warning("%s: session DB warm-up failed: %s", label, exc)

    async def _claim_off_loop(self, claim, give_back):
        """Run a state.db CLAIM (``claim()``: a heartbeat fire, a /loop tick) in the executor and return
        its result. A worker thread cannot be stopped: if the caller is cancelled while the claim waits on
        the SessionDB lock, the thread still persists it, and nobody would dispatch or abandon it (Prism
        fd214c8b7386). So the hop is shielded, and on cancel ``give_back(result)`` runs off the loop once
        the claim lands, under the caller's contextvars (profile home)."""
        hop = asyncio.ensure_future(self._run_in_executor_with_context(claim))
        try:
            return await asyncio.shield(hop)
        except asyncio.CancelledError:
            ctx = contextvars.copy_context()

            def _orphaned(done):
                if done.cancelled() or done.exception() is not None:
                    return
                result = done.result()

                def _run():
                    try:
                        give_back(result)
                    except Exception:
                        logger.warning("giving back an orphaned claim failed", exc_info=True)

                with suppress(RuntimeError):  # loop already closed: nothing left to run it on
                    done.get_loop().run_in_executor(None, ctx.run, _run)

            hop.add_done_callback(_orphaned)
            raise

    async def _session_entry_for_manager(self, event: "MessageEvent", label: str):
        """Session entry for a /goal or /heartbeat manager, or None when lookup fails. Warms the
        SessionDB cache first (a cold cache drops the first write while the reply claims it was
        set). Internal events never touch activity (idle/daily reset clock)."""
        await self._warm_goals_session_db(label)
        try:
            session_entry = await self.async_session_store.get_or_create_session(
                event.source, touch_activity=not bool(getattr(event, "internal", False)),
            )
        except Exception as exc:
            logger.debug("%s: session lookup failed: %s", label, exc)
            return None
        return session_entry if getattr(session_entry, "session_id", None) else None

    async def _manager_for_event(self, event: "MessageEvent", kind: str, load):
        """``(manager, session_entry)`` for *kind* ("goal"/"heartbeat"), or ``(None, None)``.
        ``load()`` imports the manager class and returns a ``session_id -> manager`` factory."""
        try:
            factory = load()
        except Exception as exc:
            logger.debug("%s manager unavailable: %s", kind, exc)
            return None, None
        session_entry = await self._session_entry_for_manager(event, f"{kind} manager")
        if session_entry is None:
            return None, None
        # Construction reads state.db (SessionDB.get_meta on the db lock): off the loop thread.
        return await self._run_in_executor_with_context(factory, session_entry.session_id), session_entry

    async def _get_goal_manager_for_event(self, event: "MessageEvent"):
        """Return ``(GoalManager, session_entry)`` for this event, or ``(None, None)``."""
        def _load():
            from hermes_cli.goals import GoalManager
            max_turns = self._goal_max_turns_from_config()
            return lambda sid: GoalManager(session_id=sid, default_max_turns=max_turns)
        return await self._manager_for_event(event, "goal", _load)

    async def _get_heartbeat_manager_for_event(self, event: "MessageEvent"):
        """Return ``(HeartbeatManager, session_entry)`` for this event, or ``(None, None)``."""
        def _load():
            from hermes_cli.heartbeat import HeartbeatManager
            return lambda sid: HeartbeatManager(session_id=sid)
        return await self._manager_for_event(event, "heartbeat", _load)

    @staticmethod
    def _synthetic_prompt_event(source: Any, text: str, *, internal: bool = False) -> MessageEvent:
        """Build the TEXT event used to inject a goal/heartbeat/loop prompt into a session.

        The stored source's ``message_id`` is the message that registered the watch; a synthetic
        prompt is not a reply to it, so it is dropped or every progress bubble and final reply
        would quote that stale message (Telegram DM topics route anchorless via the topic id).
        """
        source = dataclasses.replace(source, message_id=None) if getattr(source, "message_id", None) else source
        return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, internal=internal)

    def _register_heartbeat_watch(self, quick_key: str, source: Any, session_id: str) -> None:
        """Track the canonical route and start the restart-recoverable poller."""
        watch = getattr(self, "_heartbeat_watch", None)
        if watch is None:
            watch = self._heartbeat_watch = {}
        watch[quick_key] = (source, session_id)
        self._start_heartbeat_poller()

    def _unregister_heartbeat_watch(self, quick_key: str) -> None:
        (getattr(self, "_heartbeat_watch", None) or {}).pop(quick_key, None)

    async def _heartbeat_poll_once(self, watch: dict) -> None:
        """Wake each idle watched session once; leave busy sessions' ticks unclaimed."""
        for quick_key, (source, session_id) in list(watch.items()):
            try:
                with self._profile_scope_for_source(source):
                    await self._heartbeat_poll_watch(watch, quick_key, source, session_id)
            except Exception as exc:
                logger.debug("heartbeat poll for %s failed: %s", quick_key, exc)

    async def _heartbeat_poll_watch(self, watch, quick_key, source, session_id):
        await self._warm_goals_session_db("heartbeat poll")
        store = getattr(self, "session_store", None)
        if store is not None:
            current = store.peek_session_id(quick_key)
            if not current:
                watch.pop(quick_key, None)
                return
            session_id = current
            watch[quick_key] = (source, session_id)
        adapter = self._delivery_adapter_for(source)
        if adapter is None or not adapter._message_handler:
            return
        if (
            self._is_session_running(quick_key)
            or quick_key in adapter._active_sessions
            or self._queue_depth(quick_key, adapter=adapter) > 0
        ):
            return  # keep missed intervals due until user work has drained
        from hermes_cli.heartbeat import HeartbeatManager

        def _claim():
            # load_heartbeat (get_meta) and the due_prompt write both take the SessionDB lock.
            m = HeartbeatManager(session_id=session_id)
            return m, (m.due_prompt() if m.has_heartbeat() else None)

        def _give_back(claimed):
            m, p = claimed
            if p:
                m.abandon_fire()

        mgr, prompt = await self._claim_off_loop(_claim, _give_back)
        if not mgr.has_heartbeat():
            watch.pop(quick_key, None)
            return
        if not prompt:
            return
        event = self._synthetic_prompt_event(source, prompt)
        event.metadata["gateway_session_key"] = quick_key
        event._heartbeat_execution_started = False
        # Provenance read by display_kind_for_event / the turn's quiet surfaces; the event stays
        # non-internal so authorization and the emergency stop still apply.
        event._heartbeat_session_id = session_id
        # A pinned route skips topic recovery: no await between the idle
        # check and adapter claim. FIFO alone never wakes an idle session.
        try:
            await adapter.handle_message(event)
        finally:
            task = getattr(adapter, "_session_tasks", {}).get(quick_key)
            if task is not None:
                from gateway.run_heartbeat_acceptance import settle_heartbeat_attempt
                task.add_done_callback(lambda done: settle_heartbeat_attempt(event, mgr))
            elif quick_key not in adapter._active_sessions:
                await self._run_in_executor_with_context(mgr.abandon_fire)

    def _start_heartbeat_poller(self) -> None:
        """Start the single gateway-wide heartbeat poll task (idempotent)."""
        existing = getattr(self, "_heartbeat_poll_task", None)
        if existing is not None and not existing.done():
            return

        from hermes_cli.heartbeat import POLL_SECONDS

        async def _poll_loop():
            while True:
                await asyncio.sleep(POLL_SECONDS)
                from gateway.run_heartbeat_restore import restore_heartbeat_watches
                await restore_heartbeat_watches(self)
                watch = getattr(self, "_heartbeat_watch", None)
                if watch:
                    await self._heartbeat_poll_once(watch)

        try:
            task = self._heartbeat_poll_task = asyncio.create_task(_poll_loop())
            # PERMANENT once started (infinite loop) — tag it like a _spawn_supervised watcher so
            # _scale_to_zero_has_live_background_work() doesn't treat the gateway as busy forever.
            task._hermes_supervised_watcher = True  # type: ignore[attr-defined]
            _bg = getattr(self, "_background_tasks", None)
            if _bg is not None:
                _bg.add(task)
                task.add_done_callback(_bg.discard)
        except Exception:
            logger.debug("Failed to start heartbeat poller", exc_info=True)

    def _goal_notice_adapter(self, source: Any):
        adapter = self._delivery_adapter_for(source)
        if not adapter:
            logger.debug("goal continuation: no adapter for %s", getattr(source, "platform", None))
        return adapter

    async def _send_goal_status_notice(self, source: Any, message: str) -> None:
        """Send a /goal judge status line back to the originating chat/thread."""
        adapter = self._goal_notice_adapter(source)
        if not adapter:
            return
        metadata = None
        with suppress(Exception):
            metadata = self._thread_metadata_for_source(source)
        result = await adapter.send(source.chat_id, message, metadata=metadata)
        if result is not None and not getattr(result, "success", True):
            logger.warning(
                "goal continuation: status send failed: %s", getattr(result, "error", "unknown error"),
            )

    async def _defer_goal_status_notice_after_delivery(self, source: Any, message: str) -> None:
        """Send a /goal status line after the main response is delivered.

        The adapter sends the agent response after this caller returns, so for reading order use
        its one-shot post-delivery callback when available, else deliver directly (never drop).
        """
        adapter = self._goal_notice_adapter(source)
        if not adapter:
            return

        async def _deliver() -> None:
            try:
                await self._send_goal_status_notice(source, message)
            except Exception as exc:
                logger.warning("goal continuation: status send failed: %s", exc, exc_info=True)

        session_key = None
        with suppress(Exception):
            session_key = self._session_key_for_source(source)
        if session_key and not _AFTER_DELIVERY.get() and hasattr(adapter, "register_post_delivery_callback"):
            try:
                active = getattr(adapter, "_active_sessions", {}).get(session_key)
                generation = getattr(active, "_hermes_run_generation", None) if active is not None else None
                adapter.register_post_delivery_callback(session_key, _deliver, generation=generation)
                return
            except Exception as exc:
                logger.debug("goal continuation: post-delivery callback registration failed: %s", exc)
        await _deliver()

    async def _post_turn_manager(self, session_entry: Any, label: str, module: str, load):
        """Shared head of the post-turn hooks: ``load()`` imports the manager module and returns a
        ``session_id -> manager`` factory; None when unavailable / no session id. Warms the
        SessionDB cache first — a cold cache at the turn boundary drops the read/write."""
        try:
            factory = load()
        except Exception as exc:
            logger.debug("%s: %s module unavailable: %s", label, module, exc)
            return None
        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return None
        await self._warm_goals_session_db(label)
        return await self._run_in_executor_with_context(factory, sid)

    async def _post_turn_goal_continuation(
        self, *, session_entry: Any, source: Any, final_response: str, event: Any = None, goal_pin: Any = None,
    ) -> None:
        """Run the goal judge after a gateway turn (AFTER delivery) and, if still active, enqueue a
        continuation through the adapter FIFO so a simultaneous real user message takes priority."""
        def _load():
            from hermes_cli.goals import GoalManager
            max_turns = self._goal_max_turns_from_config()
            return lambda sid: GoalManager(session_id=sid, default_max_turns=max_turns)

        mgr = await self._post_turn_manager(session_entry, "goal continuation", "goals", _load)
        if mgr is None or not mgr.is_active():
            return

        _bg_procs, _active_deleg = None, 0
        with suppress(Exception):
            from hermes_cli.goals import count_active_delegations, gather_background_processes as _gather_bg
            # Only THIS session's processes (gateway turns register under turn_ctx.session_id):
            # subagents' pollers must not park the parent's goal.
            _bg_procs = _gather_bg(owner_task_id=getattr(session_entry, "session_id", None) or None)
            _active_deleg = count_active_delegations(getattr(session_entry, "session_id", None))

        # judge_goal() is a synchronous aux-LLM HTTP call (10-40 s; would block Discord heartbeats).
        # _run_in_executor_with_context carries the profile secret scope / aux runtime contextvars
        # without which aux credential resolution fails under multiplexing.
        from hermes_cli.goals import (
            DEFAULT_CHECKIN_MINUTES, DEFAULT_MAX_IDLE_CHECKINS, DEFAULT_NO_PROGRESS_TURNS, is_goal_synthetic_text,
        )
        # A turn the goal loop did not start (user message, worker/process result) re-arms idle check-ins.
        _real_event = event is not None and not is_goal_synthetic_text(getattr(event, "text", ""))
        # api_calls > 1 means the model called at least one tool (each tool round is another API call).
        _api_calls = getattr(event, "_goal_turn_api_calls", None) if event is not None else None
        _made_tools = None if _api_calls is None else int(_api_calls) > 1
        _no_progress = self._goal_int_setting("no_progress_turns", DEFAULT_NO_PROGRESS_TURNS)
        # Continuation gate (Ace 2026-10-22, option b): only a write turn or a human message re-fires
        # the continuation; wakes/pages/NO_REPLY answered with reads leave it to the idle check-in.
        _effect = getattr(event, "_goal_turn_effect", None) if event is not None else None
        _user_spoke = bool(
            _real_event and not getattr(event, "internal", False)
            and not getattr(getattr(event, "source", None), "is_bot", False)
        )
        _nudge_read_only = bool(self._goals_config().get("nudge_on_read_only_turns", False))
        # The hold targets wakes and pages, not the loop's own heartbeat: a continuation/check-in turn
        # the goal fired itself is exempt (it answered the loop, so the loop keeps going).
        _goal_turn = event is not None and not _real_event
        _checkin = self._goal_int_setting("checkin_minutes", DEFAULT_CHECKIN_MINUTES)
        _max_checkins = self._goal_int_setting("max_idle_checkins", DEFAULT_MAX_IDLE_CHECKINS)
        decision = await self._run_in_executor_with_context(
            lambda: mgr.evaluate_after_turn(
                final_response or "", **({"baseline": goal_pin[0]} if goal_pin else {}), user_initiated=True, background_processes=_bg_procs,
                active_delegations=_active_deleg, real_event=_real_event,
                made_tool_calls=_made_tools, no_progress_turns=_no_progress,
                turn_effect=_effect, user_spoke=_user_spoke or _goal_turn,
                nudge_on_read_only_turns=_nudge_read_only, checkin_minutes=_checkin, max_checkins=_max_checkins,
            ),
        )
        _sid = getattr(session_entry, "session_id", "?")
        if decision.get("held"):
            logger.info(
                "goal continuation held for %s: turn effect %s, user_spoke=%s (next nudge: idle check-in)",
                _sid, _effect, _user_spoke,
            )
        elif decision.get("should_continue"):
            logger.info(
                "goal continuation queued for %s: turn effect %s, user_spoke=%s, goal_turn=%s%s",
                _sid, _effect, _user_spoke, _goal_turn,
                f" ({decision['hold_overridden']})" if decision.get("hold_overridden") else "",
            )
        msg = decision.get("message") or ""
        # Deferred until the visible final response is delivered, else "✓ Goal achieved" precedes it.
        if msg and source is not None:
            await self._defer_goal_status_notice_after_delivery(source, msg)
        prompt = decision.get("continuation_prompt") or ""
        if not decision.get("should_continue") or not prompt or source is None:
            return
        # A /goal pause, clear or replacement that landed during the status send wins: enqueue only onto
        # the exact row this evaluation wrote (a re-set goal restarts its revision, so compare the text).
        from hermes_cli.goals import _load_goal_raw
        try:
            _, raw_now = await self._run_in_executor_with_context(
                lambda: _load_goal_raw(mgr.session_id, strict=True))
        except Exception as exc:
            logger.warning("goal continuation for %s dropped: goal re-read failed: %s", _sid, exc)
            return
        if raw_now is None or raw_now != mgr._stored:
            logger.info("goal continuation for %s dropped: goal changed during the status send", _sid)
            return
        # Enqueue via the adapter's FIFO so a user message already in flight preempts naturally.
        try:
            adapter = self._delivery_adapter_for(source)
            _quick_key = self._session_key_for_source(source)
            if adapter and _quick_key:
                # Synthetic continuation from the goal judge — not authored by the human. Without
                # this flag the shared-multi-user sender prefix would stamp "[<user>] " onto it
                # (impersonation; same class as the boot-resume ghost message, 2026-07-10).
                self._enqueue_fifo(
                    _quick_key, self._synthetic_prompt_event(source, prompt, internal=True), adapter,
                )
        except Exception as exc:
            logger.debug("goal continuation: enqueue failed: %s", exc)

    async def _post_turn_hooks_after_delivery(
        self, session_key: str, generation: int, *, agent_result: Any, source: Any, is_internal: bool,
        event: Any = None,
    ) -> None:
        """Run the post-turn hooks (the /goal judge) AFTER the reply is sent (t_3acac80f): as the
        adapter's post-delivery callback, which fires in the session task's finally after the send and
        before the session guard is released. The session stays busy while the judge runs, so a new
        message queues behind it and /stop cancels it with the turn. Inline when this turn is not the
        adapter's session task (a /retry or handoff turn) or the adapter has no callback slot."""
        kwargs = dict(agent_result=agent_result, source=source, is_internal=is_internal, event=event)
        adapter = self._delivery_adapter_for(source) if session_key else None
        owned = adapter is not None and getattr(adapter, "_session_tasks", {}).get(session_key) is asyncio.current_task()
        if not (owned and hasattr(adapter, "register_post_delivery_callback")):
            try:
                await self._run_post_turn_hooks(**kwargs)
            except Exception as exc:
                logger.debug("post-turn hook failed: %s", exc)
            return
        # /loop completion stays inline, before the send, while this turn still owns the session: deferred,
        # it would complete a wakeup tick fired during the send (bookkeeping, no LLM call). Only the judge waits.
        try:  # the class method: an instance override of _run_post_turn_hooks stands for the deferred judge
            await GatewayGoalsMixin._run_post_turn_hooks(self, **kwargs, parts=("loop completion",))
        except Exception as exc:
            logger.debug("post-turn hook failed: %s", exc)
        # Pin the goal row as of turn end: a /goal set during the send is not judged on this reply.
        with suppress(Exception):  # a failed read leaves it unpinned, never pinned as "no goal"
            from hermes_cli.goals import _load_goal_raw
            sid = self.session_store.peek_session_id(session_key)
            if isinstance(sid, str) and sid:
                kwargs["goal_pin"] = ((await self._run_in_executor_with_context(
                    lambda: _load_goal_raw(sid, strict=True)))[1],)

        # The routed profile scope (home/secret/terminal contextvars) is left before the adapter fires the
        # callback; run the hooks in the context captured here so they read the routed profile's state.db.
        ctx = contextvars.copy_context()

        async def _run() -> None:
            _AFTER_DELIVERY.set(True)  # inside the task's copy of ctx only
            await self._run_post_turn_hooks(**kwargs, parts=("goal continuation",))

        async def _hooks() -> None:
            task = asyncio.current_task()
            if not self._is_session_run_current(session_key, generation) or (task is not None and task.cancelling()):
                logger.info("post-turn hooks for %s skipped: turn was stopped before they ran", session_key)
                return
            started = time.monotonic()
            try:
                await asyncio.create_task(_run(), context=ctx)  # cancelling this await cancels the task
            finally:
                if (took := time.monotonic() - started) > 2.0:
                    logger.info("PHASE=post_turn_hooks session=%s s=%.2f (after delivery)", session_key, took)

        # Unbounded, as the inline await was: the judge and the gates (300 s each) carry their own timeouts.
        _hooks.post_delivery_timeout = None
        adapter.register_post_delivery_callback(session_key, _hooks, generation=generation)

    async def _run_post_turn_hooks(
        self, *, agent_result: Any, source: Any, is_internal: bool, event: Any = None, goal_pin: Any = None,
        parts: tuple = ("goal continuation", "loop completion"),
    ) -> None:
        """Run goal and loop bookkeeping after an agent turn returns."""
        final_text = self._final_text_for_post_turn_hooks(agent_result, event)
        try:
            session_entry = await self.async_session_store.get_or_create_session(
                source, touch_activity=not is_internal,
            )
        except Exception as exc:
            logger.debug("post-turn session resolution failed: %s", exc)
            return
        # Empty interrupted/errored responses must not drive /goal, but an in-flight /loop tick
        # still needs to be released and rescheduled.
        hooks = [("loop completion", self._post_turn_loop_completion)]
        if final_text.strip():
            hooks.insert(0, ("goal continuation", self._post_turn_goal_continuation))
        for label, hook in hooks:
            if label not in parts:
                continue
            try:
                extra = {"event": event, "goal_pin": goal_pin} if label == "goal continuation" else {}
                await hook(session_entry=session_entry, source=source, final_response=final_text, **extra)
            except Exception as exc:
                logger.debug("%s hook failed: %s", label, exc)

    @staticmethod
    def _final_text_for_post_turn_hooks(agent_result, event=None) -> str:
        """Text for /goal and /loop after a gateway turn. Streamed turns return None from
        _handle_message_with_agent (already_sent); the delivered reply is stashed on the event."""
        text = ""
        if isinstance(agent_result, dict):
            text = str(agent_result.get("final_response") or "")
        elif isinstance(agent_result, str):
            text = agent_result
        if text.strip():
            return text
        streamed = getattr(event, "_streamed_final_response", None)
        return streamed if isinstance(streamed, str) and streamed.strip() else text

    async def _post_turn_loop_completion(
        self, *, session_entry: Any, source: Any, final_response: str,
    ) -> None:
        """Complete a /loop wakeup tick after a gateway turn. No-op unless a tick is in flight
        (``awaiting_response``, set when the wakeup was injected); applies the LOOP_COMPLETE marker
        / --until judge / caps and schedules the next tick for the idle wakeup watcher."""
        def _load():
            from hermes_cli.loops import LoopManager
            return lambda sid: LoopManager(session_id=sid)

        mgr = await self._post_turn_manager(session_entry, "loop completion", "loops", _load)
        state = mgr.state if mgr is not None else None
        if state is None or not state.awaiting_response:
            return
        # The --until judge is a sync aux-LLM call — keep it off the event loop, but carry the
        # contextvars: a bare executor hop drops the profile HERMES_HOME override and secret scope,
        # so a served secondary's tick would be written into the DEFAULT profile's state.db.
        decision = await self._run_in_executor_with_context(mgr.complete_tick, final_response or "")
        msg = decision.get("message") or ""
        if msg and source is not None:
            await self._defer_goal_status_notice_after_delivery(source, msg)

    async def _loop_wakeup_fire_one(
        self, sid: str, state: Any, now: float, warned_no_route: set, profile: Optional[str] = None,
    ) -> None:
        """Inject one due /loop wakeup into its session, applying every deferral rule. ``profile`` is
        the store being scanned (None = default); a ``profile`` persisted in the route wins."""
        from hermes_cli.loops import LoopManager, goal_blocks_loop_tick

        if state.awaiting_response or now < state.next_due_at:
            return
        route = state.route or {}
        platform_name = route.get("platform", "")
        chat_id = route.get("chat_id", "")
        if not platform_name or not chat_id:
            return  # CLI / TUI-owned loop — their own schedulers drive it.
        profile = route.get("profile") or profile
        # The loop's OWN profile's adapter map, fail closed: ``self.adapters`` is the default profile's,
        # so a secondary session's wakeup would inject via the default bot on a bare chat_id (a
        # Telegram DM lands in the user's chat with the other bot).
        adapters = self._adapters_for_profile(profile)
        adapter = next((a for p, a in adapters.items() if p.value == platform_name), None)
        if adapter is None:
            if sid not in warned_no_route:
                warned_no_route.add(sid)
                logger.debug(
                    "loop wakeup: no adapter for platform %r (session %s, profile %s)", platform_name, sid, profile,
                )
            return

        # Off the loop (Aegis, 2026-09-23): the source resolver reads the session store, which
        # under 6+ live turns convoyed behind the writer lock and blocked the loop for 10-50 s.
        source = await self._run_in_executor_with_context(self._build_process_event_source, {
            "session_key": "",
            "platform": platform_name,
            "chat_id": chat_id,
            **{k: route.get(k, "") for k in ("chat_type", "thread_id", "user_id", "user_name")},
        })
        if source is None:
            return
        if profile and not getattr(source, "profile", None):
            source.profile = profile  # session key + runtime scope of the injected turn
        session_key = None
        with suppress(Exception):
            session_key = self._session_key_for_source(source)
        if session_key and session_key in self._running_agents:
            return  # busy — stays due, next scan retries
        if session_key and self._stop_latched(session_key):
            return  # /stop latched: hold the tick (stays due) instead of firing it into the held queue
        def _load():
            # Both read state.db under the SessionDB lock; a writer holding it would freeze the loop.
            return None if goal_blocks_loop_tick(sid) else LoopManager(session_id=sid)

        mgr = await self._run_in_executor_with_context(_load)
        if mgr is None or not mgr.is_due(now):
            return
        # fire_tick()/complete_tick() are writes (BEGIN IMMEDIATE) taking the SessionDB writer lock; a slow
        # writer elsewhere holding it while the loop thread blocked froze the gateway until the watchdog
        # fired. The context-preserving executor keeps the profile HERMES_HOME override under multiplex.
        wakeup = await self._claim_off_loop(mgr.fire_tick, lambda w: w and mgr.abandon_tick())
        if not wakeup:
            return
        # #85957: after the parent turn's event.complete the CLIENT owns the next turn on this stateless
        # surface. Persist the completion as a durable delivery row — never self-post it as a new role=user
        # prompt.
        # #85957: same client-owns-the-turn rule as the raw-key branch above — persist the completion as a
        # delivery row, never self-post it as a new role=user prompt.
        try:
            logger.info(
                "loop wakeup #%s — injecting for %s chat=%s thread=%s",
                mgr.state.ticks_fired if mgr.state else "?",
                platform_name, source.chat_id, source.thread_id,
            )
            await adapter.handle_message(self._synthetic_prompt_event(source, wakeup, internal=True))
            # Slash-command loops dispatch through the command path and never hit the post-turn
            # completion hook — complete the tick immediately (caps + scheduling).
            if wakeup.lstrip().startswith("/"):
                await self._run_in_executor_with_context(mgr.complete_tick, "")
        except Exception as exc:
            logger.warning("loop wakeup injection failed for %s: %s", sid, exc)
            with suppress(Exception):
                await self._run_in_executor_with_context(mgr.abandon_tick)

    async def _loop_wakeup_watcher(self, interval: float = 15.0) -> None:
        """Fire due /loop wakeups for idle gateway sessions: a coarse ticker scans persisted loops
        (SessionDB ``loop:*`` rows) and injects each due prompt via the synthetic-message path.
        Deferrals: session running a turn (FIFO would race the live turn); active non-parked /goal
        (goal owns the idle boundary); no routing metadata (one-time warning).

        Multiplex: one gateway-wide task, so ``list_active_loops`` alone reads only the launch home's
        store — a ``/loop`` set from a secondary profile's chat would never fire. Every served
        profile's store is scanned under its own runtime scope (same shape as ``_handoff_watcher``),
        and each hit is fired against that profile's adapters."""
        from gateway.run import _async_profile_runtime_scope, _resolve_handoff_watch_scopes
        from gateway.run_idle_gates import profile_has_active_loop
        await asyncio.sleep(5)  # let platforms finish connecting
        warned_no_route: set = set()

        def _scope(profile_home):
            # profile_home None = the launch profile's own store; once the process multiplexes it
            # binds its own scope instead of running on ambient env (see _scope_or_null).
            if profile_home is not None:
                return _async_profile_runtime_scope(profile_home)
            from tui_gateway.launch_profile_policy import async_launch_profile_scope_if_multiplexed
            return async_launch_profile_scope_if_multiplexed()

        async def _scan_one_store(profile_name: Optional[str]) -> None:
            from hermes_cli.loops import list_active_loops

            # Warm once per scan: the scan reads every persisted loop and a cold cache would
            # run the state.db init on the loop thread before the first read.
            await self._warm_goals_session_db("loop wakeup")
            # Off-loop too: the read is lock-free under WAL but convoys on the writer lock without it.
            active_loops = await self._run_in_executor_with_context(list_active_loops)
            now = time.time()
            for sid, state in active_loops:
                await self._loop_wakeup_fire_one(sid, state, now, warned_no_route, profile_name)

        while self._running:
            try:
                # Multiplex resolution walks the filesystem off-loop; a stalled walk on the loop
                # trips the loop-liveness watchdog (exit 75).
                for profile_name, profile_home in await _resolve_handoff_watch_scopes(self):
                    # Idle gate (run_idle_gates): skip the scope entry when the profile's store holds
                    # no active loop. The root scan (None) is unscoped and stays cheap.
                    if profile_home is not None and not await self._run_in_executor_with_context(
                            profile_has_active_loop, profile_home):
                        continue
                    async with _scope(profile_home):
                        await _scan_one_store(profile_name)
            except Exception as exc:
                logger.debug("loop wakeup watcher error: %s", exc)
            await asyncio.sleep(interval)

    # ── /goal idle wakes (gateway safety net) ───────────────────────────

    async def _goal_idle_wake_watcher(self, interval: float = 15.0) -> None:
        """Start a turn for an IDLE session whose active /goal would otherwise sit until an unrelated
        message: a timed park whose deadline passed, a process park past its 30-min cap, or a quiet
        unparked goal (Claude-Code-style check-ins: ``goals.checkin_minutes`` (30), doubling to 4x,
        at most ``goals.max_idle_checkins`` (3) between real events; ``checkin_minutes: 0`` = off).
        The CLI has its own idle hook (``_maybe_resume_parked_goal``); this is the gateway's."""
        from gateway.run import _async_profile_runtime_scope, _resolve_handoff_watch_scopes
        await asyncio.sleep(5)  # let platforms finish connecting

        def _scope(profile_home):
            if profile_home is not None:
                return _async_profile_runtime_scope(profile_home)
            from tui_gateway.launch_profile_policy import async_launch_profile_scope_if_multiplexed
            return async_launch_profile_scope_if_multiplexed()

        while self._running:
            try:
                for _profile_name, profile_home in await _resolve_handoff_watch_scopes(self):
                    async with _scope(profile_home):
                        await self._goal_idle_wake_scan()
            except Exception as exc:
                logger.debug("goal idle-wake watcher error: %s", exc)
            await asyncio.sleep(interval)

    async def _goal_idle_wake_scan(self) -> None:
        from hermes_cli.goals import (
            DEFAULT_CHECKIN_MINUTES, DEFAULT_MAX_IDLE_CHECKINS, list_active_goal_session_ids,
        )

        checkin_minutes = self._goal_int_setting("checkin_minutes", DEFAULT_CHECKIN_MINUTES)
        if not checkin_minutes:
            return
        max_checkins = self._goal_int_setting("max_idle_checkins", DEFAULT_MAX_IDLE_CHECKINS)
        await self._warm_goals_session_db("goal idle wake")
        sids = set(await self._run_in_executor_with_context(list_active_goal_session_ids))
        if not sids:
            return
        store = getattr(self, "session_store", None)
        if store is None:
            return
        entries = await self._run_in_executor_with_context(store.list_sessions)
        for entry in entries:
            if entry.session_id not in sids or entry.origin is None or getattr(entry, "suspended", False):
                continue
            try:
                await self._goal_idle_wake_one(entry, checkin_minutes, max_checkins)
            except Exception as exc:
                logger.debug("goal idle wake for %s failed: %s", entry.session_key, exc)

    def _stop_latched(self, session_key: str) -> bool:
        """True while the user's /stop holds automatic turns for this session (in-memory latch)."""
        try:
            return self._session_state(session_key).conversation.automatic_turns_paused is True
        except Exception:
            return False

    async def _goal_idle_wake_one(self, entry: Any, checkin_minutes: int, max_checkins: int) -> None:
        quick_key, session_id = entry.session_key, entry.session_id
        source = self._restored_source(entry)
        if source is None:
            return
        adapter = self._delivery_adapter_for(source)
        if adapter is None or not getattr(adapter, "_message_handler", None):
            return
        if (
            quick_key in getattr(self, "_running_agents", {})
            or quick_key in adapter._active_sessions
            or self._queue_depth(quick_key, adapter=adapter) > 0
        ):
            return  # busy: the running turn's own post-turn judge owns the next step
        if getattr(entry, "user_stopped_at", None) is not None or self._stop_latched(quick_key):
            return  # /stop latched: only a human turn resumes automatic work
        max_turns = self._goal_max_turns_from_config()

        def _wake():
            from hermes_cli.goals import GoalManager

            # GoalManager() loads the goal via SessionDB.get_meta, which waits on the db lock while
            # another thread writes: build it in the hop with idle_wake_prompt, never on the loop.
            mgr = GoalManager(session_id=session_id, default_max_turns=max_turns)
            return mgr, mgr.idle_wake_prompt(checkin_minutes=checkin_minutes, max_checkins=max_checkins)

        with self._profile_scope_for_source(source):
            mgr, prompt = await self._run_in_executor_with_context(_wake)
        if not prompt:
            return
        logger.info(
            "goal idle wake #%s for %s (session %s): %s", mgr.state.idle_wakes if mgr.state else "?",
            quick_key, session_id, prompt.splitlines()[0][:80],
        )
        event = self._synthetic_prompt_event(source, prompt, internal=True)
        event.metadata["gateway_session_key"] = quick_key
        await adapter.handle_message(event)
