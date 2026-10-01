# FOLLOWUP — cron/scheduler.py fork deltas on functions upstream EXTRACTED into siblings

Lane R10-cron (card t_af1c17cb), parity sync 2026-10-01. The sibling modules are upstream-new and outside the lane list, so
the fork deltas below were NOT applied; `cron/scheduler.py` keeps every fork-only helper they call (`_job_script_kwargs`,
`_apply_host_down_gate`, `_findings_deliver_job`, `_is_restart_killed`, `_apply_cron_default_gh_lane`, `_script_path_admitted`,
`terminate_running_scripts`, `signal_shutdown`/`is_shutting_down`, `_deliver_missed_oneshot_notices`, `live_cron_agents`, ...)
so each port is: re-thread the delta into the sibling's restructured copy and late-import the helper from `cron.scheduler`
(the sibling pattern upstream already uses). Until ported: cron/monitor.py's `_run_job_script(**_job_script_kwargs(job))`
raises TypeError (L10-gw-miscb FOLLOWUP), failed cron runs wear the success header, missed one-shot notices and the
host-down gate do not fire, and the restart-drain kill is paged.

Each section: upstream sibling that now owns the function, what the fork changed, then the raw `diff base ours` of the
fork's copy (the sibling's copy is restructured, so apply by meaning, not by patch). Full fork copies: `git show 2eb646f755:cron/scheduler.py`.

## `_deliver_result` -> `cron/scheduler_delivery.py`

