# FOLLOWUP deltas for `cli.py` (lane L13-root-py, parity 2026-10-01)

Upstream decomposed this file into a facade + `agent/*`/sibling mixins. The fork's changes to the
methods below were made on the pre-decomposition monolith; their owning method now lives in the
module named in each heading (outside lane L13), so the delta was NOT applied. Each block is
`diff -u` of the method body, merge-base -> fork (`-` = base, `+` = fork). Re-thread the `+` lines
into the named module's copy (adapt names: upstream may have refactored the body).

Applied in the facade (not listed): _run_cleanup, _finalize_signaled_kanban_worker, _record_abandoned_review_turns, HermesCLI.__init__, HermesCLI._cache_ratio_unknown, HermesCLI, HermesCLI._maybe_nudge_resume_interrupted_session, HermesCLI._fast_capability, HermesCLI._undo_last_user_turn, HermesCLI.redo_last, HermesCLI._reload_active_history_after_rewind, HermesCLI.process_command, HermesCLI.run, _goal_loop_run_id, main; module-level helpers/imports.


## target: `hermes_cli/cli_config_load.py`

### `_parse_service_tier_config`
```diff
--- base::_parse_service_tier_config
+++ fork::_parse_service_tier_config
@@ -1,9 +1,10 @@
 def _parse_service_tier_config(raw: str) -> str | None:
-    """Parse a persisted service-tier preference into a Responses API value."""
-    value = str(raw or "").strip().lower()
-    if not value or value in {"normal", "default", "standard", "off", "none"}:
-        return None
-    if value in {"fast", "priority", "on"}:
-        return "priority"
-    logger.warning("Unknown service_tier '%s', ignoring", raw)
-    return None
+    """Parse a persisted service-tier preference: None, "priority", or "ultrafast"."""
+    from hermes_cli.fast_mode_contracts import (
+        is_known_service_tier_word,
+        parse_service_tier,
+    )
+
+    if not is_known_service_tier_word(raw):
+        logger.warning("Unknown service_tier '%s', ignoring", raw)
+    return parse_service_tier(raw)
```

### `load_cli_config`
```diff
--- base::load_cli_config
+++ fork::load_cli_config
@@ -128,4 +128,5 @@
             "base_url": "",    # Direct OpenAI-compatible endpoint for subagents
             "api_key": "",     # API key for delegation.base_url (falls back to OPENAI_API_KEY)
+            "resume_on_restart": True,  # recover gateway background work after restart
         },
         "onboarding": {
```

## target: `hermes_cli/cli_shutdown.py`

### `_emit_interrupted_session_end`
```diff
--- base::_emit_interrupted_session_end
+++ fork::_emit_interrupted_session_end
@@ -1,4 +1,11 @@
-def _emit_interrupted_session_end(cli, *, reason: str = "keyboard_interrupt") -> None:
-    """Best-effort on_session_end hook for interrupted non-interactive runs."""
+def _emit_interrupted_session_end(
+    cli, *, reason: str = "keyboard_interrupt", terminal_error: str | None = None
+) -> None:
+    """Best-effort on_session_end hook for interrupted non-interactive runs.
+
+    ``terminal_error`` names how the turn was closed from outside the loop
+    (``signal_15``); blackbox stores it on the turn row so a killed worker's
+    turn is distinguishable from a user interrupt.
+    """
     agent = getattr(cli, "agent", None)
     if agent is None:
@@ -15,4 +22,5 @@
     if session_id in _handed_off_session_ids:
         return
+    turn_id = getattr(agent, "_current_turn_id", "") or ""
     if session_id:
         try:
@@ -21,18 +29,45 @@
             pass
 
+    # The turn finalizer marks the turn it already emitted for (under the
+    # per-agent emit lock) and _current_turn_id is never cleared, so a
+    # signal/Ctrl-C that lands AFTER the turn finished -- or while the worker
+    # thread is unwinding through its finalizer during the grace window --
+    # must not upsert an interrupted row over the real completed one (Prism
+    # P1 x2, #1504). Check the marker and emit under that same lock; if the
+    # lock is busy for longer than the signal path can afford, the real emit
+    # is in progress and this one stands down.
+    lock = None
     try:
-        from hermes_cli.lifecycle import invoke_hook as _invoke_hook
-        _invoke_hook(
-            "on_session_end",
-            session_id=session_id,
-            task_id=getattr(agent, "_current_task_id", "") or "",
-            turn_id=getattr(agent, "_current_turn_id", "") or "",
-            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
-            completed=False,
-            interrupted=True,
-            model=getattr(agent, "model", None),
-            platform=getattr(agent, "platform", None) or "cli",
-            reason=reason,
-        )
+        from agent.turn_finalizer import _session_end_lock
+        lock = _session_end_lock(agent)
+        if not lock.acquire(timeout=2.0):
+            return
     except Exception:
-        pass
+        lock = None
+    try:
+        if turn_id and getattr(agent, "_session_end_emitted_turn_id", None) == turn_id:
+            return
+        try:
+            from hermes_cli.lifecycle import invoke_hook as _invoke_hook
+            _invoke_hook(
+                "on_session_end",
+                session_id=session_id,
+                task_id=getattr(agent, "_current_task_id", "") or "",
+                turn_id=turn_id,
+                api_request_id=getattr(agent, "_current_api_request_id", "") or "",
+                completed=False,
+                interrupted=True,
+                model=getattr(agent, "model", None),
+                provider=getattr(agent, "provider", None) or "",
+                platform=getattr(agent, "platform", None) or "cli",
+                reason=reason,
+                terminal_error=terminal_error,
+            )
+        except Exception:
+            pass
+    finally:
+        if lock is not None:
+            try:
+                lock.release()
+            except Exception:
+                pass
```

## target: `hermes_cli/callbacks.py,hermes_cli/cli_terminal_mixin.py`

### `HermesCLI._invalidate`
```diff
--- base::HermesCLI._invalidate
+++ fork::HermesCLI._invalidate
@@ -21,5 +21,5 @@
             return
         now = time.monotonic()
-        if hasattr(self, "_app") and self._app and (now - getattr(self, "_last_invalidate", 0.0)) >= min_interval:
+        if hasattr(self, "_app") and self._app and (now - getattr(self, "_last_invalidate", float("-inf"))) >= min_interval:
             self._last_invalidate = now
             try:
```

