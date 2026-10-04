# FOLLOWUP — lane S04-cli-plugins-misc: hermes_cli/model_switch.py fork deltas that now live OUTSIDE the lane

Produced while resolving `hermes_cli/model_switch.py` (upstream facade 1869 lines + fork features
re-threaded). Upstream extracted the picker/listing code into `hermes_cli/model_switch_providers.py`
(unconflicted, NOT in this lane). The fork edited three of the moved functions; those deltas are
NOT in the merged tree until applied there. The facade re-exports every moved name
(`list_authenticated_providers`, `list_picker_providers`, `prewarm_picker_cache_async`, …) so
`hermes_cli.model_switch.<name>` keeps resolving.

## §1 `model_switch_providers.py` — fork deltas (apply by hand; diff below is base→fork)

1. **`list_authenticated_providers`**: `provider_seam.refresh("picker")` + `g = provider_seam.snapshot()`
   at the top and every `PROVIDER_REGISTRY` / `PROVIDER_OVERLAY` / `CANONICAL_PROVIDERS` / `_PROVIDER_MODELS`
   read goes through `g.` (fork feature "provider-registry generation seam", fork-features.json;
   canary `tests/fork_canaries/test_fork_canary_provider_seam.py::test_list_authenticated_providers_refreshes_before_listing`
   is RED until the `refresh("picker")` line lands). NOTE: R11-cli-a converged `auth.get_api_key_provider_status`
   on upstream's `_registry_lookup` because upstream removed the overlay read; check whether
   `provider_seam.snapshot()` still exposes `.PROVIDER_REGISTRY/.PROVIDER_OVERLAY/.CANONICAL_PROVIDERS/._PROVIDER_MODELS`
   (it is the registered FACADES set: `PROVIDER_REGISTRY`, `PROVIDER_OVERLAY`, `_PROVIDER_MODELS`,
   `CANONICAL_PROVIDERS`, …) — if yes apply verbatim, else keep only the `refresh("picker")` line
   (the lazy dicts are themselves the seam).
