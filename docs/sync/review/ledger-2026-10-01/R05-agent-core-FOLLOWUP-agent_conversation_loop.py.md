# FOLLOWUP manifest — agent/conversation_loop.py (lane R05-agent-core, parity 2026-10-01)
Fork tip (:2) = b6c8bfe8ab, upstream (:3) = 612d8e44a2, base (:1) = 26350357d7.
Upstream exploded the 8,676-line `run_conversation` monolith into `agent/turn_*.py` phase siblings
(`_LoopState` + `_run_phase`; 27 new files, auto-added by the merge with no conflict). Per brief
('upstream structure wins; re-thread fork behaviour'), conversation_loop.py keeps upstream's facade;
the fork's 101 hunks (+3,105/-? lines, 58 commits) against the monolith were classified by the
base-side context of each hunk against the MERGE_HEAD siblings (`/tmp/e45-R05-agent-core/hunkmap.py`;
`match` = share of >=12-char context lines found verbatim in the named sibling — LOW where upstream
reflowed the surrounding code, so treat the sibling as a best guess and confirm by reading).
Recover fork bodies with `git show b6c8bfe8ab:agent/conversation_loop.py` (line numbers below are
in that file) and diff against `git show 26350357d7:agent/conversation_loop.py` for the delta;
the raw diff is `/tmp/e45-R05-agent-core/agent_conversation_loop.py/fork.diff` (scratch, not durable).

## Applied in conversation_loop.py (this lane)
- 22 fork-added module-level helpers carried verbatim (consumers in turn_usage/turn_finalizer/codex_runtime/
  chat_completion_helpers/agent_runtime_helpers): `_record_served_service_tier`, `_current_turn_tail_tool_index`,
  `_build_moa_pricing_calls`, `_compressor_usage_dict`, `_canonical_usage_from_response`, `_bump_counter`,
  `_account_unaccepted_billed_call`, `_settle_unaccepted_billed_responses`, `_capture_measured_usage_anchor`,
  `_last_turn_snapshot_kwargs`, `_session_cost_status_with_known_spend`, `_SESSION_STATUS_COMPLETENESS`,
  `merge_session_cost_status`, `_moa_session_cost_status`, `_is_auth_resolution_error`, `_resolve_skills_prompt_text`,
  `_PLACEHOLDER_FINAL_TEXTS`, `_BRIDGE_CLOSER_FINAL_TEXTS`, `_BRIDGE_CLOSER_PROVIDER_RE`, `_TURN_ENDED_WITHOUT_REPLY`,
  `classify_placeholder_final_text`, `_return_interrupted` (+ their imports; `capture_usage_anchor` now from agent.usage_anchor).
- `_maybe_inject_run_budget_wrapup(agent, messages, turn_start_idx=None)`: fork current-turn scoping + upstream's
  `_DB_PERSISTED_MARKER` guard. FOLLOWUP turn_iteration_prep.py: pass `current_turn_user_idx` (fork ours:L2938).
- `_stored_prompt_runtime_mismatch` rebuilt on upstream's `identity_line_value`/`runtime_host_value` (+ Session ID);
  fork's Platform arm DROPPED on purpose (upstream #104414: platform is not an identity field). `_restore_or_build_system_prompt`
  logs the stale field; Bot Chat epoch logic = upstream `_bot_chat_prompt_stale` (absorbed).
