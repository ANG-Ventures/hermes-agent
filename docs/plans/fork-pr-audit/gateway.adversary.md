# Gateway tranche - ADVERSARY (t_6b366805)

Input: audit/gateway/verdicts @ 9d452cf95e (auditor t_35a3b292). Fork origin/main tree on that branch; upstream/main ec243785e4.
Method per row: git grep of every def/class/CONST/config key the PR added for consumers OUTSIDE the PR paths; live fleet config (15 config.yaml, read-only);
docs/sync/fork-features.json lifecycle; gateway.log* (window 2026-07-12..2026-09-25 by first/last line); every cited upstream sha checked with
`git merge-base --is-ancestor <sha> upstream/main` (all 16 OK); upstream code read at the cited symbol.

## Totals

- challenged: 44 (DROP 7, SUPERSEDED-BY-UPSTREAM 31, UNRESOLVED 6)
- STANDS 20 / OVERTURNED 7 / NEEDS-LEAD 17
- KEEP-UNPROVEN: 98 of 119 KEEP rows have no positive fire count (list at the end)

## Challenged rows

| key | auditor verdict | adversary result | caller / config / incident found | notes |
|---|---|---|---|---|
| #157 | SUPERSEDED-BY-UPSTREAM | OVERTURNED -> KEEP | fleet logs 2026-07-12..2026-09-25: PHASE=restart_backfill on 191 boots, 101 re-injected messages (425 total, reinject_failed=0), 98 of them scanning >2 channels; upstream _missed_message_backfill_channels (up plugins/platforms/discord/adapter.py:2186) defaults to allowed\|free_response = 2 channels on the main profile (~/.hermes/config.yaml:541-542), so it cannot see the thread/DM channels the fork's recent_channels state covers; 0 'Backfilling missed Discord message' lines from upstream's path in the same window. Candidate for UPSTREAM later (channel-set gap), not a drop. | fix(discord): recover messages dropped during a graceful restart drain |
| #301 | SUPERSEDED-BY-UPSTREAM | OVERTURNED -> UPSTREAM | upstream _drain_startup_restore_queue (up gateway/run_startup.py:~100) logs 'Dropping startup-restore queued message: adapter unavailable' and continues = message LOST; fork replay owner deferred instead 11x with reason=adapter on 09-19..09-23 (gateway.log*). Upstream still drops -> generic fix, port it. | fix(gateway): fail open startup restore intake (#301) |
| #763 | SUPERSEDED-BY-UPSTREAM | OVERTURNED -> UPSTREAM | auditor's up_fix cc8e5ec2af is the BASE the fork PR built on (git log -S _record_command_sync_rate_limit gives the same sha on fork). Upstream has 0 hits for _command_sync_retry_task / drift check: after a 429 it records and returns, retrying only on next reconnect. Fleet logs: 26 'rate-limited slash command sync' 09-13..09-20 07:04, 0 after the fork self-heal landed. Measured, generic, absent upstream. | fix(discord): never vacate a slash command mid-sync, and self-heal aft |
| #777 | SUPERSEDED-BY-UPSTREAM | OVERTURNED -> UPSTREAM | rides #763 (review fixes on the same retry path). | fix(discord): port command-sync retry review fixes (#777) |
| #934 | DROP | OVERTURNED -> UPSTREAM (fold into #936) | not dead code: _executor_max_workers() def (gateway/run.py:222) and its call site (run.py:32186, blame 88b0478ae3 = #934) are LIVE and are what #936 sizes through. Nothing to revert; the code travels with the #936 upstream port (audit/gateway/upstream-936). | fix(gateway): size the turn-body executor to turn admission — admitted |
| nopr:0fee1b8b64 | SUPERSEDED-BY-UPSTREAM | OVERTURNED -> KEEP | the per-token guard lives inside restore_session_vars, which tools/delegate_tool.py:4083 still calls (see nopr:1832ed4e78). | fix(gateway): per-token exception guard in restore_session_vars (revie |
| nopr:1832ed4e78 | SUPERSEDED-BY-UPSTREAM | OVERTURNED -> KEEP | live consumer outside the PR: tools/delegate_tool.py:4056/4083 imports restore_session_vars (added by #285 52b51f7d3e, background delegate restart survival). Upstream has 0 hits for restore_session_vars anywhere. Revert = runtime ImportError in delegate_task restart path. | fix(gateway): bind session context at agent turn entry |
| #129 | DROP | NEEDS-LEAD (lead: feature is live) | auditor 'fires 0 / no consumer' is false on the write side: discord.reaction_journal is set at ~/.hermes/config.yaml:567 and ~/.hermes/greenhouse/reactions.jsonl is being written right now (seq 40092 at 2026-09-25T18:40:40Z, 2366 lines since 09-24). No reader found (rg over greenhouse/, scripts/, skills-shared, cron: 0 hits; greenhouse collector reads reactions via the Discord API). So: an operator-enabled writer with no reader. Revert branch audit/gateway/revert-129 must also remove the config key. | feat(discord): opt-in raw-reaction journal for durable triage state (# |
| #221 | SUPERSEDED-BY-UPSTREAM | NEEDS-LEAD (SUPERSEDED for /branch; Ace ruling for /merge) | /branch half: upstream has _branched_from + sibling thread (dbcbd9d9db, verified ancestor) -> take upstream. /merge half: fork-features.json entry 16 lifecycle=fork-permanent (and entry 15 for the /branch Discord thread) -> needs Ace. /merge usage 0 (success path unlogged). | feat: /branch spawns a Discord thread; /merge folds a session summary  |
| #229 | DROP | NEEDS-LEAD (lead: flag IS enabled) | auditor premise 'dormant flag never enabled' is false: desktop_auto_resume: true at ~/.hermes/config.yaml:371 and profiles/aegis/config.yaml:323. The PR's only literals are logger.debug (not in INFO logs), so '0 fires' is unmeasurable rather than zero. Live consumers tui_gateway/methods_session.py:638/912/1132. Also carries the DESKTOP_REASON_RESTART_CONSUMED half of the F1 contract (see UNRESOLVED unit). D9 desktop is upstream-owned, so a DROP is plausible but it disables an operator-set knob: Ace/lead call. | feat(tui): desktop/TUI session auto-resume after backend restart (dorm |
| #231 | SUPERSEDED-BY-UPSTREAM | NEEDS-LEAD (follows #221 /merge) | fork-features.json entry 16 /merge fork-permanent. | feat(merge): summarize only post-branch delta + post note to target's  |
| #315 | SUPERSEDED-BY-UPSTREAM | NEEDS-LEAD (KEEP (route-identity layer) / ruling on sticky reset) | registry docs/sync/fork-features.json entry 17 '/fast' lifecycle=fork-permanent (Ace ruling) and entry 10 route identity upstream-intended. Symbols #315 added are consumed outside its files and are 0-hit upstream: lookup_persisted_route_identity (gateway/fork_ext/route_identity.py:39), sanitize_model_override_identity (gateway/chat_model_pins.py:29 = registry entry 0), PersistedSessionRouteLookup (gateway/run.py:3349). A revert breaks chat-model pins. At most the sticky-reset knob (preserve_route_preferences_on_manual_reset, hermes_cli/config_defaults.py:4306, not set in any fleet config) is droppable. | fix(gateway): make fast route-aware and preserve manual reset preferen |
| #339 | DROP | NEEDS-LEAD (follows #49) | same fork-permanent ruling as #49 (entry 14; test_undo_drain_guard.py is a registry test). | fix(undo): refuse /undo·/redo while a /stop'd turn is still draining ( |
| #353 | DROP | NEEDS-LEAD (follows #49) | same fork-permanent ruling as #49 (entry 14; test_undo_error_honesty.py is a registry test). | fix(undo): stop /undo·/redo reporting a false 'Nothing to undo.' on a  |
| #356 | DROP | NEEDS-LEAD (lead: test before revert) | part of the #49 unit, but the defect is not undo-only: upstream synthesizes an empty-text internal resume event (up gateway/run_startup.py:611) and has 0 hits for _empty_resume_synthetic/any empty-row guard in agent/, run_agent.py, gateway/. An empty persisted user row also breaks role alternation. Run tests/run_agent/test_empty_resume_user_row.py on upstream/main before dropping. | fix(resume): don't persist an empty user row on auto-resume (the /undo |
| #358 | SUPERSEDED-BY-UPSTREAM | NEEDS-LEAD (lead: upstream claim falsified) | auditor claimed upstream rejects empty prompt.submit via contracts; up tui_gateway/methods_prompt.py:564-700 has no empty/whitespace text check (only side-agent RPCs check 'text required' :1190). Fork rejects with 4020. Defect still present upstream, but unmeasured (no literal). UPSTREAM (5-line guard) or DROP as unmeasured. | fix(gateway): reject empty prompt.submit + skip no-op model-switch sid |
| #49 | DROP | NEEDS-LEAD (Ace ruling (fork-permanent)) | fork-features.json entry 14 '/undo and /redo' lifecycle=fork-permanent -> DROP needs Ace. Usage: /undo 2 native-slash invocations in 4.5 months. Revert unit must also remove tui_gateway/server.py:12357 _undo_session_core + :12385 _redo_session_core and their callers tui_gateway/methods_session.py:2909, methods_tools.py:1051, plus hermes_state.py _raise_if_rewind_would_orphan_tool - not in the auditor's file list. | feat: reversible half-turn /undo + /redo across CLI, gateway, TUI (#49 |
| #70 | UNRESOLVED | NEEDS-LEAD (lead ruling (split F1 keep / F2 drop?)) | F1/F2 unit: fleet knobs that exist only for this breaker are set in live config - restart_initiated_ttl_secs 600, restart_loop_threshold 3, restart_loop_window_secs 300 (~/.hermes/config.yaml:75-77); bridged by gateway/fork_ext/restart_policy.py:120-123 (registry entry 9, upstream-intended, golden tests). #229's DESKTOP_REASON_RESTART_CONSUMED mirrors F1. Zero breaker trips measured, so value is unproven, but a DROP also orphans 3 config keys and a registry entry -> lead/Ace, not an auditor call. | fix(gateway): break the restart-cascade replay loop (F1 drain-mark + F |
| #72 | UNRESOLVED | NEEDS-LEAD (lead ruling (split F1 keep / F2 drop?)) | F1/F2 unit: fleet knobs that exist only for this breaker are set in live config - restart_initiated_ttl_secs 600, restart_loop_threshold 3, restart_loop_window_secs 300 (~/.hermes/config.yaml:75-77); bridged by gateway/fork_ext/restart_policy.py:120-123 (registry entry 9, upstream-intended, golden tests). #229's DESKTOP_REASON_RESTART_CONSUMED mirrors F1. Zero breaker trips measured, so value is unproven, but a DROP also orphans 3 config keys and a registry entry -> lead/Ace, not an auditor call. | fix(gateway): close the F2 self-completing-restart-loop gap (A' record |
| #7536 | UNRESOLVED | NEEDS-LEAD (lead ruling (split F1 keep / F2 drop?)) | F1/F2 unit: fleet knobs that exist only for this breaker are set in live config - restart_initiated_ttl_secs 600, restart_loop_threshold 3, restart_loop_window_secs 300 (~/.hermes/config.yaml:75-77); bridged by gateway/fork_ext/restart_policy.py:120-123 (registry entry 9, upstream-intended, golden tests). #229's DESKTOP_REASON_RESTART_CONSUMED mirrors F1. Zero breaker trips measured, so value is unproven, but a DROP also orphans 3 config keys and a registry entry -> lead/Ace, not an auditor call. | fix(gateway): stuck-loop counter must gate on genuine interruption, no |
| #80 | UNRESOLVED | NEEDS-LEAD (lead ruling (split F1 keep / F2 drop?)) | F1/F2 unit: fleet knobs that exist only for this breaker are set in live config - restart_initiated_ttl_secs 600, restart_loop_threshold 3, restart_loop_window_secs 300 (~/.hermes/config.yaml:75-77); bridged by gateway/fork_ext/restart_policy.py:120-123 (registry entry 9, upstream-intended, golden tests). #229's DESKTOP_REASON_RESTART_CONSUMED mirrors F1. Zero breaker trips measured, so value is unproven, but a DROP also orphans 3 config keys and a registry entry -> lead/Ace, not an auditor call. | fix(gateway): authoritative restart-initiator breadcrumb for F2 breake |
| #86 | UNRESOLVED | NEEDS-LEAD (lead ruling (split F1 keep / F2 drop?)) | F1/F2 unit: fleet knobs that exist only for this breaker are set in live config - restart_initiated_ttl_secs 600, restart_loop_threshold 3, restart_loop_window_secs 300 (~/.hermes/config.yaml:75-77); bridged by gateway/fork_ext/restart_policy.py:120-123 (registry entry 9, upstream-intended, golden tests). #229's DESKTOP_REASON_RESTART_CONSUMED mirrors F1. Zero breaker trips measured, so value is unproven, but a DROP also orphans 3 config keys and a registry entry -> lead/Ace, not an auditor call. | feat(gateway): F2 breadcrumb backlog cleanup (config family, contract  |
| #97 | SUPERSEDED-BY-UPSTREAM | NEEDS-LEAD (SUPERSEDED for the run.py env clobber; keep kanban half) | hermes_cli/kanban_identity.py:48 imports tools.kanban_tools._current_session_id (added by #97); kanban_identity.py does not exist upstream and upstream kanban_tools has no _current_session_id def. The gateway env-clobber half is covered by upstream tests/gateway/test_session_env.py::test_session_key_no_race_condition_with_contextvars. Revert must be partial. | fix(gateway): stop per-session os.environ clobber across concurrent se |
| nopr:0f64a3653b | UNRESOLVED | NEEDS-LEAD (lead ruling (split F1 keep / F2 drop?)) | F1/F2 unit: fleet knobs that exist only for this breaker are set in live config - restart_initiated_ttl_secs 600, restart_loop_threshold 3, restart_loop_window_secs 300 (~/.hermes/config.yaml:75-77); bridged by gateway/fork_ext/restart_policy.py:120-123 (registry entry 9, upstream-intended, golden tests). #229's DESKTOP_REASON_RESTART_CONSUMED mirrors F1. Zero breaker trips measured, so value is unproven, but a DROP also orphans 3 config keys and a registry entry -> lead/Ace, not an auditor call. | fix(gateway): successful turns reset the stuck-loop restart counter ag |
| #180 | SUPERSEDED-BY-UPSTREAM | STANDS | D9; outside-path hits only gates.jsonl. | fix(desktop): route live slash commands (#180) |
| #198 | SUPERSEDED-BY-UPSTREAM | STANDS | upstream carries the helpers the fork's tui_gateway callers use (_session_uses_compute_host, _submit_prompt_to_compute_host, _send_compute_host_control, _get_compute_host_supervisor in up tui_gateway/compute_host_bridge.py + methods_*); fleet knob turn_isolation (config.yaml:356) is read by upstream (up hermes_cli/config_defaults.py:980, tui_gateway/server.py:1218). Registry entry 13 already lifecycle=absorbed. 7d27a31ce7 verified on upstream/main. | feat(tui_gateway): isolate dashboard turns in compute host (#198) |
| #201 | SUPERSEDED-BY-UPSTREAM | STANDS | follows #198 (7d27a31ce7 on upstream/main); remaining outside-path hits are gates.jsonl log rows, not code. | feat(tui_gateway): route isolated session metadata reads (#201) |
| #230 | SUPERSEDED-BY-UPSTREAM | STANDS | no outside consumer. | fix(discord): populate parent_chat_id on native slash events in thread |
| #256 | SUPERSEDED-BY-UPSTREAM | STANDS | 769dba1758 verified on upstream/main. Revert caveat: gateway/fork_ext/restart_policy.py (registry entry 9, golden tests tests/golden/restart_policy/) re-homes _startup_restore_drain_timeout_secs + the HERMES_STARTUP_RESTORE_DRAIN_TIMEOUT bridge; the revert must update the golden or keep the fork_ext copy. | fix(gateway): bound the startup-restore inbound gate on a slow boot-re |
| #306 | SUPERSEDED-BY-UPSTREAM | STANDS | upstream never CLEARS stale flags (0 hits for clear_stale_resume_pending/RESUME_FLAG_STALE in up gateway/) - it only skips them via the freshness window each boot. Measured value of clearing: 1 'stale resume_pending' line in the log window. Skip-forever is harmless, so the drop stands; revert must also drop the resume_flag_stale_clear entry in gateway/fork_ext/restart_policy.py:117 (registry entry 9 golden). | fix(gateway): clear stale resume markers (#306) |
| #318 | SUPERSEDED-BY-UPSTREAM | STANDS | upstream _reset_notice_session_info at up gateway/slash_commands_session.py:190; no outside consumer (hermes_cli/config.py hit is a docstring). | fix(gateway): show effective route in reset banner (#318) |
| #334 | SUPERSEDED-BY-UPSTREAM | STANDS | ad345a99d8 verified on upstream/main; fleet footer field 'latency' (config.yaml:349) is upstream's field. | feat(footer): opt-in 'latency' field — wall-clock turn duration (#334) |
| #34 | SUPERSEDED-BY-UPSTREAM | STANDS | no outside consumer; upstream typing loop honours retry_after. | fix(discord): stop typing indicator getting stuck on rate-limited chan |
| #422 | SUPERSEDED-BY-UPSTREAM | STANDS | bcec6c8d39 verified on upstream/main. | fix(test): hermetic env-detection tests — pin container/supervisor/HOM |
| #616 | SUPERSEDED-BY-UPSTREAM | STANDS | 14b50f5edd verified on upstream/main. Revert caveat: scripts/certify/dashboard_loop_certify.py is NOT on upstream and is co-owned with scripts_misc #612 (NEEDS-LEAD there) - do not delete it in the gateway revert. | fix(tui-gateway): interrupt turns after websocket disconnect (#616) |
| #633 | SUPERSEDED-BY-UPSTREAM | STANDS | no outside consumer. Behaviour differs (fork Discord cap 120s, run.py:5055, vs upstream 300s general cap) but unmeasured. | fix(gateway): bound Discord reconnect backoff (#633) |
| #738 | SUPERSEDED-BY-UPSTREAM | STANDS | aa0289f307 verified on upstream/main; 0 outside-path consumers. | fix(gateway): cap signal-driven stop drain to launchd's live ExitTimeO |
| #745 | SUPERSEDED-BY-UPSTREAM | STANDS | 100% absorbed; no consumer. | fix(gateway): persist active_agent_keys when a turn is promoted sentin |
| #807 | SUPERSEDED-BY-UPSTREAM | STANDS | 9815b44568 verified on upstream/main (same Kyzcreig change). tools/async_delegation.py _executor_max_workers is an unrelated module-local of the same name, not a consumer. | fix(gateway): run session housekeeping on its own pool so it cannot st |
| #855 | SUPERSEDED-BY-UPSTREAM | STANDS | upstream async sticker cache write; outside-path hits are generic names. | fix(gateway): move the sticker-description cache write off the event l |
| #862 | SUPERSEDED-BY-UPSTREAM | STANDS | ee8a3ded96 verified on upstream/main. | fix(gateway): move the thread-participation persist off the event loop |
| #883 | SUPERSEDED-BY-UPSTREAM | STANDS | f276ff3f6f verified on upstream/main. | fix(gateway): move the artifact transport off the event loop (ratchet  |
| #911 | SUPERSEDED-BY-UPSTREAM | STANDS | 98428d20bc verified on upstream/main. | fix(gateway): loop-wakeup watcher reads state_meta off the event loop  |
| nopr:6dc72a75d4 | SUPERSEDED-BY-UPSTREAM | STANDS | no outside consumer. | fix(gateway): synthetic internal events must never impersonate the use |

## Revert-branch findings (item 6)

- audit/gateway/revert-129: only pushed revert branch; removes the writer but leaves `discord.reaction_journal` set in ~/.hermes/config.yaml:567 (orphaned key) - see #129.
- undo unit (#49+#353+#339+#356, not yet built, card t_32ef8a2c): must also delete tui_gateway/server.py `_undo_session_core`/`_redo_session_core` and callers in tui_gateway/methods_session.py/methods_tools.py; blocked on the registry entry-14 ruling anyway.
- #256/#306 reverts touch the fork_ext restart_policy golden (registry entry 9); #616 revert must not delete scripts/certify/dashboard_loop_certify.py (co-owned, not upstream).
- nopr:1832ed4e78/nopr:0fee1b8b64 reverts would break tools/delegate_tool.py (#285).

## KEEP-UNPROVEN (98)

Rule: an unmeasured "still needed" is not a KEEP. These KEEP rows have fires = n/a, no literal, or 0 (guard never fired). Refactor/extraction/test-contract rows are listed too: their value is the feature they carry, not a measurement.

| key | fires (auditor) | original (auditor) | subject |
|---|---|---|---|
| #683 | 0 in 2026-05-10..2026-09-25 (5 literals = failure paths; success path  | PR body describes observed replay after boots; fork tests (6 files) | fix(delegation): acknowledge JSON completion outbox after delivery (#6 |
| #228 | 0 in 2026-05-10..2026-09-25 (4 literals = error paths) | live root-cause in PR body (footer built with no reasoning= fell back  | feat(gateway): honest reasoning footer + in-session switch announce +  |
| #456 | n/a | declared refactor (AST-byte-identical move to gateway/fork_ext/restart | refactor(gateway): extract restart policy helpers (#456) |
| #657 | 0 (1 literal = error path); announces themselves are chat messages, no | reproduced 503 no-eligible-sub x3 -> 429 sequence in PR body | fix(gateway): announce route and reasoning transitions consistently (# |
| #838 | 0 (1 literal = failure path) | FleetReview round-3 record on #821 head ad9c32339e | fix(gateway): close the round-3 FleetReview P1s on the shutdown budget |
| #433 | n/a | merge artifact diagnosed in PR body | fix(tui_gateway): dedup merge-introduced module-level defs + recover t |
| #821 | 0 (2 literals) | PR body (teardown timing persisted to state/gateway.teardown.json; res | fix(gateway): reserve launchd shutdown teardown headroom (#821) |
| #249 | 0 (4 literals = failure paths) | live 2026-07-08 observation in PR body | feat(fallback): announce the model-restore leg where it happens — inli |
| #160 | 0 (3 literals incl. PHASE=tg_redelivery_suppressed — no suppression ev | PR body ('hard-kill duplicate-answer guard, scope B') | feat(telegram): hard-kill boot-redelivery duplicate-answer guard (scop |
| #832 | n/a | t_13445a80 ratchet 37->26 | fix(gateway): move the deferred-restart arm off the event loop (ratche |
| #814 | n/a (no literal) | PR body; 4 fork tests | fix(gateway): stop stale queued turn recursion (#814) |
| #967 | 0 (3 literals) | t_13445a80 ratchet | fix(discord): take restart-recovery persistence off the event loop + p |
| #769 | 0 (5 literals = failure paths); attribution appears in gateway-exit-di | PR body | fix(gateway): name the killer on an unclean lifecycle-ledger exit (#76 |
| nopr:29059863f8 | 0 (2 literals = failure paths) | commit body (A4 axis) | feat(fallback): announce the re-init model snap-back (unified recovery |
| #557 | 0 (1 literal) | PR body | fix(model-switch): stop /model clobbering the session's reasoning effo |
| #772 | n/a (no literal) | PR body (2026-09-20 host load) | fix(gateway): hold instead of exit-75 when the liveness miss is host s |
| #961 | 0 (8 literals = failure paths) | t_e253d9d5 r3 | fix(gateway): restart follow-ups keep adapter-granted admission; refus |
| #137 | 0 (3 literals = failure paths) | PR body | fix(gateway): per-session quiescence + task-liveness reaper (busy-gate |
| #945 | 0 (6 literals = failure paths) | t_e253d9d5 r4 | fix(gateway): spool adapter-parked follow-ups before teardown clears t |
| nopr:b728ad49f9 | n/a | commit body | refactor(gateway): extract route identity helpers |
| #582 | 0 (1 literal) | PR body | fix(gateway): bound the post-update notification retry so an undeliver |
| #716 | n/a (no literal) | PR body | fix(discord): native slash commands delivered the gateway reply twice  |
| #562 | n/a | PR body | fix(kanban): resolve the wake's participant so an identity-less sub ca |
| #170 | 0 (1 literal) | commit body (refactor) | refactor: share inactivity watchdog polling (#170) |
| #801 | 0 (4 literals = failure paths) | PR body (#761 follow-up) | fix(gateway): charge dispatched resumes and preserve cap across rollba |
| #598 | n/a | PR body | fix(gateway): deliver model route changes durably (#598) |
| #758 | n/a | t_13445a80 ratchet baseline | test(gateway): AST contract — no synchronous subprocess/sleep calls on |
| nopr:19c2e7cc30 | n/a | commit body | refactor(gateway): extract restart failure codec |
| #639 | n/a (no literal extracted) | PR body | fix(telegram): make silent inbound update drops visible via an intake  |
| nopr:5074cd02b0 | 0 (1 literal = skip path) | commit body | feat(compaction-announce): announce hygiene compactions + Issue-8 abor |
| nopr:217d61ddce | n/a | commit body | fix(gateway): boot-resume protection marker owned by turn lifecycle, n |
| #69 | n/a (no literal) | PR body | fix(discord): stop "is typing…" orphaned by typing-loop recreate race  |
| nopr:ec4b3176ff | n/a | commit body (A1a) | feat(gateway): narrow code-skew guard to in-process import oracle (A1a |
| #96 | n/a | PR body | fix(gateway): recognize reboot_interrupted as a first-class auto-resum |
| #710 | n/a | PR body | fix(discord): thread auto_archive_duration 1440 -> 10080 (retire idle- |
| #581 | n/a | PR body ([verified]) | [verified] fix(gateway): reap never-persisted routing stubs (#581) |
| #104 | n/a | PR body | fix(gateway): revive the dead session auto-reset chat notice (#104) |
| #427 | n/a | PR body | fix(gateway): stop double-posting the STT transcript echo for one voic |
| #666 | 0 (1 literal) | PR body (#659 follow-up) | fix(gateway): redirect shape-only legacy session aliases at store load |
| #175 | n/a | PR body | feat(tui_gateway): client-identity source so desktop/dashboard/mobile  |
| #1004 | 0 (1 literal) | PR body (restart-notice family) | fix(gateway): a SIGKILLed safe-restart is announced as planned, not UN |
| nopr:2f530dd026 | n/a | commit body | feat(gateway): runtime-footer provider_model, context_full, reasoning  |
| nopr:11f8a67f01 | n/a | commit body | feat(runtime-footer): add msgs field (raw count vs hygiene hard-limit) |
| nopr:0da8ba5356 | n/a | commit body | fix(gateway): protect boot-resume recovery turns from busy-input inter |
| #396 | 0 (2 literals = failure paths) | PR body (re-land of #258) | fix(gateway): ack a message queued during startup-restore (re-land of  |
| #906 | n/a | PR body | fix(gateway): record one restart-loop ledger entry per process boot, n |
| #843 | n/a | PR body (#295 follow-up) | fix(gateway): make the delivery-ack barrier registry generation-ordere |
| #173 | n/a | PR body | feat(gateway): granular CompactionStats breakdown for manual /compress |
| #333 | n/a | PR body | feat(desktop): /footer command — runtime-metadata footer on desktop tu |
| #140 | 0 (2 literals) | PR body | fix(gateway): hard-exit after graceful shutdown so a wedged worker can |
| #184 | n/a | PR body | fix(gateway): narrow stale-code /model guard to runtime Python changes |
| nopr:ba371ef32f | n/a | commit body | fix(discord): let free-response channels quote bot mentions |
| #403 | n/a | PR body | fix(footer): honor per-model reasoning_overrides and fallback per-entr |
| #1007 | n/a | PR body | fix(gateway): refuse to start a turn whose generation was invalidated  |
| #452 | 0 (1 literal) | PR body | feat(compress): interim progress ack + per-event LCM compaction teleme |
| #627 | n/a | PR body | fix(agent): manual compression banners name their trigger at the choke |
| nopr:cf58afc340 | 0 (1 literal) | commit body (A4 axis A) | feat(model): announce mid-session /model switch in-chat (A4 axis A) |
| #316 | n/a | PR body (#44794) | fix(compress): distinguish a persist-FAILURE from a genuine no-op (#44 |
| #705 | n/a | PR body | fix(gateway): --replace waits the full drain budget before SIGKILL (st |
| #742 | 0 (1 literal) | PR body | fix(gateway): /model switch note names the LIVE route, not the stale o |
| #750 | n/a | PR body | fix(gateway): boot-resume gate looks past session_meta; cron-only drai |
| #304 | n/a | PR body | fix(kanban): stop false "profile health" stall warning when dispatcher |
| #493 | n/a | PR body | feat(gateway): config toggle for the model-switch stale-code guard (#4 |
| #352 | n/a | PR body | fix(desktop): stop live-sync duplicating every message on send (#352) |
| #527 | n/a | PR body | fix(compress): inherit resident live route (#527) |
| #405 | n/a | declared refactor | refactor: unify r:<effort> display-label mapping into one chokepoint ( |
| nopr:a11aecf67b | n/a | commit body (A5-B) | feat(gateway): post-restart auto-closeout nudge on resume note, cache- |
| #751 | n/a | PR body | fix(gateway): resolve Discord chat types for session-key migration con |
| nopr:6e862490c8 | n/a | commit body (merge reconciliation) | fix(gateway): complete fork/upstream reconciliation for slice-5 CI red |
| #1031 | n/a | t_a… card (subject) | fix(gateway): persisted route lookup off the event loop; one-hop Sessi |
| #589 | n/a | PR body | harden(kanban): single-source the creator-stamp shape rule + contract  |
| #691 | 0 (1 literal) | PR body | fix(gateway): honor durable chat pins in reset banners (#691) |
| #19 | n/a | PR body | fix(gateway): bind originating channel into session context for plugin |
| #687 | n/a (no literal) | PR body (#683 follow-up) | fix(delegation): retain refused completions and shutdown claims (#687) |
| #694 | n/a | PR body | fix(gateway): count only user messages in the /queue "(N queued)" read |
| #401 | n/a | PR body | fix(gateway): /stop cancels pending clarify prompts so the next messag |
| #197 | n/a | PR body | feat(gateway): show reasoning effort in /model switch confirmation (#1 |
| nopr:c05a81d90a | n/a | commit body | fix(gateway): boot-resume marker must survive the SENTINEL phase of th |
| #453 | n/a | PR body | fix(gateway): bind the real profile in session context, not "" (#453) |
| #939 | n/a (config default change) | 2026-09-23 Apollo freeze follow-up (aegis story sec.7) | fix(gateway): boot-resume fan-out is unbounded by default — every rest |
| #290 | n/a | PR body | fix(gateway): harden auto-resume durability and scope (#290) |
| #520 | n/a | PR body | fix(gateway): restore served provider + reasoning in turn result; re-w |
| #460 | n/a | PR body | fix(gateway): clear resume_pending for sessions that finished during a |
| #53 | n/a | PR body | fix(gateway): classify credential-resolution errors as auth failures ( |
| nopr:4ed79b6dad | n/a | commit body | fix(telegram): make network-reconnect ladder configurable, raise defau |
| #357 | n/a | PR body | fix(desktop): ship the runtime footer as metadata, not message text (# |
| #404 | n/a | PR body | fix(compress): manual /compress granular Model line shows session-trut |
| #626 | n/a | PR body | fix(gateway): manual /compress banner names its trigger (#626) |
| nopr:f29eede0e2 | n/a | commit body | fix(gateway): hand back the resume pre-claim WITHOUT stripping protect |
| #698 | n/a | PR body | fix(tui): retain a refused kanban batch instead of dropping it (#698) |
| #984 | n/a | PR body | fix(gateway): internal events reuse the pinned session-context prompt  |
| #183 | n/a | PR body | feat(gateway): show reasoning effort in the session-reset banner (#183 |
| nopr:5378b19f73 | n/a | commit body | fix(fallback): address Greptile review on the recovery-announce helper |
| nopr:3d5ecfa0ae | n/a | commit body | fix(gateway): restore fork systemd exit-0 restart branch (Linux CI sli |
| #163 | n/a | PR body (comment-only) | fix(gateway): correct reaper design comment (_touch_activity is NOT de |
| #138 | n/a | commit body | chore(gateway): address Greptile review — observable non-int row-id +  |
| nopr:4c595fc6f1 | n/a | commit body | fix(gateway): pass agent provider into runtime-footer result dict |
| nopr:59429f36db | n/a | commit body | fix(gateway): clear typing indicator on stale-result early-return path |
