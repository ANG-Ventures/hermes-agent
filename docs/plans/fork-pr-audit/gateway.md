# gateway tranche — auditor verdicts (t_35a3b292)

Rows: 181 · verdicts banked: 20 · log window scanned: 2026-05-10..2026-09-25 (540 files, 821 MiB under ~/.hermes/logs + profiles/*/logs; older history is rotated away — a 0 here means 'not in the last ~6 days', not 'never').

Upstream = NousResearch/hermes-agent main @ 59004a6235 (worktree ../up). Fork main @ ec84b3d155 (2026-09-25). `read-only:` = upstream state established by reading the upstream path, not a live run.

Counts: KEEP=9, DROP=2, UPSTREAM=4, SUPERSEDED-BY-UPSTREAM=5, UNRESOLVED=0

| PR | problem + evidence | upstream RED/GREEN (how) | upstream fix sha | fire count (window) | cost loc/conflict(syncs) | verdict | branch |
|---|---|---|---|---|---|---|---|
| #315 fix(gateway): make fast route-aware and preserve manual reset preferen | /fast resolved against global model defaults instead of the session route; manual /new and /reset dropped the session /model+/reasoning pin — PR body only (design summary, no incident/card/log line cited); 15 fork tests | GREEN for the /fast half (read-only: _handle_fast_command@gateway/slash_commands_model.py:783-804 resolves service tier per session_key via _resolve_session_service_tier); RED for the sticky-reset half (preserve_route_preferences_on_manual_reset / fast_mode_contracts.py: 0 hits upstream) | session-scoped /fast on upstream main (slash_commands_model.py); no upstream equivalent of the manual-reset preference carry-over | 0 in 2026-05-10..2026-09-25 (2 literals, both error paths) | 3863/21(3) deps=[] | **SUPERSEDED-BY-UPSTREAM** |  Highest-cost row in the tranche (3863 loc, 21 conflict files in all 3 syncs, touches run.py+session.py+slash_commands.py+cli.py+tui_gateway/server.py+15 locales). The /fast half is absorbed. The sticky-reset half is a product preference (keep the /model pin across a manual /new) that is unmeasured (0 fires, no incident) — per README rule that is not a KEEP. Lead/Ace ruling needed on whether the sticky-reset knob (default true) is wanted; if yes, re-port ONLY session-reset preservation as a small patch instead of carrying the 3.8k-line contract layer. |
| #49 feat: reversible half-turn /undo + /redo across CLI, gateway, TUI (#49 | no reversible half-turn /undo and no /redo across CLI/gateway/TUI — feature PRD/spec (docs/specs/undo-redo-half-turn-*.md); no incident | GREEN-partial (read-only: upstream has single-turn /undo — _handle_undo_command@gateway/slash_commands_session.py:431, undo_last@hermes_cli/cli_session_mixin.py:762, _cmd_undo@tui_gateway/methods_tools.py:834; no /redo, no half-turn walk, no hermes_undo.py) | upstream's own /undo (pre-existing); redo never adopted upstream | 0 in 2026-05-10..2026-09-25 (16 literals incl. the success lines 'undo operation(s) (' / 'message(s) restored).' — nobody ran /undo or /redo on any gateway in 4.5 months of retained logs) | 3007/9(3) deps=[] | **DROP** |  Feature never used in production (0 of 16 literals hit across 540 log files incl. success messages). 3007 loc, 9 conflict files (3 syncs), fork-only hermes_undo.py. Dependents: #353 #339 #356 (undo follow-ups) — revert together. Revert-branch attempt recorded in the branch pass at the end (see gateway.md footer). |
| #157 fix(discord): recover messages dropped during a graceful restart drain | Discord messages arriving during the ~3.4 min restart drain window were silently lost (no history re-read on reconnect) — PR body cites a live incident (#logs/#alerts messages during safe-restart drain never produced an 'inbound message:' log line) | GREEN (read-only: upstream has its own startup history backfill — _missed_message_backfill_task@plugins/platforms/discord/adapter.py:1108, per-channel last-bot-message cursor :1117; platform_message_id persisted on user turns in gateway/session_transcript.py + run_turn.py). Fork main carries BOTH (fork adapter.py:1520 upstream task + fork _discord_restart_backfill) | 303949acdc 2026-06-04 'fix: backfill missed Discord messages on startup' (predates the fork PR); refactored in 6e5b084b8b | 780 in 2026-05-10..2026-09-25 (PHASE=restart_backfill 390, reinject_attempts= 390) | 2296/6(3) deps=[] | **SUPERSEDED-BY-UPSTREAM** |  Fork's backfill still fires every boot (390 boots) but upstream's task does the same job on the same adapter. Before revert: confirm upstream's backfill also covers idle channels the drain never marked (fork tracks recent_channels in a shutdown-flushed state file; upstream uses last-bot-message cursor per channel). 2296 loc, 6 conflict files (3 syncs), touches agent/conversation_loop.py + run_agent.py. |
| #295 feat(gateway): distinguish and recover SELF restart handoffs (#295) | startup continuation could not distinguish a sibling replay from a per-session SELF restart handoff; deferred restart intents were not persisted/elected across boots — PR body (design summary; PROGRESS.md evidence pack); no incident id | RED (read-only: no deferred restart lifecycle upstream — deferred_restart/DeferredRestart/resume_requests: 0 hits in up/; gateway/auto_resume.py, deferred_restart.py, resume_requests.py are fork-only files) | none | 1190 in 2026-05-10..2026-09-25 (PHASE=boot_resume_scheduled — literal shared with #142/#746) | 2284/3(3) deps=[] | **KEEP** |  Load-bearing fork restart infrastructure: #761 #827 #832 #770 #857 #937 #945 #961 #1004 all build on auto_resume.py/deferred_restart.py. Measured (boot-resume scheduling fires on every boot). Cost 2284 loc but only 3 conflict files. |
| #761 fix(gateway): cap boot auto-resume per session and stop the restart-lo | a dead session was replayed in full on 10 consecutive boots (~4.5M tokens); nothing counted auto-resumes per session across boots, and the restart-loop breaker logged a skip it never performed — incident 2026-09-20 hermes-sess:20260919_022839_9b165181, Claude-bridge 'lost-session recovery burst' pages 02:09/04:28/05:02/05:36 PT | RED (read-only: _resume_pending_candidates@up/gateway/run_startup.py:531-558 gates only on restart_loop_guard + freshness window; no per-session attempt count — session_attempt_count/boot_resume_attempt: 0 hits upstream) | none | 183 in 2026-05-10..2026-09-25 (PHASE=boot_resume_skipped — literal shared with #560) | 2205/2(3) deps=[] | **KEEP** |  Generic bug class (upstream would replay a dead session on every boot too) — UPSTREAM-worthy, but the fork patch depends on fork-only auto_resume.py/fork_ext/restart_policy.py so it is not portable as-is; a fresh upstream port would be ~200 loc on run_startup.py. Cost 2205 loc / 2 conflict files. |
| #861 fix(gateway): fit the drain to the ARMED watchdog deadline, one source | drain was fitted to exit_timeout-reserve while the os._exit watchdog was armed from a different formula; they agree only when the gui-domain 60s clamp binds — FleetReview P1 record on #838 (review finding), no production incident | RED (read-only: upstream has the launchd ExitTimeOut read — read_launchd_exit_timeout_s@gateway/restart.py:122 (Kyzcreig aa0289f307) — but no single deadline source: resolve_stop_drain_deadline_s/_rearm_shutdown_watchdog 0 hits) | aa0289f307 (base only, by Kyzcreig); the two-deadline fix itself is absent | 0 in 2026-05-10..2026-09-25 (1 literal = arm failure path) | 1897/1(3) deps=[] | **UPSTREAM** |  Inert in this fleet (gui-domain jobs are clamped to 60s, the case where both formulas agree — PR body says so) — so not a fleet KEEP on measurement; it is a generic correctness fix on top of Kyzcreig's own upstream aa0289f307, hence UPSTREAM. 1897 loc mostly tests. Port needs a hand rebase (upstream split run.py into run_shutdown.py). |
| #221 feat: /branch spawns a Discord thread; /merge folds a session summary  | /branch on Discord orphaned the parent channel session; no way to fold a session summary into another session (/merge) — PR body (UX rationale), no incident | GREEN for /branch (read-only: _handle_branch_command@up/gateway/slash_commands_session.py:997 + gateway/slash_commands_branch_thread.py — sibling thread by default); RED for /merge (no /merge command upstream) | dbcbd9d9db 2026-09-20 'feat(gateway): /branch opens a sibling thread by default; --here keeps this chat (#66023)' | 0 in 2026-05-10..2026-09-25 (14 literals, all error paths — success is not logged, so usage of /merge is unmeasured) | 1719/5(3) deps=[] | **SUPERSEDED-BY-UPSTREAM** |  Take upstream's /branch. /merge is fork-only, unmeasured; if anyone uses it, re-port as a standalone slash mixin. 1719 loc, 5 conflict files (3 syncs), 15 locale files. |
| #782 fix(gateway): persist sessions.json off the event loop, and freeze the | sessions.json persisted synchronously on the event loop at every turn boundary; py-spy caught MainThread in os.replace for >=30s — py-spy on live Apollo gateway pid 89635 2026-09-20 12:26:25-12:26:55, 14 consecutive dumps in atomic_replace | RED (read-only: clear_resume_pending -> _save_entries@up/gateway/session_persistence.py:478 -> _persist_routing_data:436 -> _save_sessions_json:474, all plain def, called from loop coroutines e.g. session_recovery.py:383; no writer thread/queue) | none | n/a (only literal is the writer-shutdown error path; 0) | 1607/1(2) deps=[] | **UPSTREAM** |  Generic + measured (py-spy). Port needs a hand rebase: upstream split gateway/session.py into session_persistence.py/session_recovery.py (d7bdf2788d). Fork dependents: the loop-reachability AST tests (#758 baseline) reference this writer. |
| #198 feat(tui_gateway): isolate dashboard turns in compute host (#198) | dashboard turns ran in-process; needed a supervised compute-host child (process isolation phase 1) — docs/desktop/2026-07-04-dashboard-process-isolation-PRD.md | GREEN (read-only: up/tui_gateway/compute_host.py + host_supervisor.py exist; absorb probe 325/467 src lines = 70%, 52/61 symbols) | 7d27a31ce7 2026-07-16 'feat(dashboard): isolate turns in compute host (#65895)' (brooklyn!) | 369 in 2026-05-10..2026-09-25 (compute host started 26, stderr 343) | 1567/5(3) deps=[] | **SUPERSEDED-BY-UPSTREAM** |  Upstream landed the same design 11 days after the fork; fork delta on top of upstream's copy is what #433 dedups. Desktop/dashboard = D9 upstream-owned. Take upstream's; audit only whether tui_gateway/server.py retains fork-only dispatch glue (see #433). |
| #659 fix(gateway/discord): one session per chat — canonical resolver, fail- | one Discord chat produced two session keys (group vs channel) so a /model pin on one was bypassed by system wakes on the other — kanban t_53316ee6; Ace ruling 'multiple sessions serving one chat must be structurally impossible' | RED (read-only: canonical_chat_type / chat_model_pins / routing_identity.py: 0 hits upstream; upstream Discord adapter still keys group/channel by discord.py channel type) | none | 3 in 2026-05-10..2026-09-25 (migration lookup failed 2, legacy duplicate routes 1) — the guard is structural; steady state logs nothing | 1466/7(3) deps=[] | **KEEP** |  Ace-ruled invariant. Cost 1466 loc, 7 conflict files (3 syncs) incl. hermes_state.py + run.py + session.py. Dependents: #691 (chat pins in reset banner), #751 (concurrent chat-type resolution). |
| #289 fix(gateway): auto-continue safe interrupted turns (#289) | restart-interrupted turns were only prompted, never auto-continued; needed a fail-closed classifier (mutating tool tail -> prompt) + a 7-day once-ever cap — PR body design; fork tests test_auto_continue_interrupted_turns.py | RED-partial (read-only: upstream auto-continues every fresh restart-interrupted session unconditionally — _schedule_resume_pending_sessions@up/gateway/run_startup.py:575-623, no tail classifier, no resume_interrupted_turns knob: 0 hits) | 10bad2faf1 2026-06-14 'serialize startup auto-resume' (upstream's simpler always-auto path) | 1410 in 2026-05-10..2026-09-25 (dropbox_resume/boot_resume_scheduled) | 1426/6(3) deps=[] | **KEEP** |  Upstream resumes MORE aggressively (no safety classifier) — the fork's classifier is the safer behaviour and fires on every boot. 1426 loc, 6 conflict files. |
| #683 fix(delegation): acknowledge JSON completion outbox after delivery (#6 | delivered async-delegation completions replayed after gateway boots because consumers acked only the legacy SQLite ledger, not the producer's JSON outbox — PR body describes observed replay after boots; fork tests (6 files) | read-only: upstream delegation completion path has no JSON receipt/ack (completion_outbox 0 hits; fork-only ack-receipt symbols absent) | none found | 0 in 2026-05-10..2026-09-25 (5 literals = failure paths; success path logs nothing) | 1387/5(3) deps=[] | **KEEP** |  Measured only by absence of replays (no positive literal). Dependent #687 (retain refused completions). 1387 loc, 5 conflict files. |
| #871 fix(gateway): move the shutdown pending-message flush off the event lo | shutdown pending-message flush (one atomic write per pending session) ran inline on the event loop — t_13445a80 reachability-baseline drain (ratchet 18->17); AST reachability test | RED (read-only: cancel_background_tasks@up/gateway/platforms/base.py:4609 calls flush_pending_to_file inline at :4632) | none | n/a (2 literals = error paths, 0) | 1344/1(3) deps=[] | **UPSTREAM** |  Generic loop-hygiene fix; measured only via the AST ratchet, not a production stall. Part of the off-loop family (#782 #836 #817 #868 #832 #967 #862 #883 #855 #834 #869 #976 #1031 #970 #911 #935 #759): upstream already took 4 of these from Kyzcreig, so the rest port cleanly one at a time. |
| #836 fix(gateway): move the platform-lock acquire off the event loop (ratch | _acquire_platform_lock (file lock acquire) is a plain def called from every adapter's connect() on the loop — t_13445a80 ratchet 26->22 | RED (read-only: def _acquire_platform_lock@up/gateway/platforms/base.py:2185 is sync; called from connect() in line/whatsapp/weixin/signal adapters) | none | n/a (no literal) | 1307/1(2) deps=[] | **UPSTREAM** |  Same family as #871. 1307 loc across 9 adapter files (each call site), 1 conflict file. |
| #229 feat(tui): desktop/TUI session auto-resume after backend restart (dorm | desktop/TUI conversation did not auto-resume after a backend restart (Discord/Telegram did) — docs/desktop/2026-07-06-desktop-session-auto-resume-SPEC.md; shipped behind a DORMANT default-off flag | read-only: no equivalent upstream (desktop_session_auto_resume 0 hits); tui_gateway/session_auto_continue.py exists upstream (own design) | upstream tui_gateway/session_auto_continue.py (own mechanism) | 0 in 2026-05-10..2026-09-25 (3 literals) | 1272/3(3) deps=[] | **DROP** |  Dormant flag never enabled (0 fires in 4.5 months); desktop is D9 upstream-owned and upstream has its own session_auto_continue. 1272 loc, 3 conflict files (3 syncs). |
| #228 feat(gateway): honest reasoning footer + in-session switch announce +  | footer showed r:xhigh while the turn ran at high; session /reasoning override did not survive a gateway restart — live root-cause in PR body (footer built with no reasoning= fell back to config) | RED-partial (read-only: upstream runtime_footer.py has reasoning field but no restart-durable session override announce; announce_route 0 hits) | none | 0 in 2026-05-10..2026-09-25 (4 literals = error paths) | 1243/7(3) deps=[] | **KEEP** |  Base of the fork route-announce family (#657 #249 #598 #318 #557 #403 #405 #742 #691 #520 build on it). Measured only by its tests; the footer correctness is user-visible on every turn. 1243 loc, 7 conflict files (3 syncs) — a hotspot. |
| #456 refactor(gateway): extract restart policy helpers (#456) | restart policy helpers lived inside upstream-churned gateway/run.py and conflicted every sync — declared refactor (AST-byte-identical move to gateway/fork_ext/restart_policy.py + golden transcript) | n/a (fork-only module; reduces conflict surface by design) | n/a | n/a | 1159/3(3) deps=[] | **KEEP** |  Conflict-reduction refactor; dropping it would move 7 functions back into run.py. Keep as long as the restart family (#295 #761 …) stays. |
| #657 fix(gateway): announce route and reasoning transitions consistently (# | route/effort transitions (failover + recovery, effort-only changes) were announced inconsistently or not at all — reproduced 503 no-eligible-sub x3 -> 429 sequence in PR body | RED (read-only: no route-transition announce upstream; announce_route 0 hits) | none | 0 (1 literal = error path); announces themselves are chat messages, not log lines | 1146/7(3) deps=[] | **KEEP** |  Fleet-specific (claude-bridge sub failover). 1146 loc, 7 conflict files (3 syncs). |
| #937 fix(gateway): honor busy_policy=interrupt in restart wait; spool (not  | restart wait ignored --busy-policy interrupt (1800s deferral); follow-ups sent while draining were discarded; boot notice attributed planned=False by=- — incident 2026-09-23 Apollo 49726->7734, card t_e253d9d5 | RED (read-only: upstream drain has no interrupt-intent re-read or follow-up spool — restart_followup 0 hits; busy_policy only for slash commands in run_busy.py:931) | none | 132 in 2026-05-10..2026-09-25 (restart_followup_spooled / restart_followups_replayed) | 1069/1(3) deps=[] | **KEEP** |  Measured, incident-backed, 2 days old. Dependents #945 #961 #1004. |
| #301 fix(gateway): fail open startup restore intake (#301) | startup-restore inbound gate could hold forever / drop events; needed fail-open deadline armed before platform connects + background replay owner — PR body (design); fork tests | GREEN (read-only: _finish_startup_restore@up/gateway/run_startup.py:215-246 bounded by _startup_restore_drain_timeout_secs with a finally that always releases the gate (#116514); _wait_bounded_or_release:193; _drain_startup_restore_queue:92 continues past a bad replay) | 769dba1758 2026-07-26 (Kyzcreig, #256 port) + 00b59950ae 2026-09-02 bounded-gate helper | 19713 in 2026-05-10..2026-09-25 (PHASE=startup_restore_replay_enter/exit per boot; gate_flip caller=watchdog) | 1020/1(3) deps=[] | **SUPERSEDED-BY-UPSTREAM** |  Hottest literal in the tranche but it is per-boot telemetry, not a defect firing. Upstream's own gate (Kyzcreig-ported base + Teknium refactor) covers the fail-open; fork's extra per-event retry/backoff replay owner is not evidenced as needed. 1020 loc, 1 conflict file. |
| #827 | _pending_ | | | | 1018/3(3) | — | |
| #838 | _pending_ | | | | 1007/1(3) | — | |
| #817 | _pending_ | | | | 956/2(3) | — | |
| #868 | _pending_ | | | | 923/0(0) | — | |
| #765 | _pending_ | | | | 912/3(3) | — | |
| #790 | _pending_ | | | | 903/1(3) | — | |
| #433 | _pending_ | | | | 880/1(3) | — | |
| #821 | _pending_ | | | | 872/1(3) | — | |
| #763 | _pending_ | | | | 861/1(2) | — | |
| #249 | _pending_ | | | | 847/2(3) | — | |
| #807 | _pending_ | | | | 824/2(3) | — | |
| #160 | _pending_ | | | | 813/1(3) | — | |
| #832 | _pending_ | | | | 797/1(3) | — | |
| #80 | _pending_ | | | | 783/3(3) | — | |
| #616 | _pending_ | | | | 729/3(3) | — | |
| #70 | _pending_ | | | | 701/2(3) | — | |
| #814 | _pending_ | | | | 696/2(3) | — | |
| #967 | _pending_ | | | | 690/1(2) | — | |
| #769 | _pending_ | | | | 680/1(3) | — | |
| nopr:29059863f8 | _pending_ | | | | 679/4(3) | — | |
| #353 | _pending_ | | | | 678/5(3) | — | |
| #557 | _pending_ | | | | 675/3(3) | — | |
| #770 | _pending_ | | | | 671/1(3) | — | |
| #772 | _pending_ | | | | 670/2(3) | — | |
| #201 | _pending_ | | | | 660/4(3) | — | |
| #180 | _pending_ | | | | 660/2(3) | — | |
| #142 | _pending_ | | | | 609/3(3) | — | |
| #857 | _pending_ | | | | 606/2(3) | — | |
| #961 | _pending_ | | | | 593/2(3) | — | |
| #137 | _pending_ | | | | 581/2(3) | — | |
| #945 | _pending_ | | | | 578/1(3) | — | |
| #970 | _pending_ | | | | 574/3(3) | — | |
| #692 | _pending_ | | | | 567/1(1) | — | |
| #738 | _pending_ | | | | 566/2(3) | — | |
| #862 | _pending_ | | | | 564/1(2) | — | |
| #97 | _pending_ | | | | 563/5(3) | — | |
| nopr:b728ad49f9 | _pending_ | | | | 551/2(3) | — | |
| #582 | _pending_ | | | | 540/1(3) | — | |
| #560 | _pending_ | | | | 539/1(3) | — | |
| #840 | _pending_ | | | | 530/1(3) | — | |
| #716 | _pending_ | | | | 508/1(2) | — | |
| #562 | _pending_ | | | | 495/1(1) | — | |
| #170 | _pending_ | | | | 494/2(3) | — | |
| #801 | _pending_ | | | | 476/1(3) | — | |
| #598 | _pending_ | | | | 470/3(3) | — | |
| #883 | _pending_ | | | | 462/0(0) | — | |
| #758 | _pending_ | | | | 461/0(0) | — | |
| #318 | _pending_ | | | | 451/2(3) | — | |
| #855 | _pending_ | | | | 445/0(0) | — | |
| nopr:19c2e7cc30 | _pending_ | | | | 438/2(3) | — | |
| nopr:1832ed4e78 | _pending_ | | | | 436/2(3) | — | |
| #834 | _pending_ | | | | 430/0(0) | — | |
| #639 | _pending_ | | | | 430/0(0) | — | |
| nopr:5074cd02b0 | _pending_ | | | | 425/1(3) | — | |
| #851 | _pending_ | | | | 409/0(0) | — | |
| nopr:217d61ddce | _pending_ | | | | 403/1(3) | — | |
| #69 | _pending_ | | | | 398/2(3) | — | |
| #7536 | _pending_ | | | | 397/1(3) | — | |
| nopr:2ee2e03b68 | _pending_ | | | | 396/1(3) | — | |
| nopr:ec4b3176ff | _pending_ | | | | 389/0(0) | — | |
| #746 | _pending_ | | | | 382/3(3) | — | |
| #96 | _pending_ | | | | 377/1(3) | — | |
| #710 | _pending_ | | | | 365/1(2) | — | |
| #581 | _pending_ | | | | 362/1(2) | — | |
| #104 | _pending_ | | | | 350/2(3) | — | |
| #129 | _pending_ | | | | 348/1(2) | — | |
| #427 | _pending_ | | | | 342/1(3) | — | |
| #666 | _pending_ | | | | 339/1(2) | — | |
| #757 | _pending_ | | | | 329/1(2) | — | |
| #869 | _pending_ | | | | 328/0(0) | — | |
| #339 | _pending_ | | | | 328/3(3) | — | |
| #759 | _pending_ | | | | 326/1(3) | — | |
| #175 | _pending_ | | | | 322/2(3) | — | |
| #1004 | _pending_ | | | | 315/1(3) | — | |
| #231 | _pending_ | | | | 313/2(3) | — | |
| #72 | _pending_ | | | | 308/2(3) | — | |
| #256 | _pending_ | | | | 300/3(3) | — | |
| nopr:2f530dd026 | _pending_ | | | | 299/3(3) | — | |
| nopr:11f8a67f01 | _pending_ | | | | 294/2(3) | — | |
| nopr:0da8ba5356 | _pending_ | | | | 289/1(3) | — | |
| #356 | _pending_ | | | | 285/5(3) | — | |
| #396 | _pending_ | | | | 269/1(3) | — | |
| #306 | _pending_ | | | | 264/4(3) | — | |
| #906 | _pending_ | | | | 263/3(3) | — | |
| #936 | _pending_ | | | | 259/1(3) | — | |
| #86 | _pending_ | | | | 258/3(3) | — | |
| #843 | _pending_ | | | | 251/1(3) | — | |
| #173 | _pending_ | | | | 250/2(3) | — | |
| #333 | _pending_ | | | | 243/1(3) | — | |
| #140 | _pending_ | | | | 238/2(3) | — | |
| #358 | _pending_ | | | | 237/2(3) | — | |
| #184 | _pending_ | | | | 234/1(1) | — | |
| nopr:ba371ef32f | _pending_ | | | | 233/1(2) | — | |
| #403 | _pending_ | | | | 223/2(3) | — | |
| #1007 | _pending_ | | | | 220/1(3) | — | |
| #452 | _pending_ | | | | 218/3(3) | — | |
| #627 | _pending_ | | | | 217/4(3) | — | |
| nopr:cf58afc340 | _pending_ | | | | 217/2(3) | — | |
| #899 | _pending_ | | | | 213/2(3) | — | |
| #316 | _pending_ | | | | 212/4(3) | — | |
| #705 | _pending_ | | | | 210/1(3) | — | |
| #742 | _pending_ | | | | 207/1(3) | — | |
| nopr:7fabbdba23 | _pending_ | | | | 207/1(3) | — | |
| #34 | _pending_ | | | | 201/1(2) | — | |
| #750 | _pending_ | | | | 200/1(3) | — | |
| #304 | _pending_ | | | | 197/3(2) | — | |
| #493 | _pending_ | | | | 193/1(3) | — | |
| #884 | _pending_ | | | | 189/1(3) | — | |
| #352 | _pending_ | | | | 187/1(3) | — | |
| #527 | _pending_ | | | | 186/2(3) | — | |
| #584 | _pending_ | | | | 179/0(0) | — | |
| #976 | _pending_ | | | | 173/2(3) | — | |
| #405 | _pending_ | | | | 172/8(3) | — | |
| nopr:a11aecf67b | _pending_ | | | | 164/1(3) | — | |
| #751 | _pending_ | | | | 161/1(3) | — | |
| nopr:6e862490c8 | _pending_ | | | | 159/3(3) | — | |
| #1031 | _pending_ | | | | 155/2(3) | — | |
| #589 | _pending_ | | | | 153/2(2) | — | |
| #691 | _pending_ | | | | 149/2(3) | — | |
| #19 | _pending_ | | | | 149/1(3) | — | |
| #687 | _pending_ | | | | 149/3(3) | — | |
| #694 | _pending_ | | | | 146/1(3) | — | |
| #401 | _pending_ | | | | 146/1(3) | — | |
| #197 | _pending_ | | | | 130/3(3) | — | |
| nopr:c05a81d90a | _pending_ | | | | 126/1(3) | — | |
| nopr:0f64a3653b | _pending_ | | | | 125/1(3) | — | |
| #453 | _pending_ | | | | 124/1(3) | — | |
| #939 | _pending_ | | | | 113/2(3) | — | |
| #290 | _pending_ | | | | 112/1(3) | — | |
| #777 | _pending_ | | | | 109/1(2) | — | |
| #934 | _pending_ | | | | 107/1(3) | — | |
| #904 | _pending_ | | | | 102/1(3) | — | |
| #520 | _pending_ | | | | 98/1(3) | — | |
| nopr:6dc72a75d4 | _pending_ | | | | 96/1(3) | — | |
| #334 | _pending_ | | | | 93/3(3) | — | |
| #745 | _pending_ | | | | 91/1(3) | — | |
| #460 | _pending_ | | | | 86/1(3) | — | |
| #53 | _pending_ | | | | 84/2(3) | — | |
| #230 | _pending_ | | | | 83/1(2) | — | |
| nopr:4ed79b6dad | _pending_ | | | | 81/0(0) | — | |
| #357 | _pending_ | | | | 78/1(3) | — | |
| #935 | _pending_ | | | | 77/1(3) | — | |
| #404 | _pending_ | | | | 77/2(3) | — | |
| #626 | _pending_ | | | | 75/1(3) | — | |
| nopr:f29eede0e2 | _pending_ | | | | 75/1(3) | — | |
| #571 | _pending_ | | | | 75/2(3) | — | |
| #698 | _pending_ | | | | 73/2(3) | — | |
| #984 | _pending_ | | | | 70/1(3) | — | |
| #395 | _pending_ | | | | 70/1(3) | — | |
| #183 | _pending_ | | | | 67/1(3) | — | |
| #911 | _pending_ | | | | 65/1(3) | — | |
| #422 | _pending_ | | | | 61/1(3) | — | |
| nopr:5378b19f73 | _pending_ | | | | 60/1(3) | — | |
| #633 | _pending_ | | | | 54/1(3) | — | |
| #635 | _pending_ | | | | 43/0(0) | — | |
| nopr:3d5ecfa0ae | _pending_ | | | | 33/1(3) | — | |
| #163 | _pending_ | | | | 26/1(3) | — | |
| #138 | _pending_ | | | | 22/2(3) | — | |
| nopr:0fee1b8b64 | _pending_ | | | | 21/1(2) | — | |
| nopr:4c595fc6f1 | _pending_ | | | | 15/2(3) | — | |
| nopr:59429f36db | _pending_ | | | | 11/1(3) | — | |
