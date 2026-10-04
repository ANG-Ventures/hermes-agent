# Ledger — lane R07-delegate (parity sync 2026-10-01, card t_a711c464; round-2 of L07-delegate t_3bf08f7e)

Lane files: tools/delegate_tool.py (41 hunks), tools/async_delegation.py (34 hunks). Both are
extraction seams: upstream decomposed the module into `<stem>_<topic>.py` siblings (staged `A`,
outside this lane) while the fork added behaviour inline. Stages + scripts under
`/tmp/e45-R07-delegate/` (`assemble_dt.py`, `freenames.py`, `smoke.py`, `fndiff.py`).

| path | hunks | mode | why | residual risk |
|---|---|---|---|---|
| tools/delegate_tool.py | 41 | UP+F (upstream facade/sibling structure; fork hot path + 64 fork-new symbols re-threaded) | Upstream (67 commits): facade + 8 siblings; facade-new `_ROLES`, `_open_child_session_db` (#81267), `_apply_child_cache_ttl`, `_apply_child_compression_cap`, `_build_children`, `_oneshot_spawn_budget`, `_p`-table schema, routing_cfg (c47bf78d68), per-task images, `group`/independent_completions. Fork (19 commits): child-lifecycle supervisor (`_SteerLedger`, `TIMED_OUT_RUNNING`, `_reap_subtree`, late results, `_ChildLifecycle`, `_supervise_child_future`, hung ceiling `_get_hung_child_seconds`, `_get_child_max_wall_seconds`, `_CHILD_TIMEOUT_FLOOR_S` — #1535 #1542 #1549 #1573 #1585 #1598 #1599), explicit orchestrator opt-in role (#1541), `_regate_inherited_child_tier` (#1523), boomerang `inherit_context` fold, per-task `skills`, audited `model`/`provider`/`allow_flagship_reason` override + `strict_args`, blackbox attribution + **cron-child ContextVar capture (fork feature: cron approval gate reads ContextVar)**, lineage log line, restart-durable background delegation (`_build_durable_background_spec`, `build_recovered_delegation_runner` — live caller gateway/run.py:10387). **Resolution:** upstream header + sibling re-import block (+ the 7 sibling symbols the fork body still calls directly: `_close_subagent_steering`, `_extract_output_tail`, `_finalize_child_results`, `_looks_like_error_output`, `_stringify_tool_content`, `_recover_tasks_from_json_string`, `_validate_batch_tasks`); upstream facade-new helpers verbatim; all 64 fork-new symbols verbatim; `_build_child_agent` = upstream body with every fork hunk re-threaded (params, role opt-in, spawn log, prefill fold, re-gate+skills, lineage log, blackbox/cron block); `_run_single_child` and `delegate_task` = **fork whole-function** (POLICY-DIVERGENCE, below) with upstream's `routing_cfg`, `_coerce_task_images`/`_delegate_images`, `_oneshot_spawn_budget` spliced in; schema = upstream `_p` table + fork props (top-level `inherit_context`/`skills`/`role`/`model`/`provider`/`allow_flagship_reason`, per-task `inherit_context`/`skills`/`role`) + fork `timed_out_running` + role-opt-in description text; registry = upstream handler + fork `strict_args`/`extra_accepted_args` (+`images`). Checks: 0 markers; py_compile; free-name analysis 0 unresolved (controls: ours 0 / theirs 0); call-signature check of every sibling-imported call 0 problems; dup defs/imports none; zero-seed lint clean; import smoke OK with sibling-lane unresolved modules stubbed (`smoke.py`: facade resolves `_run_single_child`/`_build_child_agent`/`delegate_task` from the facade, `_strip_blocked_tools`/`_get_child_timeout`/`steer_subagent` from siblings; dispatch/results/child_run import; dynamic schema builds with fork props). Gate AC3 "lost" list = extraction false-loss: 44 re-imported from siblings, 11 sibling-resident and unused by facade code, 7 nested-only, 4 upstream-retired base symbols with 0 refs in the merged tree (`_inherit_parent_base_url`, `_preserve_parent_mcp_toolsets`, `_retain_recent_subagent`, `_sanitize_tool_input_summary`; fork never changed them). Converged on upstream: `override_max_tokens`/`max_output_tokens` per-child output cap removed upstream (fd3565deec) — kwarg accepted, inert. | **POLICY-DIVERGENCE (owner ruling needed before landing):** two independently built child supervisors. Facade hot path runs the FORK `_run_single_child` (inline supervisor) and FORK `delegate_task` (inline `_execute_and_aggregate` + durable async dispatch); upstream's `_ChildRun` (child_run.py) and `_Batch`/`_run_batch` (dispatch.py) are imported, intact, and importable but NOT on the hot path. Upstream child-run/dispatch behaviour therefore not carried: inactivity-bounded `child_timeout_seconds` + 80% warn (03973bd02b 97beaeeeb3), credential lease binding (c38f7f6d4c d75a51872c), no-join of wedged workers (e24e66c6d0), stale child ends sync wait (90663bff30), schema-retry in delegated context (e002cdb92d 45ab3ad57f), process accounting handoff (869228cab4 ef9239571d 3c0d90e8ef), late-attached stop kind (60559d4e0e), grandchildren die with orchestrator (dd497c3d59), per-task completion `group`s (028fe2c4c8), batch numbering per conversation (0cb996d977), /stop halts background subagents (b71359066c), wake-capable provenance gate (dffc0fa2d5), join before finite chat exit (49ef015ca3), persist API units once (d5926b2494), crash mid-unit keeps finished children (c5594ec4b3), failed child surfaced immediately (6767c06d34), parent cancellation for rejected units (946111cb11), shared timer thread (561b053f79), telemetry (25c1b008c8). Reverse option (upstream hot path): swap in theirs.py `_run_single_child`/`delegate_task` and re-thread the fork's 26+24 hunks into `_ChildRun`/`_Batch` (sibling files). Either way the sibling FOLLOWUPs below are required. **FOLLOWUP-CRITICAL tools/delegate_tool_toolsets.py**: `_strip_blocked_tools` must become the fork version (ff30e9cfb8): `strip_blocked_delegate_toolsets(toolsets, toolset_definitions=TOOLSETS, delegate_blocked_tools=DELEGATE_BLOCKED_TOOLS)` from `agent.fork_ext.tool_gate` then drop `kanban` — the `code_execution` inheritance exemption (fork-features.json; canary tests/tools/test_delegate_toolset_scope.py). fork_ext refs fork:1 merged:0 because the function now lives in the sibling; upstream's `_resolve_child_toolsets` calls the sibling copy, so a facade override would be dead. **FOLLOWUP tools/delegate_tool_registry.py** (steer ledger 1dcb249a12 + late results): `steer_subagent` — `ledger = record.get("steer_ledger"); seq = ledger.accept(text) if isinstance(ledger, _SteerLedger) else None` before `agent.steer`, `ledger.withdraw(seq)` when not accepted; `list_active_subagents` — copy `steer_ledger` key, add `depth` (= `agent._delegate_depth` or record depth+1); `_handle_control_action` list payload gains `late_results` from `_owned_late_results(parent_agent)` (late-import from the facade); `_close_subagent_steering` docstring (ledger is the missed-steer source). **FOLLOWUP tools/delegate_tool_config.py**: `_get_child_timeout` floor 30→`_CHILD_TIMEOUT_FLOOR_S` (60, 7ff750b798; import from facade lazily); `_resolve_delegation_credentials` — resolve `delegation.model` aliases via `hermes_cli.model_switch.resolve_model_pair_for_storage` (fe6da50511), custom-lane attribution `provider = get_custom_provider_pool_key(base_url) or "custom"`, recovered base_url == named provider's endpoint keeps provider identity + api_mode (6998c783ca), return `explicit_tier_overrides` ({service_tier,speed} from explicit request_overrides; b3cad0b7df — until then `_regate_inherited_child_tier` receives None, which it tolerates); `_resolve_child_credential_pool` — accept `custom:<name>` as `custom` (both branches). **FOLLOWUP tools/delegate_tool_dispatch.py** (only if the ruling flips to upstream dispatch): `join_late` (`_delegate_join_late`, `_join_late_result`), `_reap_subtree` on parent-interrupt and async-cancel, `TIMED_OUT_RUNNING` "…" icon, `_build_durable_background_spec` → `dispatch_async_delegation_batch(durable_spec=, current_boot_id=)`, registry_cap/registry_error sync fallback with note, `acknowledge_outbox_event(fallback_event_id, outcome="dropped", reason="fallback_ran")`. Upstream `_build_children` kept (unused by fork `delegate_task`; patched by tests). `group` task prop is advertised only under `delegation.independent_completions` and is ignored by the fork dispatch path. Import smoke could not run unstubbed: tools/terminal_tool.py and tools/async_delegation.py (this lane, below) still carry markers. |

## UNRESOLVED (ceiling-stop, markers intact, unstaged, never edited this round): tools/async_delegation.py — 34 hunks

Round-1 analysis above (L07-delegate.md §tools/async_delegation.py) still holds: upstream body, keep BOTH
APIs, re-thread the fork registry protocol into upstream's `_dispatch`/`_dispatch_admitted`/`_finalize`/
`_single_crash`/`_batch_crash`. Added this round so the next worker can budget before starting:

Both-changed functions (3-way sizes; `+/-` vs merge-base 26350357d7). Upstream REPLACED the dispatch/
interrupt bodies (−141/−128/−53 lines, moved into `_dispatch*`, `_interrupt_records`, `_stalled_*`);
the fork EXTENDED the base bodies in place (+78/+113/+35/+33). None is pickable by side.

| fn | base | fork (+/-) | upstream (+/-) |
|---|---|---|---|
| _persist_completion | 10 | 7 (+5/-8) | 8 (+2/-4) |
| recover_abandoned_delegations | 55 | 8 (+6/-53; body → fork `_recover_abandoned_delegations`) | 48 (+35/-42) |
| restore_undelivered_completions | 53 | 72 (+22/-3) | 24 (+15/-44) |
| claim_completion_delivery | 18 | 18 (+18/-18; → fork `_claim_completion_delivery` + `_producer_scoped`) | 14 (+3/-7) |
| claim_event_delivery | 9 | 12 (+4/-1) | 9 (+4/-4) |
| complete_event_delivery | 3 | 8 (+8/-3) | 2 (+1/-2) |
| get_durable_delegation | 17 | 21 (+4/-0) | 10 (+6/-13) |
| dispatch_async_delegation | 145 | 221 (+78/-2) | 26 (+22/-141; → `_dispatch`) |
| _finalize | 9 | 42 (+35/-2; `_begin_finalization`/`_finish_finalization` attempt guards) | 20 (+20/-9) |
| dispatch_async_delegation_batch | 131 | 235 (+113/-9; `durable_spec=`, `current_boot_id=`, registry cap → `_sync_fallback_registry_cap`) | 34 (+31/-128; → `_dispatch`/`_batch_crash`) |
| list_async_delegations | 60 | 103 (+47/-4) | 39 (+13/-34) |
| interrupt_all | 27 | 61 (+35/-1; `cancel_matching`) | 7 (+5/-25; → `_interrupt_records`) |
| interrupt_for_session | 55 | 83 (+33/-5) | 8 (+6/-53) |
| _reset_for_tests | 16 | 18 (+3/-1) | 18 (+4/-2) |

Hunk map (conflicted.py line → fork/base/upstream line counts): #3 L288 O83/B10/T8 `_same_completion_event`+
`_sync_completion`+`_canonical_terminal` vs upstream `_persist_completion`; #6 L486 O48 recover-row shape vs
upstream `_ROUTING_KEYS` packing (#7 L573); #10 L661 O42/T27 restore loop; #11 L762 O18/T74 fork claim vs
upstream `sweep_orphaned_completions` (both must exist); #15 L925 O25/T7 `_producer_scoped` vs
`is_interim_delegation_event`; #16 L968 O39 `note_event_delivery_attempt`/`_reconcile_terminal_event_receipt`;
#17 L1071 O22 `@_producer_scoped` complete_event_delivery (+`_with_retry`); #18 L1310 O17/T33 dispatch header
vs upstream `_dispatch` block; #19 L1409 O36 registry-cap admission vs upstream `active_slots`; #20 L1477 O25
`attempt_id` guard vs upstream `_records_lock` admit; #24 L1594 O42/T26 fork `_finalize` vs upstream
`dispatch_async_delegation`; #25 L1677 O6/T43 `_begin_finalization` vs upstream batch; #27 L1763 O46
`_publish_restartable_completion` vs `_push_completion_event`; #28 L1872 O69/B67/T8 fork batch vs upstream
`push_task_failure_notice`; #29 L2021 O231/B82/T4 fork batch body (the god hunk); #32 L2810 O88/T5
`_mark_recoverable`+`recover_async_delegations`+`enqueue_pending_outbox`+`acknowledge_outbox_event` vs
`_interrupt_records`; #33 L2948 O77/B49/T5 `interrupt_for_session`.

Live-caller census on THIS tree (the deciding fact for "keep both APIs"): fork registry API —
tui_gateway/server.py ×8 (`note_event_delivery_attempt`, `complete_event_delivery_with_retry`,
`acknowledge_event_outbox`, `current_boot_id`, …), tools/delegate_tool.py ×2 (`acknowledge_outbox_event`,
`current_boot_id=` — staged this round); **gateway/run.py no longer references `recover_async_delegations` /
`enqueue_pending_outbox` after lane L02 (see L02-gw-run-FOLLOWUP-gateway_run.py.md "unwired")** — the
restart-recovery boot path is a cross-file re-thread (gateway/run.py ↔ async_delegation.py ↔
async_delegation_store.py ↔ delegate_tool.py `build_recovered_delegation_runner`) and should be landed as ONE
unit. Upstream API — gateway/run_notifications.py ×3, tui_gateway/session_notifications.py ×3,
tui_gateway/methods_subagents.py ×2 (+ openrpc contract), tools/delegate_tool_dispatch.py ×5
(`record_unit_child`, `push_task_failure_notice`, `_new_delegation_id`): all must keep resolving.
Fork-only sibling `tools/async_delegation_store.py` (1579 lines) is auto-merged and already staged; it holds
`is_boot_id_alive`/`enqueue_pending_outbox` twins — dedupe against it rather than re-defining in the facade.
Budget: a full-context worker, this file alone (upstream body 1211 + fork deltas ~1,000 lines to read).