## target: `hermes_cli/cli_terminal_mixin.py`

### `HermesCLI._schedule_focus_regain_redraw`
```diff
--- base::HermesCLI._schedule_focus_regain_redraw
+++ fork::HermesCLI._schedule_focus_regain_redraw
@@ -19,5 +19,5 @@
         """
         now = time.monotonic()
-        last = getattr(self, "_last_focus_regain_redraw", 0.0)
+        last = getattr(self, "_last_focus_regain_redraw", float("-inf"))
         if now - last < min_interval:
             return
```

## target: `hermes_cli/cli_status_bar_mixin.py`

### `HermesCLI._cache_hit_rate`
```diff
--- base::HermesCLI._cache_hit_rate
+++ fork::HermesCLI._cache_hit_rate
@@ -1,3 +1,5 @@
-    def _cache_hit_rate(self, snapshot: dict, precision: int = 1) -> "tuple[float, str] | None":
+    def _cache_hit_rate(
+        self, snapshot: dict, precision: int = 1
+    ) -> "tuple[float | None, str] | None":
         """Return (cache_pct, formatted_label) or None if no cache data.
 
@@ -8,5 +10,20 @@
         so it reflects the *current* cache regime); falls back to the
         session-lifetime ratio when no delta is available.
+
+        When the window carries an UNKNOWN term the segment says so and the
+        pct is ``None``: suppressing only the delta would hand the render
+        straight to the session-lifetime fallback below, which divides the
+        same raw unflagged counters and prints the fabricated ratio the
+        suppression exists to prevent.
         """
+        if self._cache_ratio_unknown(snapshot):
+            from agent.usage_pricing import UNKNOWN_TOKENS_LABEL
+
+            # Consume the producer label only when it agrees with provenance;
+            # stale snapshots must not smuggle a percentage past this guard.
+            label = snapshot.get("cache_hit_label")
+            if label != UNKNOWN_TOKENS_LABEL:
+                label = UNKNOWN_TOKENS_LABEL
+            return None, f"◎ {label}"
         delta_pct = snapshot.get("cache_hit_pct")
         if delta_pct is not None:
```

### `HermesCLI._cache_hit_rate_style`
```diff
--- base::HermesCLI._cache_hit_rate_style
+++ fork::HermesCLI._cache_hit_rate_style
@@ -1,4 +1,10 @@
-    def _cache_hit_rate_style(self, cache_pct: float) -> str:
-        """Style for cache hit rate — higher is better (opposite of context %)."""
+    def _cache_hit_rate_style(self, cache_pct: "float | None") -> str:
+        """Style for cache hit rate — higher is better (opposite of context %).
+
+        ``None`` is the UNKNOWN arm of ``_cache_hit_rate`` — there is no pct to
+        grade, so it renders dim rather than being scored as a bad hit rate.
+        """
+        if cache_pct is None:
+            return "class:status-bar-dim"
         if cache_pct >= 70:
             return "class:status-bar-good"
```

### `HermesCLI._get_status_bar_snapshot`
```diff
--- base::HermesCLI._get_status_bar_snapshot
+++ fork::HermesCLI._get_status_bar_snapshot
@@ -48,4 +48,13 @@
             "session_total_tokens": 0,
             "session_api_calls": 0,
+            # UNKNOWN != 0 (cumulative, absorbing). Default measured so a
+            # snapshot built before any agent exists reads exactly as today.
+            "input_tokens_unknown": False,
+            "output_tokens_unknown": False,
+            "cache_read_tokens_unknown": False,
+            "cache_write_tokens_unknown": False,
+            "usage_unknown": False,
+            "session_prompt_tokens_unknown": False,
+            "session_total_tokens_unknown": False,
             "compressions": 0,
             "active_background_tasks": 0,
@@ -142,4 +151,20 @@
         snapshot["session_total_tokens"] = getattr(agent, "session_total_tokens", 0) or 0
         snapshot["session_api_calls"] = getattr(agent, "session_api_calls", 0) or 0
+        # UNKNOWN != 0, cumulative. Shared rule from agent.usage_pricing — the
+        # cumulative figures are sums over the same canonical usage the per-turn
+        # Blackbox card reads, so they use the SAME unknown vocabulary rather
+        # than a forked status-bar-only one.
+        try:
+            from agent.usage_pricing import (
+                prompt_tokens_unknown, session_total_tokens_unknown,
+                session_usage_unknown_flags,
+            )
+
+            _session_flags = session_usage_unknown_flags(agent)
+            snapshot.update(_session_flags)
+            snapshot["session_prompt_tokens_unknown"] = prompt_tokens_unknown(_session_flags)
+            snapshot["session_total_tokens_unknown"] = session_total_tokens_unknown(agent)
+        except Exception:
+            pass
 
         compressor = getattr(agent, "context_compressor", None)
@@ -169,4 +194,6 @@
         #   and CanonicalUsage.prompt_tokens = input+read+write
         try:
+            from agent.usage_pricing import UNKNOWN_TOKENS_LABEL
+
             base_model = getattr(self, "_cache_hit_baseline_model", None)
             base_prompt = int(getattr(self, "_cache_hit_baseline_prompt", 0) or 0)
@@ -198,8 +225,18 @@
             delta_prompt = cur_prompt - base_prompt
             delta_read = cur_read - base_read
+            # A cache RATIO over cumulative counters is only a measurement when
+            # both terms are. An unmeasured call contributes 0 to each sum, so
+            # a ratio computed across it is fabricated — it reads as a cache
+            # miss that never happened (or a 100% hit that did not). Suppress
+            # the percentage and render the explicit unknown label instead.
+            if snapshot.get("session_prompt_tokens_unknown") or snapshot.get(
+                "cache_read_tokens_unknown"
+            ) or snapshot.get("usage_unknown"):
+                snapshot["cache_hit_pct"] = None
+                snapshot["cache_hit_label"] = UNKNOWN_TOKENS_LABEL
             # A zero-read regime hides the segment entirely (no cache data
             # is not the same as a 0% hit worth alarming about), and the pct
             # stays a float so renderers control their own precision.
-            if delta_prompt > 0 and delta_read > 0:
+            elif delta_prompt > 0 and delta_read > 0:
                 pct = max(0.0, min(100.0, (delta_read / delta_prompt) * 100))
                 snapshot["cache_hit_pct"] = pct
```

