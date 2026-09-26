# Tranche: auto — 167 rows

absorb = substantive added src lines found verbatim on upstream/main (hit/total); sym = added def/class names found. Signal only — read README §0.

| key | subsystem | kind | loc | files | conflict-files (syncs) | absorb src | sym | date | subject |
|---|---|---|---|---|---|---|---|---|---|
| #66 | tests | tests-only | 13318 | 13 | 4(3) | 0/4 | 0/0 | 2026-06-20 | test(run_agent): split monolithic test_run_agent.py to fix CI shard timeout (#66) |
| #191 | docs | docs-only | 1351 | 3 | 0(0) | 0/0 | 0/0 | 2026-07-03 | docs(desktop): add the three 2026-07-02 PRDs (eventloop starvation, remote-mode correctness, pinned- |
| nopr:c1bc4313f4 | tests | tests-only | 739 | 3 | 0(0) | 0/0 | 0/0 | 2026-07-16 | test(compaction): cover fork formatter branches |
| nopr:0ae45ecc1a | tests | tests-only | 548 | 3 | 0(0) | 0/0 | 0/0 | 2026-07-16 | Add tool gate refactor golden capture |
| nopr:b880a46731 | tests | tests-only | 539 | 3 | 0(0) | 0/0 | 0/0 | 2026-07-16 | test: capture state ext refactor golden |
| nopr:a95008d49c | tests | tests-only | 517 | 3 | 0(0) | 0/0 | 0/0 | 2026-07-16 | test(compaction): capture fork extension golden behavior |
| nopr:4b5a1f9720 | tests | tests-only | 503 | 3 | 0(0) | 0/0 | 0/0 | 2026-07-16 | test(cron): capture scheduler fork extension golden |
| nopr:1d00ec3208 | tests | tests-only | 470 | 2 | 0(0) | 0/0 | 0/0 | 2026-07-08 | test(model): E2E behavior-contract tests for A4 axes A/B/C |
| #756 | tests | tests-only | 466 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-23 | test(ci): gate the residual live-comment constants and seams (t_a66612e1) (#756) |
| #94 | tests | tests-only | 444 | 7 | 1(1) | 0/0 | 0/0 | 2026-06-22 | test(gateway): make tests/gateway order-independent under -p randomly (5 leak classes) (#94) |
| #159 | tests | tests-only | 430 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-01 | test(telegram): lock restart/reconnect message-loss parity (regression guard) (#159) |
| #449 | tests | tests-only | 426 | 2 | 0(0) | 0/0 | 0/0 | 2026-07-27 | test: salvage wave3b coverage round 2 (compression persist-failure + delegation parking) (#449) |
| #601 | tests | tests-only | 409 | 1 | 0(0) | 0/0 | 0/0 | 2026-08-18 | test(e2e): replay multimodal 413 incident chain (#601) |
| #458 | tests | tests-only | 397 | 12 | 2(1) | 0/0 | 0/0 | 2026-07-27 | test: convert 12 wall-clock Python test assertions to deterministic signals (#458) |
| #866 | tests | tests-only | 384 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-23 | test(kanban): pin that carrying a ref does not cost the bundle beside it (#866) |
| #810 | tests | tests-only | 347 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-21 | test(delegation): pin that background children survive a mid-turn parent interrupt (#810) |
| #723 | tests | tests-only | 327 | 4 | 0(0) | 0/0 | 0/0 | 2026-09-19 | test: convert 11 wall-clock flake assertions to deterministic witnesses (batch 2, groups 1-4) + cens |
| nopr:164f4ee2ba | docs | docs-only | 325 | 2 | 0(0) | 0/0 | 0/0 | 2026-07-16 | docs(sync): parity-tooling + fork-mergeability specs (Momus-reviewed: tooling v0.4 clean APPROVE aft |
| #542 | tests | tests-only | 301 | 5 | 2(2) | 0/0 | 0/0 | 2026-08-09 | test(hermetic): gate on unrestored sys.modules purges (+2 leakers) (#542) |
| nopr:9c8811a95c | docs | docs-only | 288 | 4 | 0(0) | 0/0 | 0/0 | 2026-06-17 | spec(PRD-8): Aegis LCM store snapshot+reset for clean Phase-2 benchmark |
| #718 | tests | tests-only | 277 | 5 | 0(0) | 0/0 | 0/0 | 2026-09-19 | test: convert 8 wall-clock flake assertions to deterministic witnesses (census of all 98) (#718) |
| nopr:4225032f59 | docs | docs-only | 276 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-21 | docs(send_message): PRD for the in-turn home-channel leak fix (PR Kyzcreig#71) |
| #540 | tests | tests-only | 270 | 2 | 0(0) | 0/0 | 0/0 | 2026-08-09 | test(hermetic): fully contain fresh-import module purges (#540) |
| #343 | tests | tests-only | 255 | 3 | 0(0) | 0/0 | 0/0 | 2026-07-14 | test(blackbox): cover MoA attribution end to end (#343) |
| #82 | tests+docs | tests+docs-only | 255 | 4 | 1(2) | 0/0 | 0/0 | 2026-06-22 | fix(tests): raise soft FD limit for single-process suite runs + QA Phase-0 guards (#82) |
| #724 | tests+docs | tests+docs-only | 250 | 7 | 1(1) | 0/44 | 0/0 | 2026-09-19 | fix(ci): install checksum-pinned binaries for both Linux architectures (#724) |
| #551 | tests | tests-only | 243 | 1 | 1(1) | 0/0 | 0/0 | 2026-08-09 | test(mem0): salvage 12 Wave-2 coverage tests from a stranded branch (#551) |
| #606 | tests | tests-only | 231 | 4 | 2(2) | 0/0 | 0/0 | 2026-08-18 | test(kanban): restore sys.modules after the isolated-HERMES_HOME purge (#606) |
| #564 | tests | tests-only | 231 | 1 | 0(0) | 0/0 | 0/0 | 2026-08-10 | test(config): sweep EVERY compression knob for arrival, not one knob per incident (#564) |
| #132 | tests | tests-only | 228 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-30 | test(mem0): live e2e proving bg-review fork actually writes mem0 (#132) |
| #438 | tests | tests-only | 226 | 5 | 1(1) | 0/0 | 0/0 | 2026-07-26 | test: replace wall-clock assertions with deterministic signals (5 flakes) (#438) |
| nopr:25db74c2a1 | docs | docs-only | 223 | 2 | 0(0) | 0/0 | 0/0 | 2026-07-16 | docs(sync): fork-mergeability refactor spec v0.3 + build brief (Momus AWC-folded) |
| #114 | tests | tests-only | 215 | 2 | 2(2) | 0/0 | 0/0 | 2026-06-27 | fix(tests): strip non-sandbox log handlers so tests cant write the live agent.log (#114) |
| #549 | tests | tests-only | 213 | 5 | 1(1) | 0/0 | 0/0 | 2026-08-09 | test: convert remaining TRUE-FLAKE-RISK wall-clock assertions to ordering witnesses (#438 family) (# |
| nopr:9c6e0b4eff | tests | tests-only | 210 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-08 | test(ci): E2E no-op guard proof — zero-collect RED, marker-filter GREEN, mutation-proof (A5-A) |
| #572 | tests | tests-only | 206 | 1 | 0(0) | 0/0 | 0/0 | 2026-08-10 | test(hermeticity): guard the routing WRITE path against the production state.db (#572) |
| #454 | tests | tests-only | 195 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-27 | test(mem0): add dedicated coverage for temporal_parse window parsing (#454) |
| #885 | tests | tests-only | 192 | 2 | 0(0) | 0/0 | 0/0 | 2026-09-22 | test(gateway): make two loop-liveness tests load-independent (CI flakes) (#885) |
| #264 | tests | tests-only | 189 | 1 | 1(2) | 0/0 | 0/0 | 2026-07-10 | test: regression suite for the Claude-proxy context-length resolution chain (#264) |
| nopr:088bafc6cc | tests | tests-only | 189 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-19 | test(lcm): e2e legacy-DB migration tests against real MessageStore init |
| #89 | tests | tests-only | 169 | 8 | 2(1) | 0/0 | 0/0 | 2026-06-22 | test(gateway): fix single-process cross-file pollution + macOS platform false-fails (#89) |
| #550 | tests | tests-only | 168 | 3 | 0(0) | 0/0 | 0/0 | 2026-08-09 | fix(tests): stop tests/ shadowing real packages on sys.path; de-flake LSP wait (#550) |
| nopr:273707c004 | tests | tests-only | 166 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-20 | test(compaction-announce): real LCM-engine done-site e2e (Phase 0+2b) |
| #775 | tests | tests-only | 165 | 5 | 0(0) | 0/0 | 0/0 | 2026-09-21 | test: arm 4 inert tuple-expression assertions + AST gate for the class (#775) |
| #561 | tests | tests-only | 157 | 1 | 0(0) | 0/0 | 0/0 | 2026-08-10 | test(fd-drain): fix the FLAKY burn_fds fixture that made the high-fd guard red (#561) |
| #365 | tests | tests-only | 157 | 3 | 0(0) | 0/0 | 0/0 | 2026-07-15 | fix(tests): self-isolate the cron test files that leak fixture jobs into a live store (#365) |
| #762 | tests | tests-only | 156 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-20 | test(kanban): convert init-lock wall-clock bounds to ordering witnesses (#762) |
| #864 | tests | tests-only | 151 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-23 | test(gateway): gate the /stop WIRING and the rowid boundary, not just the mechanism (#864) |
| #663 | tests | tests-only | 150 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-10 | test(gateway): pin single-producer chat_type="channel" on Teams/Telegram/HA + adapter literal lint ( |
| #90 | tests | tests-only | 149 | 3 | 1(1) | 0/0 | 0/0 | 2026-06-22 | test(gateway): fix the dominant random-order pollution class (sys.argv collision) (#90) |
| #442 | tests | tests-only | 144 | 2 | 0(0) | 0/0 | 0/0 | 2026-07-26 | test: salvage wave3b coverage — Slack internal-event path + delegation parking boundaries (#442) |
| #599 | tests | tests-only | 137 | 1 | 1(1) | 0/0 | 0/0 | 2026-08-18 | test(requests): cover rebuilt Anthropic relay body budget (#599) |
| #546 | tests | tests-only | 135 | 5 | 2(2) | 0/0 | 0/0 | 2026-08-09 | test: harden macOS full-load hermeticity (#546) |
| #852 | tests | tests-only | 134 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-22 | test(kanban): pin partial loss x unverifiable survivor to the single upstream raise (#852) |
| nopr:ca9da42ce9 | tests | tests-only | 130 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-14 | test(browser): fleet-wide guard — every detached-Chrome launcher must suppress the macOS keychain mo |
| nopr:9da4b4005f | docs | docs-only | 125 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-01 | docs(sync): fold #120 CI-gate-traps reference into the sync PR |
| #481 | tests | tests-only | 123 | 2 | 1(1) | 0/0 | 0/0 | 2026-08-06 | fix(tests): un-skip 4 no-mock restart-cascade E2E tests that went dark (#481) |
| #417 | docs | docs-only | 118 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-24 | docs(sync): add fork parity doctrine (#417) |
| #145 | tests | tests-only | 117 | 1 | 1(2) | 0/0 | 0/0 | 2026-06-30 | test(gateway): fix flaky tui session.create race tests + drain leaked build threads (#145) |
| #478 | tests | tests-only | 116 | 2 | 1(2) | 0/0 | 0/0 | 2026-08-06 | fix(tests): isolate gateway session ContextVars across test files (#478) |
| #1058 | tests+docs | tests+docs-only | 114 | 2 | 1(1) | 0/0 | 0/0 | 2026-09-25 | test(canary): a parity merge must not bring back import-time plugin discovery (t_39f3a794) (#1058) |
| #881 | tests | tests-only | 112 | 2 | 0(0) | 0/0 | 0/0 | 2026-09-22 | fix(gitignore): make the venv patterns symlink-safe so a deploy tree reads clean (#881) |
| nopr:08fc3aff65 | tests | tests-only | 109 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-08 | fix(test): rewrite A3 guard test as a proper pytest module (was a script → CI collection abort) |
| #892 | tests | tests-only | 107 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-23 | test(cli): the title-resume-hint gate could not see naive quoting (#892) |
| nopr:334ae3a402 | tests+docs | tests+docs-only | 107 | 4 | 0(0) | 0/0 | 0/0 | 2026-07-16 | fix(golden): address Greptile P1s — corpus per-model override case used a wrong key+shape (agent.mod |
| #696 | tests | tests-only | 105 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-14 | test(delegation): quiesce persistence fixture workers before reset (#696) |
| #92 | tests | tests-only | 104 | 2 | 1(2) | 0/0 | 0/0 | 2026-06-22 | test: de-flake hardline (HERMES_ALLOW_REBOOT env leak) + delegate heartbeat (timing brittleness) (#9 |
| #42 | tests+docs | tests+docs-only | 102 | 2 | 0(0) | 0/23 | 0/0 | 2026-06-14 | ci(fleet): add posture-B floor — SAST (semgrep) + secret-scan (gitleaks) (#42) |
| nopr:9454c21830 | tests | tests-only | 101 | 2 | 1(2) | 0/0 | 0/0 | 2026-06-19 | test(hermetic): eliminate remaining cross-file pollution in tests/agent |
| #472 | tests+docs | tests+docs-only | 101 | 23 | 2(2) | 1/6 | 0/0 | 2026-08-06 | ci: route runs-on through CI_RUNNER_LABELS var (self-hosted fallback) (#472) |
| nopr:d1fa2514c1 | tests | tests-only | 100 | 2 | 1(2) | 0/0 | 0/0 | 2026-06-19 | test(hermetic): reset auxiliary_client runtime caches between tests |
| #755 | tests | tests-only | 95 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-20 | test(cli): stop the width-probe fixtures patching global shutil (-v INTERNALERROR) (#755) |
| nopr:3571713165 | tests | tests-only | 95 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-19 | test(compression): add request-level thrash integration test (Phase C) |
| #463 | tests | tests-only | 92 | 2 | 0(0) | 0/8 | 0/0 | 2026-07-28 | fix(ci): run JS autofix as trusted actor (#463) |
| #737 | tests | tests-only | 89 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-20 | test: drive deferred browser cancellation with deterministic timeout ordering (#737) |
| nopr:2278a23ab1 | tests | tests-only | 86 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-19 | test(compression): D-6 failed-summary-over-threshold on real done-path (Phase D) |
| #74 | tests+docs | tests+docs-only | 86 | 1 | 0(0) | 0/25 | 0/0 | 2026-06-21 | fix(fleet-secret-scan): range-independent changed-file scan (#74) |
| #552 | tests | tests-only | 81 | 1 | 1(1) | 0/0 | 0/0 | 2026-08-10 | test(mem0): lock the D-7 interlock's absent-capture fail-safe (#552) |
| nopr:39cec220cd | tests | tests-only | 78 | 3 | 1(2) | 0/0 | 0/0 | 2026-06-19 | test(hermetic): block macOS Keychain + HERMES_REAL_HOME leaks in agent tests |
| #741 | tests+docs | tests+docs-only | 78 | 2 | 1(1) | 0/0 | 0/0 | 2026-09-20 | test(parity): canary + manifest entry so a merge can't silently restore "connection issue" (#741) |
| nopr:20af3832ee | tests+docs | tests+docs-only | 76 | 1 | 0(0) | 1/32 | 0/0 | 2026-06-15 | ci: add fleet CI-fail alert workflow (loud #alerts ping on real failure) |
| #867 | tests | tests-only | 75 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-22 | fix(tests): test_spawn_failure_detail_distinguishes_errno was vacuous — split it into the two real e |
| #734 | tests | tests-only | 74 | 2 | 0(0) | 0/0 | 0/0 | 2026-09-19 | test: deflake the 3 load-sensitive tests blocking #724/#725 (session_hygiene teardown hang, repair-l |
| nopr:c56dd2bc4a | tests | tests-only | 73 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-08 | fix(test): align A5-A guard tests with spec (skip-storm=⚠ not RED) |
| #754 | tests | tests-only | 71 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-20 | test(gateway): drive the four-cases mid-drain release off ordering events, not a 50ms timer (#754) |
| nopr:17d7ab0c01 | tests+docs | tests+docs-only | 70 | 2 | 0(0) | 0/0 | 0/0 | 2026-07-16 | fix(golden): compaction runner imports the extracted module DIRECTLY (Greptile P2 — a re-export-chai |
| #440 | tests | tests-only | 68 | 1 | 1(2) | 0/0 | 0/0 | 2026-07-26 | test(tui_gateway): bind compute-host control stubs to the real signature (#440) |
| #702 | tests | tests-only | 66 | 5 | 0(0) | 0/2 | 0/0 | 2026-09-16 | ci: make lint + nix jobs runnable on the hardened self-hosted pool (#702) |
| #744 | tests | tests-only | 62 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-20 | test(parity): pin that claude-apx reaches the same usage-exhausted label via headers (#744) |
| #776 | tests | tests-only | 61 | 4 | 2(2) | 0/0 | 0/0 | 2026-09-21 | fix(ci): keep gate and plumbing jobs off the test runner pool (#776) |
| nopr:49da9dc43b | tests | tests-only | 59 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-21 | test(memory): harden _reindex_provider — concurrency, no-table-growth, first-provider-wins |
| #743 | tests+docs | tests+docs-only | 58 | 2 | 1(1) | 0/0 | 0/0 | 2026-09-20 | test(parity): pin that the relay and harness halves are NOT redundant (#743) |
| nopr:f4056a607a | tests | tests-only | 57 | 1 | 1(1) | 0/0 | 0/0 | 2026-07-10 | test(compression): make lock-refresh TTL test event-driven, not wall-clock |
| #1042 | docs | docs-only | 55 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-25 | docs(ci): attribution-gate rollback drill + polled re-enable read-back (#1042) |
| #735 | tests | tests-only | 55 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-19 | test: deflake pty_session wall-clock waits into predicate witnesses (#735) |
| #979 | tests | tests-only | 52 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-24 | test(kanban): gate fail-closed arm for a gitfile whose gitdir target exists (#979) |
| #108 | tests | tests-only | 52 | 1 | 1(2) | 0/0 | 0/0 | 2026-06-26 | test(tui): make browser launch-hint test deterministic across hosts (#108) |
| nopr:87b379b0f4 | docs | docs-only | 51 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-16 | docs(lcm): t80 integration evidence pack (8 lanes, 81 tests green) |
| #714 | tests | tests-only | 51 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-19 | test(agent): convert interrupt-detection stopwatch to a deterministic ordering witness (#714) |
| #646 | docs | docs-only | 50 | 1 | 1(1) | 0/0 | 0/0 | 2026-09-01 | docs(sync): record the 2026-08-31 upstream adjudication for 3 fork entries (#646) |
| #975 | tests | tests-only | 50 | 7 | 1(1) | 0/0 | 0/0 | 2026-09-24 | ci: place portable required checks on hosted runners (#975) |
| #667 | tests | tests-only | 50 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-10 | test(cron): convert cleanup-timeout stopwatch asserts to ordering witnesses (#667) |
| #732 | tests+docs | tests+docs-only | 50 | 1 | 0(0) | 0/3 | 0/0 | 2026-09-19 | ci(secret-scan): blobless checkout + batched archive (robustness, not speed) (#732) |
| #489 | tests | tests-only | 49 | 1 | 1(2) | 0/0 | 0/0 | 2026-08-07 | test(model-metadata): guard Anthropic 1M models against a fallback-table revert (#489) |
| #1049 | tests | tests-only | 47 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-25 | test(compression): permanent M43 guard — head content-match must not override a stamp (stacked on #9 |
| #538 | tests | tests-only | 47 | 1 | 0(0) | 0/0 | 0/0 | 2026-08-09 | fix(tests): restore sys.modules after the loop-dampening fixture's purge (#538) |
| #789 | tests | tests-only | 46 | 1 | 1(2) | 0/0 | 0/0 | 2026-09-20 | test(tui): stop the WS-orphan-reap lock test timing a lazy SessionDB open (#789) |
| nopr:44791a6342 | tests | tests-only | 46 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-20 | test(compaction-announce): Telegram filter pass-through + single-done-site guards (Task 8) |
| nopr:7d4f9eb63e | docs | docs-only | 45 | 1 | 1(1) | 0/0 | 0/0 | 2026-05-23 | docs: align 1Password skill with fleet service token |
| #1030 | tests | tests-only | 45 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-25 | test(ci-overflow): pin plan() diagnostic/input branches from #931 census (#1030) |
| #715 | tests+docs | tests+docs-only | 45 | 2 | 1(1) | 0/0 | 0/0 | 2026-09-19 | docs(ci): correct the js-tests pin rationale — capacity ceiling falsified by measurement (#715) |
| #210 | tests | tests-only | 42 | 1 | 1(1) | 0/0 | 0/0 | 2026-07-05 | test(interrupt): de-flake test_interrupt_child_during_api_call (wall-clock -> behavior contract) (#2 |
| #896 | tests | tests-only | 40 | 2 | 0(0) | 0/0 | 0/0 | 2026-09-23 | test(cli): one hostile feature per fixture cannot see a quote-style chooser (#896) |
| #645 | tests | tests-only | 40 | 1 | 0(0) | 0/0 | 0/0 | 2026-08-31 | test(cron): make the claim-heartbeat grace check deterministic (#645) |
| #729 | tests | tests-only | 38 | 2 | 0(0) | 0/0 | 0/0 | 2026-09-19 | test(gateway): deflake producer-scan __pycache__ race and suppression-drive timeout (#729) |
| #640 | tests | tests-only | 38 | 1 | 0(0) | 0/0 | 0/0 | 2026-08-27 | fix(tests): evict MagicMock telegram modules before the real-PTB polling suite (#640) |
| #986 | docs | docs-only | 37 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-24 | docs(ci): fleet/attribution required check + rollback (t_ebcec034) (#986) |
| #344 | tests | tests-only | 36 | 2 | 0(0) | 0/0 | 0/0 | 2026-07-14 | test(blackbox): harden MoA attribution seams (#344) |
| #981 | tests | tests-only | 34 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-25 | test: assert ordinary Anthropic request assembly preserves history (#981) |
| #143 | tests | tests-only | 33 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-30 | test(honcho): fix flaky test_stale_pending_result_is_discarded_on_read (init-thread race) (#143) |
| #15 | tests | tests-only | 30 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-06 | test(gateway): de-flake stream-consumer edit-failure tail test (#15) |
| #302 | tests+docs | tests+docs-only | 30 | 1 | 1(1) | 1/7 | 0/0 | 2026-07-11 | ci: fix save-durations artifact collision (per-artifact subdirs, no merge-multiple) (#302) |
| #850 | tests | tests-only | 28 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-22 | test(lcm): pin the fail-open paths of the Codex context-variant host helper (#850) |
| #534 | tests | tests-only | 26 | 1 | 0(0) | 0/0 | 0/0 | 2026-08-09 | test(moa): replace interrupt stopwatch with ordering witness (#534) |
| #85 | tests | tests-only | 26 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-22 | test(send_message): clear Discord forum-probe cache between tests (fix cross-file flake) (#85) |
| nopr:05fc0f5ee6 | tests | tests-only | 25 | 2 | 0(0) | 0/0 | 0/0 | 2026-07-25 | test(sync): xfail two non-merge reds with citations + follow-up cards |
| #668 | tests | tests-only | 24 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-10 | test(cron): exercise cleanup hang through both scheduler ticks (#668) |
| nopr:3f6d86520c | tests | tests-only | 23 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-16 | fix(test): mcp-discovery response deadline must exceed the discovery bound |
| nopr:4620442405 | tests | tests-only | 23 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-08 | fix(test): make A4 axis-C provider-recovery tests env-independent |
| #792 | tests | tests-only | 22 | 2 | 1(1) | 0/2 | 0/0 | 2026-09-21 | fix(ci): pin self-hosted e2e to X64 and emit stall stacks (#792) |
| #498 | tests | tests-only | 19 | 1 | 0(0) | 0/0 | 0/0 | 2026-08-07 | fix(tests): restore the aux-client stub so it stops leaking into later tests (#498) |
| #14 | tests | tests-only | 19 | 1 | 1(1) | 0/0 | 0/0 | 2026-06-06 | test(gateway): de-flake telegram topic reset tip assertion (#14) |
| #719 | tests+docs | tests+docs-only | 19 | 2 | 1(1) | 0/1 | 0/0 | 2026-09-19 | ci: cancel superseded merge_group runs (unblocks the self-hosted pool) (#719) |
| #704 | tests+docs | tests+docs-only | 19 | 1 | 1(1) | 0/0 | 0/0 | 2026-09-16 | docs(ci): record the CI_RUNNER_LABELS split and the pinned-job list (#704) |
| nopr:da373ebab0 | tests | tests-only | 18 | 2 | 0(0) | 0/0 | 0/0 | 2026-07-08 | test(ci): make A5 worktree-bytes guard resilient to worktree name (deploy reconciliation) |
| #155 | tests+docs | tests+docs-only | 18 | 4 | 1(1) | 0/1 | 0/0 | 2026-07-01 | ci: add merge_group triggers so the merge queue can gate PRs (#155) |
| nopr:d0159f3054 | docs | docs-only | 16 | 1 | 1(1) | 0/0 | 0/0 | 2026-07-16 | Register tool gate fork feature manifest |
| #567 | tests | tests-only | 16 | 1 | 0(0) | 0/0 | 0/0 | 2026-08-10 | test(gateway): replace split-brain delivery polling (#567) |
| #8 | tests | tests-only | 16 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-04 | test(file-ops): fix write_file mocks for atomic temp-file+rename path (#8) |
| #4 | tests | tests-only | 16 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-02 | test(models): make anthropic-messages warning test hermetic (#4) |
| #483 | tests | tests-only | 15 | 1 | 1(1) | 0/0 | 0/0 | 2026-08-07 | fix(tests): stop test_run_gateway_refreshes_outdated_unit_on_boot killing the runner (#483) |
| #607 | tests | tests-only | 13 | 2 | 2(2) | 0/0 | 0/0 | 2026-08-18 | test: isolate session search and platform gating state (#607) |
| #548 | tests | tests-only | 13 | 1 | 1(1) | 0/0 | 0/0 | 2026-08-09 | fix(tests): snapshot_watched must not iterate sys.modules directly (#548) |
| nopr:a09af53fdd | docs | docs-only | 11 | 1 | 1(1) | 0/0 | 0/0 | 2026-07-16 | docs(sync): track compaction fork extension |
| #628 | tests | tests-only | 11 | 1 | 0(0) | 0/0 | 0/0 | 2026-08-20 | fix(tests): deflake test_exhausted_patience_names_the_real_cause — pin busy_timeout below the shrunk |
| #451 | tests+docs | tests+docs-only | 10 | 1 | 0(0) | 0/3 | 0/0 | 2026-07-27 | ci(secret-scan): use merge_group base/head SHAs -- unbreak every queue batch (#451) |
| #355 | tests+docs | tests+docs-only | 10 | 1 | 0(0) | 0/2 | 0/0 | 2026-07-15 | chore(gitignore): ignore TypeScript compile output next to src (.js/.js.map) (#355) |
| #574 | tests | tests-only | 9 | 1 | 0(0) | 0/0 | 0/0 | 2026-08-11 | test(hermeticity): document marker-scrubbing guard boundary (#574) |
| #558 | tests | tests-only | 9 | 1 | 1(1) | 0/0 | 0/0 | 2026-08-10 | test(tools): isolate terminal path state between tests (#558) |
| #485 | tests | tests-only | 9 | 1 | 1(1) | 0/0 | 0/0 | 2026-08-07 | fix(tests): stop test_web_server_host_header shadowing the real hermes_cli package (#485) |
| #382 | tests | tests-only | 9 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-18 | test(tui): harden slash worker MCP response timeout (#382) |
| #50 | tests | tests-only | 9 | 1 | 1(1) | 0/0 | 0/0 | 2026-06-15 | test(mem0): skip self-host tests when optional mem0 package absent (#50) |
| nopr:0d0c28159d | tests | tests-only | 9 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-16 | fix(test): bisect six-fixture NONDETERMINISTIC corpus — per-SIDE parity counters |
| nopr:bcfb58e455 | tests | tests-only | 9 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-16 | fix(test): bisect six-fixture NONDETERMINISTIC corpus — per-SIDE parity counters |
| nopr:9b5c47f56a | tests | tests-only | 8 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-16 | fix(golden): state_ext runner — lazy hermes_state import in the pre-extraction fallback (Greptile P2 |
| nopr:018cd3c078 | tests+docs | tests+docs-only | 8 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-16 | Acknowledge tool gate manifest vacuous coverage |
| #648 | tests | tests-only | 7 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-04 | fix(test): kanban per-profile-cap test re-applies the memory-guard seam after its module purge (#648 |
| #409 | tests | tests-only | 7 | 1 | 1(2) | 0/0 | 0/0 | 2026-07-20 | test(tui_gateway): stub _launch_configured_cwd in completion-cwd fallback test (#409) |
| #726 | tests+docs | tests+docs-only | 7 | 1 | 0(0) | 0/0 | 0/0 | 2026-09-19 | ci(label-rerun): run the CI-wait poller on GitHub-hosted, not the self-hosted pool (#726) |
| #661 | tests+docs | tests+docs-only | 7 | 1 | 1(1) | 0/0 | 0/0 | 2026-09-09 | ci: 16 test slices on hosted runners — exit-143 runner reclaims only hit slices past ~7 min (#661) |
| #390 | tests | tests-only | 6 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-18 | test(model): assert builtin-alias survival by contract, not a hardcoded id (#390) |
| nopr:fa1c80bee8 | tests | tests-only | 6 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-16 | fix(test): key the per-side counter by full-path hash — repo.name is 'repo' for BOTH sides (Greptile |
| #91 | docs | docs-only | 5 | 2 | 0(0) | 0/0 | 0/0 | 2026-06-22 | docs(send_message): OQ2 store reconciliation (v2 spec correction) (#91) |
| #725 | tests+docs | tests+docs-only | 4 | 4 | 0(0) | 0/2 | 0/0 | 2026-09-19 | ci: partial-clone the fetch-depth:0 checkouts so history jobs stop timing out (#725) |
| #346 | tests | tests-only | 3 | 1 | 1(1) | 0/0 | 0/0 | 2026-07-14 | test(s6): document macOS mode relaxation (#346) |
| #753 | tests+docs | tests+docs-only | 3 | 1 | 1(1) | 0/1 | 0/0 | 2026-09-20 | fix(ci): include pytest nodeids in slice timeout output (#753) |
| nopr:a377afe934 | tests+docs | tests+docs-only | 2 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-01 | chore(secret-scan): allowlist new upstream test fixtures (approval, codex-responses) so CI applies t |
