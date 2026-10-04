# FOLLOWUP (lane R11-cli-a): fork deltas on hermes_cli/auth.py code that upstream EXTRACTED into siblings

Upstream decomposed auth.py (9.5k -> 2.5k lines). These fork commits touched functions that now live in sibling modules outside lane R11-cli-a, so the deltas were NOT applied. Each block is the fork's base->ours diff of ONE function; port it onto the sibling's (rewritten) copy or ledger NO-PORT-NEEDED with evidence.

Fork commits: e7d48ecaa79 #1436 kimi follow-ups (account-scoped JWT swap in codex pool sync) · 56b04bca4e5 #673 codex refresh ownership + quarantine generations · 9fc2cbb00cd #670 stale singleton pairs xAI/Nous readers · b8d5fe20e67 #669 cross-account/stale Codex singleton adoption · ae07cf84c03 #326 clear stale xai-oauth last_auth_error.

## AuthError  -> now in hermes_cli/auth_constants.py

**LOAD-BEARING:** `agent/codex_owner.py:385` reads `exc.http_status` (fork #673); without this attribute on upstream's `AuthError` that path raises `AttributeError`. Port first.

```diff
AuthError: base->ours 2 lines, base->target 13 lines, ours->target 15 lines
--- base
+++ ours
@@ -9,4 +9,5 @@
         code: Optional[str] = None,
         relogin_required: bool = False,
+        http_status: Optional[int] = None,
     ) -> None:
         super().__init__(message)
@@ -14,2 +15,3 @@
         self.code = code
         self.relogin_required = relogin_required
+        self.http_status = http_status
```

## _sync_codex_pool_entries  -> now in hermes_cli/auth_codex.py

```diff
_sync_codex_pool_entries: base->ours 73 lines, base->target 104 lines, ours->target 47 lines
--- base
+++ ours
@@ -5,46 +5,9 @@
     previous_singleton_tokens: Optional[Dict[str, str]] = None,
 ) -> None:
-    """Mirror a fresh Codex re-auth into the credential_pool OAuth entries.
+    """Update only explicitly singleton-owned Codex rows after a fresh login.
 
-    The runtime selects credentials from ``credential_pool.openai-codex``, not
-    from ``providers.openai-codex.tokens``.  A re-auth invalidates the prior
-    OAuth pair server-side, but pool entries keep holding the now-consumed
-    refresh token plus any stale error markers — so the next request spends a
-    dead token and gets a 401 ``token_invalidated``.
-
-    What gets refreshed:
-
-    * ``device_code`` — the singleton-seeded entry written by the device-code
-      OAuth flow when the user logged in via ``hermes setup`` / the model
-      picker.  Always synced with the fresh tokens.
-    * ``manual:device_code`` — entries created by ``hermes auth add openai-codex``
-      that use the same device-code OAuth mechanism.  ONLY synced if the
-      entry's existing access_token matches the *previous* singleton
-      access_token (i.e. the entry is a legacy singleton-alias from the
-      #33000 workaround era).  Manual entries whose tokens never matched the
-      singleton represent INDEPENDENT accounts added via
-      ``hermes auth add openai-codex`` and must not be overwritten by a
-      re-auth that targeted a different account (regression for #39236).
-
-      The original #33538 fix refreshed every ``manual:device_code`` entry
-      unconditionally.  That worked when ``manual:device_code`` only meant
-      "legacy alias of the singleton", but the same source string is now
-      also produced by independent-account additions, and the broad sync
-      silently clobbered distinct accounts with the latest-authenticated
-      token pair.  The access_token-match check distinguishes the two cases
-      without changing the source-string contract.
-
-    What does NOT get refreshed:
-
-    * ``manual:api_key`` and any other non-device-code manual sources — those
-      are independent credentials (an explicit API key, a different ChatGPT
-      account, etc.) and must not be overwritten by a single re-auth.
-    * ``manual:device_code`` entries whose access_token does NOT match the
-      previous singleton — see above; these are independent accounts.
-
-    Error markers (``last_status``, ``last_error_*``) are cleared ONLY on
-    entries that actually had their tokens rewritten by this re-auth.
-    Independent entries keep their own error state (their 401/429 markers
-    belong to that account's own auth flow, not this re-auth).
+    Manual rows are independent grants, even when an old workaround copied
+    identical token bytes into them. Retiring those aliases is an explicit
+    ownership migration, never an inference from account IDs or token equality.
     """
     access_token = tokens.get("access_token")
@@ -58,32 +21,6 @@
     if not isinstance(entries, list):
         return
-    # Previous singleton access_token (before this re-auth overwrote it) —
-    # used to distinguish legacy singleton-aliases from independent accounts.
-    # When None or empty, no manual entry can be treated as an alias (which
-    # is the right default for first-ever-save or a freshly initialized
-    # auth.json).
-    prev_at = None
-    if isinstance(previous_singleton_tokens, dict):
-        prev_at = previous_singleton_tokens.get("access_token") or None
     for entry in entries:
-        if not isinstance(entry, dict):
-            continue
-        source = entry.get("source")
-        if source == "device_code":
-            # Singleton-seeded mirror — always refresh.
-            refresh_this_entry = True
-        elif source == "manual:device_code":
-            # Refresh only if this entry's existing access_token matches the
-            # previous singleton access_token (i.e. it is a true alias of the
-            # singleton from the #33000 workaround era).  An entry with its
-            # own distinct token material is an independent account and must
-            # be left alone (#39236).
-            refresh_this_entry = bool(
-                prev_at and entry.get("access_token") == prev_at
-            )
-        else:
-            # ``manual:api_key`` and any future non-device-code sources.
-            refresh_this_entry = False
-        if not refresh_this_entry:
+        if not isinstance(entry, dict) or entry.get("source") != "device_code":
             continue
         entry["access_token"] = access_token
```

## _save_codex_tokens  -> now in hermes_cli/auth_codex.py

```diff
_save_codex_tokens: base->ours 9 lines, base->target 59 lines, ours->target 54 lines
--- base
+++ ours
@@ -6,10 +6,6 @@
         auth_store = _load_auth_store()
         state = _load_provider_state(auth_store, "openai-codex") or {}
-        # Capture the previous singleton tokens BEFORE overwriting them.  The
-        # pool-sync step uses this to distinguish legacy singleton-aliases
-        # (which should be refreshed) from independent accounts that
-        # ``hermes auth add openai-codex`` created (which must not be
-        # overwritten — see #39236).
-        previous_singleton_tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else None
+        # Explicit login writes the active store. Only declared device_code
+        # rows are synchronized; manual grants remain independent.
         state["tokens"] = tokens
         state["last_refresh"] = last_refresh
@@ -22,5 +18,4 @@
             tokens,
             last_refresh,
-            previous_singleton_tokens=previous_singleton_tokens,
         )
         _save_auth_store(auth_store)
```

## refresh_codex_oauth_pure  -> now in hermes_cli/auth_codex.py

```diff
refresh_codex_oauth_pure: base->ours 2 lines, base->target 146 lines, ours->target 148 lines
--- base
+++ ours
@@ -55,4 +55,5 @@
             code=CODEX_RATE_LIMITED_CODE,
             relogin_required=False,
+            http_status=response.status_code,
         )
 
@@ -101,4 +102,5 @@
             code=code,
             relogin_required=relogin_required,
+            http_status=response.status_code,
         )
 
```

## resolve_codex_runtime_credentials  -> now in hermes_cli/auth_codex.py

```diff
resolve_codex_runtime_credentials: base->ours 16 lines, base->target 189 lines, ours->target 205 lines
--- base
+++ ours
@@ -16,4 +16,12 @@
     credential. See issue #32992.
     """
+    from agent.codex_owner import resolve_runtime
+    owned = resolve_runtime(
+        force_refresh=force_refresh, refresh_if_expiring=refresh_if_expiring,
+        refresh_skew_seconds=refresh_skew_seconds,
+    )
+    if owned is not None:
+        return owned
+
     read_error: Optional[AuthError] = None
     try:
@@ -28,4 +36,12 @@
             imported = _recover_codex_tokens_from_cli(str(getattr(exc, "code", None) or "auth_error"))
             if imported:
+                # Recovery may have just created a durable singleton. Route
+                # any ensuing refresh through its owner transaction too.
+                owned = resolve_runtime(
+                    force_refresh=force_refresh, refresh_if_expiring=refresh_if_expiring,
+                    refresh_skew_seconds=refresh_skew_seconds,
+                )
+                if owned is not None:
+                    return owned
                 data = {"tokens": imported, "last_refresh": imported.get("last_refresh")}
             else:
```

## _probe_codex_quota_restored  -> now in hermes_cli/auth_codex.py

```diff
_probe_codex_quota_restored: base->ours 11 lines, base->target 82 lines, ours->target 77 lines
--- base
+++ ours
@@ -53,12 +53,7 @@
         # Best-effort ChatGPT-Account-Id from the JWT (the backend requires it
         # for some account shapes; harmless to omit for others).
-        claims = _decode_jwt_claims(token)
-        account_id = (
-            claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
-            if isinstance(claims.get("https://api.openai.com/auth"), dict)
-            else None
-        )
-        if isinstance(account_id, str) and account_id.strip():
-            headers["ChatGPT-Account-Id"] = account_id.strip()
+        account_id = get_codex_account_id(token)
+        if account_id:
+            headers["ChatGPT-Account-Id"] = account_id
         with httpx.Client(timeout=10.0) as client:
             response = client.get(_codex_usage_probe_url(base_url), headers=headers)
```

## _save_xai_oauth_tokens  -> now in hermes_cli/auth_xai.py

```diff
_save_xai_oauth_tokens: base->ours 5 lines, base->target 61 lines, ours->target 66 lines
--- base
+++ ours
@@ -42,4 +42,9 @@
         if redirect_uri:
             state["redirect_uri"] = redirect_uri
+        # Persisting good tokens means any prior refresh/login failure is
+        # resolved. Clear the stale error record so it can't mislead future
+        # triage (a revoked-token error left behind after a successful
+        # re-login/refresh looks like a live outage but isn't).
+        state.pop("last_auth_error", None)
         global_root = _global_auth_file_path()
         is_from_root = bool(
```

## resolve_nous_runtime_credentials  -> now in hermes_cli/auth_nous.py

```diff
resolve_nous_runtime_credentials: base->ours 31 lines, base->target 346 lines, ours->target 373 lines
--- base
+++ ours
@@ -5,4 +5,5 @@
     ca_bundle: Optional[str] = None,
     force_refresh: bool = False,
+    pool_state: Optional[Dict[str, Any]] = None,
 ) -> Dict[str, Any]:
     """
@@ -11,4 +12,6 @@
     Ensures access_token is a valid inference-scoped JWT, refreshing it when
     needed. Concurrent processes coordinate through the auth store file lock.
+    ``pool_state`` carries a singleton-seeded pool snapshot; only a strictly
+    newer timestamped OAuth pair may replace the state read under that lock.
 
     Returns dict with: provider, base_url, api_key, key_id, expires_at,
@@ -29,4 +32,28 @@
         persisted_state = dict(state)
         state_persisted = False
+
+        def _retain_newer_pool_pair() -> bool:
+            # Reconcile inside the refresh transaction, not via a best-effort
+            # pre-write: the singleton may have changed since the pool read it.
+            if not pool_state:
+                return False
+            from agent.credential_pool import _parse_absolute_timestamp
+
+            pool_at = _parse_absolute_timestamp(pool_state.get("obtained_at"))
+            state_at = _parse_absolute_timestamp(state.get("obtained_at"))
+            if pool_at is None or state_at is None or pool_at <= state_at:
+                return False
+            for key in ("access_token", "refresh_token", "expires_at",
+                        "obtained_at", "expires_in"):
+                if pool_state.get(key) is not None:
+                    state[key] = pool_state[key]
+            return True
+
+        def _merge_shared_state() -> bool:
+            merged = _merge_shared_nous_oauth_state(state)
+            # A stale shared mirror must not undo the retained pair either.
+            return _retain_newer_pool_pair() or merged
+
+        _retain_newer_pool_pair()
 
         def _resolve_effective_routing_metadata() -> tuple[str, str, str, str]:
@@ -152,5 +179,5 @@
                     timeout_seconds=max(timeout_seconds + 5.0, AUTH_LOCK_TIMEOUT_SECONDS)
                 ):
-                    if _merge_shared_nous_oauth_state(state):
+                    if _merge_shared_state():
                         access_token = state.get("access_token")
                         refresh_token = state.get("refresh_token")
@@ -174,5 +201,5 @@
             if force_refresh or invoke_jwt_status is not None:
                 with _nous_shared_store_lock(timeout_seconds=max(timeout_seconds + 5.0, AUTH_LOCK_TIMEOUT_SECONDS)):
-                    if _merge_shared_nous_oauth_state(state):
+                    if _merge_shared_state():
                         access_token = state.get("access_token")
                         refresh_token = state.get("refresh_token")
```