### `HermesCLI._render_spinner_text`
```diff
--- base::HermesCLI._render_spinner_text
+++ fork::HermesCLI._render_spinner_text
@@ -5,6 +5,6 @@
             return ""
         flow = self._spinner_token_flow()
-        t0 = getattr(self, "_tool_start_time", 0) or 0
-        if t0 > 0:
+        t0 = getattr(self, "_tool_start_time", None)
+        if t0:
             elapsed = time.monotonic() - t0
             if elapsed >= 60:
```

### `HermesCLI._turn_summary_emit`
```diff
--- base::HermesCLI._turn_summary_emit
+++ fork::HermesCLI._turn_summary_emit
@@ -5,5 +5,5 @@
             return
         try:
-            started = getattr(self, "_turn_summary_start", 0.0) or 0.0
+            started = getattr(self, "_turn_summary_start", None)
             elapsed = max(0.0, time.monotonic() - started) if started else 0.0
             line = collector.render(elapsed)
```

### `HermesCLI._build_status_bar_text`
```diff
--- base::HermesCLI._build_status_bar_text
+++ fork::HermesCLI._build_status_bar_text
@@ -129,6 +129,14 @@
             # list, so default bars never widen.
             total_tokens = snapshot.get("session_total_tokens", 0)
-            if total_tokens and field_set is not None and "total_tokens" in field_set:
-                parts.append(f"Σ{format_token_count_compact(total_tokens)}")
+            if (total_tokens or snapshot.get("session_total_tokens_unknown")) and field_set is not None and "total_tokens" in field_set:
+                from agent.usage_pricing import format_token_count
+
+                parts.append(
+                    "Σ" + format_token_count(
+                        total_tokens,
+                        unknown=bool(snapshot.get("session_total_tokens_unknown")),
+                        formatter=format_token_count_compact,
+                    )
+                )
             if not parts:
                 parts = [f"⚕ {snapshot['model_short']}"]
```

### `HermesCLI._get_status_bar_fragments`
```diff
--- base::HermesCLI._get_status_bar_fragments
+++ fork::HermesCLI._get_status_bar_fragments
@@ -157,6 +157,12 @@
                     # fields list, so default bars never widen.
                     total_tokens = snapshot.get("session_total_tokens", 0)
-                    if total_tokens and field_set is not None and "total_tokens" in field_set:
-                        _append(frags, " │ ", ("class:status-bar-dim", f"Σ{format_token_count_compact(total_tokens)}"))
+                    if (total_tokens or snapshot.get("session_total_tokens_unknown")) and field_set is not None and "total_tokens" in field_set:
+                        from agent.usage_pricing import format_token_count
+
+                        _append(frags, " │ ", ("class:status-bar-dim", "Σ" + format_token_count(
+                            total_tokens,
+                            unknown=bool(snapshot.get("session_total_tokens_unknown")),
+                            formatter=format_token_count_compact,
+                        )))
                     if not frags:
                         frags = [
```

## target: `hermes_cli/cli_model_switch_mixin.py`

### `HermesCLI._normalize_model_for_provider`
```diff
--- base::HermesCLI._normalize_model_for_provider
+++ fork::HermesCLI._normalize_model_for_provider
@@ -77,5 +77,74 @@
         # 1. Strip provider prefix ("openai/gpt-5.4" → "gpt-5.4")
         if "/" in current_model:
-            slug = current_model.split("/", 1)[1]
+            prefix, slug = current_model.split("/", 1)
+
+            # Distinguish a benign vendor namespace ("anthropic/…", "openai/…",
+            # "meta-llama/…") from a *provider*-qualified model whose provider
+            # did not resolve for this profile ("claude-apr/claude-fable-5").
+            # The latter means the user asked for a specific provider (often a
+            # plugins/model-providers/<name>/ plugin) that is NOT registered
+            # here — silently stripping the prefix and sending the bare model to
+            # openai-codex substitutes BOTH model and provider, which is a
+            # correctness and cost bug and surfaces only as a confusing
+            # downstream 400. Fail loudly instead. See the claude-apr incident.
+            _prefix = (prefix or "").strip().lower()
+            _is_vendor = False
+            try:
+                from hermes_cli.model_normalize import is_known_vendor_namespace
+
+                _is_vendor = is_known_vendor_namespace(_prefix)
+            except Exception:
+                # If the predicate is unavailable for any reason, fall back to
+                # the historical strip behaviour rather than blocking startup.
+                _is_vendor = True
+
+            if not _is_vendor and _prefix and _prefix != "openai-codex":
+                _provider_resolves = False
+                _resolver_error: Exception | None = None
+                try:
+                    from hermes_cli.auth import AuthError, resolve_provider
+
+                    try:
+                        resolve_provider(_prefix)
+                        _provider_resolves = True
+                    except AuthError:
+                        _provider_resolves = False
+                    except Exception as exc:  # noqa: BLE001
+                        # A registered provider whose plugin crashed internally
+                        # is a different failure from "provider not found".
+                        # Still err toward the loud error (never silently swap
+                        # providers), but surface the root cause so the two
+                        # cases are distinguishable.
+                        _provider_resolves = False
+                        _resolver_error = exc
+                except Exception as exc:  # noqa: BLE001
+                    # Resolver import failure — treat as unresolved so we err
+                    # toward the loud, informative error over a silent swap.
+                    _provider_resolves = False
+                    _resolver_error = exc
+
+                if not _provider_resolves:
+                    _cause = (
+                        f" (provider resolution raised "
+                        f"{type(_resolver_error).__name__}: {_resolver_error} — "
+                        f"this may be a crashing plugin rather than a missing "
+                        f"one)"
+                        if _resolver_error is not None
+                        else ""
+                    )
+                    raise ValueError(
+                        f"Model '{current_model}' names provider '{prefix}', "
+                        f"which does not resolve for this profile{_cause} — "
+                        f"refusing to "
+                        f"strip the prefix and run '{slug}' on the default "
+                        f"provider '{resolved_provider}' (that would silently "
+                        f"substitute both the model and the provider). If "
+                        f"'{prefix}' is a model-providers plugin, make sure the "
+                        f"profile can see it (e.g. the plugins/model-providers "
+                        f"symlink or its credential_pool entry). Otherwise pass "
+                        f"a model whose vendor prefix is recognized, or select "
+                        f"the intended provider explicitly with --provider."
+                    )
+
             if not self._model_is_default:
                 self._console_print(
```

