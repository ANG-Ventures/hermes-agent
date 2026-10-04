# FOLLOWUP deltas for `run_agent.py` (lane L13-root-py, parity 2026-10-01)

Upstream decomposed this file into a facade + `agent/*`/sibling mixins. The fork's changes to the
methods below were made on the pre-decomposition monolith; their owning method now lives in the
module named in each heading (outside lane L13), so the delta was NOT applied. Each block is
`diff -u` of the method body, merge-base -> fork (`-` = base, `+` = fork). Re-thread the `+` lines
into the named module's copy (adapt names: upstream may have refactored the body).

Applied in the facade (not listed): AIAgent.reset_session_state, AIAgent._is_provider_stream_parse_error, AIAgent.get_activity_summary, AIAgent.release_clients, AIAgent.close, AIAgent._dispatch_delegate_task; module-level helpers/imports.


NOTE on the first group: the five module-level fork-only symbols (`_has_mutable_flush_state`,
`_persisted_content_projection`, `_tool_content_mutated_since_flush`, `SwapOutcome`,
`_close_delegated_child`) ARE present in the merged `run_agent.py` facade (re-applied there); they are
listed only so the sibling ports know where to import them from (`from run_agent import ...`, lazily).
`AIAgent._try_activate_fallback` / `AIAgent.switch_model` are now `_forward(...)` stubs that pass
`*args, **kwargs` through, so the fork's extra kwargs (`error_context`, `display_reason`,
`session_reasoning_config`) need NO facade change — only the targets
(`agent/chat_completion_helpers.try_activate_fallback`, `agent/agent_runtime_helpers.switch_model`, lane
L06/L08) must accept them.

Consumers already present in the merged tree that depend on these ports: `SwapOutcome` →
`agent/agent_runtime_helpers.py`, `tools/delegate_tool.py`; `_persist_superseded` → `gateway/run.py`,
`agent/agent_init.py`; `_owner_teardown` → `tools/delegate_tool.py`; `set_blackbox_turn` →
`agent/aux_accounting.py`; `emit_unfinalized_session_end` → `agent/turn_finalizer.py`;
`_last_compaction_aborted` → `gateway/slash_commands.py`; `row_ids_out` / `update_message_finish_reason` /
`recompute_effective_last_active` → `hermes_state.py`.

## target: `NOWHERE (upstream deleted/renamed — find replacement)`

### `_has_mutable_flush_state`  (fork-only symbol)
```diff
--- base::_has_mutable_flush_state
+++ fork::_has_mutable_flush_state
@@ -0,0 +1,5 @@
+def _has_mutable_flush_state(msg: Any) -> bool:
+    """True when *msg* carries a field the fork may have stamped in place."""
+    return isinstance(msg, dict) and any(
+        msg.get(field) is not None for field in _MUTABLE_FLUSH_STATE_FIELDS
+    )
```

### `_persisted_content_projection`  (fork-only symbol)
```diff
--- base::_persisted_content_projection
+++ fork::_persisted_content_projection
@@ -0,0 +1,21 @@
+def _persisted_content_projection(msg: Dict[str, Any], content: Any) -> Any:
+    """The ``content`` value the flush writes for *msg* (image-free text).
+
+    Shared by the first write and the in-place content re-persist so both
+    store the same projection of a multimodal list.
+    """
+    _multimodal_projection = _multimodal_message_text_projection(
+        {**msg, "content": content}
+    )
+    if _multimodal_projection is not None:
+        return _multimodal_projection
+    if isinstance(content, list):
+        # List of OpenAI-style content parts: strip images, keep text.
+        _txt = []
+        for p in content:
+            if isinstance(p, dict) and p.get("type") == "text":
+                _txt.append(str(p.get("text", "")))
+            elif isinstance(p, dict) and p.get("type") in {"image", "image_url", "input_image"}:
+                _txt.append("[screenshot]")
+        return "\n".join(_txt) if _txt else None
+    return content
```

### `_tool_content_mutated_since_flush`  (fork-only symbol)
```diff
--- base::_tool_content_mutated_since_flush
+++ fork::_tool_content_mutated_since_flush
@@ -0,0 +1,15 @@
+def _tool_content_mutated_since_flush(msg: Any, content_refs: Dict[int, Any]) -> bool:
+    """True when a flushed ``role:"tool"`` row's live content was replaced.
+
+    The flush is append-only, but a mid-turn /steer and the run-budget notice
+    append text to the current turn's newest tool result AFTER the sequential
+    executor already flushed it. ``content_refs`` maps the row id to the exact
+    content object that was written; content is replaced (never edited in
+    place), so an identity check is O(1) and catches every such writer.
+    """
+    if not isinstance(msg, dict) or msg.get("role") != "tool":
+        return False
+    row_id = msg.get("_db_persisted_row_id")
+    if not isinstance(row_id, int) or row_id not in content_refs:
+        return False
+    return content_refs[row_id] is not msg.get("content")
```

### `SwapOutcome`  (fork-only symbol)
```diff
--- base::SwapOutcome
+++ fork::SwapOutcome
@@ -0,0 +1,27 @@
+class SwapOutcome(enum.Enum):
+    """Result of ``AIAgent._swap_credential`` — a tri-state, not a bool.
+
+    A credential pool can be exhausted under a cap such that a rotation selects
+    an entry whose ``runtime_api_key`` resolves to ``""``. Building an Anthropic
+    client with an empty key succeeds at construction but raises a
+    ``TypeError("Could not resolve authentication method…")`` at request time,
+    which the conversation loop then misclassifies as a non-retryable local bug
+    and aborts the turn. ``_swap_credential`` must instead refuse to install a
+    keyless client and tell the caller which kind of "no usable key" it saw:
+
+    - ``SWAPPED`` — a usable key was installed; proceed as before.
+    - ``RETRYABLE_EXHAUSTED`` — the entry's key is empty AND the entry was
+      rate-limit-exhausted (a 429) within its cooldown window. This is a
+      transient cap that self-heals on window reset → the caller falls through
+      to the normal rate-limit / cooldown / fallback path (retryable), NOT an
+      abort. The live client is left untouched.
+    - ``MISSING_CREDENTIAL`` — the entry's key is empty and there is no live
+      429 exhaustion (never rate-limited, or the 429 marker is past its TTL).
+      This is a genuine missing/dead credential (a config error) → the caller
+      surfaces a LOUD terminal, never a silent retry. The live client is left
+      untouched.
+    """
+
+    SWAPPED = "swapped"
+    RETRYABLE_EXHAUSTED = "retryable_exhausted"
+    MISSING_CREDENTIAL = "missing_credential"
```