FOLLOWUP: cron/scheduler_delivery.py needs fork: `success=` kwarg (failed runs wear the failure header, t_6bedca00 #1336); `_apply_host_down_gate` (fleet host-down gate, #1026); house page-shape wrapper (`CRON_WRAPPER_FOOTER_PREFIX` `-# cron … · job …` footer, #1495 — gateway/platforms/yuanbao.py `strip_cron_wrapper` + scheduler.py keep that shape); findings reroute (`_findings_deliver_job`, #1502/#1509).

```diff
--- base:_deliver_result
+++ fork:_deliver_result
@@ -1,3 +1,3 @@
-def _deliver_result(job: dict, content: str, adapters=None, loop=None) -> Optional[str]:
+def _deliver_result(job: dict, content: str, success: bool = True, adapters=None, loop=None, *, wrap_override: Optional[bool] = None) -> Optional[str]:
     """
     Deliver job output to the configured target(s) (origin chat, specific platform, etc.).
@@ -7,4 +7,8 @@
     the standalone HTTP path cannot encrypt.  Falls back to standalone send if
     the adapter path fails or is unavailable.
+
+    ``success`` selects the framing of the wrapped delivery (the content leads on
+    successful runs, a ⚠️ failure header carries the error as the body); both
+    end in one ``-# cron …`` footer line.
 
     Returns None on success, or an error string on failure.
@@ -32,4 +36,8 @@
         return msg
 
+    host_down_ledger: list = []
+    content, targets = _apply_host_down_gate(
+        job, content, targets, pending_ledger=host_down_ledger)
+
     from tools.send_message_tool import _send_to_platform
     from gateway.config import load_gateway_config, Platform
@@ -37,5 +45,8 @@
     # Optionally wrap the content with a header/footer so the user knows this
     # is a cron delivery.  Wrapping is on by default; set cron.wrap_response: false
-    # in config.yaml for clean output.
+    # in config.yaml for clean output.  A caller may force it off per-call via
+    # ``wrap_override=False`` — used by the fallback alert, which is a
+    # self-contained 🚨 message that should NOT wear the "Cronjob Response/Failed"
+    # envelope (the alert is about a job that usually SUCCEEDED on its fallback).
     wrap_response = True
     user_cfg = None
@@ -45,15 +56,24 @@
     except Exception:
         pass
+    if wrap_override is not None:
+        wrap_response = wrap_override
 
     if wrap_response:
+        # House page shape (t_cb147820): the content's own header leads and the
+        # wrapper folds into ONE -# footer line.  A success run no longer stacks
+        # a ✅ header over a 🔴 finding; a failure keeps its single ⚠️ header
+        # (fork PR #16 semantics), only the id/rule/hint lines are folded.
         task_name = job.get("name", job["id"])
         job_id = job.get("id", "")
-        delivery_content = (
-            f"Cronjob Response: {task_name}\n"
-            f"(job_id: {job_id})\n"
-            f"-------------\n\n"
-            f"{content}\n\n"
-            f"To stop or manage this job, send me a new message (e.g. \"stop reminder {task_name}\")."
+        footer = (
+            f'{CRON_WRAPPER_FOOTER_PREFIX}{task_name} · job {job_id} · '
+            f'reply "stop reminder {task_name}" to manage'
         )
+        body = (content or "").strip("\n")
+        if success or body.startswith(f"⚠️ **{task_name}** · rc="):
+            head = []  # the page already names the job (house shape, t_4bcf8c20)
+        else:
+            head = [f"⚠️ **Cronjob Failed: {task_name}**"]
+        delivery_content = "\n".join(head + ([body] if body else []) + [footer])
     else:
         delivery_content = content
@@ -115,6 +135,21 @@
 
     delivery_errors = []
+    delivered_chats = set()
+
+    def _note_delivered(platform_name, chat_id):
+        delivered_chats.add((str(platform_name).lower(), str(chat_id)))
+        # Ledger a deferral the moment it lands in #logs: a later target that
+        # aborts delivery (or a worker exit) must not lose the delivered row.
+        if host_down_ledger and ("discord", _HOST_DOWN_LOGS_CHAT) in delivered_chats:
+            _host_down_write_ledger(host_down_ledger)
+            host_down_ledger.clear()
+    _unprefixed_delivery_content = cleaned_delivery_content
 
     for target in targets:
+        _host_down_prefix = target.pop(_HOST_DOWN_PREFIX_KEY, None)
+        cleaned_delivery_content = (
+            f"{_host_down_prefix} {_unprefixed_delivery_content}"
+            if _host_down_prefix else _unprefixed_delivery_content
+        )
         platform_name = target["platform"]
         chat_id = target["chat_id"]
@@ -652,4 +687,5 @@
                     logger.info("Job '%s': delivered to %s:%s via live adapter", job["id"], platform_name, chat_id)
                     delivered = True
+                    _note_delivered(platform_name, chat_id)
                     # Seed the thread session only now that delivery into it
                     # succeeded (deferred from thread-open above).
@@ -844,4 +880,5 @@
 
             logger.info("Job '%s': delivered to %s:%s", job["id"], platform_name, chat_id)
+            _note_delivered(platform_name, chat_id)
             _maybe_mirror_cron_delivery(
                 job, platform_name, chat_id, mirror_text,
```

## `_deliver_to_bot_chat` -> `cron/scheduler_delivery.py`

FOLLOWUP: cron/scheduler_delivery.py needs fork: 2-line delta.

```diff
--- base:_deliver_to_bot_chat
+++ fork:_deliver_to_bot_chat
@@ -65,5 +65,5 @@
             argv,
             capture_output=True,
-            text=True,
+            text=True, encoding="utf-8", errors="replace",
             timeout=_get_bot_chat_delivery_timeout(),
             env=env,
```

## `_get_script_timeout` -> `cron/scheduler_script.py`

FOLLOWUP: cron/scheduler_script.py needs fork: delegates to `cron.fork_ext.scheduler_ext.get_script_timeout` (per-job script timeout feature).

```diff
--- base:_get_script_timeout
+++ fork:_get_script_timeout
@@ -1,31 +1,11 @@
 def _get_script_timeout() -> int:
-    """Resolve cron pre-run script timeout from module/env/config with a safe default."""
-    if _SCRIPT_TIMEOUT != _DEFAULT_SCRIPT_TIMEOUT:
-        try:
-            timeout = int(float(_SCRIPT_TIMEOUT))
-            if timeout > 0:
-                return timeout
-        except Exception:
-            logger.warning("Invalid patched _SCRIPT_TIMEOUT=%r; using env/config/default", _SCRIPT_TIMEOUT)
+    """Resolve cron pre-run script timeout from module/env/config with a safe default.
 
-    env_value = os.getenv("HERMES_CRON_SCRIPT_TIMEOUT", "").strip()
-    if env_value:
-        try:
-            timeout = int(float(env_value))
-            if timeout > 0:
-                return timeout
-        except Exception:
-            logger.warning("Invalid HERMES_CRON_SCRIPT_TIMEOUT=%r; using config/default", env_value)
-
-    try:
-        cfg = load_config() or {}
-        cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
-        configured = cron_cfg.get("script_timeout_seconds")
-        if configured is not None:
-            timeout = int(float(configured))
-            if timeout > 0:
-                return timeout
-    except Exception as exc:
-        logger.debug("Failed to load cron script timeout from config: %s", exc)
-
-    return _DEFAULT_SCRIPT_TIMEOUT
+    Delegates to the extracted fork helper (cron/fork_ext/scheduler_ext.py),
+    which carries the identical module-override -> env -> config -> default
+    resolution chain; behavior is byte-identical to the previous inline body
+    (the golden replay in tests/golden/scheduler_ext/ pins both).
+    """
+    return scheduler_ext.get_script_timeout(
+        _SCRIPT_TIMEOUT, _DEFAULT_SCRIPT_TIMEOUT, load_config=load_config, logger=logger
+    )
```

## `_run_job_script` -> `cron/scheduler_script.py`

FOLLOWUP: cron/scheduler_script.py needs fork: `timeout_seconds`/`job_name`/`job_id` kwargs via `scheduler_ext.resolve_job_script_timeout` + PHASE=cron_script_timeout log (#1027); agent-marker scrub so the script child is not an agent process (#1127, t_7fee0f83); gh-shim default lane `CRON_SCRIPT_DEFAULT_GH_LANE` / `_apply_cron_default_gh_lane` (#1280) and `agent.process_env_files` (#1254); shared scripts dir admission `_script_path_admitted`/`_shared_scripts_dir` incl. profile symlinks (#1066); `terminate_running_scripts` registry + `_is_shutdown_kill_returncode`/`_consume_restart_killed` (#462/#1429); windows text=True encoding fix (#1033). Call sites: cron/monitor.py already passes `**_job_script_kwargs(job)` (L10-gw-miscb) and tests import `CRON_SCRIPT_DEFAULT_GH_LANE` + `_run_job_script` from `cron.scheduler_script` (T-misc).

```diff
--- base:_run_job_script
+++ fork:_run_job_script
@@ -3,4 +3,8 @@
     workdir: Optional[str] = None,
     cancel_event: Optional[_CancelEventLike] = None,
+    *,
+    timeout_seconds: Optional[int] = None,
+    job_name: Optional[str] = None,
+    job_id: Optional[str] = None,
 ) -> tuple[bool, str]:
     """Execute a cron job's data-collection script and capture its output.
@@ -67,13 +71,17 @@
         return False, f"Blocked: script path is not a valid filesystem path: {script_path!r}"
     if raw.is_absolute():
+        lexical = Path(os.path.abspath(raw))
         path = raw.resolve()
     else:
+        lexical = Path(os.path.abspath(scripts_dir / raw))
         path = (scripts_dir / raw).resolve()
 
     # Guard against path traversal, absolute path injection, and symlink
-    # escape — scripts MUST reside within HERMES_HOME/scripts/.
-    try:
-        path.relative_to(scripts_dir_resolved)
-    except ValueError:
+    # escape — scripts MUST reside within HERMES_HOME/scripts/. One exception:
+    # an entry that lives (lexically) in a named profile's scripts dir and is
+    # a symlink into the fleet-shared <root>/scripts is admitted — profiles
+    # legitimately share scripts, and refusing that symlink left a job failing
+    # identically every tick for 19 h (t_04822736).
+    if not _script_path_admitted(path, lexical, scripts_dir, _get_hermes_home()):
         return False, (
             f"Blocked: script path resolves outside the scripts directory "
@@ -86,5 +94,23 @@
         return False, f"Script path is not a file: {path}"
 
+    # Refuse to START new script work once the gateway has begun draining.
+    # Without this the ticker can dispatch a fresh long-running script a second
+    # before SIGTERM and hand the drain brand-new work to wait out.
+    if is_shutting_down():
+        return False, (
+            f"Skipped: cron scheduler is shutting down (not starting {path.name})"
+        )
+
+    # The global cron.script_timeout_seconds is the hard cap; a per-job
+    # ceiling (resolve_job_script_timeout) may only LOWER it. A 15-min job
+    # with a 2 h ceiling otherwise piles up overlapping wedged instances.
     script_timeout = _get_script_timeout()
+    if timeout_seconds is not None:
+        try:
+            _job_timeout = int(timeout_seconds)
+        except (TypeError, ValueError):
+            _job_timeout = 0
+        if _job_timeout > 0:
+            script_timeout = min(script_timeout, _job_timeout)
 
     # Pick an interpreter by extension.  Bash for .sh/.bash, Python for
@@ -132,4 +158,19 @@
             }
         env = build_subprocess_env()
+        # A script child is a plain script, not an agent process. The gateway
+        # advertises itself via AI_AGENT / HERMES_AGENT in its OWN os.environ
+        # (gateway.run.main), so without this every cron script inherited the
+        # agent marker and fleet tooling keyed on it (the gh shim's profile-wins
+        # lane resolution) misclassified laned no_agent crons as the gateway
+        # profile (t_7fee0f83). Any agent a script launches re-advertises itself.
+        for _agent_marker in ("AI_AGENT", "HERMES_AGENT"):
+            env.pop(_agent_marker, None)
+        # Same reason for the gateway's agent.process_env_files overlay (the gh
+        # lane PATH shim + git credential helper, t_45c11886): a script child
+        # keeps the env it had before this gateway sourced those files.
+        from hermes_cli.process_env_files import strip_overlay
+
+        strip_overlay(env)
+        _apply_cron_default_gh_lane(env, path)
         env.update(env_overlay)
         # Use the job's workdir as the subprocess cwd when configured,
@@ -138,4 +179,9 @@
         # concurrent gateway sessions (#69396).
         _script_cwd = workdir or str(path.parent)
+        # Popen + communicate() rather than subprocess.run() so the handle is
+        # registered and the shutdown drain can actually TERMINATE this script
+        # (see terminate_running_scripts). subprocess.run() gives the caller no
+        # handle, which is why an in-flight script used to be uncancellable and
+        # the gateway drain could only wait out its full deadline.
         proc = subprocess.Popen(
             argv,
@@ -145,33 +191,56 @@
             cwd=_script_cwd,
             env=env,
+            # Own session/process-group so the shutdown drain can signal the
+            # script's WHOLE tree (bash + its grandchildren) without ever
+            # touching the gateway's own group. See terminate_running_scripts.
+            # NOTE: start_new_session lives in popen_kwargs (POSIX branch); the
+            # win32 branch replaces it with creationflags instead.
             **popen_kwargs,
         )
-        deadline = time.monotonic() + script_timeout
-        while True:
-            if cancel_event is not None and cancel_event.is_set():
-                # Same bug class as the timeout site below: a cancelled fire
-                # must not orphan own-session grandchildren either.
-                _terminate_cron_script_tree(proc)
-                _drain_script_pipes(proc)
-                return False, "Script cancelled because cron fire ownership was lost"
-            remaining = deadline - time.monotonic()
-            if remaining <= 0:
-                # Phase 4a (#85125): a script timeout must leave ZERO living
-                # descendants. killpg only reaches the script's own process
-                # group — a grandchild that called setsid (backgrounded
-                # shell jobs, watchdogs) escapes it and keeps running after
-                # the job reports failure (#71148 / #59549).
-                # agent.deadline.kill_process_tree snapshots the descendant
-                # set via psutil BEFORE signalling, so own-session
-                # grandchildren are reached too — the unified deadline
-                # layer's tree-kill (#85147, d6a5cb9725).
-                _terminate_cron_script_tree(proc)
-                _drain_script_pipes(proc)
-                return False, f"Script timed out after {script_timeout}s: {path}"
-            try:
-                stdout_raw, stderr_raw = proc.communicate(timeout=min(0.1, remaining))
-                break
-            except subprocess.TimeoutExpired:
-                continue
+        _proc_key = id(proc)
+        with _script_procs_lock:
+            _active_script_procs[_proc_key] = proc
+        try:
+            _started = time.monotonic()
+            deadline = _started + script_timeout
+            while True:
+                if cancel_event is not None and cancel_event.is_set():
+                    # Same bug class as the timeout site below: a cancelled fire
+                    # must not orphan own-session grandchildren either.
+                    _terminate_cron_script_tree(proc)
+                    _drain_script_pipes(proc)
+                    return False, "Script cancelled because cron fire ownership was lost"
+                remaining = deadline - time.monotonic()
+                if remaining <= 0:
+                    # Phase 4a (#85125): a script timeout must leave ZERO living
+                    # descendants. killpg only reaches the script's own process
+                    # group — a grandchild that called setsid (backgrounded
+                    # shell jobs, watchdogs) escapes it and keeps running after
+                    # the job reports failure (#71148 / #59549).
+                    # agent.deadline.kill_process_tree snapshots the descendant
+                    # set via psutil BEFORE signalling, so own-session
+                    # grandchildren are reached too — the unified deadline
+                    # layer's tree-kill (#85147, d6a5cb9725).
+                    _terminate_cron_script_tree(proc)
+                    _drain_script_pipes(proc)
+                    logger.warning(
+                        "PHASE=cron_script_timeout job=%s elapsed=%d timeout=%d script=%s",
+                        job_name or path.name,
+                        int(time.monotonic() - _started),
+                        script_timeout,
+                        path.name,
+                    )
+                    return False, f"Script timed out after {script_timeout}s: {path}"
+                try:
+                    stdout_raw, stderr_raw = proc.communicate(timeout=min(0.1, remaining))
+                    break
+                except subprocess.TimeoutExpired:
+                    continue
+        finally:
+            # Deregister on EVERY exit (success, cancel, timeout) so a later
+            # gateway drain never signals a dead or recycled pid (fork
+            # shutdown-drain registry; see terminate_running_scripts).
+            with _script_procs_lock:
+                _active_script_procs.pop(_proc_key, None)
 
         stdout = (stdout_raw or "").strip()
@@ -190,4 +259,17 @@
         if proc.returncode != 0:
             parts = [f"Script exited with code {proc.returncode}"]
+            if (
+                job_id
+                and is_shutting_down()
+                and _is_shutdown_kill_returncode(proc.returncode)
+            ):
+                # Killed by the gateway shutdown drain (or its backstop), not a
+                # script failure: flag it so run_one_job re-queues one fire.
+                with _script_procs_lock:
+                    _restart_killed_job_ids.add(str(job_id))
+                parts.append(
+                    "Killed by gateway shutdown mid-run; eligible for one "
+                    "re-fire after restart."
+                )
             if stderr:
                 parts.append(f"stderr:\n{stderr}")
```

## `_run_job_script_with_claim_heartbeat` -> `cron/scheduler_script.py`

FOLLOWUP: cron/scheduler_script.py needs fork: threads the script kwargs through.

```diff
--- base:_run_job_script_with_claim_heartbeat
+++ fork:_run_job_script_with_claim_heartbeat
@@ -20,4 +20,5 @@
     claim = job.get("run_claim")
     owner = str(claim.get("by") or "") if isinstance(claim, dict) else ""
+    script_kwargs = _job_script_kwargs(job)
     if not (
         isinstance(schedule, dict)
@@ -25,5 +26,7 @@
         and owner
     ):
-        return _run_job_script(script_path, workdir=workdir, cancel_event=cancel_event)
+        return _run_job_script(
+            script_path, workdir=workdir, cancel_event=cancel_event, **script_kwargs
+        )
 
     job_id = str(job.get("id") or "")
@@ -56,8 +59,12 @@
             exc_info=True,
         )
-        return _run_job_script(script_path, workdir=workdir, cancel_event=cancel_event)
+        return _run_job_script(
+            script_path, workdir=workdir, cancel_event=cancel_event, **script_kwargs
+        )
 
     try:
-        return _run_job_script(script_path, workdir=workdir, cancel_event=cancel_event)
+        return _run_job_script(
+            script_path, workdir=workdir, cancel_event=cancel_event, **script_kwargs
+        )
     finally:
         stop.set()
```

## `_build_job_prompt` -> `cron/scheduler_prompt.py`

FOLLOWUP: cron/scheduler_prompt.py needs fork: `late_fire_note` for a one-shot fired inside the restart catch-up window (`LATE_FIRE_KEY`, #1087) + delivery-target hint binding (`_bind_cron_delivery_target_hint`).

```diff
--- base:_build_job_prompt
+++ fork:_build_job_prompt
@@ -21,4 +21,9 @@
     if extra_prompt:
         user_prompt = f"{user_prompt}\n\n## Run Context\n{extra_prompt}"
+    # One-shot fired late by the restart catch-up (t_9bfdd7e3): say so up
+    # front so the agent re-checks a time-sensitive action before taking it.
+    from cron.jobs import late_fire_note
+    if job.get(LATE_FIRE_KEY):
+        user_prompt = f"{late_fire_note(job[LATE_FIRE_KEY])}\n\n{user_prompt}"
     prompt = user_prompt
     skills = job.get("skills")
@@ -36,5 +41,7 @@
             success, script_output = prerun_script
         else:
-            success, script_output = _run_job_script(script_path)
+            success, script_output = _run_job_script(
+                script_path, **_job_script_kwargs(job)
+            )
         if success:
             if script_output:
```

## `tick` -> `cron/scheduler_tick.py`

FOLLOWUP: cron/scheduler_tick.py needs fork: `_deliver_missed_oneshot_notices(adapters=adapters, loop=loop)` right after `get_due_jobs()` (LOUD missed one-shot notices, #1087); `is_shutting_down()` gate — don't tick for due jobs while the scheduler is shutting down (#1307); `signal_shutdown`/`clear_shutdown` are exported from cron/scheduler.py.

```diff
--- base:tick
+++ fork:tick
@@ -32,4 +32,5 @@
     # lock": that previously made the scheduler appear healthy (tick returned
     # 0, heartbeat recorded success) while no job ever ran again (#87644).
+    _dispatch_release = None
     lock_fd = None
     try:
@@ -84,4 +85,22 @@
             logger.debug("Cron dispatch paused while gateway drains existing work")
             return 0
+        # A dying process must not scan for due jobs (t_1f4598ad). Anything it
+        # dispatches is refused ("Skipped: cron scheduler is shutting down"),
+        # which records last_status=error, advances next_run_at a whole period
+        # and consumes a restart_requeue marker the next boot needed. Leave
+        # due jobs and markers untouched for the next process's ticker.
+        if is_shutting_down():
+            logger.debug("Cron tick skipped: scheduler is shutting down")
+            return 0
+        # Shared-checkout admission hold (gateway/checkout_admission.py): a
+        # gate exposing ``admit()`` returns a release callable that must span
+        # the whole dispatch window, so a hold engaged mid-tick still sees
+        # every job this tick registers (get_running_job_ids) or refuses it.
+        _dispatch_admit = getattr(can_dispatch, "admit", None)
+        if callable(_dispatch_admit):
+            _dispatch_release = _dispatch_admit()
+            if _dispatch_release is None:
+                logger.debug("Cron dispatch refused by shared-checkout admission hold")
+                return 0
 
         # Dead-owner claim reclaim (#86721): execution rows carry their owner
@@ -117,4 +136,5 @@
 
         due_jobs = get_due_jobs()
+        _deliver_missed_oneshot_notices(adapters=adapters, loop=loop)
 
         # Bound the in-flight set BEFORE the dedup guard is consulted, so a
@@ -220,4 +240,8 @@
             claimed_job = dict(claimed) if isinstance(claimed, dict) else dict(job)
             claimed_job["execution_id"] = job["execution_id"]
+            # The persisted record the CAS returns never carries the transient
+            # late-fire stamp from the due-scan; carry it across.
+            if job.get(LATE_FIRE_KEY):
+                claimed_job[LATE_FIRE_KEY] = job[LATE_FIRE_KEY]
             return run_one_job(
                 claimed_job,
@@ -430,4 +454,9 @@
         return sum(_results)
     finally:
+        if _dispatch_release is not None:
+            try:
+                _dispatch_release()
+            except Exception:
+                logger.debug("cron dispatch admission release failed", exc_info=True)
         if fcntl:
             try:
```

## Other

- `cron/scheduler_failure_copy.py` (upstream-new): fork treated `"quota" in lower` as a rate-limit reason in `_summarize_cron_failure_for_delivery`; port into `classify_cron_failure_reason` if the copy table lacks it.
- `cron/scheduler_tick.py`: `tick()` must consult `cron.scheduler.is_shutting_down()` (#1307) and call `_deliver_missed_oneshot_notices` (see `tick` above).
- `agent.inactivity_watch` (fork #170 shared watchdog polling): upstream's `_run_agent_with_watchdog` keeps its own inline loop; the fork's `build_activity_diagnostic` refactor was NOT re-applied (cosmetic). The import stays for `wait_for_future_or_inactivity` if a later port wants it; drop if unused.