## target: `hermes_cli/cli_info_mixin.py`

### `HermesCLI.show_banner`
```diff
--- base::HermesCLI.show_banner
+++ fork::HermesCLI.show_banner
@@ -125,4 +125,11 @@
                 self._show_tool_availability_warnings()
 
+        # Nudge to resume a prior turn that was cut off mid-flight (restart /
+        # reboot / terminal-close). The tool+API work is already persisted, so
+        # `hermes chat -c` recovers it without re-running anything. Only on a
+        # FRESH launch (never when already resuming), best-effort.
+        if not getattr(self, "_resumed", False):
+            self._maybe_nudge_resume_interrupted_session()
+
         # Warn about low context lengths (common with local servers). Keep
         # this tied to the runtime guard so guidance cannot drift again.
```

### `HermesCLI._fast_command_available`
```diff
--- base::HermesCLI._fast_command_available
+++ fork::HermesCLI._fast_command_available
@@ -1,8 +1,9 @@
     def _fast_command_available(self) -> bool:
+        """True when the route supports ANY static tier (fast/priority or ultrafast)."""
         try:
-            from hermes_cli.models import model_supports_fast_mode
+            return (
+                HermesCLI._fast_capability(self).supported
+                or HermesCLI._fast_capability(self, "ultrafast").supported
+            )
         except Exception:
             return False
-        agent = getattr(self, "agent", None)
-        model = getattr(agent, "model", None) or getattr(self, "model", None)
-        return model_supports_fast_mode(model)
```

### `HermesCLI._show_usage`
```diff
--- base::HermesCLI._show_usage
+++ fork::HermesCLI._show_usage
@@ -49,14 +49,32 @@
         elapsed = format_duration_compact((datetime.now() - self.session_start).total_seconds())
 
+        # UNKNOWN != 0, cumulative + absorbing. Shared rule (the same
+        # format_token_count the Blackbox card and status bar use) so one
+        # unmeasured call in the window renders that term "unknown" here
+        # instead of a right-aligned number the user would read as measured.
+        from agent.usage_pricing import (
+            format_token_count, prompt_tokens_unknown,
+            session_total_tokens_unknown, session_usage_unknown_flags,
+        )
+
+        _flags = session_usage_unknown_flags(agent)
+        _any = session_total_tokens_unknown(agent)
+        _prompt_unknown = prompt_tokens_unknown(_flags)
+
+        def _tok(value: int, unknown: bool) -> str:
+            return format_token_count(
+                value, unknown=unknown, formatter=lambda v: f"{int(v):,}"
+            ).rjust(10)
+
         print("  📊 Session Token Usage")
         print(f"  {'─' * 40}")
         print(f"  Model:                     {agent.model}")
-        print(f"  Input tokens:              {input_tokens:>10,}")
-        print(f"  Output tokens:             {output_tokens:>10,}")
+        print(f"  Input tokens:              {_tok(input_tokens, _flags['input_tokens_unknown'] or _flags['usage_unknown'])}")
+        print(f"  Output tokens:             {_tok(output_tokens, _flags['output_tokens_unknown'] or _flags['usage_unknown'])}")
         if reasoning_tokens:
             print(f"  ↳ Reasoning (subset):      {reasoning_tokens:>10,}")
-        print(f"  Prompt tokens (total):     {prompt:>10,}")
-        print(f"  Completion tokens:         {completion:>10,}")
-        print(f"  Total tokens:              {total:>10,}")
+        print(f"  Prompt tokens (total):     {_tok(prompt, _prompt_unknown)}")
+        print(f"  Completion tokens:         {_tok(completion, _flags['output_tokens_unknown'] or _flags['usage_unknown'])}")
+        print(f"  Total tokens:              {_tok(total, _any)}")
         print(f"  API calls:                 {calls:>10,}")
         print(f"  Session duration:          {elapsed:>10}")
```

## target: `hermes_cli/cli_session_mixin.py`

### `HermesCLI._show_session_status`
```diff
--- base::HermesCLI._show_session_status
+++ fork::HermesCLI._show_session_status
@@ -1,4 +1,6 @@
     def _show_session_status(self):
         """Show gateway-style status for the current CLI session."""
+        from agent.usage_pricing import format_token_count, session_total_tokens_unknown
+
         session_meta = {}
         if self._session_db:
@@ -97,5 +99,5 @@
             f"Created: {created_at.strftime('%Y-%m-%d %H:%M')}",
             f"Last Activity: {updated_at.strftime('%Y-%m-%d %H:%M')}",
-            f"Tokens: {total_tokens:,}",
+            f"Tokens: {format_token_count(total_tokens, unknown=session_total_tokens_unknown(agent), formatter=lambda n: f'{n:,}')}",
             f"Agent Running: {'Yes' if is_running else 'No'}",
         ])
```