### `_close_delegated_child`  (fork-only symbol)
```diff
--- base::_close_delegated_child
+++ fork::_close_delegated_child
@@ -0,0 +1,21 @@
+def _close_delegated_child(child: Any, reason: str) -> bool:
+    """Hand a delegate_task child to its owner's teardown door, if it has one.
+
+    delegate_task stamps ``_owner_teardown`` on every child it runs. That door
+    defers the close while the child's run or any of its turns is live, so a
+    parent's close()/release_clients() can never close a SessionDB under a
+    running child turn (docs/dev/delegate-child-lifecycle.md, I2). Read from
+    the instance dict: a Mock child must not invent a door. Returns True when
+    the door took the child.
+    """
+    try:
+        door = vars(child).get("_owner_teardown")
+    except TypeError:
+        door = None
+    if not callable(door):
+        return False
+    try:
+        door(reason)
+    except Exception:
+        logger.debug("delegated child teardown door failed", exc_info=True)
+    return True
```

### `AIAgent._try_activate_fallback`
```diff
--- base::AIAgent._try_activate_fallback
+++ fork::AIAgent._try_activate_fallback
@@ -1,4 +1,12 @@
-    def _try_activate_fallback(self, reason: "FailoverReason | None" = None) -> bool:
+    def _try_activate_fallback(
+        self,
+        reason: "FailoverReason | None" = None,
+        error_context: Optional[Dict[str, Any]] = None,
+        *,
+        display_reason: "FailoverReason | None" = None,
+    ) -> bool:
         """Forwarder — see ``agent.chat_completion_helpers.try_activate_fallback``."""
         from agent.chat_completion_helpers import try_activate_fallback
-        return try_activate_fallback(self, reason)
+        return try_activate_fallback(
+            self, reason, error_context=error_context, display_reason=display_reason,
+        )
```

## target: `agent/agent_runtime_helpers.py`

### `AIAgent.switch_model`
```diff
--- base::AIAgent.switch_model
+++ fork::AIAgent.switch_model
@@ -1,4 +1,11 @@
-    def switch_model(self, new_model, new_provider, api_key='', base_url='', api_mode=''):
-        """Forwarder — see ``agent.agent_runtime_helpers.switch_model``."""
+    def switch_model(self, new_model, new_provider, api_key='', base_url='',
+                     api_mode='', **kwargs):
+        """Forwarder — see ``agent.agent_runtime_helpers.switch_model``.
+
+        ``**kwargs`` carries the optional ``session_reasoning_config`` through
+        without pinning its sentinel default in two places.
+        """
         from agent.agent_runtime_helpers import switch_model
-        return switch_model(self, new_model, new_provider, api_key, base_url, api_mode)
+        return switch_model(
+            self, new_model, new_provider, api_key, base_url, api_mode, **kwargs
+        )
```

## target: `agent/status_output.py`

### `AIAgent._emit_status`
```diff
--- base::AIAgent._emit_status
+++ fork::AIAgent._emit_status
@@ -1,3 +1,3 @@
-    def _emit_status(self, message: str) -> None:
+    def _emit_status(self, message: str) -> bool:
         """Emit a lifecycle status message to both CLI and gateway channels.
 
@@ -8,4 +8,13 @@
         This helper never raises — exceptions are swallowed so it cannot
         interrupt the retry/fallback logic.
+
+        Returns ``True`` when the gateway-delivery leg accepted the message (a
+        ``status_callback`` exists, did not raise, and did not explicitly reject
+        it); ``False`` when there is no callback (CLI-only context / throwaway
+        agent), the callback raised, or it returned ``False``.
+        ``True`` means accepted/scheduled, not adapter-confirmed: gateway sends
+        complete asynchronously and log a WARNING if that later leg fails.
+        This return is purely additive — existing callers ignore it — and lets a
+        caller that NEEDS delivery (the compaction announce) detect a lost send.
         """
         try:
@@ -13,7 +22,11 @@
         except Exception:
             pass
-        if self.status_callback:
+        status_callback = getattr(self, "status_callback", None)
+        if status_callback:
             try:
-                self.status_callback("lifecycle", message)
+                accepted = status_callback("lifecycle", message)
+                return accepted is not False
             except Exception:
                 logger.debug("status_callback error in _emit_status", exc_info=True)
+                return False
+        return False
```

## target: `agent/chat_completion_nonstream.py,agent/status_output.py`

### `AIAgent._emit_wait_notice`
```diff
--- base::AIAgent._emit_wait_notice
+++ fork::AIAgent._emit_wait_notice
@@ -14,6 +14,7 @@
 
         Never raises — a wait notice must not break the API-call wait loop.
+        A wait notice is liveness, not progress (``progress=False``).
         """
-        self._touch_activity(text)
+        self._touch_activity(text, progress=False)
         _thinking_cb = getattr(self, "thinking_callback", None)
         if _thinking_cb:
```

## target: `agent/session_persistence.py`

### `AIAgent._apply_persist_user_message_override`
```diff
--- base::AIAgent._apply_persist_user_message_override
+++ fork::AIAgent._apply_persist_user_message_override
@@ -12,5 +12,6 @@
         override = getattr(self, "_persist_user_message_override", None)
         timestamp = getattr(self, "_persist_user_message_timestamp", None)
-        if idx is None or (override is None and timestamp is None):
+        platform_id = getattr(self, "_persist_user_message_platform_id", None)
+        if idx is None or (override is None and timestamp is None and platform_id is None):
             return
         if 0 <= idx < len(messages):
@@ -44,2 +45,10 @@
                 if timestamp is not None:
                     msg["timestamp"] = timestamp
+                # Platform-side message id (e.g. Discord message.id) — metadata,
+                # load-bearing for restart drain-window recovery dedup: it lets
+                # backfill-on-reconnect ask ``has_platform_message_id`` whether an
+                # interrupted turn already reached the transcript. Stamped here
+                # (in addition to build_turn_context) so it survives the override
+                # path. Drain-window message-loss SPEC D-10.
+                if platform_id is not None:
+                    msg["platform_message_id"] = platform_id
```

