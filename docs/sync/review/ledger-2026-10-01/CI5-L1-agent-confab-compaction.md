# CI5 ledger — lane L1-agent-confab-compaction (parity PR #1624, CI round 5)

Branch `sync/upstream-2026-10-01-ci-L1-agent-confab-compaction` off `117c1c249d60f49b57c476a3d8ec049e2c2d78b3`.
Card t_65a47698. Reds from CI run 36967435601 (20 files / 196 reds); manifest
`tools/lanes-r5/L1-agent-confab-compaction.md`. Verified narrowly per file through
`~/.hermes/scripts/test-gate` with a sandboxed HOME (never the full suite); "green" below is the
file's narrow run on the lane head. Commits: 0813ef705ec, cb0f5645c50, 8a118addcb4, d2acfa7f223,
b7d255259bf, ca1ad6d2095, bab4cea73c2 (+ this ledger).

Legend: FIXED-CODE (fork behaviour restored onto upstream's structure) · FIXED-TEST (test pinned a
moved symbol / stale shape, or a fork fixture adapted to an upstream mechanism the merge adopts) ·
INHERITED (fails on fork/main too) · OPEN.

## Root causes (shared across files)

1. **FIXED-CODE — confab tool-call notice recovery (lead signature, 137 StopIteration).**
   `agent/turn_response_intake.py`, `agent/turn_final_response.py`, `agent/conversation_loop.py`:
   fork #942's announce-once + `TOOL_CALL_NOTICE_TEXT` re-prompt (fork/main `conversation_loop.py`
   L8667/L9626; F04/R05 TODO rows) was never re-threaded onto upstream's extracted `turn_*.py`.
   Without the nudge the notice-stripped empty reply fell into `turn_empty_response` → one extra
   provider call per scripted notice → mock queue exhausted. Ported: `normalize_model_response`
   announces once per turn and carries `_confab_notice`/`_new_confab_notice` on its verdict (new
   `_LoopState` slots); `finish_text_response` re-prompts BEFORE the empty-response ladder, persists
   the metadata-only `system` event row, shares the 3-stall `_dropped_toolcall_retries` budget, ends
   the turn failed/`tool_call_recovery_exhausted`.
2. **FIXED-CODE — `agent/turn_context.py::build_api_messages`** lost the fork's
   `is_metadata_only_tool_notice` skip (notice rows reached the wire as empty system rows).
3. **FIXED-CODE — compaction trigger attribution** (fork 2026-08-20 audit): every
   `_compress_context` call site lost its `trigger_reason=` label in the `turn_*` extraction
   (compactions logged `trigger=UNATTRIBUTED`, announce rendered no clause). Re-threaded on
   `turn_preflight`, `turn_context_compaction`, `turn_overflow` (kwarg on `compress` /
   `compress_scored_by_tokens`), `turn_recovery` (tier_reduction), `gateway/run_turn` +
   `gateway/run` (session_hygiene), and the shared manual core
   `conversation_compression_manual.compress_now` (CLI/TUI/ACP route through it).
4. **FIXED-CODE — reply re-anchor vs. kept head** (`agent/conversation_compression_reply_anchor.py`):
   upstream #118900 located the folded reply's slot by the LAST content-equal follower, binding the
   engine's kept HEAD row when the trailing user turn is a content twin of it (fork #942 guard
   `test_plugin_equal_head_cannot_match_overlapping_tail`), pulling the notice out of order. Applied
   the fork's head-first overlap rule (`_kept_head_len`, same as `_reinsert_tool_notice_events`).
5. **FIXED-CODE — measured 872K gpt-6 Codex windows** (`agent/model_metadata.py`): upstream carried the
   gpt-5.6 900K verdict onto gpt-6-sol/luna (family prefixes) and bumped astra to 900K; the fork
   measured all three at `max_context_window=872,000` and lists them EXACTLY. Fork table + exact-only
   eligibility restored. `hermes_cli/models_validate.py`: policy-aware `-900k` rejection hint restored.
