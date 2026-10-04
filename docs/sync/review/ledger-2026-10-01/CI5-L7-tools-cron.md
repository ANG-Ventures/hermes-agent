# CI round-5 lane L7-tools-cron — #1624 reds (run 36967435601, head 7bd633e015)

Branch `sync/upstream-2026-10-01-ci-L7-tools-cron` off 117c1c249d6. Card t_746cdae3.
Narrow verification: `e45-pt-L7.sh` (pytest via `~/.hermes/scripts/test-gate`, sandboxed HOME), max 3 files per call.
Linux-only tests ran on ace-ai (`/srv/ci/scratch/t_746cdae3`, venv python 3.14).

## Legend
FIXED-CODE = merge dropped/broke fork behaviour, restored in source. FIXED-TEST = the test pinned a symbol/fixture
shape upstream moved, or an upstream-only test adapted to the fork contract. INHERITED = fails on fork/main too.

## Commit 9d19e821465 (prior run)
- tests/tools/test_cron_auto_model.py — FIXED-CODE + FIXED-TEST. agent/tool_executor.py: #922 creator-model ContextVar
  branch matched legacy name `cronjob`; canonical is `cronjob_manage` after upstream's rename (branch was dead).
  hermes_cli/model_switch.py: restored fork builtin alias gpt-5.5 -> openai-codex (36134d8944b). Tests: dispatch
  `cronjob_manage`; create noop.sh in the sandbox scripts dir (upstream 7ecbe50d98 requires the script file to exist).
  93/93.
- tests/cron/test_cron_store_no_auto_sentinel.py — FIXED-TEST (same `cronjob_manage` + noop.sh shape). 8/8.
- tests/cron/test_lifecycle_guard_python_heredoc_parity.py — FIXED-CODE. cron/lifecycle_guard.py
  `_profile_derived_self_names` used `home == get_default_hermes_root()` as its trusted basis; upstream's
  get_default_hermes_root returns an arbitrary HERMES_HOME, so a sandbox home fabricated a hash-suffixed self identity
  and every explicit launchctl/systemctl gateway target read as a sibling (fail-open). Same basis as
  hermes_cli.gateway._profile_suffix. 66/66.

## This run (uncommitted work from the prior run, re-verified, + new)
- tests/cron/test_cron_fallback_alert_e2e.py — FIXED-CODE. run_agent.py re-exports `get_tool_definitions`
  (fork test patches `run_agent.get_tool_definitions`); agent/agent_init.py `_load_tools` reads tool definitions /
  requirements through the run_agent facade (`_ra()`) instead of importing model_tools directly. 2/2 (17 in batch).