### `AIAgent._flush_messages_to_session_db`
```diff
--- base::AIAgent._flush_messages_to_session_db
+++ fork::AIAgent._flush_messages_to_session_db
@@ -6,6 +6,11 @@
         """Serialize direct and turn-boundary session flushes per agent."""
         persist_lock = getattr(self, "_session_persist_lock", None)
+        flush_unlocked = getattr(self, "_flush_messages_to_session_db_unlocked", None)
+        if flush_unlocked is None:
+            flush_unlocked = AIAgent._flush_messages_to_session_db_unlocked.__get__(
+                self, type(self)
+            )
         if persist_lock is None:
-            return self._flush_messages_to_session_db_unlocked(messages, conversation_history)
+            return flush_unlocked(messages, conversation_history)
         with persist_lock:
-            return self._flush_messages_to_session_db_unlocked(messages, conversation_history)
+            return flush_unlocked(messages, conversation_history)
```

### `AIAgent._flush_messages_to_session_db_unlocked`
```diff
--- base::AIAgent._flush_messages_to_session_db_unlocked
+++ fork::AIAgent._flush_messages_to_session_db_unlocked
@@ -44,4 +44,5 @@
         _ov_content = getattr(self, "_persist_user_message_override", None)
         _ov_timestamp = getattr(self, "_persist_user_message_timestamp", None)
+        _ov_platform_id = getattr(self, "_persist_user_message_platform_id", None)
         try:
             # Retry row creation if the earlier attempt failed transiently.
@@ -74,4 +75,8 @@
             flushed_session_id = getattr(self, "_flushed_db_message_session_id", None)
             if flushed_session_id != current_session_id or self._last_flushed_db_idx == 0:
+                self._flushed_db_message_ids = set()
+                self._flushed_db_row_ids = {}
+                self._flushed_db_content_refs = {}
+                self._interrupt_close_repersisted_ids = set()
                 seed_ids = set()
             else:
@@ -80,8 +85,58 @@
                     seed_ids = set()
             self._flushed_db_message_session_id = current_session_id
+            flushed_ids = set()
+            flushed_row_ids = getattr(self, "_flushed_db_row_ids", None)
+            if not isinstance(flushed_row_ids, dict):
+                flushed_row_ids = {}
+                self._flushed_db_row_ids = flushed_row_ids
+            content_refs = getattr(self, "_flushed_db_content_refs", None)
+            if not isinstance(content_refs, dict):
+                content_refs = {}
+                self._flushed_db_content_refs = content_refs
+            repersisted_ids = getattr(self, "_interrupt_close_repersisted_ids", None)
+            if not isinstance(repersisted_ids, set):
+                repersisted_ids = set()
+                self._interrupt_close_repersisted_ids = repersisted_ids
             history_ids = {
                 id(item) for item in (conversation_history or [])
                 if isinstance(item, dict)
             }
+            # Count of superseded-turn CONTENT rows suppressed by the gate below
+            # (a /stop'd zombie's continued writes). Logged after the loop so a
+            # future "why did my stopped turn's rows vanish/appear" is diagnosable.
+            _suppressed_superseded_rows = 0
+            # Resolve the superseded flag ONCE, fail-open: any error reading it
+            # leaves suppression OFF so a guard bug can never lose a real row
+            # (a stray late row is cosmetic — #339 already stops /undo racing it;
+            # a dropped real row is data loss). See I5.
+            try:
+                _persist_superseded = bool(getattr(self, "_persist_superseded", False))
+            except Exception:
+                _persist_superseded = False
+            # Pairing-safety for the superseded gate: track the tool_call ids
+            # whose owning assistant(tool_calls) row WE suppress. A `tool` result
+            # may be suppressed ONLY if its owner was also suppressed (never
+            # persisted) — otherwise it would orphan an already-durable
+            # assistant(tool_calls) row into a dangling tool call (the #48879
+            # role-alternation corruption the whole carve-out exists to prevent).
+            #
+            # 🔴 AGENT-SCOPED, not per-flush (Greptile-B1′): the assistant(tool_calls)
+            # row flushes in a DIFFERENT flush from its tool result within one
+            # iteration (conversation_loop.py: append assistant → flush →
+            # _execute_tool_calls appends result → flush). A per-flush set would be
+            # empty when the result's flush runs, letting it persist orphaned. So
+            # the suppressed-id set lives on the AGENT and survives the whole drain;
+            # it is created lazily ONLY when superseded (a normal turn never
+            # allocates or consults it) and dies with the agent object on eviction
+            # (fresh agent per turn ⇒ naturally reset; clear_interrupt() does NOT
+            # clear _persist_superseded by design, so the drain stays superseded).
+            _suppressed_tool_call_ids = None
+            if _persist_superseded:
+                _suppressed_tool_call_ids = getattr(
+                    self, "_superseded_suppressed_tool_call_ids", None
+                )
+                if not isinstance(_suppressed_tool_call_ids, set):
+                    _suppressed_tool_call_ids = set()
+                    self._superseded_suppressed_tool_call_ids = _suppressed_tool_call_ids
 
             # Bounded scan: skip the longest identity-matched prefix of the
@@ -94,4 +149,16 @@
             # forces a full re-scan). Identity match ⇒ identical skip decision,
             # so starting after the matched prefix is behavior-preserving.
+            #
+            # FORK EXCEPTION (parity merge 2026-08-08): the identity premise
+            # above is NOT sufficient on its own. Identity proves the same dict
+            # OBJECT, not the same CONTENT — and the fork mutates an already-
+            # flushed assistant tail IN PLACE when a turn is interrupted
+            # (close_interrupted_tool_sequence stamps finish_reason=
+            # "interrupt_close" on the existing dict). Skipping it as part of
+            # the matched prefix silently loses that stamp on re-flush, so an
+            # interrupted turn reads back as a clean one and resume logic
+            # mis-classifies it. Stop the prefix skip at the first message that
+            # carries a mutation-sensitive field, so those rows are always
+            # re-examined.
             _scan_start = 0
             _prev_prefix = getattr(self, "_db_flush_scan_prefix", None)
@@ -102,4 +169,8 @@
                     and messages[_scan_start] is _prev_prefix[_scan_start]
                     and bool(messages[_scan_start].get(_DB_PERSISTED_MARKER))
+                    and not _has_mutable_flush_state(messages[_scan_start])
+                    and not _tool_content_mutated_since_flush(
+                        messages[_scan_start], content_refs
+                    )
                 ):
                     _scan_start += 1
@@ -113,16 +184,73 @@
                 if not isinstance(msg, dict):
                     continue
+                msg_id = id(msg)
+                # A flushed tool row whose content was replaced in place (the
+                # /steer marker or run-budget notice appended after the
+                # per-result flush): stamp the sent bytes as the row's
+                # api_content sidecar so the reloaded history replays what was
+                # sent and keeps the steer (t_a17e2305).
+                if _tool_content_mutated_since_flush(msg, content_refs):
+                    _row_id = msg["_db_persisted_row_id"]
+                    _sent = _persisted_content_projection(msg, msg.get("content"))
+                    try:
+                        if isinstance(_sent, str) and _sent:
+                            self._session_db.set_message_api_content(
+                                self.session_id, _row_id, _sent,
+                            )
+                        content_refs[_row_id] = msg.get("content")
+                    except Exception as _e:
+                        logger.warning(
+                            "tool api_content re-persist failed (row=%s): %s", _row_id, _e,
+                        )
                 # Never write ephemeral recovery scaffolding to the session
                 # store. The flush is append-only (it only advances
                 # _last_flushed_db_idx via identity tracking), so a synthetic
                 # message committed by a mid-turn persist cannot be un-written
-                # when the end-of-turn drop removes it from the in-memory list —
-                # the resumed transcript would then replay synthetic
-                # "(empty)"/nudge/thinking-prefill turns as if they were genuine
-                # context. Skip regardless of position: an answered nudge leaves
-                # the synthetic pair buried mid-list, not just at the tail.
+                # when the end-of-turn drop removes it from the in-memory list.
                 if _is_ephemeral_scaffolding(msg):
                     continue
+                if msg_id in flushed_ids:
+                    # Already persisted by identity. One field can still change
+                    # after the initial flush: close_interrupted_tool_sequence
+                    # mutates an existing plain-text assistant tail in place,
+                    # setting finish_reason="interrupt_close" (the resume
+                    # discriminator). Re-persist just that column once so the
+                    # flag survives reload instead of being silently dropped.
+                    if (
+                        msg.get("finish_reason") == _INTERRUPT_CLOSE_FINISH_REASON
+                        and msg_id not in repersisted_ids
+                    ):
+                        row_id = flushed_row_ids.get(msg_id)
+                        if row_id is not None:
+                            try:
+                                self._session_db.update_message_finish_reason(
+                                    self.session_id, row_id,
+                                    _INTERRUPT_CLOSE_FINISH_REASON,
+                                )
+                                repersisted_ids.add(msg_id)
+                            except Exception as _e:
+                                logger.warning(
+                                    "interrupt_close re-persist failed (row=%s): %s",
+                                    row_id, _e,
+                                )
+                    continue
                 if msg.get(_DB_PERSISTED_MARKER):
+                    if (
+                        msg.get("finish_reason") == _INTERRUPT_CLOSE_FINISH_REASON
+                        and msg_id not in repersisted_ids
+                    ):
+                        row_id = msg.get("_db_persisted_row_id") or flushed_row_ids.get(msg_id)
+                        if row_id is not None:
+                            try:
+                                self._session_db.update_message_finish_reason(
+                                    self.session_id, row_id,
+                                    _INTERRUPT_CLOSE_FINISH_REASON,
+                                )
+                                repersisted_ids.add(msg_id)
+                            except Exception as _e:
+                                logger.warning(
+                                    "interrupt_close re-persist failed (row=%s): %s",
+                                    row_id, _e,
+                                )
                     continue
                 # Already-durable messages: either carried over from the loaded
@@ -132,4 +260,67 @@
                     msg[_DB_PERSISTED_MARKER] = True
                     continue
+                # ── Superseded-turn write gate (append-time generation gate) ──
+                # Runs AFTER every "already durable" skip above, so it only ever
+                # sees a genuinely NEW, about-to-be-written row. When the gateway
+                # invalidated this turn's run generation (/stop, /new, stale-agent
+                # eviction — see _persist_superseded), the turn is a "zombie" whose
+                # continued writes are unwanted: they land AFTER the user stopped
+                # the turn and (pre-#339) let a later /undo land somewhere the drain
+                # clobbers. Suppress a superseded turn's NEW rows — PAIRING-SAFELY.
+                #
+                # 🔴 LOAD-BEARING CARVE-OUT (I1): the interrupt-close tail
+                # (finish_reason == _INTERRUPT_CLOSE_FINISH_REASON, written by
+                # close_interrupted_tool_sequence) MUST still persist — it is the
+                # role-alternation repair AND the deliberate restart-loop backstop
+                # / auto-continue signal (hermes #45230/#49201/#49243). Never gate it.
+                #
+                # 🔴 PAIRING SAFETY (I1 / Greptile-B1): a `tool` result must NOT be
+                # suppressed if its owning assistant(tool_calls) is already durable
+                # (persisted in a PRIOR flush, before /stop) — that would orphan the
+                # call = the #48879 corruption. Because this gate now runs only on
+                # NEW rows, an already-persisted owner is skipped above and its id
+                # is never added to _suppressed_tool_call_ids, so its result passes
+                # through. Only a pair whose BOTH halves are new (arrived after
+                # /stop) is dropped atomically. Fail-open: _persist_superseded was
+                # resolved with getattr-default-False in a try/except above.
+                if _persist_superseded and (
+                    msg.get("finish_reason") != _INTERRUPT_CLOSE_FINISH_REASON
+                ):
+                    if msg.get("role") == "tool":
+                        # Suppress ONLY when the owning assistant(tool_calls) is
+                        # also being suppressed (this or any prior drain flush);
+                        # otherwise the owner is already durable and this result
+                        # must land (no orphan).
+                        if msg.get("tool_call_id") in _suppressed_tool_call_ids:
+                            _suppressed_superseded_rows += 1
+                            continue
+                        # else: owner already persisted → let the result through.
+                    elif msg.get("role") == "assistant":
+                        # The zombie's continued content: suppress. If it carries
+                        # tool_calls, record their ids so the matching results
+                        # (this or a later drain flush) are suppressed too,
+                        # keeping the pair atomic (agent-scoped set, B1′).
+                        _tcs = msg.get("tool_calls")
+                        if isinstance(_tcs, list):
+                            for _tc in _tcs:
+                                if isinstance(_tc, dict):
+                                    _tcid = _tc.get("id") or _tc.get("tool_call_id")
+                                else:
+                                    _tcid = getattr(_tc, "id", None)
+                                if _tcid:
+                                    _suppressed_tool_call_ids.add(_tcid)
+                        _suppressed_superseded_rows += 1
+                        continue
+                    else:
+                        # 🔴 FAIL-OPEN on any OTHER role (user/system/unexpected):
+                        # a zombie turn only ever writes assistant + tool rows, so
+                        # a NEW user/system row here is anomalous — and dropping a
+                        # real user message would be data loss (I5). Persist it
+                        # normally; log for diagnosability. Greptile-P2.
+                        logger.debug(
+                            "persist gate: superseded turn produced an unexpected "
+                            "new %r row for session %s — persisting (fail-open)",
+                            msg.get("role"), getattr(self, "session_id", "?"),
+                        )
                 role = msg.get("role", "unknown")
                 content = msg.get("content")
@@ -142,4 +333,5 @@
                     _row_api_content = None
                 _row_timestamp = msg.get("timestamp")
+                _row_platform_id = msg.get("platform_message_id")
                 # Apply the persist override to THIS row's written values only
                 # (never to the live dict). A multimodal override is a complete
@@ -183,4 +375,11 @@
                     if _ov_timestamp is not None:
                         _row_timestamp = _ov_timestamp
+                    # The interrupted-turn platform id (#48677 backfill dedupe):
+                    # stamp it on the WRITTEN row so a later
+                    # backfill-on-reconnect's has_platform_message_id sees the
+                    # turn is already persisted and skips re-processing it. The
+                    # live dict is never mutated (parity with content/timestamp).
+                    if _ov_platform_id is not None and not _row_platform_id:
+                        _row_platform_id = _ov_platform_id
                 # Store the sidecar only when it actually differs.
                 if _row_api_content == content:
@@ -205,18 +404,9 @@
                 ):
                     _row_api_content = content
-                # Persist multimodal tool results as their text summary only —
-                # base64 images would bloat the session DB and aren't useful
-                # for cross-session replay.
-                if _is_multimodal_tool_result(content):
-                    content = _multimodal_text_summary(content)
-                elif isinstance(content, list):
-                    # List of OpenAI-style content parts: strip images, keep text.
-                    _txt = []
-                    for p in content:
-                        if isinstance(p, dict) and p.get("type") == "text":
-                            _txt.append(str(p.get("text", "")))
-                        elif isinstance(p, dict) and p.get("type") in {"image", "image_url", "input_image"}:
-                            _txt.append("[screenshot]")
-                    content = "\n".join(_txt) if _txt else None
+                # Persist the same image-free projection used at the next turn
+                # boundary. This keeps live-history and rebuilt requests
+                # byte-identical instead of accounting for a tiny text row while
+                # the in-memory request silently retains base64 pixels.
+                content = _persisted_content_projection(msg, content)
                 tool_calls_data = None
                 if hasattr(msg, "tool_calls") and isinstance(msg.tool_calls, list) and msg.tool_calls:
@@ -230,5 +420,10 @@
                     "role": role,
                     "content": content,
-                    "tool_name": msg.get("tool_name"),
+                    # Tool rows built without ``tool_name`` (interrupt/invalid-call
+                    # stubs) still carry the ``name`` the wire sends; keep it so the
+                    # reload restores it (t_a17e2305).
+                    "tool_name": msg.get("tool_name") or (
+                        msg.get("name") if role == "tool" else None
+                    ),
                     "tool_calls": tool_calls_data,
                     "tool_call_id": msg.get("tool_call_id"),
@@ -243,4 +438,5 @@
                     "_compressed_summary": bool(msg.get(COMPRESSED_SUMMARY_METADATA_KEY)),
                     "timestamp": _row_timestamp,
+                    "platform_message_id": _row_platform_id,
                     "api_content": _row_api_content,
                     # Standalone reference handoffs are always hidden, even
@@ -281,4 +477,5 @@
             # minus the partial-prefix case that could double-pay counters).
             if _batch_rows:
+                _batch_row_ids: List[int] = []
                 self._session_db.append_messages_batch(
                     session_id=self.session_id,
@@ -287,4 +484,5 @@
                         self, "_active_compression_lock_holder", None
                     ),
+                    row_ids_out=_batch_row_ids,
                     turn_lease_holder=getattr(
                         self, "_active_session_turn_lease_holder", None
@@ -295,7 +493,60 @@
                     or 300.0,
                 )
+                # Upstream marker sync: stamps _DB_PERSISTED_MARKER and copies
+                # the canonical row id / repaired content back onto the live
+                # dicts (repaired rows carry _row_id / _canonical_content).
                 from agent.transcript_repair import sync_flushed_message_markers
 
                 sync_flushed_message_markers(_batch_msgs, _batch_rows)
+                # Fork bookkeeping: positional row-id capture for the
+                # interrupt_close in-place re-persist path + flush id sets.
+                for _idx, _written in enumerate(_batch_msgs):
+                    _written_id = id(_written)
+                    flushed_ids.add(_written_id)
+                    _row_id = (
+                        _batch_row_ids[_idx]
+                        if _idx < len(_batch_row_ids)
+                        else None
+                    )
+                    if not isinstance(_row_id, int) and isinstance(
+                        _written.get("_row_id"), int
+                    ):
+                        # Repaired-in-place rows get no freshly-inserted id but
+                        # carry their durable one (stamped by the batch repair).
+                        _row_id = _written["_row_id"]
+                    if isinstance(_row_id, int):
+                        flushed_row_ids[_written_id] = _row_id
+                        _written["_db_persisted_row_id"] = _row_id
+                        if _written.get("role") == "tool":
+                            content_refs[_row_id] = _written.get("content")
+                        # If this message was appended already carrying the flag,
+                        # it is durably persisted — no later re-persist needed.
+                        if _written.get("finish_reason") == _INTERRUPT_CLOSE_FINISH_REASON:
+                            repersisted_ids.add(_written_id)
+                    elif _written.get("finish_reason") == _INTERRUPT_CLOSE_FINISH_REASON:
+                        # Load-bearing: without a row id we cannot later re-persist an
+                        # in-place interrupt_close mutation, so a lost flag would fall
+                        # back to the old "skip unfinished work" resume behavior.
+                        # append_messages_batch fills row_ids_out with ints in prod;
+                        # a miss here means a mock/altered API — make it loud.
+                        logger.warning(
+                            "flush: append_messages_batch returned no row id (%r) for an "
+                            "interrupt_close message; in-place re-persist will be skipped",
+                            _row_id,
+                        )
+            if flushed_ids and current_session_id:
+                try:
+                    self._session_db.recompute_effective_last_active(current_session_id)
+                except Exception as _e:
+                    logger.warning(
+                        "Session DB effective_last_active recompute failed: %s", _e
+                    )
+            if _suppressed_superseded_rows:
+                logger.info(
+                    "persist: suppressed %d superseded-turn content row(s) for session %s "
+                    "(turn was /stop'd or /new'd; interrupt-close tail preserved)",
+                    _suppressed_superseded_rows,
+                    getattr(self, "session_id", "?"),
+                )
             # The intrinsic markers are now the sole source of truth. Reset the
             # one-shot seed so no id() outlives this flush to alias a message
```

