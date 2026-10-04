# FOLLOWUP (lane R11-cli-a): fork deltas on hermes_cli/web_server.py code upstream EXTRACTED into siblings/routers

Each block = the fork's base->ours diff of ONE function; port onto the sibling's copy or ledger NO-PORT-NEEDED.

## _process_start_marker  -> now in hermes_cli/web_server_lifecycle.py

```diff
_process_start_marker: base->ours 2 lines, base->target 42 lines, ours->target 42 lines
--- base
+++ ours
@@ -66,5 +66,5 @@
         ["ps", "-p", str(pid), "-o", "lstart="],
         capture_output=True,
-        text=True,
+        text=True, encoding="utf-8", errors="replace",
         check=False,
     )
```

## _write_dashboard_ready_file  -> now in hermes_cli/web_server_lifecycle.py

```diff
_write_dashboard_ready_file: base->ours 5 lines, base->target 31 lines, ours->target 34 lines
--- base
+++ ours
@@ -15,5 +15,8 @@
         path = Path(target)
         path.parent.mkdir(parents=True, exist_ok=True)
-        payload = json.dumps({"port": int(actual_port)}, separators=(",", ":"))
+        payload = json.dumps(
+            {"port": int(actual_port), "project_root": str(PROJECT_ROOT)},
+            separators=(",", ":"),
+        )
         with tempfile.NamedTemporaryFile(
             "w",
```

## get_model_options  -> now in hermes_cli/web_routers/models.py

```diff
get_model_options: base->ours 8 lines, base->target 47 lines, ours->target 55 lines
--- base
+++ ours
@@ -34,4 +34,9 @@
             # sync picker build (config load, pricing, refresh probes) runs
             # off the event loop under the requested profile.
+            #
+            # Most desktop surfaces should only list providers the user has
+            # already configured. Onboarding opts into the full provider
+            # universe via include_unconfigured=1 so it can still render setup
+            # affordances for providers that are not yet authenticated.
             with _profile_scope(profile):
                 return build_model_options_payload(
@@ -40,4 +45,7 @@
                     include_unconfigured=bool(include_unconfigured),
                     refresh=bool(refresh),
+                    # fork parity: honour the user's model.picker hide/order
+                    # config on desktop picker opens (see build_models_payload).
+                    apply_picker_prefs=True,
                 )
 
```

## _list_cron_jobs_sync  -> now in hermes_cli/web_routers/cron.py

```diff
_list_cron_jobs_sync: base->ours 4 lines, base->target 32 lines, ours->target 36 lines
--- base
+++ ours
@@ -5,6 +5,6 @@
 
     jobs: List[Dict[str, Any]] = []
-    for item in _cron_profile_dicts():
-        name = str(item.get("name") or "")
+    for profile in _cron_profile_dicts():
+        name = str(profile.get("name") or "")
         if not name:
             continue
```

