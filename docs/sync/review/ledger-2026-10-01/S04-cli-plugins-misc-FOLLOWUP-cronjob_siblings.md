# FOLLOWUP — lane S04-cli-plugins-misc: tools/cronjob_tools.py fork deltas that now live OUTSIDE the lane

Upstream split `tools/cronjob_tools.py` into `tools/cronjob_prompt_scan.py` + `tools/cronjob_job_args.py`
(both unconflicted, NOT in this lane). Two fork deltas land on moved code:

## §1 `tools/cronjob_prompt_scan.py::_CRON_THREAT_PATTERNS` — `read_secrets` false positive (fork #618, 1238c90147)
Upstream still has the base pattern `r'cat\s+[^\n]*(\.env|credentials|...)'`. The fork tightened it to
`r'\bcat[ \t]+[^\n]*(\.env|credentials|\.netrc|\.pgpass|id_rsa|id_ed25519|id_ecdsa)'` — a word boundary so
`concat`, `.cat`, `bobcat ...` etc. do not match, and `[ \t]+` so a `cat` at the end of one line followed by a
`credentials` mention on the NEXT line does not produce a fake secrets-read hit (mirrors the identical fix in
`tools/threat_patterns.py`, papercut pc-49dc8417). One-line change. Canary tests live in
`tests/tools/test_cronjob_tools.py` (grep `read_secrets`).

## §2 `tools/cronjob_job_args.py::_validate_cron_script_path` — fork admission rules (#1066 702ce55dd2 + scheduler Rules #14/#16b)
Three fork additions on top of upstream's body (diff below, base → fork):
1. Reject INLINE SCRIPT CONTENT (`"\n" in raw or raw.startswith("#!")`) — the runner treats the blob as a path
   and fails every tick with `[Errno 63] File name too long`, a double-silent dead job.
2. Reject FILENAME + ARGS (`raw != raw.split()[0]`) — the runner does not split args off.
3. When `validate_within_dir` reports an escape, consult `cron.scheduler._script_path_admitted(resolved,
   abspath, scripts_dir, get_hermes_home())` — the SAME exception the fire-time guard applies: an in-dir
   symlink into the fleet-shared `<root>/scripts` is a legitimate shared script (profile un-nesting).
4. Keep NOT requiring the file to exist at create/update time (Rule #16b).
```diff
--- base
+++ ours
@@ -14,4 +14,30 @@
 
     raw = script.strip()
+
+    # Reject INLINE SCRIPT CONTENT pasted into `script=` instead of a filename.
+    # A multi-line body (or a shell shebang / shell operators) is the #1 cron
+    # misconfig: the runner treats the whole blob as a path and fails every tick
+    # with `[Errno 63] File name too long` — a double-silent dead job (it never
+    # runs AND its own failure alert never fires). Catch it at the API boundary
+    # so creation fails loudly instead. (scheduler Rule #14.)
+    if "\n" in raw or raw.startswith("#!"):
+        return (
+            "`script` must be a FILENAME under ~/.hermes/scripts/, not inline "
+            "script content. Write the script to e.g. ~/.hermes/scripts/my-job.sh "
+            "(chmod +x), then pass script=\"my-job.sh\"."
+        )
+
+    # Reject FILENAME + ARGS (`foo.sh --flag`): the runner does not split args
+    # off, so it looks for a file literally named `foo.sh --flag` and fails
+    # `Script not found`. Make a thin wrapper that hardcodes the flag and point
+    # the job at the wrapper's bare filename instead.
+    if raw != raw.split()[0]:
+        first = raw.split()[0]
+        return (
+            f"`script` must be a bare filename with no arguments. Got {raw!r}. "
+            f"The runner does not split args off {first!r}; make a wrapper "
+            f"script in ~/.hermes/scripts/ that hardcodes the flags and pass "
+            f"just its filename."
+        )
 
     # Reject absolute paths and ~ expansion at the API boundary.
@@ -31,7 +57,30 @@
     containment_error = validate_within_dir(scripts_dir / raw, scripts_dir)
     if containment_error:
+        # Same exception as the fire-time guard: an in-dir symlink into the
+        # fleet-shared <root>/scripts is a legitimate shared script.
+        import os
+
+        from cron.scheduler import _script_path_admitted
+
+        try:
+            if _script_path_admitted(
+                (scripts_dir / raw).resolve(),
+                Path(os.path.abspath(scripts_dir / raw)),
+                scripts_dir,
+                get_hermes_home(),
+            ):
+                containment_error = None
+        except (OSError, RuntimeError, ValueError):
+            pass
+    if containment_error:
         return (
             f"Script path escapes the scripts directory via traversal: {raw!r}"
         )
 
+    # NOTE: we intentionally do NOT require the script file to already exist at
+    # create/update time — creating the job first and writing the script
+    # moments later is a supported workflow (scheduler Rule #16b: a script
+    # created after the job is fine; the transient "Script not found" clears on
+    # the next successful run). We only reject inputs that can NEVER be a valid
+    # filename (inline content, args) above.
     return None
```

## §3 Policy notes for the orchestrator (decided provisionally in favour of the FORK; relitigate if wanted)
- **Tool name**: registered as upstream's `cronjob_manage` (the merged `toolsets.py`, `tools/delegate_tool_toolsets.py`,
  16 locale files and 6 upstream test files already say so). Fork tests dispatch `registry.dispatch("cronjob", …)`
  — `tests/tools/test_cron_auto_model.py`, `tests/tools/test_cron_model_arg_coercion.py`, `tests/tools/test_cronjob_tools.py`,
  `tests/tools/test_registry_unknown_args.py` (and `tests/cron/test_agent_scheduling_gate.py` disables toolset
  `"cronjob"`, which is still the toolset name) → test lane repoints the TOOL name only. One test imports
  `_cronjob_tool_handler` → now `_cronjob_handler`.
- **Model-facing `model` / `allow_flagship_reason`**: upstream deliberately removed `model`/`provider`/`base_url`
  from the model-facing surface ("the agent must not point unattended spend at a different model"). The fork
  exposes `model` (object or flat string) + `allow_flagship_reason` and fences them with the cross-vendor
  refusal, the flagship ban (`validate_worker_model`) and the `auto` sentinel (pin to the creating agent's own
  model). Fork kept (fork-features: cron cross-vendor refusal; tests test_cron_auto_model / test_cron_model_arg_coercion).
  `base_url` is NOT forwarded from the model surface (upstream's stance kept; the fork schema never exposed it).
- **`reasoning_effort`**: CLI-only on BOTH sides (fork policy pin 2026-08-30 matches upstream) — converged;
  the fork's up-front validation (clear error on create AND update) kept.