## target: `agent/turn_explainers.py`

### `AIAgent._format_turn_completion_explanation`
```diff
--- base::AIAgent._format_turn_completion_explanation
+++ fork::AIAgent._format_turn_completion_explanation
@@ -1,5 +1,8 @@
     @staticmethod
     def _format_turn_completion_explanation(
-        turn_exit_reason: str, persistence_cause: Optional[str] = None
+        turn_exit_reason: str,
+        cause_or_agent: "Any" = None,
+        *,
+        persistence_cause: Optional[str] = None,
     ) -> str:
         """Render a user-facing explanation for an abnormal turn ending.
@@ -105,5 +108,16 @@
             )
         if reason == "session_persistence_failed":
-            cause = persistence_cause or "unknown"
+            # The second parameter carries EITHER upstream's classified cause
+            # string ('compression' / 'locked' / 'disk' / ... — see
+            # hermes_state.classify_persistence_error) OR the fork's live
+            # agent (evidence-based diagnosis: is_disk_full_error + the
+            # restart-mid-turn forensics). Accept both shapes so every
+            # existing caller and both test contracts keep working.
+            cause = (
+                persistence_cause
+                if persistence_cause is not None
+                else (cause_or_agent if isinstance(cause_or_agent, str) else None)
+            )
+            agent = cause_or_agent if not isinstance(cause_or_agent, str) else None
             if cause == "compression":
                 return (
@@ -160,10 +174,50 @@
                     "again."
                 )
-            return (
+            base = (
                 prefix
                 + "the turn was stopped because session storage could not be "
                 "written (the transcript would have been lost on restart). "
-                "Check the state database health (`hermes doctor`), then "
-                "send your message again."
+            )
+            # Fork evidence path (2026-08-10 incident): do NOT assert a cause
+            # we did not measure. Prefer, in order: a REAL disk-full signal
+            # (is_disk_full_error, the same detector the TUI gateway uses),
+            # then a restart landing mid-turn, then an honest "not identified".
+            if agent is not None:
+                exc = getattr(agent, "_session_persistence_error", None)
+                try:
+                    from hermes_state import is_disk_full_error
+                    disk_full = is_disk_full_error(exc)
+                except Exception:
+                    disk_full = False
+                if disk_full:
+                    return base + (
+                        "The disk is full — free some space, then send your "
+                        "message again."
+                    )
+                if bool(getattr(agent, "_shutdown_landed_mid_turn", False)):
+                    return base + (
+                        "The gateway shut down or restarted mid-turn, so the "
+                        "append could not complete. Nothing was lost — send your "
+                        "message again now that it is back up."
+                    )
+                try:
+                    from gateway.shutdown_forensics import shutdown_landed_within
+                    if shutdown_landed_within(300.0):
+                        return base + (
+                            "The gateway shut down or restarted mid-turn, so the "
+                            "append could not complete. Nothing was lost — send "
+                            "your message again now that it is back up."
+                        )
+                except Exception:
+                    pass
+                detail = f" ({type(exc).__name__}: {exc})" if exc is not None else ""
+                return base + (
+                    f"The cause was not identified{detail}. Check the disk "
+                    "(`df -h`), state.db permissions, and whether the gateway "
+                    "restarted mid-turn, then send your message again."
+                )
+            return base + (
+                "The cause was not identified. Check the state database "
+                "health (`hermes doctor`), then send your message again."
             )
         # Unknown/diagnostic-only reasons (e.g. "unknown", guardrail_halt
```