6. **FIXED-CODE — per-class skew calibration** (`turn_context_compaction`, `turn_preflight`,
   `turn_request_assembly`; R05 TODO `turn_preflight L3548`): the fork P2 trigger
   (`note_rough_sent` / `calibrated_tokens` / `should_compress_request` / `trigger_compare_tokens_for`
   with `messages` threaded for content-class classification) was dropped from both extracted gates;
   the whole per-class arm was dead code. Restored (rough vs anchored split; request assembly stashes
   `agent._request_pressure_rough`). Idle-resume blackbox bookkeeping (`idle_compaction_fired` +
   before/after tokens, C5 #42) restored in `_idle_compaction`.
7. **FIXED-CODE — request-body byte budget** (new sibling `agent/turn_body_budget.py`):
   `agent/request_body_budget.py` survived the merge uncalled — the byte preflight after request
   middleware, the terminal re-check after execution middleware, and the `body_too_large` 413 recovery
   (remediate retained images once, else fail actionably, never text compaction) all lived in the fork
   loop. Wired at `turn_api_request.build_api_request` (verdict gains `return`+`result`, honoured by
   `_run_api_retry_loop`), `turn_api_call._perform_api_call`, `turn_api_error.handle_api_error`
   (`ApiErrorVerdict` carries `api_messages`). `turn_iteration_prep`: once-per-turn "image lifecycle
   invariant violated" warning restored.
8. **FIXED-CODE — minor:** `chat_completion_helpers._image_part_chars` expresses the learned
   per-image TOKEN cost under the fork's shared 3.5 chars/token divisor (was ×4 → inflated 4/3.5);
   `_summary_text` reads `tool_calls` via getattr.

Upstream mechanisms the merge ADOPTS (fork fixtures adapted, contracts kept; restoring the fork
side would red a live upstream test on the merged tree): 0f4587e336f preflight deferral to real
usage (`test_context_compressor::test_switch_waits_for_new_provider_evidence`); #118900 reply
re-anchor; 8f0322da5b8 `failed_turn` boundary row; fdbcdef9146 tail count floor bounded by the token
budget (`test_message_floor_does_not_unboundedly_override_soft_ceiling`); 751d8526e35 durable
`message_uid`/`timestamp` on restored rows; `tests/agent/test_run_agent` helpers moved to
`tests/run_agent/_run_agent_helpers`; `hermes_cli.models` → `models_validate` split; the lazy
`model_metadata.requests` shim replaced by `model_metadata_http`; #121486 non-JWT credentials refused
on chatgpt.com; chat-mode iteration summary routed through `_build_api_kwargs`/`_interruptible_api_call`.

## Per file

| red file | disposition | what / why | narrow result |
|---|---|---|---|
| tests/agent/test_413_compression.py | FIXED-CODE + FIXED-TEST | root cause 7 (byte budget, 5 reds) + image-invariant warning; estimator patch seams → `agent.model_metadata` (mid-turn readers import lazily; `turn_context` keeps its binding); rough-growth preflight makes the estimate the deciding signal via `note_usage_less_response()` under the adopted deferral | 41 passed |
| tests/agent/test_codex_context_policy.py | FIXED-CODE + FIXED-TEST | root cause 5 (872K table, 25 reds; picker hint 1); `requests.get` patch → `model_metadata_http.get` (17); `validate_requested_model` from `models_validate` (4); live-catalog test hands a JWT-shaped token (#121486) | 64 passed |
| tests/agent/test_compaction_failed_summary_donepath.py | FIXED-TEST | fdbcdef9146 folds the old 4-pair tail (probe: fork/main 16→12 rows / 87K post; merged 16→6 / 41K — genuinely effective); thrash rebuilt from the REQUIRED anchor pair (last user #10896 + last assistant #29824). Done-site code intact | 1 passed |
| tests/agent/test_compaction_trigger_coverage.py | FIXED-CODE + FIXED-TEST | root cause 3; manual-surface guard pins the label in the shared `compress_now` core and follows cli → `hermes_cli/cli_session_mixin.py`, acp → `acp_adapter/commands.py` | 4 passed |
| tests/agent/test_compress_context_progress_timeout.py | FIXED-TEST | upstream #114594's exact-equal clamp literals replaced by the fork `reconcile_timeouts` invariants (idle lifted strictly above the aux deadline, ceiling admits one fallback) already pinned by `tests/gateway/test_compress_abort_honesty.py` | 20 passed |
| tests/agent/test_compression_concurrent_fork.py | FIXED-TEST | restored user row now also carries upstream `message_uid`/`timestamp`; compare role/content (contract: CONTENT survives) | 65 passed |
| tests/agent/test_confab_matrix_bigargs_lcm.py | FIXED-CODE | root causes 1–2 | 16 passed |
| tests/agent/test_confab_matrix_image_builtin.py | FIXED-CODE + FIXED-TEST | root causes 1–2; `_arm_builtin_compressor` seeds `last_real_prompt_tokens` (0f4587e336f deferral; cold-start mock can never anchor) in the shared e2e fixtures | 16 passed |
| tests/agent/test_confab_matrix_image_lcm.py | FIXED-CODE | root causes 1–2 | 16 passed |
| tests/agent/test_confab_matrix_merge_builtin.py | FIXED-CODE + FIXED-TEST | as image_builtin | 16 passed |
| tests/agent/test_confab_matrix_merge_lcm.py | FIXED-CODE | root causes 1–2 | 16 passed |
| tests/agent/test_confab_matrix_twins_builtin.py | FIXED-CODE + FIXED-TEST | as image_builtin | 16 passed |
| tests/agent/test_confab_notice_compaction_stats.py | FIXED-CODE | root cause 1 | 1 passed |
| tests/agent/test_confab_notice_e2e.py | FIXED-CODE + FIXED-TEST | root causes 1, 2, 4; `_arm_builtin_compressor`; placement tests assert the notice's neighbour (#118900 re-anchors rows after the engine output) and carry real weight in the folded middle (one-word rows made the candidate GROW and the anti-growth guard refused the compaction); `failed_turn` boundary row excluded from the "no model assistant row" assert (8f0322da5b8) | 64 passed |
| tests/agent/test_confab_notice_head_twins_e2e.py | FIXED-CODE | root causes 1, 2, 4 | 16 passed |
| tests/agent/test_confab_notice_parallel_e2e.py | FIXED-CODE | root causes 1, 2, 4 | 24 passed |
| tests/agent/test_context_estimator_multimodal.py | FIXED-CODE + FIXED-TEST | root cause 8 (image chars under the shared divisor); the text contract ("no image pricing applied") pinned against the fork divisor, not upstream's char/4 literal | 3 passed |
| tests/agent/test_idle_compaction_lock_and_guards.py | FIXED-CODE | root cause 6 (idle blackbox bookkeeping) | 5 passed |
| tests/agent/test_iteration_limit_summary_display_fields.py | FIXED-CODE + FIXED-TEST | `_summary_text` getattr(tool_calls); doubles sit on the adopted `_build_api_kwargs` / `_interruptible_api_call` seams, wire contract unchanged | 3 passed |
| tests/agent/test_length_continuation_thinking_exhaustion.py | FIXED-TEST | helper imports → `tests.run_agent._run_agent_helpers` (L2 left this file to L1) | 10 passed |
| tests/agent/test_per_class_skew_calibration.py | FIXED-CODE + FIXED-TEST | root cause 6; the two AST guards follow the production sites into `turn_context_compaction` / `turn_preflight` (module getters only) | 36 passed |

## Neighbours run (not in the manifest; regression guard for touched modules)

`tests/gateway/test_compress_abort_honesty.py` 38 passed + 1 pre-existing (`test_timed_out_locale_key_exists_and_is_not_the_error_variant`, identical without this lane's diff) ·
`tests/agent/test_compression_last_assistant_anchor.py` 6 pre-existing reds on the lane base (fixture `None is not None` at L117), unchanged by root cause 4, not in any round-5 manifest ·
`tests/hermes_cli/test_model_validation.py` 6 failed + 1 error identical with/without root cause 5 ·
`tests/agent/test_model_metadata.py` still patches `model_metadata.requests.get` (4 reds, not in any manifest; same FIXED-TEST shape as codex_context_policy — left for the owner lane) ·
green: test_preflight_compression_gate, test_turn_context_compaction, test_engine_preflight_wire, test_preflight_lock_defer, test_preflight_compression_cap_e2e, test_native_preflight_estimate, test_non_stream_stale_timeout, hermes_cli/test_load_progress, test_413_image_payload_recovery, test_request_body_budget, test_turn_api_error_stream_parse, test_turn_api_call_interrupt, test_image_shrink_recovery, test_retry_exhaustion_partial_retention.

## Overlap notes for the orchestrator

- `agent/model_metadata.py` (872K table) and `hermes_cli/models_validate.py` (hint): no other lane
  touches them on its branch (checked L2/L3a/L3b/L4/L7 diffs).
- `gateway/run.py` / `gateway/run_turn.py`: two one-line `trigger_reason="session_hygiene"` kwargs
  (L3a/L3b gateway lanes may touch neighbouring lines).
- `agent/turn_api_error.py` `ApiErrorVerdict` gained `api_messages`; `turn_api_request.ApiRequestBuild`
  gained `thinking_spinner` + `result` — any lane adding verdict fields in the same dataclasses will
  conflict trivially.
