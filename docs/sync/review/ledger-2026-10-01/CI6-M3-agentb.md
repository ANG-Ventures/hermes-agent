# CI round-6 lane M3-agentb — ledger (card t_a9e63183)

Base: fold head 93556253a7 on `sync/upstream-2026-10-01`. Branch `sync/upstream-2026-10-01-ci-M3-agentb`.
Commits: b092ac07fd, e4898591a7, 1907f390b3, 39fc441327, f478db127b (+ this ledger).
CI evidence: run 37038759229. Classification per the round-5 brief (fork/main baseline = `$R/base`,
upstream side = 612d8e44a2). Proof: `scripts/test-gate` via `e45-pt-M3b.sh`, 3 files per call.

Final re-proof over all 33 manifest files (11 calls): 958 passed, 0 failed.

| File | Verdict | What / why |
|---|---|---|
| tests/agent/test_iteration_limit_summary_session_user.py | FIXED-CODE | turn_stop_gates / turn_context_compaction: re-threaded the fork stop-gate so the budget summary call carries the session user (round 1); stale patch targets repointed. |
| tests/agent/test_kanban_stop.py | FIXED-CODE | turn_stop_gates: fork kanban /stop gate re-threaded onto upstream's split loop (round 1). |
| tests/agent/test_model_metadata.py | FIXED-TEST | patch targets moved by the upstream split; literals updated to the merged metadata table (round 1). |
| tests/agent/test_placeholder_final_text_backstop.py | FIXED-CODE | turn_final_response: fork placeholder-final-text backstop restored on upstream's finalizer seam (round 1). |
| tests/agent/test_pool_exhaustion_scope_label.py | FIXED-TEST | patch target repointed to the module that now reads it (round 1). |
| tests/agent/test_preflight_announce_visibility.py | FIXED-CODE | turn_context_compaction: preflight announce visibility (fork) re-threaded (round 1). |
| tests/agent/test_primary_runtime_restore.py | FIXED-CODE | agent_init tool loader honors both patch contracts (run_agent.* and model_tools.*) (e4898591a7). |
| tests/agent/test_provider_parity.py | FIXED-TEST | green after the round-1/2 src fixes; no edit of its own. |
| tests/agent/test_provider_profile_recovery_seam.py | FIXED-CODE | turn_recovery: terminal-failure handoff + quota reset line restored (round 2); doubles updated for upstream kwargs. |
| tests/agent/test_provider_reset_cooldown.py | FIXED-CODE | turn_recovery cooldown ceil restored (round 2). |
| tests/agent/test_quota_gate_fallback_wiring.py | FIXED-CODE | turn_api_error quota-gate fallback wiring (round 2); patch targets repointed. |
| tests/agent/test_reasoning_effort_module.py | FIXED-CODE | chat_completion_helpers reasoning delta flatten (round 2). |
| tests/agent/test_reasoning_only_stop_persistence.py | FIXED-CODE | green via the round-1 turn_final_response re-thread. |
| tests/agent/test_reasoning_shape_boundaries.py | FIXED-CODE | green via the round-2 reasoning delta flatten. |
| tests/agent/test_recovery_diagnostic_producers.py | FIXED-CODE | green via the round-2 turn_recovery handoff. |
| tests/agent/test_redact.py | FIXED-TEST | stale patch target repointed (round 2). |
| tests/agent/test_resume_interlock.py | FIXED-TEST | patches `model_tools.handle_function_call` — where agent_init reads it after the upstream split (39fc441327). |
| tests/agent/test_retry_after_pool_exhausted.py | FIXED-CODE | turn_recovery.compute_error_backoff: rate-limit Retry-After routed through the fork's `resolve_retry_after` + `pool_seat_exhaustion_state` (2026-09-16 exhausted-seat policy) — dropped by the merge; AST wiring checks repointed conversation_loop -> turn_recovery (39fc441327). |
| tests/agent/test_run_agent_codex_responses.py | FIXED-CODE | green via the round-2 chat_completion_helpers fixes. |
| tests/agent/test_safety_refusal_display.py | FIXED-CODE | turn_api_error / turn_recovery: every classified-driven `_try_activate_fallback` call threads `display_reason` (+ `error_context`) — fork 2026-07-12 safety-refusal/TLS announce label; AST check walks both split modules (39fc441327). |
| tests/agent/test_stream_retry_backoff.py | FIXED-TEST | upstream-only test; `_is_provider_stream_parse_error` double accepts the fork's `http_status=` kwarg (f478db127b). |
| tests/agent/test_stream_unmask_5xx.py | FIXED-TEST | upstream-only test; `_StreamingCall` stub carries `clients.diag` (fork reads http_status off the client pair) (f478db127b). |
| tests/agent/test_streaming.py | FIXED-CODE | `_retry_after_drop` now passes the attempt's http_status to the fork's stream_diag classifier so `buffer_anthropic_tool_input` (#107830) fires on a jiter "expected value" parse error; `_AnthropicEventStream` stub carries a 200 response like the real MessageStream (f478db127b). |
| tests/agent/test_subprocess_env_guard.py | FIXED-CODE | fork guard (passes on fork/main) flagged 4 merged-in raw `**os.environ` spawn sites: curator_shared x2 + trace_upload git probes -> `noninteractive_git_env()` base; kanban_db._pid_create_time `ps` probe -> `build_subprocess_env(scrub_secrets=False, inherit_profile_home=False, extra={LC_ALL: C})` (f478db127b). |
| tests/agent/test_tool_name_db_persistence.py | FIXED-CODE | session_persistence row builder uses the fork's `_persisted_content_projection` (exact producer `text_summary` sidecar) instead of upstream's `_durable_content`; its lazy import was broken (`message_sanitization` -> `tool_dispatch_helpers`), which was also silently failing every tool-row re-persist (f478db127b). |
| tests/agent/test_turn_finalizer_background_review_no_parent_failure.py | FIXED-TEST | `_BudgetAgent` gains `_emit_diagnostic_status` (upstream routes the budget notice there); patches `kanban_db_connect.connect` / `kanban_db_dispatch._record_task_failure` — the split modules finalize_turn reads, same targets as test_turn_finalizer_iteration_limit_exit (f478db127b). |
| tests/agent/test_turn_finalizer_cleanup_guard.py | FIXED-TEST | repinned to fork #1056: on_session_end fires for EVERY turn (persist-disabled review forks included, via emit_session_end) while post_llm_call is suppressed; patch moved to `hermes_cli.lifecycle.invoke_hook` since both paths import it lazily (f478db127b). |
| tests/agent/test_turn_handoff_wiring.py | FIXED-TEST | AST wiring check repointed (round 2). |
| tests/agent/test_turn_usage_accumulator.py | FIXED-TEST | upstream moved `_turn_calls` from a run_conversation local to the per-turn `_LoopState` dataclass (same no-agent-attribute re-entrancy property); assertion reads `_LoopState` (f478db127b). |
| tests/agent/test_usage_pricing_vendor_fallback.py | FIXED-TEST | upstream bdde674296 priced gpt-6-astra's 272K whole-request tier into the snapshot; the 1M-token fixture crossed it. Fixture now 100K tokens, oracle = base rate / 10 (f478db127b). |
| tests/agent/test_usage_unknown_persisted_export.py | FIXED-TEST | AST anchors repointed: cumulative counters -> `agent/turn_usage.py`; CLI cache-ratio block -> `hermes_cli/cli_status_bar_mixin.py` (+ the post-try label stamps, `self._cache_ratio_unknown` bound from cli.py); langfuse summary-dict seam -> the unified `canonical = ... CanonicalUsage(...)` stmt (a764c189e7). `_show_usage` shell double gains `_print_account_limits` (#42904) (f478db127b). |
| tests/agent/test_usage_unknown_r6_round2.py | FIXED-TEST | `_FinalizerAgent` carries `_fallback_activated=False` / `_primary_runtime=None` / `last_served_model=None` as data — upstream's `result_model_fields` read the `__getattr__` lambda as a truthy runtime (f478db127b). |
| tests/agent/test_warning_presentation.py | FIXED-TEST | upstream-only test; `_touch_activity` double accepts `progress=` (fork marks wait notices as liveness, `progress=False`) (f478db127b). |

## FOLLOWUP (outside this manifest)
- FOLLOWUP->M3-agenta: `tests/agent/test_curator_shared_scope.py::TestGateRewiring::test_background_write_guard_permits_in_scope_shared` is red on the fold base 93556253a7 independent of this lane (verified by reverting this lane's curator_shared.py edit: still red — the guard refuses an `external_dirs` skill the test expects permitted). Not touched here.

## Blast radius (src touched outside the manifest's own tests)
- curator_shared / trace_upload / multimodal lifecycle: test_curator_shared_git, test_trace_upload, test_multimodal_turn_lifecycle -> 36 passed, 1 skipped.
- turn_finalizer / session_persistence: test_turn_finalizer_final_response_persistence, test_turn_finalizer_iteration_limit_exit, test_turn_finalizer_cleanup_guard, test_turn_finalizer_background_review_no_parent_failure, test_multimodal_tool_result_spill, test_tool_name_db_persistence -> 34 passed.
- kanban_db._pid_create_time: tests/hermes_cli/test_stale_pid_guard.py (in the 42-passed curator batch).

## Operational note
13:09 PT: a stray `git stash` from this lane hit the SHARED refs/stash of the lane worktrees and `pop` surfaced lane-M2-hermes-clia's WIP here. Reconciled within ~3 min (patch re-applied here, M2's WIP restored verbatim with `stash apply --index`, stray stash dropped); noted on t_e45c8c8d. No commits affected.