## target: `agent/activity_tracking.py`

### `AIAgent._touch_activity`
```diff
--- base::AIAgent._touch_activity
+++ fork::AIAgent._touch_activity
@@ -5,6 +5,22 @@
         provenance: Optional[ActivityProvenance] = None,
         force_persist: bool = False,
+        progress: bool = True,
+        heartbeat: bool = False,
     ) -> None:
         """Update the last-activity timestamp and description (thread-safe).
+
+        ``progress=False`` marks a pure wait ticker (e.g. "still waiting on
+        the provider"): it refreshes liveness but not ``_last_progress_ts``,
+        which the kanban stall detector reads to tell a live wrapper from a
+        progressing loop (t_7d034e3b).
+
+        ``heartbeat=True`` marks a periodic liveness tick fired while ONE
+        tool call is still running (tool-activity heartbeat, "terminal
+        command running (Ns elapsed)"). It still advances ``_last_progress_ts``
+        (the kanban stall detector counts a running tool as progress), but
+        neither it nor a ``progress=False`` ticker advances
+        ``_last_progress_event_ts``: the delegate hung-child detector keys on
+        that, so a child blocked inside one API call or one tool call is not
+        mistaken for one making progress (docs/dev/delegate-child-lifecycle.md).
 
         Also bridges to the kanban board's heartbeat fields when this
@@ -32,4 +48,8 @@
 
         self._last_activity_ts = time.time()
+        if progress:
+            self._last_progress_ts = self._last_activity_ts
+            if not heartbeat:
+                self._last_progress_event_ts = self._last_activity_ts
         self._last_activity_desc = bound_activity_description(desc)
         self._last_activity_provenance = normalize_activity_provenance(provenance)
@@ -40,5 +60,7 @@
                     inject_new_comments_from_env,
                 )
-                heartbeat_current_worker_from_env()
+                heartbeat_current_worker_from_env(
+                    progress_at=getattr(self, "_last_progress_ts", None),
+                )
                 # Fold any new operator notes into the running turn (OUT-OF-BAND
                 # steer) so the user can talk to a live task without a restart.
```