- tests/cron/test_cron_kanban_env_isolation.py — FIXED-CODE + FIXED-TEST. hermes_cli/kanban_db_dispatch.py
  `_default_spawn`: pop `HERMES_DELEGATED_CHILD_CONTEXT` at the grant boundary (upstream; a dispatcher launched from
  an agent's shell carries the descendant fence, the granted worker must not). Test fixture worker claims the
  single-use `HERMES_KANBAN_OWNER_PID=pending` grant (fork t_09b90233, what hermes_cli.main does at boot) and passes
  receipt metadata (fork kanban_receipt gate). 15/15 on ace-ai (linux-only).
- tests/cron/test_cron_store_concurrency.py — FIXED-CODE. hermes_cli/cron.py: `_print_vanished_job_warning` rides
  `_print_active_jobs_summary` (both branches), so the vanished-job guard fires on every status path incl. the
  external-provider path. 14/14.
- tests/cron/test_cron_workdir.py — FIXED-TEST. run_script double accepts the fork's `_job_script_kwargs` (**kw). 16/16.
- tests/cron/test_fire_fence_bounded_wait.py — FIXED-TEST. In-flight state keyed per (home, job id) since upstream
  cb647a018ff: fixture seeds `sched._inflight_key(...)`. 3/3.
- tests/cron/test_fixture_leak_selfisolation.py — FIXED-TEST. Harness names `test_ticker_stall.py` (upstream renamed
  `test_ticker_stall_60703.py`). 1/1.
- tests/cron/test_lifecycle_guard_heredoc_data.py — FIXED-CODE. cron/lifecycle_guard.py `_contains_unsafe_gateway_action`:
  a path that `_mask_read_only_python_paths` removed from a heredoc body is provably read-only data (#1017/#1348) and
  is excluded from the raw-command mention candidates; everything else stays. 28/28.
- tests/cron/test_quota_hold.py — FIXED-CODE (upstream-only test). cron/scheduler.py
  `_should_suppress_transient_failure_page` never swallows the alert when `_quota_hold_seconds` is set: the job is
  parked past a known provider window (#89376) and will not re-fire next tick, so the hold notice is the operator's
  only signal. Fork transient-suppression contract otherwise unchanged. 4/4 (+26/27 suppression suite; the 1 red there
  is INHERITED, see below).
- tests/tools/test_blocked_command_guidance.py — FIXED-CODE. tools/terminal_tool_guards.py: fork guidance copy
  (`process(action="wait", timeout=...)` for bounded jobs) restored in the nohup/`&` recipes. 8/8.
- tests/tools/test_browser_real_profile.py — FIXED-TEST. Container carve-out is now
  `hermes_constants._container_or_chmod_skipped` (bound into hermes_cli.config at import); patch both names instead of
  the removed `hermes_cli.config._is_container`. 65/65.
- tests/tools/test_code_execution_git_lane_env.py — FIXED-CODE + FIXED-TEST. `_scrub_child_env` moved to
  tools/code_execution_env.py upstream; fork git-lane carry (t_45c11886 / C3 #1254: GIT_AUTHOR_*/GIT_COMMITTER_*,
  credential-helper-only GIT_CONFIG_* group, HERMES_AGENT passthrough, multiplexed-profile HERMES_HOME repoint)
  re-threaded into the new module. Test imports from the new module. 1/1 (collection error before).
- tests/tools/test_cron_model_arg_coercion.py — FIXED-TEST. Handler is `ct._cronjob_handler` (upstream rename). 13/13.
- tests/tools/test_gateway_foreground_deafness.py — FIXED-CODE + FIXED-TEST. tools/terminal_tool.py `_plan_execution`:
  fork #1012 — a messaging-gateway turn keeps the REFUSAL for over-cap foreground timeouts (promotion to a tracked
  background process is for CLI/TUI/kanban turns). Test patches `tools.terminal_tool_backends._create_environment`
  (upstream move). 32/32.
- tests/tools/test_launchctl_guard_diagnostic.py — FIXED-TEST (upstream-only). Fork allows `launchctl bootstrap` of an
  EXISTING non-gateway plist (test_lifecycle_guard_bootstrap_existing_plist; launchd reads its Label). The diagnostic
  test now bootstraps through a staged copy that does not exist at scan time, the shape the label-independent
  rejection is about. Fork guard not weakened. 2/2 (+35 existing-plist suite).
- tests/tools/test_lazy_sdk_probe_importable.py — DELETED (fork test of the retired `tools.lazy_deps.ensure_importable`
  / `_ensure_<sdk>_sdk` probes). Upstream retired lazy_deps to an old-updater stub (6a5a6a05d2d) and replaced the
  surface with `pm.extras.ensure_import` + `tools.environments.remote_common.ensure_lazy_dep`; tests/pm/test_no_lazy_deps.py
  (CI lint) forbids production imports of tools.lazy_deps, and tests/pm/test_extras.py::test_available_counts_sys_modules_fakes
  pins the property this file tested (an importable / sys.modules-faked SDK is available). Evidence: in-process check
  `sys.modules['modal']=ModuleType -> ensure_lazy_dep('modal')` returns without attempting an install.
- tests/tools/test_request_tool_approval.py — FIXED-TEST. `_get_approval_mode` lives in tools.approval_context
  (upstream split); patch it there. 13/13.
- tests/tools/test_skill_manager_create_shared.py — FIXED-CODE. tools/skill_manager_tool.py name validator uses
  `fullmatch` (`$` accepts a trailing newline; fork AC10 "foo\n" is not a name). 35/35.
- tests/tools/test_snapshot_session_id_leak.py — FIXED-TEST. Upstream's `export -p` dump test asserted HERMES_HOME
  survives; fork #543 excludes exact-name HERMES_HOME (a replayed snapshot must never repoint another session's home;
  HERMES_HOME_BACKUP survives). Test pinned to the fork contract, with the _BACKUP survivor asserted. 4/4.
- tests/tools/test_terminal_task_cwd.py — FIXED-CODE. tools/terminal_tool_background.py `_spawn`/`spawn_background_process`
  pass `durable_output=bool(notify_on_complete)` to spawn_local (fork t_1191e078: file-backed output so a notify child
  survives a gateway restart) — dropped when upstream extracted the background path. 7/7.
- tests/tools/test_timeout_transport_drain.py — FIXED-CODE + FIXED-TEST. The fork `_run_single_child` supervisor
  (#1535/#1542) is the hot path and closes through the teardown door, so upstream's `_defer_close_after_timeout`
  (which drains the abandoned worker's transports, #94248 native half) was never reached. Split the drain into
  `_drain_abandoned_child_transports` (delegate_tool_child_run.py) and call it from the fork timeout path when the
  child future is still running. Test double carries a frozen `last_activity_ts` (R07/L6 ruling: a child idle for the
  whole cap is timed out; unknown/advancing clocks are timed_out_running). 4/4.
- tests/tools/test_web_backend_breaker.py — FIXED-CODE. tools/web_tools_extract.py: `_breaker_extract` (402/401
  dead-backend breaker, fork #1562) + `_failed_extract_batch` + the keyed `web.extract_fallbacks` chain (#1516) restored
  into `_dispatch_extract` (upstream's extraction kept only the keyless rescue). 19/19.
- tests/tools/test_web_keyed_fallbacks.py — FIXED-CODE. Same change; hooks (`_rescue_extract`, `_rescue_eligible`,
  `_keyed_fallbacks`, `_load_web_config`) are read through the `tools.web_tools` facade (the name tests patch;
  `_rescue_extract` re-exported there). tools/web_tools_truncate.py `_trim_results` keeps `served_by`/`fallback_from`
  (and whole metadata for `local-pdf`) — the fork trim did; upstream's dropped every metadata key. 6/6 (+ web
  neighbours: extract_robustness/timeout/keyless_rescue/keyless_fallback/truncate/dict_urls/local_pdf all green).
- tests/tools/test_windows_native_support.py — FIXED-CODE. hermes_cli/kanban_db_dispatch.py `_classify_worker_exit`
  and `_default_spawn` read `_recent_worker_exits` through the `kanban_db` facade (`_record_worker_returncode` writes
  the facade's dict; a rebound dict there must be the one classified). 16/16 (+ kanban_quota_exit / infra_exit green).

## INHERITED / out of lane (fail identically on the untouched base 117c1c249d6; not chased)
- tests/hermes_cli/test_kanban_worker_exit_trailer.py (3) — not in any manifest; red at base.
- tests/cron/test_cron_transient_failure_suppression.py::test_summarizer_bare_quota_gets_quota_framing — red at base
  (the failure summarizer now routes bare "quota" through agent/turn_failure_copy billing wording); not in any manifest.
- tests/tools/test_delegate_timeout_cleanup.py, test_delegate_liveness_timeout.py (2), test_delegate_timed_out_running.py
  ::test_list_shows_depth_and_parent_for_two_level_tree, tests/tools/test_kanban_worker_authority_isolation.py
  ::test_genuine_dispatcher_worker_remains_authorized — L6 / L4 manifests; fixed on those lane branches.

## Overlap notes for the orchestrator
- tools/delegate_tool.py (+1 import line, +5 lines in the timeout except) and tools/delegate_tool_child_run.py — L6 also
  touches delegate_tool.py (different hunks: import block + timeout entry `last_event_age`). Expect a trivial merge.
- hermes_cli/kanban_db_dispatch.py — L4 touches `_terminate_reclaimed_worker` / reaper; my hunks are
  `_classify_worker_exit`, `_default_spawn` (two spots). Disjoint.
- cron/scheduler.py `_should_suppress_transient_failure_page` — one guard clause; no other lane lists scheduler.py.
