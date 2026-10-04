# R08-tools FOLLOWUP: fork deltas of tools/environments/base.py whose home moved OUTSIDE the lane

Round 1 had left `tools/environments/base.py` and `tools/environments/local.py` as **verbatim
`:2:` (fork) copies** — the gate reported `AC5 identical to OURS (upstream discarded)` and 18/20
upstream-only symbols lost. That was a fake-green: 17 merged files import
`served_profile_child_env`, 7 import `kill_live_foreground_processes`, `gateway/media_fetch.py`
imports `FileFetchError` — all from these two modules. R2 re-resolved both from upstream's
facade and re-threaded the fork deltas. Two of base.py's three fork deltas now live in upstream's
extracted siblings, which are NOT conflicted and belong to no lane. They need the ports below
(orchestrator / fix-pass). Nothing in the fork delta is lost on local.py.

## 1. tools/environments/base_output.py — #553 FD_SETSIZE output blackout (efd03b48fa)

Upstream `_drain_fd_select` still uses `select.select([fd], [], [], 0.1)` and `return`s on
`ValueError` → at stdout fd >= 1024 EVERY command returns `{"output": "", "returncode": 0}`
(and `write_file` post-write verification reports a bogus "did not persist"). Fork fix = `poll()`
+ a loud `drain_error` marker. Port (fork text is in `git show fork/main:tools/environments/base.py`
lines ~1143-1310 and `_finalize_wait_result` ~1470-1505):

- `import errno` at top of base_output.py.
- `_drain_stdout(...)`: create `drain_error: list[str] = []`, pass it to `_drain_fd_select`;
  expose it to the caller (simplest: return it from `_drain_stdout`/store on the collector, e.g.
  `output.drain_error = drain_error`, and have `_start_drain_thread` callers read it).
- Replace `_drain_fd_select` body with the poll() loop:
  ```python
  poller = select.poll()
  try:
      poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
  except (ValueError, OSError) as exc:
      drain_error.append(f"{type(exc).__name__}: {exc}"); return
  try:
      while True:
          if stop is not None and stop.is_set(): return
          try:
              events = poller.poll(100)
          except OSError as exc:
              if getattr(exc, "errno", None) == errno.EINTR: continue
              drain_error.append(f"{type(exc).__name__}: {exc}"); return
          except ValueError as exc:
              if proc.poll() is None: drain_error.append(f"{type(exc).__name__}: {exc}")
              return
          if events:
              try: chunk = os.read(fd, 4096)
              except (ValueError, OSError): return
              if not chunk: return
              output.append(decoder.decode(chunk)); idle_after_exit = 0
          elif proc.poll() is not None:
              idle_after_exit += 1
              if idle_after_exit >= 3: return
  finally:
      try: poller.unregister(fd)
      except (KeyError, ValueError, OSError): pass
  ```
- `_finalize_wait_result(collector, rendered, returncode, drain_error=None)`: when
  `drain_error` is non-empty prepend the fork's marker
  `"[hermes] OUTPUT CAPTURE FAILED — the drain thread aborted (<reason>). Any output below is
  INCOMPLETE ... use execute_code as a bypass.\n"`, `logger.error(...)`, and set
  `result["drain_error"] = drain_error[0]`. `base.py` (`BaseEnvironment._wait_for_process`,
  3 call sites at theirs L496/L518/L548) passes the list through.
- Fork tests: `tests/tools/test_terminal_fd_setsize_drain.py` (if present on fork/main; grep
  `drain_error` under tests/).

## 2. tools/environments/base_session_env.py — #543 per-execution identity + HERMES_HOME snapshot leak (7b5f1d686b)

Upstream absorbed HALF of #543 (its #90782 unsets `HERMES_DELEGATED_CHILD_CONTEXT HERMES_CRON_SESSION`).
Still missing vs fork:

- `_SNAPSHOT_EXCLUDED_ENV_REGEX`: prefix must accept both dump forms
  `^(declare -x |export )(...)`, and the alternation needs `HERMES_HOME=` (EXACT, `=`-anchored —
  `HERMES_HOME_BACKUP` etc. are user vars and must survive) and `HERMES_KANBAN_` (prefix).
- `_export_dump_excluding_session_vars`: add `${!HERMES_KANBAN_*}` to the prefix unsets and
  `HERMES_HOME` to the exact-name unsets (next to `HERMES_UI_SESSION_ID`). Why: the snapshot is
  replayed in OTHER sessions; a captured HERMES_HOME repoints them at a foreign state.db /
  auth.json / kanban (mechanism-migration partial carryover, skill upstream-parity-merge
  `references/mechanism-migration-partial-carryover.md`).