### `AIAgent._persist_session_activity_if_due`
```diff
--- base::AIAgent._persist_session_activity_if_due
+++ fork::AIAgent._persist_session_activity_if_due
@@ -17,9 +17,12 @@
         from agent.session_activity import (
             SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS,
+            SESSION_ACTIVITY_PERSIST_NEVER,
             normalize_activity_provenance,
         )
 
         now_mono = time.monotonic()
-        last_mono = getattr(self, "_session_activity_last_persist_mono", 0.0)
+        last_mono = getattr(
+            self, "_session_activity_last_persist_mono", SESSION_ACTIVITY_PERSIST_NEVER
+        )
         if (now_mono - last_mono) < SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS:
             return
```

## target: `agent/client_lifecycle.py`

### `AIAgent._swap_credential`
```diff
--- base::AIAgent._swap_credential
+++ fork::AIAgent._swap_credential
@@ -1,4 +1,48 @@
-    def _swap_credential(self, entry) -> None:
+    def _swap_credential(self, entry) -> "SwapOutcome":
+        """Install the pool ``entry``'s credential, or refuse if it has no key.
+
+        Returns a :class:`SwapOutcome` (tri-state, NOT a bool): ``SWAPPED`` when a
+        usable key was installed; ``RETRYABLE_EXHAUSTED`` / ``MISSING_CREDENTIAL``
+        when the entry resolves an empty key and NO client is installed (see the
+        enum docstring). The empty-key refusal prevents building an Anthropic
+        client with ``api_key=""``, which would raise a non-retryable
+        ``TypeError("Could not resolve authentication method…")`` at request time
+        and abort the turn — masking a transient rate-limit as a local bug. This
+        mirrors the ``if entry_key:`` guard ``_restore_primary_runtime`` already
+        applies before its own ``_swap_credential`` call.
+        """
         runtime_key = getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", "")
+
+        # Guard: never install a keyless client. Distinguish a transient
+        # 429-exhaustion (retryable, self-heals on window reset) from a genuine
+        # missing/dead credential (loud config error) using the entry's own
+        # rate-limit-exhaustion marker, bounded by its cooldown TTL.
+        if not (isinstance(runtime_key, str) and runtime_key.strip()):
+            try:
+                from agent.credential_pool import _exhausted_until
+                exhausted_until = _exhausted_until(entry)
+            except Exception:  # noqa: BLE001 - never crash the recovery path on this probe
+                exhausted_until = None
+            is_ratelimit_exhausted = (
+                getattr(entry, "last_error_code", None) == 429
+                and exhausted_until is not None
+                and time.time() < exhausted_until
+            )
+            if is_ratelimit_exhausted:
+                logger.info(
+                    "Credential rotation: pool entry %s has no usable key and is "
+                    "429-exhausted within its window — treating as retryable "
+                    "rate-limit (not installing a keyless client).",
+                    getattr(entry, "id", "?"),
+                )
+                return SwapOutcome.RETRYABLE_EXHAUSTED
+            logger.warning(
+                "Credential rotation: pool entry %s has no usable key and no live "
+                "429 exhaustion — treating as MISSING credential (config error), "
+                "not a transient rate-limit.",
+                getattr(entry, "id", "?"),
+            )
+            return SwapOutcome.MISSING_CREDENTIAL
+
         runtime_base = getattr(entry, "runtime_base_url", None) or getattr(entry, "base_url", None) or self.base_url
         self._credential_pool_entry_id = getattr(entry, "id", None)
@@ -26,5 +70,5 @@
             self.api_key = runtime_key
             self.base_url = runtime_base.rstrip("/") if isinstance(runtime_base, str) else runtime_base
-            return
+            return SwapOutcome.SWAPPED
 
         self.api_key = runtime_key
@@ -34,2 +78,11 @@
         self._reapply_route_client_config(route_changed=route_changed)
         self._replace_primary_openai_client(reason="credential_rotation")
+        # Fork contract: ``_swap_credential`` returns a tri-state SwapOutcome,
+        # never None (upstream's copy of this tail returns nothing because
+        # upstream's signature is a bare bool). Keep the terminal return HERE,
+        # in the caller — NOT inside ``_reapply_route_client_config``, which
+        # upstream extracted as a void helper and which the env-refresh path
+        # also calls. The 2026-08-07 merge put the rebuild + return inside the
+        # helper, so every rotation rebuilt the client TWICE (once in the
+        # helper, once here).
+        return SwapOutcome.SWAPPED
```