### `HermesCLI.undo_last`
```diff
--- base::HermesCLI.undo_last
+++ fork::HermesCLI.undo_last
@@ -1,135 +1,45 @@
     def undo_last(self, n: int = 1, prefill: bool = True):
-        """Back up N user turns: truncate history, soft-delete on disk, prefill.
+        """Undo N half-turns via the shared undo core and render its prefill.
 
-        Walks backwards N user messages and discards everything from the
-        Nth-from-last user message onward (its assistant response, tool
-        calls, etc.). ``n`` defaults to 1 (the last exchange); ``/undo 3``
-        backs up three user turns. If ``n`` exceeds the number of user
-        turns, it backs up to the oldest one.
+        For the default ``n=1`` with warm history available, prefer the
+        upstream carrier-rewind contract (full user-turn rewind bound
+        warm↔durable, live-ask-only prefill); the half-turn core remains the
+        path for explicit counts and for callers driving straight off the
+        durable transcript (gateway /undo, warm-less CLI states).
+        """
+        if not self.session_id:
+            print("(._.) No active session to undo.")
+            return
+        if (
+            n == 1
+            and self._session_db is not None
+            and self.conversation_history
+            and self._undo_last_user_turn(prefill=prefill)
+        ):
+            return
+        try:
+            import hermes_undo
 
-        Beyond the in-memory ``conversation_history`` slice, this also:
-          • soft-deletes the truncated rows in SessionDB (``active=0``) so
-            they're hidden from re-prompts and search but kept for audit;
-          • notifies memory providers via ``on_session_switch(rewound=True)``;
-          • mirrors /branch's agent surgery (system-prompt invalidation +
-            flush-index reset);
-          • when ``prefill`` is set and an input buffer is available,
-            pre-fills the composer with the backed-up message text so it
-            can be edited and resubmitted.
-
-        ``prefill=False`` is used by callers that drive the undo
-        programmatically (e.g. checkpoint rollback) and don't want to
-        touch the user's input buffer.
-        """
-        if not self.conversation_history:
-            print("(._.) No messages to undo.")
+            if self._session_db is not None:
+                hermes_undo._session_db = self._session_db
+            result = hermes_undo.undo(self.session_id, n)
+        except Exception as e:
+            logger.debug("undo: failed: %s", e)
+            print(f"(._.) Undo failed: {e}")
             return
 
-        if n < 1:
-            n = 1
-
-        # Walk backwards collecting the indices of the last N *real* user
-        # messages (exclude display_kind timeline rows and compaction
-        # handoffs — same predicate as user_originated_turn_view, resume
-        # turn counting, and /retry; #80622).
-        from agent.context_compressor import (
-            history_before_user_originated_turn,
-            user_originated_turn_view,
-        )
-        from run_agent import _is_ephemeral_scaffolding
-
-        warm_history = list(self.conversation_history)
-
-        user_indices = [
-            index
-            for index, message in enumerate(warm_history)
-            if not _is_ephemeral_scaffolding(message)
-            and user_originated_turn_view(message) is not None
-        ]
-
-        if not user_indices:
-            print("(._.) No user message found to undo.")
+        # NOTE(parity): upstream 1e5b5074 added a display_kind predicate to the
+        # old user-index walk here; the fork relocated undo into the shared
+        # hermes_undo core (half-turn rewind), so that walk no longer exists.
+        rewound_ids = list(result.get("rewound_ids") or [])
+        if not rewound_ids:
+            print("(._.) Nothing to undo.")
             return
 
-        turns_undone = min(n, len(user_indices))
-        target_ordinal = len(user_indices) - turns_undone
-        cut_idx = user_indices[target_ordinal]
-
-        removed_count = len(warm_history) - cut_idx
-        truncated, live_view = history_before_user_originated_turn(
-            warm_history, cut_idx
-        )
-        removed_text = self._undo_content_to_text(live_view.get("content"))
-
-        # Soft-delete the truncated rows on disk so re-prompts and search
-        # see the clean transcript while the rows survive for audit.
-        rewound_rows = 0
-        if self._session_db is not None and self.session_id:
-            try:
-                truncated, durable_live_view, result = (
-                    self._rewind_persisted_user_turn(
-                        warm_history=warm_history,
-                        user_ordinal=target_ordinal,
-                        warm_live_view=live_view,
-                    )
-                )
-                # Canonicalize the editable prefill before mutation. The raw
-                # physical carrier contains the reference summary wrapper.
-                durable_text = self._undo_content_to_text(
-                    durable_live_view.get("content")
-                )
-                if durable_text:
-                    removed_text = durable_text
-                rewound_rows = result.get("rewound_count", 0)
-            except Exception as e:
-                logger.debug("undo: durable rewind failed: %s", e)
-                print(f"(x_x) Undo failed; history was not changed: {e}")
-                return
-
-        # Publish only after the durable rewind succeeds (or no store exists).
-        self.conversation_history = truncated
-
-        # Agent surgery: invalidate the system-prompt cache and reset the
-        # flush index so the next turn re-flushes from the truncated head.
-        if self.agent is not None:
-            if hasattr(self.agent, "_invalidate_system_prompt"):
-                try:
-                    self.agent._invalidate_system_prompt()
-                except Exception:
-                    pass
-            if hasattr(self.agent, "_last_flushed_db_idx"):
-                try:
-                    self.agent._last_flushed_db_idx = len(self.conversation_history)
-                except Exception:
-                    pass
-            if hasattr(self.agent, "_session_messages"):
-                self.agent._session_messages = self.conversation_history
-            if hasattr(self.agent, "_db_flush_scan_prefix"):
-                self.agent._db_flush_scan_prefix = self.conversation_history[:]
-            # Notify memory providers — same hook /branch fires, with the
-            # rewound flag so per-turn document caches invalidate (#6672, #21910).
-            try:
-                _mm = getattr(self.agent, "_memory_manager", None)
-                if _mm is not None and self.session_id:
-                    _mm.on_session_switch(
-                        self.session_id,
-                        parent_session_id="",
-                        reset=False,
-                        rewound=True,
-                    )
-            except Exception:
-                pass
-
-        turn_word = "turn" if turns_undone == 1 else "turns"
-        msg_count = rewound_rows or removed_count
-        print(
-            f"(^_^)b Undid {turns_undone} {turn_word} ({msg_count} message(s)). "
-            f"Backed up to: \"{removed_text[:60]}{'...' if len(removed_text) > 60 else ''}\""
-        )
-        remaining = len(self.conversation_history)
-        print(f"  {remaining} message(s) remaining in history.")
-
-        # Pre-fill the composer with the backed-up message so the user can
-        # edit and resubmit (Claude-Code-style). Editable, not auto-sent.
-        if prefill and removed_text:
-            self._prefill_input_buffer(removed_text)
+        self._reload_active_history_after_rewind(rewound=True)
+        count = len(rewound_ids)
+        prefill_text = result.get("prefill_text")
+        print(f"(^_^)b Undid {result.get('half_turns', n)} half-turn(s) ({count} message(s)).")
+        print(f"  {len(self.conversation_history)} message(s) remaining in history.")
+        if prefill and isinstance(prefill_text, str):
+            self._prefill_input_buffer(prefill_text)
```

### `HermesCLI._manual_compress`
```diff
--- base::HermesCLI._manual_compress
+++ fork::HermesCLI._manual_compress
@@ -139,4 +139,7 @@
                     approx_tokens=approx_tokens,
                     focus_topic=focus_topic or None,
+                    # Attribution (2026-08-20 audit): manual /compress from the
+                    # CLI carries the same trigger label as the gateway path.
+                    trigger_reason="manual_compress_command",
                     force=True,
                     defer_context_engine_notification=True,
```

