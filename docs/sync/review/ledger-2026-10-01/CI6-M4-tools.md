# CI round-6 lane M4-tools — ledger (card t_c0f1bf24)

Base: fold head 93556253a7 (sync/upstream-2026-10-01). Branch: `sync/upstream-2026-10-01-ci-M4-tools`.
CI evidence: run 37038759229 (#1624). Manifest: tools/lanes-r5/M4-tools.md (32 files). Every file re-proved
narrowly through `scripts/test-gate` (sandboxed HOME, ≤3 files per call) on the lane head. Commits carry the
per-cluster detail; this ledger is the per-file verdict. Fork side = `fork/main`, upstream side = 612d8e44a2.

Verdict vocabulary: FIXED-CODE (merge regression, fork behaviour restored onto upstream's structure) /
FIXED-TEST (test pinned a contract the merge legitimately replaced, or a double that upstream's new seam
rejects) / PINNED-UPSTREAM-CONTRACT (upstream-only test for a contract the fork rejected on purpose; xfail
strict=False with the evidence) / FOLLOWUP→lane.

| file | verdict | detail | narrow result |
|---|---|---|---|
| tests/tools/test_approval_mode_off_bypass.py | FIXED-CODE | tools/approval.py: every bypass site goes through `is_approval_bypass_active()` again (merge re-introduced a hand-rolled `_yolo_active()` that dropped `approvals.mode=off`, fork 2026-09-08 incident); `_get_approval_mode` facade delegate. | green (174 with approval siblings) |
| tests/tools/test_async_delegation.py | FIXED-CODE + PINNED-UPSTREAM-CONTRACT (3) | 34 reds cleared by the retirement/prune guards + registry re-threads (commits 1–4). 3 upstream-only tests pin per-task completion units / `group` / crash-mid-unit child records (028fe2c4c8, c5594ec4b3, #116000) that live in upstream's `_Batch/_run_batch` (delegate_tool_dispatch) — imported, importable, NOT on the fork hot path per R07-delegate POLICY-DIVERGENCE (fork `delegate_task` runs a split call as ONE unit; same ruling the L6 lane applied to test_delegation_recovery_diagnostics). `xfail(strict=False)` with that reason: flips to XPASS the day the ruling reverses. | 34 passed, 3 xfailed |
| tests/tools/test_async_delegation_numeric_binding_boundary.py | FIXED-CODE | `_dispatch_admitted` tolerates an executor double returning no future (fork doubles) instead of rejecting as submission_failed. | green (204 with siblings) |
| tests/tools/test_async_delegation_registry_boundaries.py | FIXED-CODE | `_prune_completed_locked` no longer hashes a runner-supplied status (fork hostile-`__hash__` guard). | green |
| tests/tools/test_async_delegation_terminal_receipts.py | FIXED-CODE (+1 test line) | `tools/process_registry` re-exports `_format_async_delegation` (fork facade symbol upstream moved); dispatch helper assertion carries the payload. | green |
| tests/tools/test_cronjob_tools.py | FIXED-CODE + FIXED-TEST | `tools/cronjob_tools` re-exports `_CRON_THREAT_PATTERNS` (moved to cronjob_prompt_scan); registry.dispatch takes the canonical `cronjob_manage` (alias mapping is executor-side). | green (165 with siblings) |
| tests/tools/test_delegate.py | FIXED-CODE (7) + FIXED-TEST (8) | CODE: `delegate_tool_config._resolve_child_credential_pool` accepts `custom:<name>` as custom on BOTH sides (fork lane-label spelling; R07 FOLLOWUP) — the labeled lane fell into the generic branch and `credential_pool_matches_provider` refused it; `_direct_endpoint_credentials` stamps `provider="custom:<name>"` for a REGISTERED endpoint (fork lane attribution for the blackbox ledger / usage_pricing; labeling only); `delegate_tool._classify_child_outcome` treats a non-empty `error` with no `failed` key as failed/error and the entry carries `failure_reason` (upstream child_run contract adopted onto the fork hot path); `_run_single_child` binds the leased entry BY ID + endpoint check with re-lease (upstream #68237, from `_lease_child_credential`) instead of the shared `current()` cursor, fork SwapOutcome gate kept, legacy pools without list `entries()` fall back to `current()`. TEST: TestChildSystemPrompt ×2 + orchestrator-below-floor — goal is the child's first USER turn (upstream 0aa178736a) and the nested-children note is upstream's wording already pinned by test_delegate_depth_prompt.py (BASE); `test_blocked_tools_constant` cronjob→`cronjob_manage` (upstream rename e16ad33a9d; executor canonicalizes the alias); `test_build_child_agent_inherits_active_client_endpoint` live key travels with live URL (upstream #90009 superseded the 2608f78b93 contract; fork never changed it); `..._assigns_parent_pool_when_shared` double gets `provider` (upstream #68237 endpoint-scoped pools; a bare MagicMock never matches); `..._child_dedicated_db_follows_parents_db_path` compares resolved paths (upstream #81267 registry keys by resolved path; macOS /private/tmp only, not a CI red); `..._forwards_runtime_request_overrides_and_output_cap` asserts the cap is ABSENT (upstream fd3565deec removed the per-child output cap; R07 converged on upstream, the two upstream sibling tests in the same class assert absence). | 175 passed |
| tests/tools/test_delegate_batch_tag.py | FIXED-CODE | batch tag header + `[set N · i/n]` completion lines (upstream 0cb996d977) re-threaded onto the fork `delegate_task`. | green (38 with control_actions) |
| tests/tools/test_delegate_child_timeout_floor.py | FIXED-CODE | `_parse_timeout` floors at `_CHILD_TIMEOUT_FLOOR_S` (60 s, fork 7ff750b798; R07 FOLLOWUP). | 8 passed |
| tests/tools/test_delegate_control_actions.py | FIXED-CODE | see batch_tag (shared cause). | green |
| tests/tools/test_delegate_firepower_guard.py | FIXED-CODE | agent/tool_executor: cron creator-model binding canonicalizes the tool name so the legacy `cronjob` entry still binds the CREATOR (fork #922). | 15 passed |
| tests/tools/test_delegate_output_schema.py | FIXED-CODE | `schema_note` on an unvalidated-but-kept final answer (upstream child_run parity). | green (40 with restart_recovery) |
| tests/tools/test_delegate_restart_recovery.py | FIXED-CODE | `task_transcripts` passed to the async registry (upstream #116000: recovered events carry transcript locators/tails). | green |
| tests/tools/test_delegate_stale_wait_release.py | FIXED-TEST | upstream-only: on the fork hot path the hang ceiling `hung_child_seconds` (#1599, `timeout_phase=no_progress`) ends a frozen child's wait; upstream's stale-heartbeat verdict is off-path. | 2 passed |
| tests/tools/test_delegation_recovery_diagnostics.py | FIXED-TEST | kwargs-only child double → positional (fork `_run_single_child`); a split call is ONE unit regardless of independent_completions (R07). | 3 passed |
| tests/tools/test_execute_code_session_provenance.py | FIXED-CODE | `code_execution_env._scrub_child_env` re-threads the fork `_inject_session_id` tail (#636/C3); facade re-exports `_scrub_child_env` / `_HERMES_CHILD_ALLOWED`. | 18 passed |
| tests/tools/test_execute_code_surfaces_blocks.py | FIXED-CODE + FIXED-TEST | every gateway-lifecycle refusal carries `blocked_by`=GATEWAY_LIFECYCLE_BLOCK_MARKER again (`_blocked_json` gains `blocked_by`); source scan repointed to the extracted gateway_lifecycle_block. RED-proofed (unstamping one refusal fails it). | 11 passed |
| tests/tools/test_file_operations.py | FIXED-TEST | upstream-side double answers the fork's write_file text-verify `cat` so the patch_replace byte-exact verify is the one under test (fork keeps the no-sha256sum fallback; fork-only tests pin it). | 54 passed, 6 skipped |
| tests/tools/test_file_state_registry.py | FIXED-TEST | fork-only: sibling reads before its whole-file write (upstream #65604 read-before-write blocker), same adaptation the handler test in the file already carried. | green |
| tests/tools/test_hardline_blocklist.py | FIXED-CODE | tools/approval_detection: fork `HERMES_ALLOW_REBOOT` opt-in restored (reboot/shutdown family downgrades hardline→DANGEROUS; every other hardline pattern untouched). Dropped when upstream extracted detection. | 263 passed |
| tests/tools/test_kanban_authority_ambient_reads.py | FIXED-TEST | fork-only, property kept: `_require_orchestrator_tool` raises `_Reject` (handlers return it); sandbox passthrough seal asserts the property. | green (36 with siblings) |
| tests/tools/test_kanban_comment_provenance_tools.py | FIXED-TEST | forged attribution is refused by the strict-parameter gate instead of silently dropped; nothing lands. | green |
| tests/tools/test_kanban_descendant_scope.py | FIXED-TEST | upstream-only: completions carry a structured receipt (fork receipt gate #1621; prose alone is refused). | green (17 with siblings) |
| tests/tools/test_kanban_provenance.py | FIXED-TEST | upstream-only: a dispatched worker cannot mint on the placeholder `default` lane (kanban_worker_policy); worker-created card homed on the owning task (`_resolve_birth_session`, C6 #1118). | green |
| tests/tools/test_kanban_redaction.py | FIXED-TEST | structured receipt (as descendant_scope). | green |
| tests/tools/test_kanban_session_attribution.py | FIXED-CODE + FIXED-TEST | `tools/send_message_tool` restores `_is_dispatcher_owned_worker_process` / `_check_send_message` verbatim from fork/main (#636 worker notify-channel seal, dropped by the merge); spawn source scan repointed to kanban_db_dispatch (split). | green |
| tests/tools/test_poll_drain_bounded.py | FIXED-CODE | `tools/process_registry` reader with no Popen handle finishes handle-lost (fork #788 guard). | green |
| tests/tools/test_process_reader_exit.py | FIXED-CODE | same reader guard. | green |
| tests/tools/test_process_registry_list_exit.py | FIXED-CODE | `_release_finished_handles` (upstream-new) tolerates Popen-shaped handles without stderr/stdin. | green |
| tests/tools/test_read_secrets_cat_word_boundary.py | FIXED-CODE | hardline/approval cluster (see approval_mode_off_bypass). | green (165 with cronjob_tools) |
| tests/tools/test_refresh_agent_mcp_tools.py | FIXED-CODE | tools/mcp_tool_agent between-turns refresh reads tool names off the NORMALIZED schema (fork #47707) — the wrapper read dropped every lcm_* tool on the first MCP refresh (2026-07-18 LCM amputation). | 21 passed |
| tests/tools/test_web_extract_timeout.py | FIXED-TEST | upstream-only: rescue hook patched on the tools.web_tools facade (the fork's patch seam, L7). | green |

## Blast radius (narrow, lane head)

test_delegate_child_lifecycle + toolset_scope + firepower_guard 68 ✓ · images + depth_prompt + recovery_diagnostics 20 ✓ ·
failed_return_reaps + failure_copy + request_overrides 25 ✓ · external_provider + fallback_matrix + interrupted_partial_output 23 ✓
(round-6 src seams: delegate_tool / delegate_tool_config); earlier clusters' batteries are in the commit messages.

## FOLLOWUP (R07-delegate, not reds in this manifest — not touched)

- FOLLOWUP→R07/owner: `delegate_tool_config._resolve_delegation_credentials` does not resolve `delegation.model` aliases via
  `resolve_model_pair_for_storage` (fork fe6da50511; only the per-call `model=` path at delegate_tool.py:3746 does).
- FOLLOWUP→R07/owner: `delegate_tool_toolsets._strip_blocked_tools` is still upstream's (fork ff30e9cfb8 `strip_blocked_delegate_toolsets`
  + the `code_execution` exemption); tests/tools/test_delegate_toolset_scope.py is green on the lane head, so no red forced it.
- FOLLOWUP→R07/owner: `_resolve_child_credential_pool` still resolves `custom:` pools by `get_custom_provider_pool_key(base_url, provider_name=requested_provider)` (upstream #45763) — the fork passed base_url only; kept upstream's richer resolver.