### `AIAgent._anthropic_messages_create`
```diff
--- base::AIAgent._anthropic_messages_create
+++ fork::AIAgent._anthropic_messages_create
@@ -1,3 +1,5 @@
-    def _anthropic_messages_create(self, api_kwargs: dict, *, client: Any = None):
+    def _anthropic_messages_create(
+        self, api_kwargs: dict, *, client: Any = None, on_response: Any = None
+    ):
         # When a request-local client is supplied it was already credential-
         # refreshed in ``_create_request_anthropic_client``; only the shared
@@ -17,4 +19,7 @@
             # parsed Message drops. No-ops on providers that don't send the
             # matching header families (x-ratelimit-* / x-nous-credits-*).
-            on_response=self._capture_anthropic_response_headers,
+            # A caller-supplied callback is call-scoped (it wraps this one);
+            # swapping a callback on the shared agent would race
+            # interrupt-abandoned workers and transpose two calls' headers.
+            on_response=on_response or self._capture_anthropic_response_headers,
         )
```

## target: `agent/stream_delivery.py`

### `AIAgent._fire_stream_delta`
```diff
--- base::AIAgent._fire_stream_delta
+++ fork::AIAgent._fire_stream_delta
@@ -44,4 +44,8 @@
             ):
                 text = text.lstrip("\n")
+            # Final safety floor before any callback sees the delta.  A lone
+            # surrogate here crashes gateway/CLI UTF-8 writes before the
+            # accumulator can sanitize it.
+            text = _sanitize_surrogates(text)
         if not text:
             return
```