### `HermesCLI._print_exit_summary`
```diff
--- base::HermesCLI._print_exit_summary
+++ fork::HermesCLI._print_exit_summary
@@ -59,5 +59,5 @@
             print(f"  hermes --resume {self.session_id}{profile_flag}")
             if session_title:
-                print(f"  hermes -c \"{session_title}\"{profile_flag}")
+                print(f"  hermes -c {hint_value(session_title)}{profile_flag}")
             print()
             print(f"Session:        {self.session_id}")
```

## target: `hermes_cli/cli_process_notifications.py`

### `HermesCLI._drain_process_notifications`
```diff
--- base::HermesCLI._drain_process_notifications
+++ fork::HermesCLI._drain_process_notifications
@@ -10,5 +10,5 @@
         from tools.async_delegation import (
             claim_event_delivery,
-            complete_event_delivery,
+            complete_event_delivery_with_retry,
         )
 
@@ -22,3 +22,3 @@
                 continue
             self._pending_input.put(synthetic_message)
-            complete_event_delivery(event, claim)
+            complete_event_delivery_with_retry(event, claim)
```

## target: `agent/turn_facade.py,hermes_cli/cli_chat_turn_mixin.py`

### `HermesCLI.chat`
```diff
--- base::HermesCLI.chat
+++ fork::HermesCLI.chat
@@ -178,4 +178,12 @@
             with persist_lock:
                 _stage_user_message()
+
+        if self.session_id:
+            try:
+                from hermes_undo import on_user_message_appended
+
+                on_user_message_appended(self.session_id)
+            except Exception as e:
+                logger.debug("redo clear on user append failed: %s", e)
 
         ChatConsole().print(f"[{_accent_hex()}]{'─' * 40}[/]")
```

## target: `hermes_cli/cli_single_query.py`

### `_run_kanban_goal_loop_q`
```diff
--- base::_run_kanban_goal_loop_q
+++ fork::_run_kanban_goal_loop_q
@@ -46,4 +46,5 @@
 
     max_turns = task.goal_max_turns or _DEF_TURNS
+    expected_run_id = _goal_loop_run_id(task_id)
 
     def _run_turn(prompt: str) -> str:
@@ -58,4 +59,8 @@
         ):
             cli.session_id = cli.agent.session_id
+        if isinstance(result, dict) and result.get("failed"):
+            from hermes_cli.kanban_worker_exit import WorkerExit
+            print(f"\nsession_id: {cli.session_id}", file=sys.stderr)
+            raise WorkerExit(result)
         resp = result.get("final_response", "") if isinstance(result, dict) else str(result)
         if resp:
@@ -66,5 +71,5 @@
         c = _kb.connect()
         try:
-            return _kb.goal_run_status(c, task_id, worker_run_id)
+            return _kb.goal_run_status(c, task_id, expected_run_id)
         finally:
             try:
@@ -76,9 +81,17 @@
         c = _kb.connect()
         try:
+            # OWNERSHIP GUARD (2026-08-07). Without expected_run_id a ZOMBIE run
+            # — one the dispatcher already closed and replaced — can block a card
+            # that a LIVE successor run owns and is actively working. Observed on
+            # the parity-merge relay: runs 68/69/70 kept receiving goal-loop
+            # continuation re-prompts after their run rows were closed, and one
+            # of them blocked the card out from under its successor. Every other
+            # worker-side lifecycle mutation already passes this guard
+            # (tools/kanban_tools.py:365,811,910,974); this path was the only
+            # unguarded one. block_task appends `AND current_run_id = ?` when the
+            # id is supplied, so a stale run's write is refused instead of
+            # silently winning.
             _kb.block_task(
-                c,
-                task_id,
-                reason=reason,
-                expected_run_id=worker_run_id,
+                c, task_id, reason=reason, expected_run_id=expected_run_id
             )
         finally:
```

### `main._signal_handler_q`
```diff
--- base::main._signal_handler_q
+++ fork::main._signal_handler_q
@@ -8,4 +8,14 @@
             _agent = getattr(cli, "agent", None)
             if _agent is not None:
+                # An external termination (not Ctrl-C) ends this turn: stamp
+                # the marker BEFORE interrupting, because the loop's own
+                # finalizer may write the turn's real row during the grace
+                # window below (it then wins over _finalize_signaled_kanban_worker)
+                # and only it can carry the marker into that row.
+                import signal as _sigmod
+                if signum != _sigmod.SIGINT:
+                    _agent._turn_terminal_error = (
+                        getattr(_agent, "_current_turn_id", None), f"signal_{int(signum)}",
+                    )
                 request_hard_interrupt(_agent, f"received signal {signum}")
                 try:
@@ -39,12 +49,27 @@
             except Exception:
                 pass
+            # Leave evidence BEFORE dying (card t_0c1ebbae): os._exit skips the
+            # atexit receipt, so an externally-ended worker used to vanish with
+            # no last words and a Popen rc of 0 the dispatcher read as a
+            # "protocol violation". One line into the run's log segment, and a
+            # signal receipt the dispatcher classifies as ``signaled``.
+            try:
+                sys.stderr.write(
+                    f"\n[kanban-worker] pid {os.getpid()} received signal {signum} "
+                    f"at {time.strftime('%Y-%m-%dT%H:%M:%S%z')} — terminated from "
+                    f"outside; exiting\n"
+                )
+            except Exception:
+                pass
+            try:
+                from hermes_cli.kanban_worker_exit import write_exit_status
+                write_exit_status(128 + int(signum), exit_class="signaled")
+            except Exception:
+                pass
             # os._exit(0) skips atexit AND SessionDB's token-drain hook, so
             # flush + finalize the session store here or the worker's turn
             # (and its usage deltas) never become durable (#88583 / #50881
             # class). Best-effort under the SIGALRM deadman above.
-            try:
-                _flush_one_shot_session_store(cli)
-            except Exception:
-                pass
+            _finalize_signaled_kanban_worker(cli, signum)
             try:
                 import logging as _lg
```

## target: `NOWHERE (upstream deleted/renamed — find replacement)`

### `_run_kanban_goal_loop_q._run_turn`
```diff
--- base::_run_kanban_goal_loop_q._run_turn
+++ fork::_run_kanban_goal_loop_q._run_turn
@@ -10,4 +10,8 @@
         ):
             cli.session_id = cli.agent.session_id
+        if isinstance(result, dict) and result.get("failed"):
+            from hermes_cli.kanban_worker_exit import WorkerExit
+            print(f"\nsession_id: {cli.session_id}", file=sys.stderr)
+            raise WorkerExit(result)
         resp = result.get("final_response", "") if isinstance(result, dict) else str(result)
         if resp:
```