- `_run_conversation_turn`: `_last_compaction_aborted/_abort_reason` per-turn reset; Blackbox `_LoopState._turn_calls` +
  `agent._served_service_tier/_blackbox_turn_calls/_turn_original_user_message`; `_settle_unaccepted_billed_responses` at
  turn start and end; `fallback_events.clear_pending` (C6 #1211); `mark_live_route_unsupported` on codex_app_server.

## FOLLOWUP: fork hunks whose code upstream moved into a sibling (re-thread there)
Format: `ours:L<line> +add/-rem match <ratio>  <first added line>`. Siblings are outside this lane; nothing below is applied.

### agent/conversation_loop.py (facade — hunks whose context no longer exists there; locate by content, likely turn_api_call/turn_recovery/turn_response_check): 19 hunks, +237/-15
```
FOLLOWUP ours:L3089   +1   -0    match 0%  api_msg.pop(_MULTIMODAL_TEXT_SUMMARY_KEY, None)
FOLLOWUP ours:L3141   +5   -0    match 0%  api_msg.pop("timestamp", None)
FOLLOWUP ours:L3411   +15  -0    match 0%  try:
FOLLOWUP ours:L3433   +4   -0    match 0%  _rough_pressure_tokens = request_pressure_tokens
FOLLOWUP ours:L3763   +50  -0    match 0%  def _body_budget_failure(error_message: str) -> Dict[str, Any]:
FOLLOWUP ours:L3835   +5   -1    match 33%  if agent._try_activate_fallback(reason=FailoverReason.rate_limit):
FOLLOWUP ours:L4468   +4   -0    match 33%  # Floor site (by design, no reason=): an empty/malformed
FOLLOWUP ours:L4546   +3   -0    match 33%  # Floor site (by design, no reason=): invalid-response
FOLLOWUP ours:L4731   +3   -1    match 33%  if agent._try_activate_fallback(
FOLLOWUP ours:L5106   +1   -1    match 0%  if truncated_tool_call_retries < 3:
FOLLOWUP ours:L5299   +59  -2    match 0%  )
FOLLOWUP ours:L5434   +20  -2    match 0%  if response is not None:
FOLLOWUP ours:L6985   +1   -0    match 0%  FailoverReason.stream_parse,
FOLLOWUP ours:L7197   +17  -2    match 33%  from agent.quota_registry_gate import (
FOLLOWUP ours:L7817   +10  -0    match 0%  and not _is_auth_resolution_error(api_error)
FOLLOWUP ours:L8488   +8   -0    match 25%  elif _backoff_policy == "pool_capacity":
FOLLOWUP ours:L9591   +27  -5    match 0%  _placeholder_route = classify_placeholder_final_text(
FOLLOWUP ours:L9949   +3   -0    match 40%  # Floor site (by design, no reason=): repeated empty
FOLLOWUP ours:L10292  +1   -1    match 0%  # Workers must close their run (complete, block, or review handoff).
```

### agent/turn_usage.py: 11 hunks, +239/-34
```
FOLLOWUP ours:L5222   +1   -0    match 60%  _moa_ref_pricing_calls: list[dict[str, Any]] = []
FOLLOWUP ours:L5231   +12  -0    match 50%  if _moa_client is not None and hasattr(
FOLLOWUP ours:L5265   +11  -0    match 64%  output_unknown = bool(canonical_usage.output_tokens_unknown)
FOLLOWUP ours:L5371   +8   -4    match 25%  _new_anchor = _capture_measured_usage_anchor(
FOLLOWUP ours:L5464   +114 -8    match 79%  _record_served_service_tier(agent, response)
FOLLOWUP ours:L5609   +45  -6    match 43%  _agg_cost_model = _call_route["model"]
FOLLOWUP ours:L5663   +6   -0    match 20%  _cost_status = _session_cost_status_with_known_spend(
FOLLOWUP ours:L5678   +5   -1    match 60%  agent.session_cost_status = merge_session_cost_status(
FOLLOWUP ours:L5727   +25  -4    match 85%  cost_status=_cost_status,
FOLLOWUP ours:L5767   +7   -1    match 50%  from agent.usage_pricing import verbose_token_usage_log_args
FOLLOWUP ours:L5786   +5   -10   match 64%  cache_line = cache_stats_line(
```

### agent/turn_api_call.py: 9 hunks, +111/-12
```
FOLLOWUP ours:L35     +8   -1    match 9%  emit_image_eviction_attempt_telemetry,
FOLLOWUP ours:L4162   +48  -0    match 80%  try:
FOLLOWUP ours:L4217   +23  -0    match 80%  _call_route.update(_live_route(agent))
FOLLOWUP ours:L4269   +5   -0    match 75%  _settle_unaccepted_billed_responses(agent, _turn_calls, turn_id)
FOLLOWUP ours:L4283   +8   -0    match 50%  from agent.chat_completion_helpers import _live_route
FOLLOWUP ours:L4309   +1   -0    match 25%  agent._inflight_request_route = None
FOLLOWUP ours:L4957   +6   -1    match 17%  if agent._try_activate_fallback(
FOLLOWUP ours:L6813   +9   -10   match 33%  return _return_interrupted(
FOLLOWUP ours:L8371   +3   -0    match 33%  "turn_handoff_saved": bool(_handoff_notice),
```

### agent/turn_recovery.py: 7 hunks, +362/-17
```
FOLLOWUP ours:L55     +6   -0    match 38%  from agent.tool_dispatch_helpers import (
FOLLOWUP ours:L6215   +127 -0    match 25%  if classified.reason == FailoverReason.body_too_large:
FOLLOWUP ours:L6999   +152 -0    match 100%  _is_pool_capacity = (
FOLLOWUP ours:L7247   +3   -1    match 67%  if agent._try_activate_fallback(
FOLLOWUP ours:L7406   +1   -0    match 40%  trigger_reason="overflow_413",
FOLLOWUP ours:L7726   +1   -0    match 40%  trigger_reason="overflow_context",
FOLLOWUP ours:L8391   +72  -16   match 26%  _resp_headers = getattr(getattr(api_error, "response", None), "headers", None)
```

### agent/turn_final_response.py: 5 hunks, +83/-49
```
FOLLOWUP ours:L2673   +28  -0    match 20%  _turn_calls: List[Dict[str, Any]] = []
FOLLOWUP ours:L9626   +41  -0    match 40%  _tool_notice_nudge = TOOL_CALL_NOTICE_TEXT.get(
FOLLOWUP ours:L10147  +0   -48   match 44%  
FOLLOWUP ours:L10314  +13  -1    match 43%  try:
FOLLOWUP ours:L10558  +1   -0    match 60%  _turn_calls=_turn_calls,
```

### agent/turn_iteration_prep.py: 5 hunks, +58/-26
```
FOLLOWUP ours:L2776   +1   -0    match 50%  _prior_image_invariant_warned = False
FOLLOWUP ours:L2788   +10  -0    match 67%  from hermes_cli.kanban_worker_route import apply_pending_live_route
FOLLOWUP ours:L2880   +32  -25   match 21%  _si = _current_turn_tail_tool_index(messages, current_turn_user_idx)
FOLLOWUP ours:L2938   +1   -1    match 33%  _maybe_inject_run_budget_wrapup(agent, messages, current_turn_user_idx)
FOLLOWUP ours:L2971   +14  -0    match 25%  if not _prior_image_invariant_warned:
```

### agent/turn_preflight.py: 4 hunks, +9/-1
```
FOLLOWUP ours:L3548   +6   -1    match 17%  and _should_compress_request(
FOLLOWUP ours:L3608   +1   -0    match 40%  trigger_reason="pre_api_pressure",
FOLLOWUP ours:L6939   +1   -0    match 60%  trigger_reason="tier_reduction",
FOLLOWUP ours:L7571   +1   -0    match 40%  trigger_reason="overflow_context",
```

### agent/turn_response_check.py: 4 hunks, +78/-3
```
FOLLOWUP ours:L7892   +22  -2    match 23%  _may_fallback = classified.should_fallback
FOLLOWUP ours:L8144   +11  -1    match 43%  if agent._try_activate_fallback(
FOLLOWUP ours:L8335   +29  -0    match 50%  from agent.quota_registry_gate import (
FOLLOWUP ours:L8649   +16  -0    match 20%  _confab_notice = getattr(normalized, "confab_notice", None)
```

### agent/turn_api_request.py: 3 hunks, +68/-6
```
FOLLOWUP ours:L3957   +57  -0    match 50%  from agent.request_body_budget import (
FOLLOWUP ours:L5200   +10  -6    match 22%  if response is not None:
FOLLOWUP ours:L9484   +1   -0    match 25%  trigger_reason="threshold",
```

### agent/turn_context.py: 2 hunks, +13/-1
```
FOLLOWUP ours:L3029   +12  -0    match 25%  _omit_interrupt_close = provider_owns_transcript(agent.provider)
FOLLOWUP ours:L3117   +1   -1    match 20%  and msg.get("role") in _API_CONTENT_SIDECAR_ROLES
```

### agent/turn_context_compaction.py: 2 hunks, +23/-3
```
FOLLOWUP ours:L3522   +15  -0    match 50%  
FOLLOWUP ours:L3567   +8   -3    match 36%  "Pre-API compression: ~%s compared tokens >= %s threshold "
```

### agent/turn_finalizer.py: 2 hunks, +10/-2
```
FOLLOWUP ours:L4595   +5   -1    match 40%  close_interrupted_tool_sequence(
FOLLOWUP ours:L8521   +5   -1    match 40%  close_interrupted_tool_sequence(
```

### agent/turn_api_error.py: 2 hunks, +29/-2
```
FOLLOWUP ours:L5114   +2   -2    match 20%  f"retrying ({truncated_tool_call_retries}/3)..."
FOLLOWUP ours:L6095   +27  -0    match 40%  from agent import fallback_events as _fbe
```

### agent/turn_facade_lease.py: 2 hunks, +12/-1
```
FOLLOWUP ours:L7946   +11  -0    match 20%  elif classified.reason == FailoverReason.malformed_conversation:
FOLLOWUP ours:L10341  +1   -1    match 20%  "a terminal run handoff — nudging to finish"
```

### agent/turn_tool_round.py: 1 hunks, +10/-0
```
FOLLOWUP ours:L9317   +10  -0    match 60%  try:
```

### agent/turn_empty_response.py: 1 hunks, +10/-1
```
FOLLOWUP ours:L9987   +10  -1    match 50%  _cost_prefix = (
```

### agent/turn_stop_gates.py: 1 hunks, +1/-0
```
FOLLOWUP ours:L10303  +1   -0    match 40%  tools=getattr(agent, "tools", None),
```

## Known fork features inside the moved hunks (name them when re-threading)
- Blackbox per-call usage commit (turn_usage: `_turn_calls.append`, `_canonical_usage_from_response`, `merge_session_cost_status`, aux cost, cache tier #978; token-stats honesty USAGE_UNKNOWN latches).
- Relay/pool fallback: `FailoverReason.pool_exhausted/pool_stalled/relay_draining/stream_parse/body_too_large/malformed_conversation/endpoint_not_found` branches (turn_recovery, turn_response_check, turn_api_error; `relay_drain_wait`, `capacity_retry_wait`, `pool_capacity` backoff policy, quota_registry_gate, `_fbe` fallback_events ledger C6).
- Kanban worker: `apply_pending_live_route` (turn_iteration_prep), stop-guard wording 'close their run (complete, block, or review handoff)', `_settle_unaccepted_billed_responses`.
- Placeholder/bridge-closer final text classification (`classify_placeholder_final_text`, turn_final_response ours:L9591).
- confab notice (`confab_notice_status`, TOOL_CALL_NOTICE_TEXT nudge, turn_final_response/turn_response_check).
- request_body_budget 413 remediation (`_body_budget_failure`, turn_api_request ours:L3957 / turn_recovery ours:L6215).
- `_return_interrupted` + `close_interrupted_tool_sequence` interrupt-close rows (turn_api_call ours:L6813, turn_finalizer ours:L4595/L8521).
- Pre-API compression skew gate (`_should_compress_request`, `_trigger_compare_tokens_for`, turn_preflight/turn_context_compaction).