### `AIAgent._fire_reasoning_delta`
```diff
--- base::AIAgent._fire_reasoning_delta
+++ fork::AIAgent._fire_reasoning_delta
@@ -1,4 +1,6 @@
     def _fire_reasoning_delta(self, text: str) -> None:
         """Fire reasoning callback if registered."""
+        if isinstance(text, str):
+            text = _sanitize_surrogates(text)
         # Single-writer guard (#65991): fence out a superseded stream's
         # reasoning deltas the same way as content deltas.
```

## target: `agent/compression_facade.py`

### `AIAgent._compress_context`
```diff
--- base::AIAgent._compress_context
+++ fork::AIAgent._compress_context
@@ -8,4 +8,5 @@
         focus_topic: str = None,
         force: bool = False,
+        trigger_reason: str = None,
         defer_context_engine_notification: bool = False,
         commit_fence=None,
@@ -17,4 +18,8 @@
         auto-compress abort.  Auto-compress callers use the default
         ``force=False``.
+
+        ``trigger_reason`` (optional) names WHY this compaction fired
+        (threshold / overflow_413 / overflow_context / tier_reduction) so the
+        in-chat announce can show it. ``None`` → no reason clause.
         """
         from agent.conversation_compression import (
@@ -72,5 +77,5 @@
                     approx_tokens=approx_tokens, task_id=task_id,
                     focus_topic=focus_topic,
-                    force=force,
+                    force=force, trigger_reason=trigger_reason,
                     defer_context_engine_notification=(
                         defer_context_engine_notification
@@ -143,4 +148,15 @@
                         total_ceiling,
                     )
+                    # Fork parity: rotation-independent ABORT signal. A
+                    # timed-out compaction produces NOTHING to persist, so
+                    # ``_last_compaction_persist_failed`` stays False and the
+                    # id is unchanged — the exact surface signature of a
+                    # genuine "nothing to compress" no-op. Without this flag
+                    # the gateway renders the bland "No changes: transcript
+                    # preserved" for a run that actually died on a stalled
+                    # summariser. Mirrors the #44794 persist-failure signal.
+                    self._last_compaction_aborted = True
+                    self._last_compaction_abort_reason = "timeout"
+                    self._last_compaction_abort_waited = float(waited)
                     touch = getattr(self, "_touch_activity", None)
                     if callable(touch):
```

## target: `agent/conversation_loop.py,agent/turn_facade.py`

### `AIAgent.run_conversation`
```diff
--- base::AIAgent.run_conversation
+++ fork::AIAgent.run_conversation
@@ -8,4 +8,5 @@
         persist_user_message: Optional[Any] = None,
         persist_user_timestamp: Optional[float] = None,
+        persist_user_platform_id: Optional[str] = None,
         persist_user_display_kind: Optional[str] = None,
         persist_user_display_metadata: Optional[Dict[str, Any]] = None,
@@ -24,5 +25,7 @@
         from agent.aux_accounting import (
             reset_accounting_context,
+            reset_blackbox_turn,
             set_accounting_context,
+            set_blackbox_turn,
         )
         from agent import relay_runtime
@@ -63,4 +66,5 @@
         token = None
         acct_token = None
+        bb_token = None
         task_started = False
         task_finished = False
@@ -361,4 +365,8 @@
                 getattr(self, "session_id", None),
             )
+            # Blackbox per-call ledger for aux calls: bind this turn's id (the
+            # one turn_context adopts from _relay_pending_turn_id) so aux rows
+            # land under it with attribution='aux:<task>' (t_39628ae3).
+            bb_token = set_blackbox_turn(self, relay_turn_id)
             from agent.auxiliary_client import scoped_runtime_main
 
@@ -382,4 +390,5 @@
                         persist_user_message,
                         persist_user_timestamp=persist_user_timestamp,
+                        persist_user_platform_id=persist_user_platform_id,
                         persist_user_display_kind=persist_user_display_kind,
                         persist_user_display_metadata=persist_user_display_metadata,
@@ -394,4 +403,10 @@
                     # outer finally: a refresher firing between stop and join
                     # would otherwise set an interrupt that survives the clear.
+            # Early returns inside run_conversation bypass finalize_turn and
+            # with it the once-per-turn on_session_end hook; emit it here so
+            # every turn (and its Blackbox turn_api_calls) gets a turns row.
+            from agent.turn_finalizer import emit_unfinalized_session_end
+
+            emit_unfinalized_session_end(self, relay_turn_id, result=result)
             terminal = result if isinstance(result, dict) else {}
             if terminal.get("interrupted") is True:
@@ -410,4 +425,10 @@
             return result
         except BaseException as exc:
+            try:
+                from agent.turn_finalizer import emit_unfinalized_session_end
+
+                emit_unfinalized_session_end(self, relay_turn_id, exc=exc)
+            except Exception:
+                pass
             if isinstance(exc, (KeyboardInterrupt, InterruptedError)) or (
                 type(exc).__name__ == "CancelledError"
@@ -476,4 +497,6 @@
                     if acct_token is not None:
                         reset_accounting_context(acct_token)
+                    if bb_token is not None:
+                        reset_blackbox_turn(bb_token)
                     if token is not None:
                         reset_conversation_context(token)
```
