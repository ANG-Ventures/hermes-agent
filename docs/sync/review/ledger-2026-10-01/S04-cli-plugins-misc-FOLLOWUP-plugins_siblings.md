# FOLLOWUP — lane S04-cli-plugins-misc: hermes_cli/plugins.py fork deltas that now live OUTSIDE the lane

Upstream split `hermes_cli/plugins.py` (7182→2331 lines) into `plugins_{dispatch,loader,ledger,manifest,
state,...}.py` (all unconflicted, NOT in this lane). The facade keeps every fork delta whose function is
still on the facade (see ledger row). The fork deltas below land on functions upstream MOVED and are
NOT in the merged tree until applied there.

## §1 `hermes_cli/plugins_dispatch.py::PluginDispatchMixin.invoke_hook` — fork hook-timeout rewrite
Fork commits (ang-fleet-workers / Kyzcreig, 2026-09): "ordinary concurrent invocations are not timeouts and
must not block one another in the multiplexed gateway". Upstream's `invoke_hook` (lines ~214-350) still
carries the base-era `_hook_running_callbacks` IN-FLIGHT gate: a second concurrent invocation of the same
callback is skipped (and for `pre_tool_call` fails CLOSED with a block) while the first is still running.
Fork behaviour to port:
1. Drop `_hook_running_callbacks` entirely (gate + token bookkeeping in the worker `finally`). Suppression
   starts ONLY after an invocation actually exceeds its budget (`_hook_timeout_suppressed_until`).
   Then remove `self._hook_running_callbacks` from `PluginManager.__init__` (facade, kept for now so
   upstream's dispatch code does not AttributeError) and `.clear()` in `plugins_ledger.py::_unload_scoped` L299.
2. Per-callback `fail_closed = bool(getattr(cb, "_hermes_timeout_fail_closed", hook_name in _HOOK_TIMEOUT_FAIL_CLOSED_HOOKS))`
   — shell hooks (`agent/shell_hooks.py`) preserve their individual `fail_closed` setting.
3. Worker `_runner` binds `_context/_done/_outcome/_failure` as DEFAULT ARGUMENTS (snapshotted at definition
   time). A closure resolves free variables at CALL time: a worker abandoned on timeout keeps running and by
   the time it writes back the loop has advanced, so a free `done`/`outcome` would release the NEXT
   callback's wait early and overwrite its result.
4. Messages carry identity + numbers: `_PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE = "pre_tool_call plugin callback
   {callback} timed out after {elapsed:.3f}s (budget {budget:g}s)"` (formatted by
   `_pre_tool_call_timeout_block(callback, elapsed, budget)`), and a DISTINCT
   `_PRE_TOOL_CALL_SUPPRESSED_BLOCK_MESSAGE = "pre_tool_call plugin callback {callback} is suppressed after an
   earlier timeout (retry in {remaining:.0f}s)"` (`_pre_tool_call_suppressed_block(callback, remaining)`), so
   the shell-hook policy gates (`hermes-shell-hook-policy-gates` skill) can tell suppressed from timed-out.
   Both helpers + the messages belong in `plugins_dispatch.py` (where upstream put the message constant);
   the facade re-exports `_PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE` from there already.
5. `callback_name = _callback_label(cb)` everywhere `getattr(cb, "__name__", repr(cb))` appears in
   plugins_dispatch.py (L63, L258, L276, L502 and `invoke_hook`/`_deliver_event`/`invoke_middleware`):
   `repr()` of a `functools.partial(fn, api_token)` renders the token into a MODEL-VISIBLE refusal message.
   `_callback_label` is on the facade (`from hermes_cli.plugins import _callback_label` late import, same
   pattern as `_resolve_hook_callback_timeout` at L218/L494). Fork test:
   `tests/hermes_cli/test_plugins.py::test_anonymous_callback_label_is_stable_not_a_heap_address`.
6. Timeout log line: `"Hook '%s' callback %s timed out after %.3fs (budget %gs) — skipping"` with the measured
   elapsed; suppression log: `"... suppressed after callback timeout (%.1fs remaining)"`.

### Fork diff (base → fork) of the moved methods
```diff
######## PluginManager.invoke_hook
--- base
+++ ours
@@ -11,8 +11,10 @@
         policy hook ``pre_tool_call`` are bounded by
         ``plugins.hook_callback_timeout`` (default 30s). On timeout the worker
         is abandoned (not joined) so we do not reintroduce the #6622 hang.
-        Timed-out or still-running ``pre_tool_call`` callbacks fail closed
-        with a block directive; other bounded hooks fail open (skip).
+        Timed-out ``pre_tool_call`` callbacks fail closed by default; shell
+        hooks preserve their individual ``fail_closed`` setting. Other bounded
+        hooks fail open (skip). A timeout suppresses later invocations of that
+        callback briefly, but ordinary concurrent invocations remain independent.
 
         ``subagent_stop`` (and any hook in ``_HOOK_CALLER_THREAD_HOOKS``)
         always runs on the caller thread to preserve the documented parent-
@@ -42,66 +44,82 @@
         results: List[Any] = []
         timeout = _resolve_hook_callback_timeout()
         use_timeout = _hook_uses_callback_timeout(hook_name, timeout)
-        fail_closed = hook_name in _HOOK_TIMEOUT_FAIL_CLOSED_HOOKS
-
         for cb in callbacks:
-            callback_name = getattr(cb, "__name__", repr(cb))
+            callback_name = _callback_label(cb)
             callback_key = (hook_name, id(cb))
+            fail_closed = bool(
+                getattr(
+                    cb,
+                    "_hermes_timeout_fail_closed",
+                    hook_name in _HOOK_TIMEOUT_FAIL_CLOSED_HOOKS,
+                )
+            )
             try:
                 if use_timeout:
-                    token = object()
                     now = time.monotonic()
                     with self._hook_timeout_lock:
                         suppressed_until = self._hook_timeout_suppressed_until.get(
                             callback_key
                         )
-                        running = callback_key in self._hook_running_callbacks
-                        if (
-                            suppressed_until is not None and suppressed_until > now
-                        ) or running:
+                        if suppressed_until is not None and suppressed_until > now:
+                            remaining = suppressed_until - now
                             logger.warning(
-                                "Hook '%s' callback %s skipped after previous "
-                                "timeout or while still running",
+                                "Hook '%s' callback %s suppressed after callback "
+                                "timeout (%.1fs remaining)",
                                 hook_name,
                                 callback_name,
+                                remaining,
                             )
                             if fail_closed:
-                                results.append(_pre_tool_call_timeout_block())
+                                results.append(
+                                    _pre_tool_call_suppressed_block(
+                                        callback_name, remaining
+                                    )
+                                )
                             continue
                         if suppressed_until is not None:
                             self._hook_timeout_suppressed_until.pop(callback_key, None)
-                        self._hook_running_callbacks[callback_key] = token
 
                     context = contextvars.copy_context()
                     done = threading.Event()
                     outcome: Dict[str, Any] = {}
                     failure: Dict[str, Exception] = {}
 
+                    # Every object the worker touches is bound as a default
+                    # argument, so it is snapshotted into the thread's own frame
+                    # at definition time. A closure resolves a free variable at
+                    # CALL time: a worker abandoned on timeout keeps running,
+                    # and by the time it writes back the loop has advanced, so
+                    # a free `done`/`outcome`/`failure` would resolve to the
+                    # NEXT callback's objects — releasing that callback's wait
+                    # early and overwriting its result with the abandoned one's.
+                    # Re-creating the objects per iteration (above) does not
+                    # help; only the binding does.
                     def _runner(
                         _cb: Callable[..., Any] = cb,
-                        _key: tuple = callback_key,
-                        _token: object = token,
+                        _context: contextvars.Context = context,
+                        _done: threading.Event = done,
+                        _outcome: Dict[str, Any] = outcome,
+                        _failure: Dict[str, Exception] = failure,
                     ) -> None:
                         try:
                             # Route through _invoke_hook_callback so the
                             # additive-payload signature filtering (narrow
                             # legacy callbacks) applies on the worker too.
-                            outcome["value"] = context.run(
+                            _outcome["value"] = _context.run(
                                 self._invoke_hook_callback, _cb, kwargs
                             )
                         except Exception as exc:
-                            failure["exc"] = exc
+                            _failure["exc"] = exc
                         finally:
-                            with self._hook_timeout_lock:
-                                if self._hook_running_callbacks.get(_key) is _token:
-                                    self._hook_running_callbacks.pop(_key, None)
-                            done.set()
+                            _done.set()
 
                     thread = threading.Thread(
                         target=_runner,
                         name=f"hermes-hook-{callback_name}"[:40],
                         daemon=True,
                     )
+                    started_at = time.monotonic()
                     thread.start()
                     if not done.wait(timeout=timeout):
                         # Do not join — that would reintroduce the #6622 hang.
@@ -110,14 +128,21 @@
                                 time.monotonic()
                                 + self._hook_timeout_suppression_seconds
                             )
+                        elapsed = time.monotonic() - started_at
                         logger.warning(
-                            "Hook '%s' callback %s timed out after %gs — skipping",
+                            "Hook '%s' callback %s timed out after %.3fs "
+                            "(budget %gs) — skipping",
                             hook_name,
                             callback_name,
+                            elapsed,
                             timeout,
                         )
                         if fail_closed:
-                            results.append(_pre_tool_call_timeout_block())
+                            results.append(
+                                _pre_tool_call_timeout_block(
+                                    callback_name, elapsed, timeout
+                                )
+                            )
                         continue
                     if "exc" in failure:
                         raise failure["exc"]
######## PluginManager._deliver_event
--- base
+++ ours
@@ -29,7 +29,7 @@
                     logger.warning(
                         "Event '%s' subscriber %s raised: %s",
                         item.event,
-                        getattr(callback, "__name__", repr(callback)),
+                        _callback_label(callback),
                         exc,
                     )
         finally:
######## PluginManager.invoke_middleware
--- base
+++ ours
@@ -16,7 +16,7 @@
                 logger.warning(
                     "Middleware '%s' callback %s raised: %s",
                     kind,
-                    getattr(cb, "__name__", repr(cb)),
+                    _callback_label(cb),
                     exc,
                 )
         return results
```

## §2 `hermes_cli/plugins_loader.py::_evict_modules` L140 (and any other `for n in sys.modules` scan)
Fork fix (blackbox plugin, 2026-09-29 18:24:52): snapshot first — `for name in [n for n in list(sys.modules)
if ...]`. Another thread importing mid-scan raises "dictionary changed size during iteration", which failed the
whole plugin load (its hooks never registered, so the worker's turn had calls and no turns row). The fork
applied it in `_load_directory_module` (both the pre-exec evict and the failure-path evict) and in
`_clear_plugin_submodules`; upstream consolidated all three into `_evict_modules`, so it is ONE line there.

## §3 `_resolve_hook_callback_timeout` memo keys on `hermes_cli.config._load_config_cache_sig(path)[1]`
The fork keyed on `hermes_cli.config._config_cache_signature`, which the merged `config.py` (lane R12-cli-b)
no longer defines; the facade now uses upstream's `_load_config_cache_sig` (same `(mtime, size)`-signature
semantics incl. the managed overlay). If config.py renames it again the memo silently degrades to a config read
per call (`sig=None` path) — `tests/gateway/test_no_sync_work_per_inbound_message.py::
test_hook_timeout_is_resolved_without_re_reading_config_every_call` is the canary.
