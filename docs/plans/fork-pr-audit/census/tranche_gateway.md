# Tranche: gateway — 181 rows

absorb = substantive added src lines found verbatim on upstream/main (hit/total); sym = added def/class names found. Signal only — read README §0.

| key | subsystem | kind | loc | files | conflict-files (syncs) | absorb src | sym | date | subject |
|---|---|---|---|---|---|---|---|---|---|
| #315 | gateway | code | 3863 | 45 | 21(3) | 53/452 | 5/22 | 2026-07-12 | fix(gateway): make fast route-aware and preserve manual reset preferences (#315) |
| #49 | other:tui_gateway | code | 3007 | 36 | 9(3) | 33/250 | 7/24 | 2026-06-15 | feat: reversible half-turn /undo + /redo across CLI, gateway, TUI (#49) |
| #157 | plugins/platforms | code | 2296 | 13 | 6(3) | 45/298 | 4/20 | 2026-07-01 | fix(discord): recover messages dropped during a graceful restart drain window (#157) |
| #295 | gateway | code | 2284 | 10 | 3(3) | 37/368 | 9/33 | 2026-07-11 | feat(gateway): distinguish and recover SELF restart handoffs (#295) |
| #761 | gateway | code | 2205 | 13 | 2(3) | 5/199 | 5/16 | 2026-09-20 | fix(gateway): cap boot auto-resume per session and stop the restart-loop breaker claiming a skip it  |
| #861 | gateway | code | 1897 | 4 | 1(3) | 8/308 | 1/5 | 2026-09-23 | fix(gateway): fit the drain to the ARMED watchdog deadline, one source (#838 P1 close-out) (#861) |
| #221 | gateway | code | 1719 | 22 | 5(3) | 27/259 | 0/13 | 2026-07-07 | feat: /branch spawns a Discord thread; /merge folds a session summary into any session (#221) |
| #782 | gateway | code | 1607 | 4 | 1(2) | 7/84 | 0/7 | 2026-09-20 | fix(gateway): persist sessions.json off the event loop, and freeze the class (#782) |
| #198 | other:tui_gateway | code | 1567 | 5 | 5(3) | 325/467 | 52/61 | 2026-07-05 | feat(tui_gateway): isolate dashboard turns in compute host (#198) |
| #659 | gateway | code | 1466 | 25 | 7(3) | 26/334 | 8/34 | 2026-09-09 | fix(gateway/discord): one session per chat — canonical resolver, fail-closed write guard, chat-level |
| #289 | gateway | code | 1426 | 9 | 6(3) | 23/224 | 9/21 | 2026-07-10 | fix(gateway): auto-continue safe interrupted turns (#289) |
| #683 | gateway | code | 1387 | 10 | 5(3) | 28/127 | 1/8 | 2026-09-13 | fix(delegation): acknowledge JSON completion outbox after delivery (#683) |
| #871 | gateway | code | 1344 | 5 | 1(3) | 3/107 | 0/7 | 2026-09-22 | fix(gateway): move the shutdown pending-message flush off the event loop (ratchet 18 -> 17) (#871) |
| #836 | gateway/platforms | code | 1307 | 11 | 1(2) | 6/149 | 0/7 | 2026-09-22 | fix(gateway): move the platform-lock acquire off the event loop (ratchet 26 -> 22) (#836) |
| #229 | other:tui_gateway | code | 1272 | 4 | 3(3) | 26/276 | 2/26 | 2026-07-08 | feat(tui): desktop/TUI session auto-resume after backend restart (dormant flag) (#229) |
| #228 | gateway | code | 1243 | 15 | 7(3) | 21/193 | 3/13 | 2026-07-07 | feat(gateway): honest reasoning footer + in-session switch announce + restart-durable overrides (#22 |
| #456 | gateway | code | 1159 | 10 | 3(3) | 12/109 | 1/8 | 2026-08-06 | refactor(gateway): extract restart policy helpers (#456) |
| #657 | gateway | code | 1146 | 13 | 7(3) | 3/112 | 0/2 | 2026-09-09 | fix(gateway): announce route and reasoning transitions consistently (#657) |
| #937 | gateway | code | 1069 | 6 | 1(3) | 24/184 | 1/14 | 2026-09-23 | fix(gateway): honor busy_policy=interrupt in restart wait; spool (not drop) follow-ups while drainin |
| #301 | gateway | code | 1020 | 5 | 1(3) | 20/165 | 1/5 | 2026-07-11 | fix(gateway): fail open startup restore intake (#301) |
| #827 | gateway | code | 1018 | 7 | 3(3) | 7/115 | 8/18 | 2026-09-22 | fix(gateway): bound turn concurrency + boot-resume fan-out (P1 2026-09-21 starvation) (#827) |
| #838 | gateway | code | 1007 | 5 | 1(3) | 6/107 | 1/2 | 2026-09-22 | fix(gateway): close the round-3 FleetReview P1s on the shutdown budget (#838) |
| #817 | gateway | code | 956 | 10 | 2(3) | 32/119 | 5/12 | 2026-09-21 | fix(gateway): move the runtime-status write off the event loop (ratchet 44 -> 41) (#817) |
| #868 | gateway/platforms | code | 923 | 5 | 0(0) | 1/97 | 1/10 | 2026-09-22 | fix(gateway): move the weixin durable writes off the event loop (ratchet 15 -> 13) (#868) |
| #765 | gateway | code | 912 | 8 | 3(3) | 3/98 | 1/6 | 2026-09-20 | fix(gateway): bound and surface a /stop'd turn that still holds the turn lease (#765) |
| #790 | gateway | code | 903 | 7 | 1(3) | 22/202 | 3/14 | 2026-09-21 | fix(gateway): tell resumed sessions WHY the gateway was restarted after an unclean exit (#790) |
| #433 | other:tui_gateway | code | 880 | 6 | 1(3) | 0/3 | 0/0 | 2026-07-26 | fix(tui_gateway): dedup merge-introduced module-level defs + recover two dead compute-host fixes (#4 |
| #821 | gateway | code | 872 | 7 | 1(3) | 16/155 | 0/6 | 2026-09-21 | fix(gateway): reserve launchd shutdown teardown headroom (#821) |
| #763 | plugins/platforms | code | 861 | 4 | 1(2) | 17/107 | 1/7 | 2026-09-20 | fix(discord): never vacate a slash command mid-sync, and self-heal after a 429 (#763) |
| #249 | gateway | code | 847 | 4 | 2(3) | 0/122 | 0/2 | 2026-07-08 | feat(fallback): announce the model-restore leg where it happens — inline restore + pre-run re-init ( |
| #807 | gateway | code | 824 | 5 | 2(3) | 23/153 | 7/14 | 2026-09-21 | fix(gateway): run session housekeeping on its own pool so it cannot starve turn bodies (#807) |
| #160 | gateway | code | 813 | 5 | 1(3) | 12/141 | 9/17 | 2026-07-01 | feat(telegram): hard-kill boot-redelivery duplicate-answer guard (scope B) (#160) |
| #832 | gateway | code | 797 | 5 | 1(3) | 5/82 | 3/12 | 2026-09-21 | fix(gateway): move the deferred-restart arm off the event loop (ratchet 37 -> 26) (#832) |
| #80 | gateway | code | 783 | 3 | 3(3) | 7/94 | 0/8 | 2026-06-21 | fix(gateway): authoritative restart-initiator breadcrumb for F2 breaker (#80) |
| #616 | other:tui_gateway | code | 729 | 5 | 3(3) | 46/137 | 5/8 | 2026-08-20 | fix(tui-gateway): interrupt turns after websocket disconnect (#616) |
| #70 | gateway | code | 701 | 3 | 2(3) | 14/112 | 0/12 | 2026-06-21 | fix(gateway): break the restart-cascade replay loop (F1 drain-mark + F2 circuit-breaker) (#70) |
| #814 | gateway | code | 696 | 6 | 2(3) | 27/104 | 0/7 | 2026-09-22 | fix(gateway): stop stale queued turn recursion (#814) |
| #967 | gateway/platforms | code | 690 | 5 | 1(2) | 12/87 | 7/10 | 2026-09-25 | fix(discord): take restart-recovery persistence off the event loop + plugin-scope atomic-write lint  |
| #769 | gateway | code | 680 | 4 | 1(3) | 17/143 | 2/14 | 2026-09-20 | fix(gateway): name the killer on an unclean lifecycle-ledger exit (#769) |
| nopr:29059863f8 | gateway | code | 679 | 8 | 4(3) | 2/56 | 0/1 | 2026-07-08 | feat(fallback): announce the re-init model snap-back (unified recovery announce + (new turn)/(re-ini |
| #353 | gateway | code | 678 | 21 | 5(3) | 4/104 | 0/3 | 2026-07-15 | fix(undo): stop /undo·/redo reporting a false 'Nothing to undo.' on a swallowed error (#353) |
| #557 | gateway | code | 675 | 5 | 3(3) | 2/60 | 1/3 | 2026-08-10 | fix(model-switch): stop /model clobbering the session's reasoning effort (#467 contract) (#557) |
| #770 | gateway | code | 671 | 3 | 1(3) | 10/125 | 0/8 | 2026-09-20 | fix(gateway): cancel scheduled-but-unstarted boot resumes at shutdown instead of admitting them into |
| #772 | gateway | code | 670 | 6 | 2(3) | 1/96 | 1/5 | 2026-09-20 | fix(gateway): hold instead of exit-75 when the liveness miss is host starvation (#772) |
| #201 | other:tui_gateway | code | 660 | 4 | 4(3) | 76/120 | 6/6 | 2026-07-05 | feat(tui_gateway): route isolated session metadata reads (#201) |
| #180 | other:tui_gateway | code | 660 | 6 | 2(3) | 59/150 | 5/8 | 2026-07-02 | fix(desktop): route live slash commands (#180) |
| #142 | gateway | code | 609 | 4 | 3(3) | 2/36 | 0/3 | 2026-06-30 | fix(gateway): auto-surface a drain-interrupted self-restart + PHASE observability + reboot-parity pr |
| #857 | gateway | code | 606 | 4 | 2(3) | 6/111 | 0/4 | 2026-09-22 | fix(gateway): a /stop'd turn must never be auto-resumed after a restart (#857) |
| #961 | gateway | code | 593 | 5 | 2(3) | 4/68 | 1/6 | 2026-09-24 | fix(gateway): restart follow-ups keep adapter-granted admission; refused replays reported lost (t_43 |
| #137 | gateway | code | 581 | 7 | 2(3) | 5/57 | 0/4 | 2026-06-30 | fix(gateway): per-session quiescence + task-liveness reaper (busy-gateway deferred restart never fir |
| #945 | gateway | code | 578 | 5 | 1(3) | 9/87 | 2/3 | 2026-09-24 | fix(gateway): spool adapter-parked follow-ups before teardown clears them (t_e253d9d5 r4, stacked on |
| #970 | gateway | code | 574 | 5 | 3(3) | 1/20 | 0/1 | 2026-09-25 | fix(gateway): /model persist + rehydrate never probe /v1/models on the event loop (t_515b7fce) (#970 |
| #692 | gateway | code | 567 | 2 | 1(1) | 12/52 | 2/3 | 2026-09-13 | fix(kanban): retry contended gateway dispatcher leadership (#692) |
| #738 | gateway | code | 566 | 5 | 2(3) | 58/100 | 7/8 | 2026-09-20 | fix(gateway): cap signal-driven stop drain to launchd's live ExitTimeOut (#738) |
| #862 | gateway/platforms | code | 564 | 6 | 1(2) | 16/25 | 2/2 | 2026-09-22 | fix(gateway): move the thread-participation persist off the event loop (ratchet 17 -> 16) (#862) |
| #97 | gateway | code | 563 | 8 | 5(3) | 4/22 | 1/1 | 2026-06-22 | fix(gateway): stop per-session os.environ clobber across concurrent sessions (v3-latch bug class) (# |
| nopr:b728ad49f9 | gateway | code | 551 | 7 | 2(3) | 7/33 | 0/3 | 2026-07-16 | refactor(gateway): extract route identity helpers |
| #582 | gateway | code | 540 | 2 | 1(3) | 2/42 | 0/2 | 2026-08-11 | fix(gateway): bound the post-update notification retry so an undeliverable marker terminates (#582) |
| #560 | gateway | code | 539 | 3 | 1(3) | 4/68 | 0/2 | 2026-08-10 | fix(gateway): don't spawn boot-resume turns for sessions with nothing to resume (#560) |
| #840 | gateway | code | 530 | 2 | 1(3) | 3/72 | 0/2 | 2026-09-22 | fix(gateway): make the restart-failure-counts read-modify-write atomic (#840) |
| #716 | gateway/platforms | code | 508 | 3 | 1(2) | 4/37 | 0/1 | 2026-09-19 | fix(discord): native slash commands delivered the gateway reply twice (#716) |
| #562 | gateway | code | 495 | 2 | 1(1) | 4/74 | 0/2 | 2026-08-10 | fix(kanban): resolve the wake's participant so an identity-less sub can't mint a phantom session (#5 |
| #170 | gateway | code | 494 | 4 | 2(3) | 11/96 | 2/12 | 2026-07-04 | refactor: share inactivity watchdog polling (#170) |
| #801 | gateway | code | 476 | 10 | 1(3) | 9/79 | 2/4 | 2026-09-21 | fix(gateway): charge dispatched resumes and preserve cap across rollbacks (#801) |
| #598 | gateway | code | 470 | 5 | 3(3) | 8/49 | 2/5 | 2026-08-18 | fix(gateway): deliver model route changes durably (#598) |
| #883 | gateway/platforms | code | 462 | 3 | 0(0) | 10/25 | 1/1 | 2026-09-23 | fix(gateway): move the artifact transport off the event loop (ratchet 15 -> 14) (#883) |
| #758 | plugins/platforms | code | 461 | 3 | 0(0) | 0/1 | 0/0 | 2026-09-20 | test(gateway): AST contract — no synchronous subprocess/sleep calls on the event loop (+ whatsapp co |
| #318 | gateway | code | 451 | 4 | 2(3) | 17/43 | 2/3 | 2026-07-12 | fix(gateway): show effective route in reset banner (#318) |
| #855 | gateway | code | 445 | 4 | 0(0) | 5/14 | 1/1 | 2026-09-22 | fix(gateway): move the sticker-description cache write off the event loop (ratchet 18 -> 17) (#855) |
| nopr:19c2e7cc30 | gateway | code | 438 | 8 | 2(3) | 2/35 | 0/3 | 2026-07-16 | refactor(gateway): extract restart failure codec |
| nopr:1832ed4e78 | gateway | code | 436 | 4 | 2(3) | 15/53 | 2/4 | 2026-07-10 | fix(gateway): bind session context at agent turn entry |
| #834 | gateway | code | 430 | 3 | 0(0) | 5/56 | 1/9 | 2026-09-22 | fix(gateway): move the transcript cap-drop spool write off the event loop (ratchet 26 -> 22) (#834) |
| #639 | plugins/platforms | code | 430 | 2 | 0(0) | 11/70 | 1/3 | 2026-08-27 | fix(telegram): make silent inbound update drops visible via an intake sentinel (#639) |
| nopr:5074cd02b0 | gateway | code | 425 | 2 | 1(3) | 9/67 | 0/1 | 2026-06-20 | feat(compaction-announce): announce hygiene compactions + Issue-8 abort guard (Phases 0/2/3) |
| #851 | gateway/platforms | code | 409 | 2 | 0(0) | 4/57 | 0/2 | 2026-09-22 | fix(gateway): make the scoped-lock ownership decision and its release one critical section (#851) |
| nopr:217d61ddce | gateway | code | 403 | 2 | 1(3) | 0/5 | 0/0 | 2026-07-10 | fix(gateway): boot-resume protection marker owned by turn lifecycle, not wrapper |
| #69 | gateway/platforms | code | 398 | 4 | 2(3) | 2/65 | 2/5 | 2026-06-20 | fix(discord): stop "is typing…" orphaned by typing-loop recreate race (#69) |
| #7536 | gateway | code | 397 | 3 | 1(3) | 1/26 | 0/1 | 2026-07-08 | fix(gateway): stuck-loop counter must gate on genuine interruption, not clean drain (#7536) |
| nopr:2ee2e03b68 | gateway | code | 396 | 4 | 1(3) | 8/77 | 1/4 | 2026-07-10 | feat(gateway): resume-request dropbox — external resume asks without touching sessions.json |
| nopr:ec4b3176ff | gateway | code | 389 | 2 | 0(0) | 2/65 | 0/3 | 2026-07-08 | feat(gateway): narrow code-skew guard to in-process import oracle (A1a) |
| #746 | gateway | code | 382 | 5 | 3(3) | 0/23 | 0/0 | 2026-09-20 | feat(gateway): agent.resume_interrupted_turns=always — continue a sibling turn past an incomplete mu |
| #96 | gateway | code | 377 | 2 | 1(3) | 0/21 | 0/1 | 2026-06-22 | fix(gateway): recognize reboot_interrupted as a first-class auto-resume reason (#96) |
| #710 | plugins/platforms | code | 365 | 4 | 1(2) | 2/9 | 0/0 | 2026-09-19 | fix(discord): thread auto_archive_duration 1440 -> 10080 (retire idle-thread archiver) (#710) |
| #581 | gateway | code | 362 | 3 | 1(2) | 5/41 | 0/2 | 2026-08-11 | [verified] fix(gateway): reap never-persisted routing stubs (#581) |
| #104 | gateway | code | 350 | 3 | 2(3) | 0/25 | 0/1 | 2026-06-25 | fix(gateway): revive the dead session auto-reset chat notice (#104) |
| #129 | plugins/platforms | code | 348 | 2 | 1(2) | 8/56 | 1/5 | 2026-06-30 | feat(discord): opt-in raw-reaction journal for durable triage state (#129) |
| #427 | gateway | code | 342 | 3 | 1(3) | 5/30 | 0/3 | 2026-07-25 | fix(gateway): stop double-posting the STT transcript echo for one voice message (#427) |
| #666 | gateway | code | 339 | 3 | 1(2) | 3/70 | 0/3 | 2026-09-10 | fix(gateway): redirect shape-only legacy session aliases at store load, before startup replay can wr |
| #757 | plugins/platforms | code | 329 | 2 | 1(2) | 3/47 | 1/6 | 2026-09-20 | fix(discord): surface discord.py heartbeat-blocked events as a structured PHASE=event_loop_blocked l |
| #869 | gateway | code | 328 | 3 | 0(0) | 0/16 | 0/0 | 2026-09-22 | fix(gateway): move the whole boot lifecycle record off the event loop (ratchet 18 -> 17) (#869) |
| #339 | gateway | code | 328 | 19 | 3(3) | 1/59 | 0/1 | 2026-07-14 | fix(undo): refuse /undo·/redo while a /stop'd turn is still draining (#339) |
| #759 | gateway/platforms | code | 326 | 4 | 1(3) | 3/34 | 2/8 | 2026-09-20 | fix(gateway): stop blocking the event loop with the system-proxy probe and boot recovery (#759) |
| #175 | other:tui_gateway | code | 322 | 6 | 2(3) | 7/47 | 1/1 | 2026-07-02 | feat(tui_gateway): client-identity source so desktop/dashboard/mobile aren't all "tui" (#175) |
| #1004 | gateway | code | 315 | 4 | 1(3) | 10/79 | 0/6 | 2026-09-24 | fix(gateway): a SIGKILLed safe-restart is announced as planned, not UNPLANNED (#1004) |
| #231 | gateway | code | 313 | 18 | 2(3) | 1/24 | 0/0 | 2026-07-07 | feat(merge): summarize only post-branch delta + post note to target's origin (#231) |
| #72 | gateway | code | 308 | 2 | 2(3) | 2/54 | 0/2 | 2026-06-21 | fix(gateway): close the F2 self-completing-restart-loop gap (A' record-mark-at-gate + C1 skill detec |
| #256 | gateway | code | 300 | 4 | 3(3) | 10/52 | 1/2 | 2026-07-10 | fix(gateway): bound the startup-restore inbound gate on a slow boot-resume turn (#256) |
| nopr:2f530dd026 | gateway | code | 299 | 4 | 3(3) | 4/45 | 0/3 | 2026-06-18 | feat(gateway): runtime-footer provider_model, context_full, reasoning fields |
| nopr:11f8a67f01 | gateway | code | 294 | 3 | 2(3) | 9/42 | 0/1 | 2026-06-21 | feat(runtime-footer): add msgs field (raw count vs hygiene hard-limit) |
| nopr:0da8ba5356 | gateway | code | 289 | 2 | 1(3) | 1/14 | 0/1 | 2026-07-10 | fix(gateway): protect boot-resume recovery turns from busy-input interrupts |
| #356 | gateway | code | 285 | 5 | 5(3) | 1/31 | 0/1 | 2026-07-15 | fix(resume): don't persist an empty user row on auto-resume (the /undo '(no text)' bug) (#356) |
| #396 | gateway | code | 269 | 2 | 1(3) | 6/23 | 0/2 | 2026-07-18 | fix(gateway): ack a message queued during startup-restore (re-land of #258) (#396) |
| #306 | gateway | code | 264 | 6 | 4(3) | 5/38 | 0/3 | 2026-07-11 | fix(gateway): clear stale resume markers (#306) |
| #906 | gateway | code | 263 | 6 | 3(3) | 3/43 | 3/4 | 2026-09-23 | fix(gateway): record one restart-loop ledger entry per process boot, not per resume scan (#906) |
| #936 | gateway | code | 259 | 3 | 1(3) | 5/39 | 5/6 | 2026-09-23 | fix(gateway): remove the turn-body executor cap — admission is the only concurrency control (Ace rul |
| #86 | gateway | code | 258 | 3 | 3(3) | 6/23 | 0/1 | 2026-06-22 | feat(gateway): F2 breadcrumb backlog cleanup (config family, contract gate, watcher guardrail) (#86) |
| #843 | gateway/platforms | code | 251 | 3 | 1(3) | 3/33 | 0/2 | 2026-09-22 | fix(gateway): make the delivery-ack barrier registry generation-ordered and scoped (#843) |
| #173 | gateway | code | 250 | 3 | 2(3) | 4/35 | 0/0 | 2026-07-02 | feat(gateway): granular CompactionStats breakdown for manual /compress (#173) |
| #333 | other:tui_gateway | code | 243 | 10 | 1(3) | 10/56 | 0/1 | 2026-07-14 | feat(desktop): /footer command — runtime-metadata footer on desktop turns (#333) |
| #140 | gateway | code | 238 | 4 | 2(3) | 5/47 | 0/2 | 2026-06-30 | fix(gateway): hard-exit after graceful shutdown so a wedged worker can't strand the gateway (#140) |
| #358 | other:tui_gateway | code | 237 | 2 | 2(3) | 10/18 | 0/0 | 2026-07-15 | fix(gateway): reject empty prompt.submit + skip no-op model-switch side effects (#358) |
| #184 | gateway | code | 234 | 2 | 1(1) | 1/39 | 1/3 | 2026-07-04 | fix(gateway): narrow stale-code /model guard to runtime Python changes only (#184) |
| nopr:ba371ef32f | plugins/platforms | code | 233 | 2 | 1(2) | 2/20 | 0/0 | 2026-05-29 | fix(discord): let free-response channels quote bot mentions |
| #403 | gateway | code | 223 | 3 | 2(3) | 5/42 | 0/2 | 2026-07-19 | fix(footer): honor per-model reasoning_overrides and fallback per-entry effort (#403) |
| #1007 | gateway | code | 220 | 2 | 1(3) | 1/5 | 0/0 | 2026-09-24 | fix(gateway): refuse to start a turn whose generation was invalidated during pre-flight (#1007) |
| #452 | gateway | code | 218 | 20 | 3(3) | 1/37 | 0/1 | 2026-07-27 | feat(compress): interim progress ack + per-event LCM compaction telemetry (#452) |
| #627 | gateway | code | 217 | 6 | 4(3) | 1/4 | 0/0 | 2026-08-20 | fix(agent): manual compression banners name their trigger at the chokepoint (#627) |
| nopr:cf58afc340 | gateway | code | 217 | 3 | 2(3) | 2/77 | 0/2 | 2026-07-08 | feat(model): announce mid-session /model switch in-chat (A4 axis A) |
| #899 | gateway | code | 213 | 5 | 2(3) | 4/30 | 1/2 | 2026-09-23 | feat(gateway): make the user-turn reserve a config knob (gateway.user_turn_reserve) (#899) |
| #316 | gateway | code | 212 | 19 | 4(3) | 1/26 | 0/0 | 2026-07-12 | fix(compress): distinguish a persist-FAILURE from a genuine no-op (#44794) (#316) |
| #705 | gateway | code | 210 | 3 | 1(3) | 3/27 | 0/1 | 2026-09-16 | fix(gateway): --replace waits the full drain budget before SIGKILL (state.db corruption class) (#705 |
| #742 | gateway | code | 207 | 2 | 1(3) | 0/12 | 0/0 | 2026-09-20 | fix(gateway): /model switch note names the LIVE route, not the stale override (#742) |
| nopr:7fabbdba23 | gateway | code | 207 | 4 | 1(3) | 0/24 | 0/0 | 2026-08-19 | fix(gateway): dropbox resume requests stamp kind=self and carry the handoff — self-restart resume no |
| #34 | plugins/platforms | code | 201 | 2 | 1(2) | 6/27 | 1/1 | 2026-06-11 | fix(discord): stop typing indicator getting stuck on rate-limited channels (#34) |
| #750 | gateway | code | 200 | 4 | 1(3) | 2/10 | 0/0 | 2026-09-20 | fix(gateway): boot-resume gate looks past session_meta; cron-only drain overrun keeps .clean_shutdow |
| #304 | gateway | code | 197 | 3 | 3(2) | 1/30 | 0/1 | 2026-07-11 | fix(kanban): stop false "profile health" stall warning when dispatcher is throttled (#304) |
| #493 | gateway | code | 193 | 2 | 1(3) | 4/26 | 0/1 | 2026-08-07 | feat(gateway): config toggle for the model-switch stale-code guard (#493) |
| #884 | gateway | code | 189 | 3 | 1(3) | 8/55 | 0/2 | 2026-09-23 | fix(gateway): a safe-restart we REQUESTED is not an unexplained death — suppress the boot notice (#8 |
| #352 | other:tui_gateway | code | 187 | 4 | 1(3) | 3/43 | 0/1 | 2026-07-15 | fix(desktop): stop live-sync duplicating every message on send (#352) |
| #527 | gateway | code | 186 | 2 | 2(3) | 4/28 | 0/0 | 2026-08-08 | fix(compress): inherit resident live route (#527) |
| #584 | plugins/platforms | code | 179 | 2 | 0(0) | 3/19 | 0/1 | 2026-08-11 | fix(telegram): retry transient media downloads (#584) |
| #976 | gateway | code | 173 | 3 | 2(3) | 6/11 | 0/2 | 2026-09-24 | fix(gateway): keep AIAgent construction + skill scan off the asyncio thread (Discord heartbeat stall |
| #405 | gateway | code | 172 | 8 | 8(3) | 1/23 | 1/1 | 2026-07-19 | refactor: unify r:<effort> display-label mapping into one chokepoint (reasoning_label) (#405) |
| nopr:a11aecf67b | gateway | code | 164 | 2 | 1(3) | 0/4 | 0/0 | 2026-07-08 | feat(gateway): post-restart auto-closeout nudge on resume note, cache-safe (A5-B) |
| #751 | gateway | code | 161 | 2 | 1(3) | 0/8 | 1/1 | 2026-09-20 | fix(gateway): resolve Discord chat types for session-key migration concurrently (311 serial lookups  |
| nopr:6e862490c8 | gateway | code | 159 | 4 | 3(3) | 6/17 | 0/0 | 2026-07-10 | fix(gateway): complete fork/upstream reconciliation for slice-5 CI reds |
| #1031 | gateway | code | 155 | 3 | 2(3) | 0/3 | 0/0 | 2026-09-25 | fix(gateway): persisted route lookup off the event loop; one-hop SessionStore ratchet (t_ac9e21cf) ( |
| #589 | gateway | code | 153 | 4 | 2(2) | 0/21 | 0/2 | 2026-08-13 | harden(kanban): single-source the creator-stamp shape rule + contract tests (#589) |
| #691 | gateway | code | 149 | 3 | 2(3) | 1/12 | 0/0 | 2026-09-13 | fix(gateway): honor durable chat pins in reset banners (#691) |
| #19 | gateway | code | 149 | 2 | 1(3) | 2/10 | 0/0 | 2026-06-08 | fix(gateway): bind originating channel into session context for plugin slash commands (#19) |
| #687 | other:tui_gateway | code | 149 | 4 | 3(3) | 0/9 | 0/0 | 2026-09-13 | fix(delegation): retain refused completions and shutdown claims (#687) |
| #694 | gateway | code | 146 | 2 | 1(3) | 1/25 | 0/2 | 2026-09-14 | fix(gateway): count only user messages in the /queue "(N queued)" readout (#694) |
| #401 | gateway | code | 146 | 2 | 1(3) | 0/3 | 0/0 | 2026-07-19 | fix(gateway): /stop cancels pending clarify prompts so the next message isn't swallowed (#401) |
| #197 | gateway | code | 130 | 20 | 3(3) | 1/24 | 0/1 | 2026-07-05 | feat(gateway): show reasoning effort in /model switch confirmation (#197) |
| nopr:c05a81d90a | gateway | code | 126 | 2 | 1(3) | 0/2 | 0/0 | 2026-07-11 | fix(gateway): boot-resume marker must survive the SENTINEL phase of the recovery turn |
| nopr:0f64a3653b | gateway | code | 125 | 2 | 1(3) | 1/2 | 0/0 | 2026-07-10 | fix(gateway): successful turns reset the stuck-loop restart counter again |
| #453 | gateway | code | 124 | 2 | 1(3) | 0/3 | 0/0 | 2026-07-27 | fix(gateway): bind the real profile in session context, not "" (#453) |
| #939 | gateway | code | 113 | 5 | 2(3) | 0/7 | 0/0 | 2026-09-23 | fix(gateway): boot-resume fan-out is unbounded by default — every restart-interrupted session resume |
| #290 | gateway | code | 112 | 4 | 1(3) | 1/8 | 0/0 | 2026-07-10 | fix(gateway): harden auto-resume durability and scope (#290) |
| #777 | plugins/platforms | code | 109 | 3 | 1(2) | 0/10 | 0/1 | 2026-09-21 | fix(discord): port command-sync retry review fixes (#777) |
| #934 | gateway | code | 107 | 3 | 1(3) | 0/20 | 1/1 | 2026-09-23 | fix(gateway): size the turn-body executor to turn admission — admitted turns queued 2151-2356 s behi |
| #904 | gateway | code | 102 | 5 | 1(3) | 0/13 | 0/0 | 2026-09-23 | fix(gateway): ONE restart line per boot — planned names who/how, unplanned names the cause; queue-ac |
| #520 | gateway | code | 98 | 2 | 1(3) | 1/10 | 0/0 | 2026-08-08 | fix(gateway): restore served provider + reasoning in turn result; re-wire served-route persist (#520 |
| nopr:6dc72a75d4 | gateway | code | 96 | 2 | 1(3) | 2/4 | 0/0 | 2026-07-10 | fix(gateway): synthetic internal events must never impersonate the user |
| #334 | gateway | code | 93 | 5 | 3(3) | 8/13 | 1/2 | 2026-07-14 | feat(footer): opt-in 'latency' field — wall-clock turn duration (#334) |
| #745 | gateway | code | 91 | 2 | 1(3) | 1/1 | 0/0 | 2026-09-20 | fix(gateway): persist active_agent_keys when a turn is promoted sentinel -> agent (#745) |
| #460 | gateway | code | 86 | 2 | 1(3) | 1/3 | 0/0 | 2026-07-27 | fix(gateway): clear resume_pending for sessions that finished during a timed-out drain (#460) |
| #53 | gateway | code | 84 | 6 | 2(3) | 3/11 | 0/0 | 2026-06-16 | fix(gateway): classify credential-resolution errors as auth failures (#53) |
| #230 | plugins/platforms | code | 83 | 2 | 1(2) | 1/3 | 0/0 | 2026-07-07 | fix(discord): populate parent_chat_id on native slash events in threads (#230) |
| nopr:4ed79b6dad | gateway/platforms | code | 81 | 3 | 0(0) | 1/16 | 0/0 | 2026-05-29 | fix(telegram): make network-reconnect ladder configurable, raise default tolerance |
| #357 | other:tui_gateway | code | 78 | 7 | 1(3) | 2/22 | 0/0 | 2026-07-15 | fix(desktop): ship the runtime footer as metadata, not message text (#357) |
| #935 | gateway | code | 77 | 2 | 1(3) | 0/1 | 0/0 | 2026-09-23 | fix(gateway): route the stderr log handler through the async QueueListener — a WARNING on the event  |
| #404 | gateway | code | 77 | 2 | 2(3) | 0/7 | 0/0 | 2026-07-19 | fix(compress): manual /compress granular Model line shows session-truthful r:<effort> (#404) |
| #626 | gateway | code | 75 | 2 | 1(3) | 1/4 | 0/0 | 2026-08-20 | fix(gateway): manual /compress banner names its trigger (#626) |
| nopr:f29eede0e2 | gateway | code | 75 | 2 | 1(3) | 2/12 | 0/0 | 2026-07-11 | fix(gateway): hand back the resume pre-claim WITHOUT stripping protection |
| #571 | other:tui_gateway | code | 75 | 2 | 2(3) | 3/11 | 0/1 | 2026-08-10 | fix(tui-gateway): redact serve prompt fallback logs (#571) |
| #698 | other:tui_gateway | code | 73 | 2 | 2(3) | 2/11 | 0/1 | 2026-09-14 | fix(tui): retain a refused kanban batch instead of dropping it (#698) |
| #984 | gateway | code | 70 | 2 | 1(3) | 2/12 | 0/0 | 2026-09-24 | fix(gateway): internal events reuse the pinned session-context prompt (prefix-cache A→B→A) (#984) |
| #395 | gateway | code | 70 | 2 | 1(3) | 2/5 | 0/0 | 2026-07-18 | fix(gateway): draining gateway must not consume resume-dropbox requests (#395) |
| #183 | gateway | code | 67 | 2 | 1(3) | 1/7 | 0/0 | 2026-07-03 | feat(gateway): show reasoning effort in the session-reset banner (#183) |
| #911 | gateway | code | 65 | 2 | 1(3) | 0/1 | 0/0 | 2026-09-23 | fix(gateway): loop-wakeup watcher reads state_meta off the event loop (10-50 s loop blocks; Discord  |
| #422 | gateway | code | 61 | 7 | 1(3) | 2/9 | 1/1 | 2026-07-25 | fix(test): hermetic env-detection tests — pin container/supervisor/HOME probes (#422) |
| nopr:5378b19f73 | gateway | code | 60 | 2 | 1(3) | 1/13 | 0/0 | 2026-07-08 | fix(fallback): address Greptile review on the recovery-announce helper |
| #633 | gateway | code | 54 | 2 | 1(3) | 1/12 | 1/1 | 2026-08-20 | fix(gateway): bound Discord reconnect backoff (#633) |
| #635 | plugins/platforms | code | 43 | 2 | 0(0) | 0/8 | 0/0 | 2026-08-22 | fix(telegram): give media downloads their own generous per-request timeouts (#635) |
| nopr:3d5ecfa0ae | gateway | code | 33 | 1 | 1(3) | 1/2 | 0/0 | 2026-07-10 | fix(gateway): restore fork systemd exit-0 restart branch (Linux CI slice 3) |
| #163 | gateway | code | 26 | 2 | 1(3) | 0/2 | 0/0 | 2026-07-01 | fix(gateway): correct reaper design comment (_touch_activity is NOT dead code) + hoist compaction_st |
| #138 | gateway | code | 22 | 2 | 2(3) | 0/10 | 0/0 | 2026-06-30 | chore(gateway): address Greptile review — observable non-int row-id + None-vs-empty comment (#138) |
| nopr:0fee1b8b64 | gateway | code | 21 | 1 | 1(2) | 1/7 | 0/0 | 2026-07-10 | fix(gateway): per-token exception guard in restore_session_vars (review) |
| nopr:4c595fc6f1 | gateway | code | 15 | 3 | 2(3) | 0/7 | 0/0 | 2026-06-20 | fix(gateway): pass agent provider into runtime-footer result dict |
| nopr:59429f36db | gateway | code | 11 | 1 | 1(3) | 0/2 | 0/0 | 2026-05-28 | fix(gateway): clear typing indicator on stale-result early-return path |
