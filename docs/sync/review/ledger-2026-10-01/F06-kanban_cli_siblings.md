# F06-kanban_cli_siblings — round-4 re-thread ledger (card t_10c5d53e)

Targets: `hermes_cli/kanban_parser.py`, `hermes_cli/kanban_ops.py`, `hermes_cli/kanban_boards.py`,
`hermes_cli/kanban_output.py`. Inputs: R01-cli-kanban FOLLOWUP §1-4 + `followup-src/e45-R01-cli-kanban/`.
Method per brief: upstream structure kept (table-driven `_SPECS`, sibling→facade late imports), fork behaviour re-threaded.

Gates per file: `py_compile` OK ×4; `ruff --select F821,F811,F823,F401` clean ×4; `git add` ×4 (staged).

## Items

| target::symbol | status | evidence | test(s) |
|---|---|---|---|
| kanban_parser::_SPECS 12 missing verbs (`budget home-index home-lint lane-model notify-repair pins priority reopen requeue triage-resolve update workspace`) | PORTED | all 12 in `sub.choices`; `update` as `edit` alias; `lane-model`/`workspace` nested via `children=`; parser↔`_HANDLERS` audit: 0 handlers without verb, parser-only verbs = {boards, clone, base-check, repair} (dispatched before `_HANDLERS` in `kanban_command`) | parser proof script (below); test_kanban_lane_model, test_kanban_priority_verb, test_kanban_triage_exit, test_kanban_notify_repair, test_kanban_home_index |
| kanban_parser::per-verb missing dests (assign/reassign request_changes+worker_ok; claim review; comment body_file; complete draft_ok/reason/superseded_by/survivor_*; create allow_flagship/force_reason/home/parent_kind/pin_sub/pin_sub_fallback/reasoning_effort/session; edit clear_model/model_override/no_worker/session; link kind; list flat_all/home; notify-subscribe wake; reclaim operator; request-changes coverage/foreign_ok/operator; request-review allow_same_actor; schedule at/now; set-model all_active/allow_flagship/clear_effort/live/model_json/pin_sub/pin_sub_fallback/reasoning_effort/reclaim/task_ids/where) | PORTED | script over `FOLLOWUP-kanban_parser-missing-dests.txt`: **unresolved dests: []**; `--help` renders for all 61 verbs | test_kanban_cli*, test_kanban_create_body_file (parse part), test_kanban_superseded_outcome, test_kanban_sub_pin_optin, test_kanban_schedule_wake, test_kanban_no_worker, test_kanban_notify_subscribe_cli |
| kanban_parser::GLOBAL foreign_ok/operator/takeover_home (`--takeover/--foreign-ok`, `--operator`, `--keep-home/--transfer-home`) | PORTED | `_add_home_guard_flags(sub)` runs after `_add_commands`, late-imports `_HOME_GUARDED_ACTIONS` from the facade (sibling→facade, no shim); `assign t bob --takeover r --transfer-home` → `foreign_ok='r' takeover_home='transfer'` | test_kanban_home_session (CLI parts pass; 4 reds inherited, see below) |
| kanban_parser::`clone` (url/dest/git_opts) + `base-check` | PORTED | `_cmd(..., setup=_clone_setup)` delegates to `kanban_clone.add_arguments` / `kanban_branch_base.add_arguments` (late import); `clone URL -q --depth 1` → `git_opts=['--quiet','--depth=1']` | test_kanban_clone (all pass) |
| kanban_parser::set-model shape `task_ids nargs=* + --model` | PORTED | fork shape restored; facade's BRIDGE for upstream `<task_id> [model]` stays harmless (never fires: `task_id` dest no longer exists on set-model) | test_kanban_sub_pin_optin, test_kanban_pinned_sub_refusal, test_kanban_claimed_card_pin |
| kanban_parser::`repair --board` (after the verb) | PORTED | `default=argparse.SUPPRESS` so the global `--board` survives when omitted; `--board b repair --board c` → `c` | test_kanban_db_repair, test_kanban_quota_admission_repair |
| kanban_parser::`_intermix_optional_positionals` (#1166) | PORTED | applied after the home-guard flags; `comment t --author me hello world` → `text=['hello','world']` | test_kanban_cli, test_kanban_body_file |
| kanban_parser::`gc --done-retention-days/--dry-run`, `dispatch --max-running/--ignore-load-gate`, `repair --reclassify-quota-crashes/--dry-run`, `promote --json/--dry-run` | PORTED | in `_SPECS`; `gc` retention flags keep upstream's `_nonnegative_int` type (upstream behaviour) | test_kanban_gc_retention, test_kanban_cli_dispatch_load_gate, test_kanban_cli_dispatch_passthrough, test_kanban_quota_admission_repair |
| kanban_parser::`promote --force` (fork) | DROPPED | upstream `promote_task` has no `force=` (#106195); the merged facade `_cmd_promote` never reads `args.force`; L01b ledger records the same decision. Fork test `test_promote_refusal_names_the_scheduled_exit` calls `kb.promote_task(force=)` directly → inherited red owned by kanban_db (XFOLLOWUP below) | — |
| kanban_parser::`create --workspace` default | NO-PORT-NEEDED | upstream default `None` kept on purpose: facade `_parse_workspace_flag` distinguishes "omitted" from explicit `scratch` (project opt-out) | test_kanban_cli |
| kanban_ops::`_one_shot_load_gate` + `_dispatch_limit_notes` | PORTED | new defs; `from hermes_cli import kanban_load_gate`; `profiles.get_active_profile_name` for override attribution | test_kanban_cli_dispatch_load_gate (all pass) |
| kanban_ops::`_cmd_dispatch` (load gate, `--max` additive vs `--max-running` ceiling, per-profile cap dict, `load_gate_override` events via `kbc.write_txn`+`kb._append_event`, spawned_unwatched via `kbn.list_notify_subs`, route/kind/source/prio per spawn, budget-paused banner, lock-skip via `kb.format_dispatch_lock_skip`, gate auto-resolved/closed-unmerged, collision warnings, respawn-guard detail, no-worker count, HUMAN-review wording, review-awaiting-human + stranded-by-triage + woken/unwoken, JSON keys incl. `load_gate`/`limit_notes`) | PORTED | re-threaded into upstream's body (kept upstream's `reaped_terminal_workers`, `rate_limited`, `_print_json(ascii=True)`, `kbd._positive_int`); `_collect_review_awaiting_human/_print_review_awaiting_human/_print_stranded_by_triage/_fmt_respawn_guard_detail` late-imported from the facade; live smoke `dispatch --dry-run` prints `Load gate: admitting load1=… allowance=4` + fork lines | test_kanban_cli_dispatch_passthrough, test_kanban_cli_dispatch_load_gate, test_kanban_dispatch_collision_warning, test_kanban_dispatch_lock_observability (1 inherited red) |
| kanban_ops::`_cmd_daemon` (`parent_satisfied_sticky` in did_work + summary; `load_gate=gate_from_config()`) | PORTED | both deltas threaded; upstream's `describe_suppression` held-back text kept | — (no file drives daemon --force) |
| kanban_ops::`_cmd_tail`, `_cmd_watch` | NO-PORT-NEEDED | manifest: fork == base | test_kanban_watch_banner (pass) |
| kanban_ops::`_cmd_gc` + `_gc_remove_workspaces` (`workspaces_root(stale_pin_ok=True)`, `--done-retention-days`, `--dry-run` listing, `process_cwd_snapshot_scope`, `safe_remove_workspace_dir`, worktree lane `conn=/reason="gc_archived"`, `gc_status_audit` count) | PORTED | upstream's `shutil.rmtree` + `_is_managed_scratch_path` guard replaced by the fork's strict choke point (2026-09-20 incident); upstream's `retention days >= 0` early error + `0 disables` kept | test_kanban_gc_retention (pass), test_kanban_workspace_deletion_guard `-k gc` 9/9 pass, test_kanban_workspace_retention (3 reds: 2 inherited-class `link child running` in kanban_db, 1 `_has_active_children(conn=None)` in kanban_db — see XFOLLOWUP) |
| kanban_ops::`_cmd_repair` (`--reclassify-quota-crashes` via `kanban_quota_repair`, `--dry-run` readonly conn / refusal without the flag) | PORTED | `kb.connect_readonly()` / `kbc.connect()` (sibling, per card note); smoke: `repair --dry-run` → `--dry-run requires --reclassify-quota-crashes` | test_kanban_quota_admission_repair, test_kanban_db_repair (pass) |
| kanban_boards::`_board_task_counts` under `kb.enumerating_boards()`; `_cmd_boards_list` loop via `kb.enumerating_each(boards)` | PORTED | both helpers exist on merged kanban_db (L780/L871); smoke `boards list` OK | test_kanban_cli (boards tests pass) |
| kanban_boards::other 9 defs | NO-PORT-NEEDED | manifest: fork == base | — |
| kanban_output::`_fmt_task_line(t, refusal=None)` + `_fmt_refusal` + `_REFUSAL_VISIBLE_STATUSES` + `_pin_badge` | PORTED | `format_pin_badge` late-imported from `hermes_cli.model_policy` | test_kanban_cli list tests |
| kanban_output::`_task_to_dict` fork fields (reasoning_effort, pin_sub_reason, pin_sub_fallback, unhomed, no_worker) | PORTED | `getattr` form so a Task without the column still serialises | test_kanban_cli `--json` |
| kanban.py (facade) drop inline shadows of `_fmt_task_line/_task_to_dict/_pin_badge/_fmt_refusal` and import from kanban_output | XFOLLOWUP hermes_cli/kanban.py | not my target; the facade's copies are now byte-equivalent in behaviour to kanban_output's — orchestrator can swap the import block at L29-35 and delete L148-204 | — |

## Tests run (file-scoped via e45-pt.sh; every red binary-classified against `base` via e45-ptd.sh)

PASS (all green): test_kanban_cli_auto_subscribe, test_kanban_cli_dispatch_load_gate, test_kanban_cli_dispatch_passthrough,
test_kanban_cli_exit_status, test_kanban_cli_non_owner_complete, test_kanban_body_file, test_kanban_clone, test_kanban_db_repair,
test_kanban_dispatch_collision_warning, test_kanban_gc_retention, test_kanban_no_worker, test_kanban_notify_subscribe_cli,
test_kanban_priority_verb, test_kanban_quota_admission_repair, test_kanban_watch_banner, test_kanban_lane_model, test_kanban_triage_exit,
test_kanban_superseded_outcome, test_kanban_notify_repair, test_kanban_notify_repair_unhomed, test_kanban_pinned_sub_refusal,
test_kanban_claimed_card_pin, test_kanban_block_review_parking, test_kanban_pool_ratelimit_gates, test_kanban_respawn_guard_pr_owner,
test_kanban_closed_unmerged_gate, test_kanban_workspace_mount, test_kanban_pr_freshness, test_kanban_lane_fallback_composition.
Counts: batch1 38/39, batch2 193/200 (+1 skip), batch3 554/584, re-run of 14 green files 193/193.

Reds — none pass on base AND fail merged because of my four files; each is outside my targets:

| test | class | cause (file) |
|---|---|---|
| test_kanban_cli::test_kanban_edit_updates_documented_task_fields | XFOLLOWUP kanban_db | upstream test expects event kind `reprioritized`; merged `_set_priority` path (fork) records `priority_set`. kanban_db has both spellings (`kanban_db.py:9782` vs `kanban_db_connect.py:1098`); L01b to pick one. Not in base (upstream-only test). |
| test_kanban_create_body_file (2) | XFOLLOWUP kanban_db / TEST | upstream-only test (d8b64d4a6d); fork `create_task` prepends `origin: unhomed (...)` provenance line to the body (`kanban_db.py:3385`) when `HERMES_KANBAN_ALLOW_UNHOMED_CREATE` is set. Test asserts verbatim body. Design decision for orchestrator (fork provenance vs upstream verbatim). |
| test_kanban_dispatch_lock_observability::test_other_process_reports_parent_holder_in_one_tick | XFOLLOWUP-TEST | subprocess calls `kb.dispatch_once` on the facade; upstream moved it to `kanban_db_dispatch` (test already imports kbd — change `kb.dispatch_once`→`kbd.dispatch_once`). Passes on base. |
| test_kanban_home_index::test_sync_hook_lives_inside_write_txn_commit_path | XFOLLOWUP kanban_db_connect | `_sync_home_index` not in `write_txn` commit path; not my target. Passes on base. |
| test_kanban_workspace_retention (3): test_enclosing_scratch_card_is_still_an_owner, test_gc_defers_linked_parent_until_child_finishes[scratch/worktree] | XFOLLOWUP kanban_db(_graph) | (a) `safe_remove_workspace_dir` → `_has_active_children(conn=None)` when called without `conn` (`kanban_db.py:10536`); (b) `link_tasks` refuses `child is already running` (upstream running-child check, `kanban_db.py:6234`) — the test links after claiming. Both pass on base; cause in kanban_db, not the CLI (my `_gc_remove_workspaces` always passes `conn=`). |
| test_kanban_workspace_deletion_guard (13) | XFOLLOWUP kanban_db | 12× `_has_active_children(conn=None)` (same (a)), 1× worktree-lane refusal audit missing (`kbw._cleanup_worktree_workspace` without conn). `-k gc` subset (the lane that drives my `_cmd_gc`) is 9/9 green. All pass on base. |
| test_kanban_schedule_wake::test_promote_refusal_names_the_scheduled_exit | XFOLLOWUP kanban_db | calls `kb.promote_task(force=)`; merged has no `force` (upstream #106195, L01b DROPPED). Passes on base. |
| test_kanban_home_session (4) | XFOLLOWUP tools/kanban_tools + kanban_db + TEST | `KANBAN_UNBLOCK_SCHEMA` lacks `foreign_ok` (tools lane); `_claim_and_open_run/_extend_live_stale_claim/_request_review_txn` unguarded writers (kanban_db AST lint); `test_every_cli_verb_reaching_a_guarded_writer_binds_the_actor` regexes `handlers = {` in facade source — merged spells it `_HANDLERS = {` (source-reading test; propose regex `_?[Hh]andlers = \{`). All pass on base. |
| test_kanban_review_lifecycle (2) | XFOLLOWUP kanban_db | `request_changes` implementer resolution / `review_requested.implementer` payload; DB-level tests, no CLI. Pass on base. |
| test_kanban_lane_review_regressions (2) | XFOLLOWUP-TEST | `kb.dispatch_once` → `kbd.dispatch_once` (facade re-export dropped upstream). Pass on base. |
| test_kanban_worker_route_runtime_pin (20) | XFOLLOWUP tests | `ModuleNotFoundError: tests.run_agent.test_fallback_reasoning_override` — file exists on base, absent merged (another lane's test tree). |
| test_kanban_sub_pin_optin::test_spawn_argv_carries_pinned_provider_model_and_effort | inherited | fails on base too (`cmd[-m +1] == 'hermes_cli.main'`). |

## Parser proof (card requirement)
Script: build parser in a sandbox HOME, read `FOLLOWUP-kanban_parser-missing-dests.txt`, assert every verb present and every dest in the verb's `_actions`; render `--help` for each verb.
Output: `missing verbs still absent: []` / `unresolved dests: []` / `--help OK for 61 verbs` / `handlers without verb: []`.

## Live smoke (sandbox HERMES_HOME, `run_slash`)
`help`, `create … --json`, `list --json` (fork fields present), `dispatch --dry-run` (load gate line + fork summary), `gc --dry-run`, `boards list`, `pins`, `lane-model show`, `repair --dry-run` → all exercised, no tracebacks.

## Counts
ported 17 · no_port_needed 3 · dropped 1 · xfollowup 1 (facade import swap) + 12 red groups ledgered · tests_pass 978 · tests_fail_inherited 1 · tests_fail_xfollowup 50