- Fork tests: `tests/tools/test_env_snapshot_excludes_session_vars.py` / grep
  `_SNAPSHOT_EXCLUDED_ENV_REGEX` under tests/ for the HOME-exact + dispatcher-var assertions.

## 3. tools/environments/local.py — decisions taken IN lane (no followup, for the record)

- `_scrub_delegated_child_kanban_env` (#636 OWNER_PID seal) kept and composed INTO upstream's
  `_finalize_child_env` (and the `scrub_secrets=False` tail of `build_subprocess_env`) BEFORE
  upstream's `delegated_child_subprocess_env`. Upstream's scrub is a superset for the
  "child env carries HERMES_KANBAN_TASK" case (it drops KANBAN_ENV_KEYS and fences the board root);
  the seal remains the contract fork tests assert
  (`tests/tools/test_kanban_authority_ambient_reads.py` §4) and covers any path where the
  upstream scrub does not fire. Dispatcher spawn is unaffected: `kanban_db_dispatch.py` sets
  `HERMES_KANBAN_TASK` / `KANBAN_OWNER_PID_PENDING` AFTER `build_subprocess_env`.
- Residual risk (policy): under upstream's model a worker's TERMINAL children no longer see
  `HERMES_KANBAN_TASK` at all (they see the `HERMES_DELEGATED_CHILD_CONTEXT=<board root>` fence).
  Fleet scripts that detect "am I in a worker lane" by `HERMES_KANBAN_TASK` (e.g. fleet-merge.sh
  land-request routing in hermes-home) should key on either var. Not a code defect here.
- `apply_session_scratch_env` (#1341 per-session scratch dir) now applied once in
  `_finalize_child_env` (covers `_make_run_env`, `_sanitize_subprocess_env`, `build_subprocess_env`
  scrub path, `hermes_subprocess_env`).
- `_resolve_shell_init_files(explicit_only=False)` (c2bd33e237 #1276) restored;
  `tools/process_registry.py` (staged by L07) calls it with `explicit_only=True`.
- 7 AC3 "lost" symbols (`_bash_starts`, `_git_bash_aslr_help`, `_git_root_from_bash`,
  `_group_alive`, `_inject_context_hermes_home`, `_looks_like_msys_spawn_failure`,
  `_mandatory_aslr_enabled`) were merge-BASE symbols the fork never edited; upstream removed
  them and nothing in the merged tree references them (`_bash_starts` now lives in `pm/shell.py`).
  The other 10 relocated to `local_env_policy.py` / `local_pythonpath.py` / `base.py`.

## 4. tools/file_operations_search.py — fork search deltas (#615 + rg-version robustness)

Fork source: `git show fork/main:tools/file_operations.py` (search for each symbol).
- Add module-level `_OS_ERROR_DIAGNOSTIC_RE = re.compile(r"\(os error \d+\)\s*$")`; in the
  diagnostics splitter (`_search_stdout_and_limit` / the `startswith("rg: ")` loop at ~L114-160)
  treat a matching line as a diagnostic before shape classification (afe793a77fc: rg 0.10 emits
  per-file I/O errors without the `rg: ` prefix).
- Add `_LOOKAROUND_RE`, `_PCRE2_ONLY_ERROR_MARKERS`, `_pattern_needs_pcre2()`,
  `_is_pcre2_only_syntax_error()`; in `_search_with_rg` append `--pcre2` when
  `_pattern_needs_pcre2(pattern)` (next to the `--multiline` append at ~L888) and retry once with
  `--pcre2` inserted at index 1 when the first run's stdout matches `_is_pcre2_only_syntax_error`
  (#615).
- Replace `_is_line_oriented_newline_error` (L171) with the version-stable predicate:
  `"not allowed" in lowered and "literal" in lowered and ("\\n" in error or "newline" in lowered)`.

## 5. tools/web_tools_{extract,rescue,truncate}.py — extract side of #1516/#1562/#1517

- `web_tools_extract._dispatch_extract`: wrap `provider.extract` in the fork's `_breaker_extract`
  (open breaker → per-URL skip errors; exception → `record_failure`; any non-error result →
  `record_success`), then walk `web_tools._keyed_fallbacks("extract", provider.name)` while
  `_failed_extract_batch(results, urls)` (all errored and not all policy-blocked), stamping
  `metadata.served_by/fallback_from` on successes from a non-primary vendor and never caching
  them. Fork body: `git show fork/main:tools/web_tools.py` L1429-1480 + helpers L50-105.
- `web_tools_rescue._rescue_search`: log at DEBUG instead of WARNING when
  `web_backend_breaker.open_until(provider_name)` is truthy (a dead backend logs once per episode).
- `web_tools_truncate._trim_results`: pass through `metadata` keys `served_by`/`fallback_from`,
  and the whole `metadata` dict when `served_by == "local-pdf"`.

## 6. tools/terminal_tool_{background,guards,lifecycle}.py — fork background/guard deltas (BLOCKING)

Fork source: `git show fork/main:tools/terminal_tool.py` (background spawn block ~L3580-3840;
lifecycle guard block ~L3346-3460).
- `terminal_tool_background.spawn_background_process(...)`: add `completion_required: bool = False`
  (terminal_tool.py forwards it when True — bot_mode_dm's peer-reply spawn, #1533; until added that
  call TypeErrors). On the local spawn pass `durable_output=bool(notify_on_complete)` to
  `process_registry.spawn_local` (#1104 restart-durable notify children; canary
  `tests/tools/test_terminal_task_cwd.py` asserts `calls[0]["durable_output"] is False`). Where
  `proc_session.notify_on_complete = True` is set (L197): `if completion_required:
  proc_session.completion_required = True`, then `proc_session.agent_notify_mode =
  resolve_agent_notify_mode()` (from `tools.process_registry`) and include `"agent_notify_mode"`
  in the gateway watcher registration payload. Local backend only, before spawn:
  `from tools.script_snapshot import snapshot_tmp_script_command; command, snapshot_info =
  snapshot_tmp_script_command(command, get_session_env("HERMES_SESSION_ID", "") or "")` and
  `result_data["script_snapshot"] = snapshot_info` when set (#1341). Fork hint text for silent
  background spawns ("collect the outcome yourself with process(action='wait'...)") replaces the
  "you almost certainly wanted notify_on_complete=true" hint.
- `terminal_tool_guards.gateway_lifecycle_block`: every blocked result dict gains
  `"blocked_by": GATEWAY_LIFECYCLE_BLOCK_MARKER` (import from `cron.lifecycle_guard`; staged
  `tools/code_execution_tool.py` raises on that marker, #767); the launchctl-submit refusal text
  says "launchctl submit" (bootstrap of a SIBLING gateway's plist / an EXISTING non-gateway plist is
  allowed); the lifecycle refusal names `describe_self_gateway_identity()` ("...THIS gateway...;
  SIBLING gateway labels are allowed", fail-closed sentence when identity is unknown)
  (3bdf2accb84, 02be1bf864). Depends on `cron/lifecycle_guard.py` (L10 lane, still unresolved)
  keeping `GATEWAY_LIFECYCLE_BLOCK_MARKER` + `describe_self_gateway_identity`.
- `terminal_tool_lifecycle.py`: `_disk_usage_cache = {"timestamp": float("-inf"), ...}` (2c2adef9ed
  -inf monotonic seeds; an AST lint on fork/main flags `0.0`), and read the threshold via
  `tools.terminal_tool._disk_warning_gb()` instead of the import-time constant.

## 7. tools/skill_manager_{guards,batch}.py — shared-tree exceptions

- `skill_manager_guards.py` ~L177 (`is_external_skill_path(skill_dir)` refusal): make it
  `if is_external_skill_path(skill_dir) and not is_shared_curatable_path(skill_dir):` (import both
  from `agent.skill_utils`). ~L200 before `skill_usage.load_usage()` in the curator-managed check:
  `if is_shared_curatable_path(skill_dir): return None` (try/except → debug log). Rationale: a
  skill in a SHARED-CURATABLE dir is already an explicit opt-in to autonomous curation
  (`skills.shared_curatable`); requiring per-skill `hermes curator adopt` would disable shared-tree
  maintenance wholesale. Fork text: `git show fork/main:tools/skill_manager_tool.py` L373-445.
- `skill_manager_batch.py` ~L258 `create_targets = {... _smt._resolve_skill_dir(names[i],
  op.get("category")) ...}`: pass `local=bool(op.get("local"))` and catch
  `_smt.SkillCreateError` → `fail(i, str(exc))` style per-op rejection (the fork never precomputed
  create targets, so this seam is new on the merged tree).