## target: `hermes_cli/cli_single_query.py,hermes_cli/kanban_db.py`

### `_run_kanban_goal_loop_q._task_status`
```diff
--- base::_run_kanban_goal_loop_q._task_status
+++ fork::_run_kanban_goal_loop_q._task_status
@@ -2,5 +2,5 @@
         c = _kb.connect()
         try:
-            return _kb.goal_run_status(c, task_id, worker_run_id)
+            return _kb.goal_run_status(c, task_id, expected_run_id)
         finally:
             try:
```

## target: `hermes_cli/cli_single_query.py,hermes_cli/goals.py,hermes_cli/kanban_native_worker.py`

### `_run_kanban_goal_loop_q._block`
```diff
--- base::_run_kanban_goal_loop_q._block
+++ fork::_run_kanban_goal_loop_q._block
@@ -2,9 +2,17 @@
         c = _kb.connect()
         try:
+            # OWNERSHIP GUARD (2026-08-07). Without expected_run_id a ZOMBIE run
+            # — one the dispatcher already closed and replaced — can block a card
+            # that a LIVE successor run owns and is actively working. Observed on
+            # the parity-merge relay: runs 68/69/70 kept receiving goal-loop
+            # continuation re-prompts after their run rows were closed, and one
+            # of them blocked the card out from under its successor. Every other
+            # worker-side lifecycle mutation already passes this guard
+            # (tools/kanban_tools.py:365,811,910,974); this path was the only
+            # unguarded one. block_task appends `AND current_run_id = ?` when the
+            # id is supplied, so a stale run's write is refused instead of
+            # silently winning.
             _kb.block_task(
-                c,
-                task_id,
-                reason=reason,
-                expected_run_id=worker_run_id,
+                c, task_id, reason=reason, expected_run_id=expected_run_id
             )
         finally:
```


## target: facade methods whose BODIES upstream moved into mixins (delta NOT applied in `cli.py`)

- `HermesCLI.__init__` -> `hermes_cli/cli_init_mixin.py` (`_init_model_routing`/`_init_runtime_state`): startup `-m <alias>` / `provider/model` resolution via `hermes_cli.model_switch.resolve_startup_model_arg`, `self._explicit_provider` + `self._kanban_pin_rate_limited` (kanban dispatcher pin, t_4fe0700a), `float("-inf")` monotonic inits.
- `HermesCLI.run` -> `hermes_cli/cli_tui_mixin.py:1849` `_last_config_check = float("-inf")`.
- `main` (`-q` path) -> `hermes_cli/cli_single_query.py` (`_install_single_query_signal_handlers`, `_run_single_query_mode`): `_turn_terminal_error` stamp on non-SIGINT, stderr evidence line + `kanban_worker_exit.write_exit_status(128+signum, exit_class="signaled")`, `_finalize_signaled_kanban_worker(cli, signum)` (defined in the facade) instead of bare `_flush_one_shot_session_store`, goal loop skipped on failed result, `WorkerExit(apply_pin_refusal_to_result(...))` instead of exit-code arithmetic, pinned-worker cooldown -> rate_limit WorkerExit, `report_exit` in the finally when `HERMES_KANBAN_EXIT_FILE` is set.
- `HermesCLI.process_command`: APPLIED (`/redo`, `/boomerang` as `_handle_*_command`, built-in-preferring prefix expansion in `_expand_slash_prefix`); the fork `/undo` half-turn wording lives in `cli_session_mixin.py::_handle_undo_command` -> see that target above.