2. **`list_authenticated_providers`** tail: `model_catalog.excluded_providers` post-filter over `results`
   via `provider_row_is_excluded` (facade, `from hermes_cli.model_switch import provider_row_is_excluded`
   late import — fork #712 d74b3d99c8a). Upstream already threads `excluded_providers=` into the
   sections; verify whether sections 3/3b/4 (user `providers:`, bare custom, `custom_providers:`) are
   gated — if not, the post-pass is still needed.
3. **`list_authenticated_providers`** current-model injection: namespace-aware `_same_model`
   (`a == b or a.endswith("/"+b) or b.endswith("/"+a)`) so a bare `claude-opus-4-8` is not injected a
   second time next to `claude-app/claude-opus-4-8` (9ea23030a87).
4. **`list_picker_providers`**: after `_prepend_moa_picker_provider`, drop excluded rows with
   `provider_row_is_excluded`; skip `_PICKER_HIDDEN_FAILOVER_LANE_RE.match(slug) and slug != current`
   (adf3b840499 + 72f20564d11 hide numbered claude-apx-N/bpx-N lanes); `return _apply_picker_preferences(filtered, current_provider=_cur)`
   (306e24a600e `model.picker.hide/order`). All three helpers live on the facade; import late to avoid
   the facade→sibling module-level cycle. Fork test: `tests/hermes_cli/test_list_picker_providers.py`.
5. **`_collect_authed_provider_slugs`**: same `g = provider_seam.snapshot()` reads as (1).

### Diff (base → fork) for the three functions
```diff
_collect_authed_provider_slugs: base->ours 11 lines, base->target 189 lines, ours->target 190 lines
--- base
+++ ours
@@ -22,4 +22,5 @@
     from hermes_cli.providers import HERMES_OVERLAYS, ALIASES as _PROVIDER_ALIAS_TABLE
     from hermes_cli.models import _AGGREGATOR_PROVIDERS as _AGG_PROVIDERS, CANONICAL_PROVIDERS
+    g = provider_seam.snapshot()
 
     _excluded_set = {str(p).strip().lower() for p in excluded if p}
@@ -53,5 +54,5 @@
         if not isinstance(pdata, dict):
             continue
-        pconfig = PROVIDER_REGISTRY.get(hermes_id)
+        pconfig = g.PROVIDER_REGISTRY.get(hermes_id)
         if pconfig and pconfig.auth_type != "api_key":
             continue
@@ -84,5 +85,5 @@
     # --- Section 2: Hermes-only providers (HERMES_OVERLAYS) ---
     _mdev_to_hermes = {v: k for k, v in PROVIDER_TO_MODELS_DEV.items()}
-    for pid, overlay in HERMES_OVERLAYS.items():
+    for pid, overlay in g.HERMES_OVERLAYS.items():
         if pid.lower() in seen:
             continue
@@ -106,5 +107,5 @@
         if not has_creds and overlay.auth_type == "api_key":
             for _key in (pid, hermes_slug):
-                pcfg = PROVIDER_REGISTRY.get(_key)
+                pcfg = g.PROVIDER_REGISTRY.get(_key)
                 if pcfg and pcfg.api_key_env_vars:
                     if any(_scoped_key_env(ev) for ev in pcfg.api_key_env_vars):
@@ -131,10 +132,10 @@
 
     # --- Section 2b: Canonical providers cross-check ---
-    for _cp in CANONICAL_PROVIDERS:
+    for _cp in g.CANONICAL_PROVIDERS:
         if _cp.slug.lower() in seen:
             continue
         if _cp.slug.lower() in _excluded_set:
             continue
-        _cp_config = PROVIDER_REGISTRY.get(_cp.slug)
+        _cp_config = g.PROVIDER_REGISTRY.get(_cp.slug)
         _cp_has_creds = False
         if _cp_config and _cp_config.api_key_env_vars:
list_authenticated_providers: base->ours 50 lines, base->target 1380 lines, ours->target 1416 lines
--- base
+++ ours
@@ -62,4 +62,8 @@
         clear_provider_models_cache, get_curated_nous_model_ids,
     )
+    # Hot registration: give refresh callbacks a chance to publish missing
+    # names, then pin ONE generation for every surface this listing reads.
+    provider_seam.refresh("picker")
+    g = provider_seam.snapshot()
 
     # Explicit refresh: drop every provider's cached model-id list so the
@@ -169,5 +173,5 @@
 
     # Build curated model lists keyed by hermes provider ID
-    curated: dict[str, list[str]] = dict(_PROVIDER_MODELS)
+    curated: dict[str, list[str]] = dict(g._PROVIDER_MODELS)
     curated["openrouter"] = [mid for mid, _ in OPENROUTER_MODELS]
     # "nous" pulls from the remote model-catalog manifest published at
@@ -286,5 +290,5 @@
         # source of truth.  models.dev can have wrong mappings (e.g.
         # minimax-cn → MINIMAX_API_KEY instead of MINIMAX_CN_API_KEY).
-        pconfig = PROVIDER_REGISTRY.get(hermes_id)
+        pconfig = g.PROVIDER_REGISTRY.get(hermes_id)
         # Skip non-API-key auth providers here — they are handled in
         # section 2 (HERMES_OVERLAYS) with proper auth store checking.
@@ -374,5 +378,5 @@
     _mdev_to_hermes = {v: k for k, v in PROVIDER_TO_MODELS_DEV.items()}
 
-    for pid, overlay in HERMES_OVERLAYS.items():
+    for pid, overlay in g.HERMES_OVERLAYS.items():
         if pid.lower() in seen_slugs:
             continue
@@ -408,5 +412,5 @@
         if not has_creds and overlay.auth_type == "api_key":
             for _key in (pid, hermes_slug):
-                pcfg = _auth_registry.get(_key)
+                pcfg = g.PROVIDER_REGISTRY.get(_key)
                 if pcfg and pcfg.api_key_env_vars:
                     if any(os.environ.get(ev) for ev in pcfg.api_key_env_vars):
@@ -564,5 +568,5 @@
         _canon_provs = []
 
-    for _cp in _canon_provs:
+    for _cp in g.CANONICAL_PROVIDERS:
         if _cp.slug.lower() in seen_slugs:
             continue
@@ -571,5 +575,5 @@
 
         # Check credentials via PROVIDER_REGISTRY (auth.py)
-        _cp_config = _auth_registry.get(_cp.slug)
+        _cp_config = g.PROVIDER_REGISTRY.get(_cp.slug)
         _cp_has_creds = False
         if _cp_config and _cp_config.api_key_env_vars:
@@ -1296,4 +1300,23 @@
             _section4_emitted_slugs.add(slug.lower())
 
+    # Apply final ``model_catalog.excluded_providers`` post-filter.
+    #
+    # The per-section gates above only cover the built-in rows (sections 1, 2
+    # and 2b). Sections 3 (``providers:``), 3b (the bare ``provider: custom`` +
+    # ``base_url`` form) and 4 (``custom_providers:``) had no exclusion gate at
+    # all, so an excluded user endpoint still reached every consumer of this
+    # function — web_server /api/models, the kanban dashboard, the ACP adapter,
+    # tui_gateway and moa_cmd — while ``hermes model`` (main.py) hid it. This
+    # runs as a post-pass over ``results`` rather than as a fourth per-section
+    # gate so a section added later cannot reintroduce the omission.
+    #
+    # ``inventory.build_models_payload`` applies the SAME predicate again after
+    # it injects its own rows (the virtual ``moa`` row and the unconfigured
+    # canonical skeletons), which this function never sees.
+    if _excluded:
+        results = [
+            r for r in results if not provider_row_is_excluded(r, _excluded)
+        ]
+
     # Apply final ``providers.<name>.enabled: false`` post-filter — covers
     # built-in PROVIDER_REGISTRY rows (sections 1-2) which would otherwise
@@ -1327,9 +1350,22 @@
     # which branch emitted the row.
     if current_model:
+        # Namespace-aware presence check. ``current_model`` is stored bare in
+        # config (e.g. ``claude-opus-4-8``) while a provider's curated catalog
+        # entries are namespaced (``claude-app/claude-opus-4-8``). A plain
+        # ``current_model not in _models`` never matches the namespaced entry,
+        # so the current model gets injected a SECOND time and the picker shows
+        # it twice (the platform adapters strip the prefix for display, so both
+        # rows render identically). Treat the two as the same model when one is
+        # the other with only a namespace prefix removed — matched on the ``/``
+        # boundary so distinct aggregator entries that merely share a trailing
+        # name (``openai/gpt-5`` vs ``azure/gpt-5``) are NOT collapsed.
+        def _same_model(a: str, b: str) -> bool:
+            return a == b or a.endswith("/" + b) or b.endswith("/" + a)
+
         for _row in results:
             if not _row.get("is_current") or _row.get("native_catalog_empty"):
                 continue
             _models = _row.get("models") or []
-            if current_model not in _models:
+            if not any(_same_model(current_model, m) for m in _models):
                 _row["models"] = [current_model, *_models]
                 _row["total_models"] = _row.get("total_models", len(_models)) + 1
list_picker_providers: base->ours 24 lines, base->target 68 lines, ours->target 92 lines
--- base
+++ ours
@@ -42,8 +42,30 @@
     if include_moa:
         providers = _prepend_moa_picker_provider(providers, current_provider=current_provider)
+        # ``list_authenticated_providers`` filtered ``excluded_providers`` over
+        # its OWN rows; the virtual moa row is injected afterwards and would
+        # otherwise reappear in the gateway/Telegram/Discord picker despite
+        # being excluded. Same shared predicate as the two other choke points
+        # (list_authenticated_providers, inventory.build_models_payload).
+        _excl_norm = {
+            str(p).strip().lower() for p in (excluded_providers or []) if p
+        }
+        if _excl_norm:
+            providers = [
+                p for p in providers
+                if not provider_row_is_excluded(p, _excl_norm)
+            ]
 
     filtered: List[dict] = []
+    _cur = str(current_provider or "").strip().lower()
     for p in providers:
         slug = str(p.get("slug", "")).lower()
+        # Hide numbered Claude failover lanes (claude-{api-proxy,bridge}-fN) from
+        # the interactive picker — they're internal auto-failover targets, not
+        # hand-selectable providers, and 20+ of them crowd real providers past
+        # the dropdown's 25-option cap. Keep the lane visible only if it's the
+        # CURRENTLY-active provider (so a user on a lane can still see/change it).
+        # Typed `/model <lane>/...` and failover routing are unaffected.
+        if _PICKER_HIDDEN_FAILOVER_LANE_RE.match(slug) and slug != _cur:
+            continue
         if slug == "openrouter":
             try:
@@ -62,3 +84,3 @@
         filtered.append(p)
 
-    return filtered
+    return _apply_picker_preferences(filtered, current_provider=_cur)
```

## §2 `not_listed` producer (models_validate.py / models.py — not this lane)
`switch_model` now surfaces `_configured_but_unlisted_message(...)` when `validation.get("not_listed")`
is truthy (fork 2026-09-29: `/model k3` during a quota window read as a typo). The fork's producer sat in
`hermes_cli/models.py::validate_requested_model` (`"not_listed": True` when the id is absent from a live
listing). Upstream moved validation to `hermes_cli/models_validate.py`, which has no `not_listed` key →
the hook is inert until the producer is ported there. Fork test: `tests/hermes_cli/test_model_switch_configured_unlisted.py`.

## §3 Test-lane notes (files outside this lane)
- Fork contract kept: `_load_direct_aliases() -> (dict, ok)`. Upstream-only tests index the result
  directly and need `aliases, _ = _load_direct_aliases()`: `tests/hermes_cli/test_model_alias_credentials.py`
  (L85, L120, L137), `tests/hermes_cli/test_startup_model_routing.py` (L150).
- Fork tests patch `hermes_cli.models.validate_requested_model` / `hermes_cli.models.detect_provider_for_model`;
  upstream's `_validate_switch` imports from `hermes_cli.models_validate` and `_route_from_model_input`
  from `hermes_cli.models` → repoint the validate patch target in
  `tests/hermes_cli/test_model_switch_inline_provider_syntax.py`, `test_model_switch_provider_slash_form.py`,
  `tests/hermes_cli/test_kanban_c7_slice_a.py` (and any other fork-only switch_model test).
- `parse_model_flags` (tuple wrapper) dropped: upstream removed it, zero callers in the merged tree
  (only a comment in gateway/run.py names it).
- Runtime import proof of `hermes_cli.model_switch` passes under a sandboxed HOME; the full
  `switch_model` path needs `agent.native_compaction` → `agent.auxiliary_client`, which still carries
  markers (lane R03) — smoke stubbed that module only.
