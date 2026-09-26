# Tranche: scripts+misc — 117 rows

absorb = substantive added src lines found verbatim on upstream/main (hit/total); sym = added def/class names found. Signal only — read README §0.

| key | subsystem | kind | loc | files | conflict-files (syncs) | absorb src | sym | date | subject |
|---|---|---|---|---|---|---|---|---|---|
| nopr:15e1d95f14 | scripts | code | 4080 | 19 | 0(0) | 0/1 | 0/0 | 2026-06-18 | docs(lcm-qa): preserve Arm-A/Arm-B campaign QA evidence + armab script tweak |
| nopr:26f69158d2 | scripts | code | 2854 | 4 | 0(0) | 0/63 | 0/2 | 2026-06-18 | feat(lcm-arm-b): PRD-8.2 gate split — recovery-correctness vs condensation-reliability |
| nopr:b7cb1472ec | scripts | code | 2618 | 17 | 1(1) | 32/654 | 47/143 | 2026-07-10 | feat(scripts): hermes_parity — parity-sync CLI (status/start/gates/bisect/ack/finish/clean) |
| #747 | scripts | code | 2477 | 5 | 0(0) | 2/142 | 2/2 | 2026-09-20 | ci: harden publish-e2e-evidence with bounded transient tolerance (#747) |
| #736 | scripts | code | 2447 | 3 | 1(1) | 24/284 | 15/25 | 2026-09-20 | ci: make the live-comment poller rate-limit aware (measured 11 -> 2.25 charged calls/cycle) (#736) |
| #268 | scripts | code | 1892 | 17 | 6(3) | 59/562 | 9/17 | 2026-07-10 | feat(livesync): cross-surface live session sync — Phase 1 (session.changes polling) (#268) |
| #780 | other:ui-tui | code | 1681 | 18 | 4(3) | 15/384 | 0/2 | 2026-09-21 | fix(confab-notice): close the 5 FleetReview P1s on the OOB notice consumer (#764 merged mid-run) (#7 |
| #612 | scripts | code | 1636 | 6 | 0(0) | 4/92 | 7/12 | 2026-08-19 | docs: recover stranded July design docs + dashboard PRD v1.6 fold (INV-7 client-disconnect) (#612) |
| #748 | scripts | code | 1625 | 3 | 1(1) | 6/106 | 0/3 | 2026-09-20 | ci: fix FleetReview round-6 poller findings (status loss, stale artifacts, shutdown budget) (#748) |
| #932 | other:receipts | code | 1601 | 4 | 0(0) | 8/143 | 12/20 | 2026-09-23 | CI overflow Phase 0: capability preflight and baseline receipt (#932) |
| #439 | other:gates.jsonl | code | 1504 | 3 | 0(0) | 0/37 | 0/0 | 2026-07-27 | docs(sync): land 2026-07-23 parity evidence — forkdelta review, 257 acks, gate PASS records (#439) |
| #194 | scripts | code | 1418 | 7 | 4(3) | 85/278 | 31/43 | 2026-07-05 | feat(tui_gateway): add process isolation phase 0 spike (#194) |
| #957 | scripts | code | 1351 | 11 | 0(0) | 2/61 | 3/4 | 2026-09-24 | ci: relay fleet/attribution to merge-group candidates (#957) |
| #931 | scripts | code | 1311 | 11 | 0(0) | 13/322 | 26/33 | 2026-09-23 | CI overflow phase 1: pure planner and CAS ledger (#931) |
| #116 | other:eval | code | 1209 | 12 | 4(3) | 11/372 | 5/17 | 2026-06-27 | feat(mem0): wire mem0 into background-review (mem0_remember + dedup ladder) (#116) |
| nopr:2f33571c20 | other:staging | code | 1136 | 5 | 0(0) | 10/392 | 11/20 | 2026-07-08 | feat(fleet): partial-restart ledger + parity re-drive actuator (A1b, staged) |
| #938 | scripts | code | 1121 | 10 | 2(2) | 14/214 | 14/18 | 2026-09-24 | feat(ci): overflow P2b — tests.yml placement wiring with local fallback (t_8e737f2c) (#938) |
| nopr:a0367856d4 | scripts | code | 1025 | 5 | 0(0) | 10/280 | 17/33 | 2026-06-16 | feat(lcm): add Aegis stress and config preflight gates |
| #307 | scripts | code | 978 | 4 | 4(3) | 310/370 | 29/33 | 2026-07-12 | feat(dashboard): AC-4 turn-isolation certify harness + synthetic GIL-heavy turn seam (#307) |
| nopr:e29944b04e | scripts | code | 967 | 2 | 0(0) | 19/372 | 15/44 | 2026-06-16 | feat(lcm): add live recovery statistical harness |
| nopr:7ee021200e | scripts | code | 912 | 2 | 0(0) | 10/332 | 14/34 | 2026-06-16 | feat: add LCM benchmark battery |
| #860 | scripts | code | 854 | 3 | 0(0) | 14/176 | 4/15 | 2026-09-22 | fix(gateway): release the heavy-read permit when the pre-yield region raises (#860) |
| #79 | scripts | code | 822 | 14 | 2(2) | 7/158 | 1/6 | 2026-06-22 | feat(blackbox): per-class cost breakdown (SPEC-C — engine + plugin + backfill) (#79) |
| #721 | other:contributors | code | 816 | 6 | 2(2) | 0/18 | 0/0 | 2026-09-19 | ci: scope merge_group to the batch's real diff (drop the always-full 44-job matrix) (#721) |
| #112 | scripts | code | 757 | 6 | 1(2) | 8/139 | 5/8 | 2026-06-27 | feat(compaction): skew telemetry + render harness + fix stale-rough median contamination (#112) |
| #873 | scripts | code | 705 | 2 | 0(0) | 1/146 | 1/12 | 2026-09-22 | fix(guard): pre-yield permit check excused conditionally-exiting arms and credited unreachable relea |
| nopr:3cd73eb4d4 | scripts | code | 680 | 4 | 0(0) | 19/223 | 7/16 | 2026-06-16 | test: add isolated LCM smoke gate |
| #863 | scripts | code | 655 | 3 | 0(0) | 1/96 | 2/9 | 2026-09-22 | fix(guard): pre-yield permit check missed line-adjacent leaks and partial multi-permit releases (#86 |
| #894 | other:staging | code | 629 | 7 | 1(3) | 0/17 | 0/1 | 2026-09-23 | fix(cli): the six residual pasteable-hint sites must survive a real shell (#894) |
| nopr:4b3698271f | scripts | code | 619 | 3 | 0(0) | 5/88 | 2/4 | 2026-06-16 | fix(lcm): null-safe lcm_grep query + Tier-0 QA battery (PRD-7) |
| nopr:eb38ffc5b2 | scripts | code | 568 | 8 | 0(0) | 19/157 | 14/37 | 2026-07-16 | Add refactor equivalence harness |
| nopr:8dcc69611c | scripts | code | 554 | 4 | 2(2) | 13/178 | 5/15 | 2026-07-08 | feat(skills): write-time skill-hygiene guard (A3) |
| #766 | scripts | code | 528 | 8 | 3(3) | 3/60 | 0/3 | 2026-09-20 | ci: per-slice runs-on — self-hosted first, GitHub-hosted overflow (#766) |
| #752 | scripts | code | 527 | 2 | 1(3) | 2/84 | 0/4 | 2026-09-20 | ci(run_tests_parallel): a file killed at the per-file timeout is a HANG, not a no-op (#752) |
| nopr:d9d2266dfe | scripts | code | 446 | 4 | 0(0) | 7/129 | 12/27 | 2026-06-16 | feat(lcm): add upstream drift metadata checker |
| nopr:481ddd77b6 | scripts | code | 430 | 7 | 0(0) | 0/46 | 0/2 | 2026-06-17 | PRD-8.1: probe-isolation harness (Arm A --probe-kind exact default, Arm B free-standing+collision co |
| nopr:6f5e60d30b | scripts | code | 419 | 3 | 0(0) | 3/93 | 0/5 | 2026-07-16 | feat(parity): wire catchup ack lint and tests gate |
| #476 | scripts | code | 409 | 3 | 1(1) | 3/49 | 1/3 | 2026-08-06 | ci: fail closed in umbrella result gate (#476) |
| #731 | scripts | code | 379 | 4 | 3(3) | 1/61 | 0/3 | 2026-09-19 | ci: size test workers from the cgroup CPU quota, not the host core count (#731) |
| nopr:510d8a2293 | scripts | code | 373 | 4 | 0(0) | 4/93 | 0/9 | 2026-07-10 | feat(hermes_parity): activate the 3 deferred merge-trap AST linters |
| nopr:589edef52a | scripts | code | 368 | 3 | 0(0) | 1/120 | 0/0 | 2026-06-17 | PRD-8.1 build: VOID-redraw+void_rate (AC-2), enforcing param-pin (AC-7/CB-2), K=2 char (AC-3b) |
| #436 | scripts | code | 334 | 5 | 1(1) | 0/24 | 0/1 | 2026-07-26 | docs(sync): absorption sweep — register absorbed turn-isolation family + extract deltas (#436) |
| nopr:1d5ebbe55a | scripts | code | 322 | 1 | 0(0) | 7/147 | 6/14 | 2026-06-16 | feat(lcm): Arm-B live node-served recovery harness (PRD-7) |
| #929 | scripts | code | 303 | 3 | 1(3) | 1/23 | 0/1 | 2026-09-23 | fix(ci): plugin-scoped test matrix runs the plugin's cross-tree consumers (#929) |
| nopr:834f0a0111 | scripts | code | 240 | 3 | 0(0) | 1/105 | 0/0 | 2026-06-18 | feat(lcm): staged K=2 disambiguation cutover campaign + reboot-survival autofire |
| nopr:f923a9e07c | scripts | code | 234 | 3 | 0(0) | 4/57 | 0/2 | 2026-06-18 | feat(lcm): PRD-8.3 multi-fact disambiguation — identifier fidelity + mandatory escalation |
| #297 | scripts | code | 213 | 5 | 3(3) | 1/65 | 0/2 | 2026-07-11 | [verified] ci: scope plugin tests and deflake rerank guard (#297) |
| nopr:4899803ced | scripts | code | 213 | 2 | 0(0) | 2/71 | 3/7 | 2026-06-16 | fix(lcm): real session-mode live-recovery driver for Aegis shakedown |
| nopr:2609ba7523 | scripts | code | 191 | 1 | 0(0) | 1/100 | 0/0 | 2026-06-17 | feat(PRD-8): implement Aegis LCM store reset runbook (dry-run verified) |
| nopr:86c0e7d510 | scripts | code | 189 | 1 | 0(0) | 1/73 | 3/13 | 2026-07-16 | fix(parity): stabilize bisect classification |
| nopr:ae486b7f0a | scripts | code | 188 | 1 | 0(0) | 3/89 | 3/3 | 2026-06-10 | mem0 destructive-tools: Phase-0 SDK kill-gate spike (PASS) |
| nopr:a8ac516b63 | scripts | code | 180 | 1 | 0(0) | 5/56 | 1/5 | 2026-06-16 | fix(lcm-arm-b): semantic probes + contention guard + front-loaded condensation |
| nopr:651f62e145 | scripts | code | 168 | 1 | 0(0) | 4/83 | 2/10 | 2026-07-16 | feat(parity): add catchup test selection |
| #87 | scripts | code | 159 | 2 | 0(0) | 1/30 | 1/1 | 2026-06-22 | fix(blackbox): checkpoint-safe, self-pruning backfill backups (SPEC-D) (#87) |
| nopr:fae13e5d9c | scripts | code | 152 | 1 | 1(3) | 2/55 | 1/2 | 2026-07-08 | feat(ci): loud no-op test guard — count executed tests, whole-suite-zero + explicit 0-collect RED (A |
| #793 | scripts | code | 151 | 4 | 2(3) | 1/19 | 0/1 | 2026-09-21 | ci: add opt-in hosted ARM slice trial (#793) |
| #347 | scripts | code | 141 | 4 | 2(3) | 1/4 | 0/0 | 2026-07-14 | fix(tests): fold full-suite review findings (#347) |
| nopr:591ae3ef55 | scripts | code | 141 | 3 | 0(0) | 0/31 | 0/2 | 2026-07-16 | fix(refactor-equiv): golden runners refuse to write into a real HERMES_HOME |
| nopr:595b6e78ae | scripts | code | 124 | 1 | 0(0) | 1/38 | 1/6 | 2026-07-16 | feat(parity): add fork manifest lint |
| nopr:7d33c982d8 | scripts | code | 123 | 1 | 0(0) | 1/57 | 1/3 | 2026-06-16 | feat(lcm): multi-sentinel-per-session Arm-B batching (~Kx cheaper) |
| #272 | scripts | code | 122 | 3 | 0(0) | 0/43 | 1/1 | 2026-07-10 | fix(livesync): RC-1 probe stored-id contract + derivation settle cap (#272) |
| nopr:7c3d5cdd0f | other:providers | code | 121 | 2 | 0(0) | 1/44 | 0/2 | 2026-07-08 | feat(telemetry): fN alias collision lint + stop composite model strings leaking to charts (A4 axis C |
| #695 | other:ui-tui | code | 87 | 1 | 0(0) | 8/11 | 0/0 | 2026-09-14 | test(tui): settle deferred rows before unmount compensation checks (#695) |
| #188 | other:web | code | 85 | 2 | 2(1) | 1/23 | 0/0 | 2026-07-03 | feat(dashboard): web chat client self-declares source='dashboard' (#188) |
| nopr:9f0c416088 | scripts | code | 83 | 1 | 0(0) | 0/28 | 0/0 | 2026-06-17 | feat(lcm-campaign): self-contained tightened-A + semantic-B campaign, venv-pinned |
| nopr:e0cf699f81 | scripts | code | 73 | 1 | 0(0) | 1/31 | 0/2 | 2026-06-16 | fix(lcm): Arm-B harness follows session rollover to node-bearing session |
| nopr:8e11a9370d | scripts | code | 68 | 3 | 0(0) | 0/4 | 0/0 | 2026-07-16 | fix(parity): fall back to sys.executable when no fleet venv on a CI clean checkout |
| #113 | scripts | code | 64 | 1 | 0(0) | 2/22 | 0/1 | 2026-06-27 | fix(render-proof): --real-model resolves like a live turn (register runtime-main) (#113) |
| nopr:20a4f2a589 | scripts | code | 63 | 2 | 1(3) | 0/10 | 0/0 | 2026-07-08 | fix(ci): A5-A explicit-noop gate is OPT-IN (--strict-noop), not default-on |
| #189 | other:node_modules | code | 60 | 6 | 0(0) | 4/22 | 0/0 | 2026-07-03 | fix(desktop): restore pinned drag-reorder over server-synced pins (#189) |
| #1006 | scripts | code | 60 | 3 | 1(1) | 0/5 | 0/0 | 2026-09-24 | fix(ci): bind overflow placement plan to the executing run_attempt (t_e1845f76) (#1006) |
| nopr:4bf7d5f9cb | scripts | code | 56 | 3 | 0(0) | 2/15 | 1/2 | 2026-07-16 | fix(parity): address Greptile review — $VENV rewrite on persisted bisect tails (derive venv from sys |
| nopr:28be2e65c8 | scripts | code | 55 | 4 | 0(0) | 2/29 | 3/3 | 2026-07-16 | fix(refactor-equiv): address Greptile review — freeze datetime.datetime.now via subclass seam (RC-A  |
| nopr:edcf3cf5d6 | scripts | code | 53 | 2 | 0(0) | 2/23 | 0/1 | 2026-06-16 | test(lcm): add Haiku session-mode shakedown controls |
| #928 | scripts | code | 50 | 2 | 0(0) | 2/6 | 0/0 | 2026-09-23 | test: gate Windows progress sampling on explicit release (#928) |
| nopr:87c73d7d86 | scripts | code | 50 | 1 | 1(3) | 0/13 | 0/0 | 2026-07-08 | fix(ci): A5-A no-op guard must not RED legit skips + testless files |
| nopr:b9d2be48c9 | scripts | code | 50 | 2 | 0(0) | 0/3 | 0/0 | 2026-07-16 | fix(review): guard test no longer writes into the real home; resolve candidate tmp roots |
| #150 | scripts | code | 49 | 2 | 2(2) | 2/9 | 0/1 | 2026-07-01 | ci: skip Python test matrix for built dashboard browser bundles (#150) |
| nopr:fdee6d93f2 | scripts | code | 46 | 2 | 0(0) | 0/12 | 0/0 | 2026-06-19 | fix(lcm): K=2 campaign — per-stage store reset + abort-safe done-marker |
| nopr:cbe8b15d22 | other:hermes_state_ext.py | code | 45 | 2 | 0(0) | 1/1 | 0/0 | 2026-07-16 | fix(state-ext): keep the hermes_state logger name — logger identity is an observable output; renamin |
| nopr:e84170e4f8 | scripts | code | 44 | 2 | 0(0) | 0/1 | 0/0 | 2026-06-18 | fix(lcm-arm-a): route gateway LCM writes to --lcm-db via LCM_DATABASE_PATH |
| nopr:0c96621b85 | scripts | code | 43 | 1 | 0(0) | 0/19 | 0/0 | 2026-06-16 | chore(lcm): batched Arm-B campaign w/ validation gate before full N=180 |
| nopr:51810bd85f | scripts | code | 42 | 1 | 0(0) | 0/17 | 0/1 | 2026-07-16 | test(cron): register scheduler extension mutations |
| #703 | other:contributors | code | 40 | 5 | 0(0) | 0/0 | 0/0 | 2026-09-16 | ci: close the remaining self-hosted-pool gaps on the ACE-AI runner pool (#703) |
| #118 | other:toolsets.py | code | 38 | 2 | 1(1) | 0/0 | 0/0 | 2026-06-29 | fix(mem0): make mem0_remember RESIDENT in tools[] so background-review can actually call it (#118) |
| #153 | scripts | code | 38 | 2 | 2(2) | 0/8 | 0/2 | 2026-07-01 | ci: skip the Python test matrix for media assets + git metadata (#153) |
| nopr:0242647e1f | scripts | code | 37 | 1 | 1(3) | 0/11 | 0/0 | 2026-07-08 | fix(ci): A5-A gate 2 aggregates per-path before deciding no-op RED |
| nopr:84da9fa36a | scripts | code | 37 | 1 | 0(0) | 0/14 | 0/1 | 2026-07-16 | Register tool gate refactor mutations |
| nopr:84f1ed6e45 | scripts | code | 37 | 1 | 0(0) | 0/13 | 0/1 | 2026-07-16 | test(refactor-equiv): add compaction mutations |
| nopr:6e83005ed4 | other:gates.jsonl | code | 36 | 10 | 1(3) | 1/9 | 0/0 | 2026-07-16 | fix(ci): post-merge desktop reconciliation — electron node:test → vitest imports (3 files, upstream  |
| nopr:f3dfb89c2a | scripts | code | 36 | 2 | 0(0) | 0/11 | 0/0 | 2026-06-18 | feat(lcm): AC-5 baseline-repro toggle (--no-escalation) for K=2 disambiguation campaign |
| nopr:1f8392335e | scripts | code | 34 | 2 | 0(0) | 1/16 | 0/0 | 2026-06-16 | feat(lcm-qa): heartbeat campaign milestones to #hermes-lcm |
| nopr:79715124fa | scripts | code | 33 | 3 | 0(0) | 0/3 | 0/0 | 2026-07-16 | test(restart_codec): add negative-scalar corpus case + scalar-branch clamp mutation |
| nopr:df420d15df | scripts | code | 29 | 1 | 0(0) | 1/13 | 0/0 | 2026-06-16 | chore(lcm): sequential Arm-A->Arm-B N=180 campaign runner (PRD-7) |
| nopr:d5cec6ec16 | scripts | code | 25 | 1 | 0(0) | 0/9 | 0/0 | 2026-06-17 | fix(lcm-arm-b): interpreter guard + don't score import errors as recovery misses |
| #944 | scripts | code | 21 | 2 | 0(0) | 0/1 | 0/0 | 2026-09-23 | ci(e2e-evidence): install gh-image from inside its dir; gh reads any path as OWNER/REPO (#944) |
| nopr:97e91756c2 | scripts | code | 20 | 1 | 0(0) | 1/9 | 0/2 | 2026-07-16 | feat(parity): separate vacuous coverage acks |
| nopr:48376f7570 | scripts | code | 16 | 2 | 0(0) | 0/1 | 0/0 | 2026-06-16 | fix(lcm): Wilson lower-bound gate default 0.95 -> 0.90 per PRD-6 |
| #1029 | scripts | code | 12 | 2 | 0(0) | 0/2 | 0/0 | 2026-09-25 | test(ci-overflow): gate reserve() label allowlist (C4) (#1029) |
| nopr:57abdd627b | scripts | code | 12 | 2 | 1(1) | 0/2 | 0/0 | 2026-07-16 | fix(manifest): golden.json is a data file, not a pytest nodeid — guard it via paths; exclude manifes |
| #62 | scripts | code | 10 | 2 | 0(0) | 0/3 | 0/0 | 2026-06-18 | fix(scripts): add explicit encoding for lcm report writes (#62) |
| nopr:c2cf19f6fd | scripts | code | 10 | 2 | 0(0) | 0/4 | 0/0 | 2026-06-18 | fix(lcm): use nohup instead of setsid for macOS K=2 autofire |
| #749 | other:contributors | code | 9 | 2 | 0(0) | 0/0 | 0/0 | 2026-09-20 | ci: fleet CI-fail alert subscribes to the parent CI workflow (has never fired) (#749) |
| #169 | scripts | code | 8 | 3 | 3(2) | 0/2 | 0/0 | 2026-07-01 | ci: widen test matrix to 10 slices + 12 workers per slice (#169) |
| nopr:a3fa06a6e4 | scripts | code | 8 | 1 | 0(0) | 0/2 | 0/0 | 2026-06-16 | fix(lcm-campaign): drain 45s after Arm A before Arm B (DB-flush race) |
| #728 | other:contributors | code | 7 | 3 | 0(0) | 0/0 | 0/0 | 2026-09-20 | fix(tests): repoint 3 dead gateway.run.__file__ mocks to gateway.slash_commands (#728) |
| nopr:1b8af38096 | scripts | code | 7 | 2 | 2(3) | 0/5 | 0/0 | 2026-07-16 | fix(ci): parity-sync CI gates — map 5 new upstream contributor emails in AUTHOR_MAP; reword test doc |
| #151 | other:greptile.json | code | 6 | 1 | 0(0) | 0/0 | 0/0 | 2026-07-01 | chore(ci): add greptile.json trivial-PR fast lane (#151) |
| nopr:6ca97b5bcd | scripts | code | 6 | 1 | 1(2) | 0/1 | 0/0 | 2026-06-30 | fix(ci): map bare Kyzcreig@users.noreply.github.com in AUTHOR_MAP |
| #105 | scripts | code | 4 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-25 | docs(blackbox): fix backwards VACUUM INTO comment in backfill backup (#105) |
| nopr:fb3008a7ba | scripts | code | 4 | 1 | 0(0) | 0/2 | 0/0 | 2026-06-17 | fix(lcm-campaign): staged Arm A N=120 shakedown needs --allow-underpowered-live |
| nopr:a0a5e8a083 | other:staging | code | 2 | 1 | 0(0) | 0/1 | 0/0 | 2026-07-08 | fix(lint): explicit encoding on runtime-parity-check lockfile open (ruff blocking) |
| nopr:40a6040932 | scripts | code | 2 | 1 | 1(2) | 0/2 | 0/0 | 2026-07-01 | fix(ci): map new upstream contributors in AUTHOR_MAP (talmax1124, janrenz) |
| nopr:aae6a302db | scripts | code | 2 | 1 | 1(3) | 0/1 | 0/0 | 2026-07-08 | fix(lint): explicit encoding on read_text in A5-A testless detection (ruff blocking) |
| #22 | scripts | code | 1 | 1 | 1(2) | 0/1 | 0/0 | 2026-06-08 | chore(attribution): map apollo@daemonarchy.local → Kyzcreig in AUTHOR_MAP (#22) |
| #847 | empty | empty | 0 | 0 | 0(0) | 0/0 | 0/0 | 2026-09-22 | fix(gateway): make the restart-failure-counts read-modify-write atomic (#847) |
| #805 | empty | empty | 0 | 0 | 0(0) | 0/0 | 0/0 | 2026-09-21 | fix(gateway): move the rich-sent index write off the event loop (ratchet 51 -> 47) (#805) |