### `HermesCLI.__init__`
```diff
--- base
+++ fork
@@ -146,9 +146,9 @@
         self._stream_table_buf: list[str] = []
         self._in_stream_table = False
         self._pending_edit_snapshots = {}
-        self._last_input_mode_recovery = 0.0
+        self._last_input_mode_recovery = float("-inf")  # monotonic; 0.0 = "just now" on fresh boot
         self._input_mode_recovery_notice_shown = False
-        self._last_termios_drift_check = 0.0
+        self._last_termios_drift_check = float("-inf")
         self._termios_drift_notice_shown = False
         
         # Configuration - priority: CLI args > env vars > config file
@@ -175,6 +175,31 @@
         # through MoA instead of hitting the real provider with an unknown
         # model (#56828). A ``moa:`` prefix wins over an explicit ``--provider``.
         _moa_provider_override, self.model = _normalize_moa_model(self.model)
+        # Resolve `-m <alias>` / `-m <provider>/<model>` exactly like the
+        # interactive `/model` command would (config `model.aliases` +
+        # inline provider qualification). Without this the raw string went
+        # to the current provider, 400'd, and the fallback chain silently
+        # served a different provider+model with only a one-line banner.
+        # Applies ONLY to an explicit CLI arg; config `model.default` is
+        # already a concrete id. A ``moa:`` prefix was handled above.
+        _inline_provider_override: Optional[str] = None
+        if model and not _moa_provider_override:
+            from hermes_cli.model_switch import resolve_startup_model_arg
+            _cfg_provider_for_alias = (
+                provider
+                or _nested_provider
+                or CLI_CONFIG["model"].get("provider")
+                or ""
+            )
+            _inline_provider_override, self.model = resolve_startup_model_arg(
+                self.model,
+                _cfg_provider_for_alias,
+                CLI_CONFIG.get("providers") if isinstance(CLI_CONFIG.get("providers"), dict) else None,
+                CLI_CONFIG.get("custom_providers") if isinstance(CLI_CONFIG.get("custom_providers"), list) else None,
+            )
+            if provider and _inline_provider_override and _inline_provider_override != provider:
+                # `--provider X -m Y/model`: the explicit flag wins, like /model.
+                _inline_provider_override = None
         # Read max_tokens from config (env var override: HERMES_MAX_TOKENS)
         _env_mt = os.environ.get("HERMES_MAX_TOKENS")
         if _env_mt:
@@ -207,11 +232,16 @@
 
         self._explicit_api_key = api_key
         self._explicit_base_url = base_url
+        # Raw ``--provider`` flag. A kanban worker treats it as the
+        # dispatcher's pin and refuses auth-time substitution (t_4fe0700a).
+        self._explicit_provider = provider
+        self._kanban_pin_rate_limited: Optional[str] = None
 
         # Provider selection is resolved lazily at use-time via _ensure_runtime_credentials().
         self.requested_provider = (
             _moa_provider_override
             or provider
+            or _inline_provider_override
             or _nested_provider
             or CLI_CONFIG["model"].get("provider")
             or os.getenv("HERMES_INFERENCE_PROVIDER")
@@ -548,7 +578,7 @@
         self._pet_kitty_pending: str = ""
         self._pet_frame_idx: int = 0
         self._pet_lock = threading.Lock()
-        self._pet_cfg_checked: float = 0.0
+        self._pet_cfg_checked: float = float("-inf")
         self._pet_anim_running: bool = False
         self._pet_anim_thread = None
         # Transient reaction beats (wave/jump/failed) + steady reasoning flag.


```
### `HermesCLI.run`
```diff
--- base
+++ fork
@@ -216,7 +216,7 @@
         _cfg_path = _get_config_path()
         self._config_mtime: float = _cfg_path.stat().st_mtime if _cfg_path.exists() else 0.0
         self._config_mcp_servers: dict = self.config.get("mcp_servers") or {}
-        self._last_config_check: float = 0.0  # monotonic time of last check
+        self._last_config_check: float = float("-inf")  # monotonic time of last check
 
         # Clarify tool state: interactive question/answer with the user.
         # When the agent calls the clarify tool, _clarify_state is set and


```
### `main`
```diff
--- base
+++ fork
@@ -303,6 +303,16 @@
         try:
             _agent = getattr(cli, "agent", None)
             if _agent is not None:
+                # An external termination (not Ctrl-C) ends this turn: stamp
+                # the marker BEFORE interrupting, because the loop's own
+                # finalizer may write the turn's real row during the grace
+                # window below (it then wins over _finalize_signaled_kanban_worker)
+                # and only it can carry the marker into that row.
+                import signal as _sigmod
+                if signum != _sigmod.SIGINT:
+                    _agent._turn_terminal_error = (
+                        getattr(_agent, "_current_turn_id", None), f"signal_{int(signum)}",
+                    )
                 request_hard_interrupt(_agent, f"received signal {signum}")
                 try:
                     _grace = float(os.getenv("HERMES_SIGTERM_GRACE", "1.5"))
@@ -334,14 +344,29 @@
                     _sig_mod.alarm(5)
             except Exception:
                 pass
+            # Leave evidence BEFORE dying (card t_0c1ebbae): os._exit skips the
+            # atexit receipt, so an externally-ended worker used to vanish with
+            # no last words and a Popen rc of 0 the dispatcher read as a
+            # "protocol violation". One line into the run's log segment, and a
+            # signal receipt the dispatcher classifies as ``signaled``.
+            try:
+                sys.stderr.write(
+                    f"\n[kanban-worker] pid {os.getpid()} received signal {signum} "
+                    f"at {time.strftime('%Y-%m-%dT%H:%M:%S%z')} — terminated from "
+                    f"outside; exiting\n"
+                )
+            except Exception:
+                pass
+            try:
+                from hermes_cli.kanban_worker_exit import write_exit_status
+                write_exit_status(128 + int(signum), exit_class="signaled")
+            except Exception:
+                pass
             # os._exit(0) skips atexit AND SessionDB's token-drain hook, so
             # flush + finalize the session store here or the worker's turn
             # (and its usage deltas) never become durable (#88583 / #50881
             # class). Best-effort under the SIGALRM deadman above.
-            try:
-                _flush_one_shot_session_store(cli)
-            except Exception:
-                pass
+            _finalize_signaled_kanban_worker(cli, signum)
             try:
                 import logging as _lg
                 _lg.shutdown()
@@ -567,7 +592,10 @@
                         # out (→ sticky block). Gated on the env vars the
                         # dispatcher sets in `_default_spawn`; a no-op for every
                         # normal worker and every non-kanban `-q` run.
-                        if os.environ.get("HERMES_KANBAN_GOAL_MODE") == "1":
+                        if (
+                            os.environ.get("HERMES_KANBAN_GOAL_MODE") == "1"
+                            and not (isinstance(result, dict) and result.get("failed"))
+                        ):
                             try:
                                 _run_kanban_goal_loop_q(cli, response)
                             except Exception as _goal_exc:
@@ -588,20 +616,21 @@
                         # 5-hour quota window can't trip the circuit breaker and
                         # permanently block the card. Non-kanban runs keep the
                         # plain 0/1 contract automation wrappers expect.
-                        _exit_code = 0
-                        if isinstance(result, dict) and result.get("failed"):
-                            _exit_code = 1
-                            if os.environ.get("HERMES_KANBAN_TASK") and result.get(
-                                "failure_reason"
-                            ) in ("rate_limit", "billing"):
-                                try:
-                                    from hermes_cli.kanban_db import (
-                                        KANBAN_RATE_LIMIT_EXIT_CODE as _RL_CODE,
-                                    )
-                                    _exit_code = _RL_CODE
-                                except Exception:
-                                    _exit_code = 1
-                        sys.exit(_exit_code)
+                        from hermes_cli.kanban_worker_exit import WorkerExit
+                        from hermes_cli.kanban_worker_route import (
+                            apply_pin_refusal_to_result,
+                        )
+                        raise WorkerExit(apply_pin_refusal_to_result(cli.agent, result))
+
+                # A pinned kanban worker whose provider is in cooldown exits
+                # rate-limited (retry-preserving) instead of running elsewhere.
+                if getattr(cli, "_kanban_pin_rate_limited", None):
+                    from hermes_cli.kanban_worker_exit import WorkerExit
+                    raise WorkerExit({
+                        "failed": True,
+                        "failure_reason": "rate_limit",
+                        "error": cli._kanban_pin_rate_limited,
+                    })
 
                 # Exit with error code if credentials or agent init fails
                 sys.exit(1)
@@ -628,7 +657,12 @@
                 cli.chat(query, images=single_query_images or None)
                 cli._print_exit_summary(clear_screen=False)
         finally:
-            _finalize_single_query(cli)
+            try:
+                _finalize_single_query(cli)
+            finally:
+                if os.environ.get("HERMES_KANBAN_EXIT_FILE"):
+                    from hermes_cli.kanban_worker_exit import report_exit
+                    report_exit(sys.exc_info()[1])
         return
     
     # Run interactive mode
```
