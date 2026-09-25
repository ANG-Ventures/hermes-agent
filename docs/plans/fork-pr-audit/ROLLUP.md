# Fork-PR audit — ROLLUP (lead t_03e35f0e)

## 1. Verdicts per tranche

| tranche | KEEP | UPSTREAM | SUPERSEDED-BY-UPSTREAM | DROP | UNRESOLVED | total |
|---|---|---|---|---|---|---|
| gateway | 92 | 19 | 26 | 42 | 2 | 181 |
| agent | 117 | 20 | 85 | 18 | 0 | 240 |
| hermes_cli | 133 | 4 | 16 | 8 | 16 | 177 |
| plugins | 68 | 7 | 4 | 5 | 0 | 84 |
| cron+tools | 44 | 37 | 21 | 9 | 0 | 111 |
| scripts+misc | 76 | 1 | 3 | 32 | 5 | 117 |
| auto | 134 | 5 | 17 | 10 | 1 | 167 |
| auto-cherry-pick | 0 | 0 | 2 | 0 | 0 | 2 |
| auto-desktop-retired | 0 | 0 | 0 | 37 | 0 | 37 |
| **all** | **664** | **93** | **174** | **161** | **24** | **1116** |

## 2. Fork lines that DROP / SUPERSEDED would delete

Census `loc` (add+del of the original PR, an upper bound — later rows edit the same lines): DROP 53,897, SUPERSEDED-BY-UPSTREAM 88,161, UPSTREAM (deleted once merged upstream) 30,313, KEEP 334,962, UNRESOLVED 7,867; all rows 515,200.

Real `git diff --shortstat <merge-base> <branch>` of every revert branch on origin (lead run 2026-09-25):

| branch | shortstat | status after adversary/lead |
|---|---|---|
| `audit/agent/revert-pr186` | 2 files changed, 2 insertions(+), 96 deletions(-) | LIVE |
| `audit/agent/revert-pr236` | 5 files changed, 4 insertions(+), 173 deletions(-) | LIVE |
| `audit/agent/revert-pr257` | 10 files changed, 982 deletions(-) | VOID (rows now UPSTREAM×2) |
| `audit/agent/revert-pr600` | 2 files changed, 1 insertion(+), 70 deletions(-) | LIVE |
| `audit/agent/revert-pr621` | 12 files changed, 18 insertions(+), 769 deletions(-) | LIVE |
| `audit/agent/revert-pr622` | 6 files changed, 30 insertions(+), 11 deletions(-) | no row cites it |
| `audit/agent/revert-pr806` | 2 files changed, 3 insertions(+), 99 deletions(-) | VOID (rows now UPSTREAM×1) |
| `audit/agent/revert-pr959` | 2 files changed, 9 insertions(+), 48 deletions(-) | LIVE |
| `audit/agent/revert-pr997` | 2 files changed, 217 deletions(-) | VOID (rows now KEEP×1) |
| `audit/cron_tools/revert-211` | 6 files changed, 20 insertions(+), 1103 deletions(-) | LIVE |
| `audit/cron_tools/revert-351` | 2 files changed, 151 deletions(-) | VOID (rows now SUPERSEDED-BY-UPSTREAM×1) |
| `audit/cron_tools/revert-387` | 2 files changed, 1 insertion(+), 177 deletions(-) | LIVE |
| `audit/cron_tools/revert-f504b1c928` | 8 files changed, 3 insertions(+), 735 deletions(-) | LIVE |
| `audit/gateway/revert-129` | 2 files changed, 349 deletions(-) | LIVE |
| `audit/gateway/revert-229` | 5 files changed, 2 insertions(+), 1127 deletions(-) | LIVE |
| `audit/gateway/revert-49-undo` | 56 files changed, 307 insertions(+), 4979 deletions(-) | LIVE |
| `audit/hermes_cli/revert-36134d8944` | 2 files changed, 3 insertions(+), 26 deletions(-) | LIVE |
| `audit/hermes_cli/revert-619` | 2 files changed, 83 deletions(-) | LIVE |
| `audit/hermes_cli/revert-712` | 4 files changed, 296 deletions(-) | LIVE |
| `audit/hermes_cli/revert-760` | 2 files changed, 97 deletions(-) | LIVE |
| `audit/hermes_cli/revert-802` | 3 files changed, 7 insertions(+), 711 deletions(-) | VOID (rows now KEEP×1) |
| `audit/scripts_misc/revert-author-map-stale` | 1 file changed, 3 deletions(-) | LIVE |
| `audit/scripts_misc/revert-ci-overflow-phase0` | 4 files changed, 1602 deletions(-) | LIVE |
| `audit/scripts_misc/revert-ci-review-comment` | 4 files changed, 87 insertions(+), 4230 deletions(-) | LIVE |
| `audit/scripts_misc/revert-desktop-update-test` | 2 files changed, 14 insertions(+), 36 deletions(-) | LIVE |
| `audit/scripts_misc/revert-greptile` | 1 file changed, 6 deletions(-) | LIVE |
| `audit/scripts_misc/revert-lcm-campaign-harness` | 44 files changed, 13138 deletions(-) | LIVE — REBUILD: drops files the adversary/lead kept (see FINAL conflicts) |
| `audit/scripts_misc/revert-livesync` | 9 files changed, 21 insertions(+), 841 deletions(-) | LIVE |
| `audit/scripts_misc/revert-livesync-probes` | 3 files changed, 444 deletions(-) | no row cites it |
| `audit/scripts_misc/revert-mem0-bgr` | 15 files changed, 7 insertions(+), 1764 deletions(-) | LIVE |
| `audit/scripts_misc/revert-oneshot-probes` | 3 files changed, 1004 deletions(-) | LIVE — REBUILD: drops files the adversary/lead kept (see FINAL conflicts) |
| `audit/scripts_misc/revert-perclass-backfill` | 2 files changed, 540 deletions(-) | LIVE |
| `audit/scripts_misc/revert-self-hosted-pool-gaps` | 3 files changed, 3 insertions(+), 25 deletions(-) | VOID (rows now KEEP×1) |
| `audit/scripts_misc/revert-staging` | 6 files changed, 1201 deletions(-) | LIVE |
| `audit/scripts_misc/revert-web-source` | 2 files changed, 82 deletions(-) | LIVE |
| `audit/final-revert-desktop-retired` | 2538 files changed, 277183 insertions(+), 49162 deletions(-) | LIVE — includes upstream desktop drift since the 08-07 merge-base (D9 takes it wholesale); fork-only part = 37 rows |

## 3. Upstream PRs to open (one slice card each)

| card | keys | branch(es) |
|---|---|---|
| t_21eb16a1 | #257, #303 | to build |
| t_ef01d4bc | #203, #204 | audit/cron_tools/upstream-203 |
| t_6dd3bc1c | #1052 | audit/agent/upstream-1052 |
| t_1b907b4d | #1025 | to build |
| t_2f79e876 | #917 | to build |
| t_e8099117 | #878 | audit/agent/upstream-878 |
| t_38e016d1 | #841 | to build |
| t_ffea11fd | #730 | to build |
| t_221decac | #671 | to build |
| t_8745d8fe | #602 | to build |
| t_cfb75eb5 | #324 | to build |
| t_835842d2 | #300 | to build |
| t_234502cd | #27 | to build |
| t_95a0e5c4 | #1027 | audit/cron_tools/upstream-1027 |
| t_d6dd9867 | #1017, #933, #920 | audit/cron_tools/upstream-920 |
| t_cf98616a | #830 | audit/cron_tools/upstream-830 |
| t_8b3280d5 | #767 | audit/cron_tools/upstream-767 |
| t_4cbe80c7 | #740 | audit/cron_tools/upstream-740 |
| t_8104aac7 | #632 | audit/cron_tools/upstream-632 |
| t_4d18a826 | #594, #319 | audit/cron_tools/upstream-319 |
| t_c2e90a10 | #591 | to build |
| t_552fce3d | #532 | to build |
| t_cbdab1dc | #521 | audit/cron_tools/upstream-521 |
| t_6791824d | #477 | audit/cron_tools/upstream-477 |
| t_e1f37c7c | #278 | audit/cron_tools/upstream-278 |
| t_f1a79491 | #199 | audit/cron_tools/upstream-199 |
| t_e6ede09e | nopr:10a1424335, nopr:116493ff97, nopr:c559189dd2 | audit/cron_tools/upstream-ssh-116493ff97 |
| t_a11dae63 | #976 | audit/gateway/upstream-976 |
| t_505f76f8 | #936 | audit/gateway/upstream-936 |
| t_e3cc317e | #935 | audit/gateway/upstream-935 |
| t_33c27f60 | #934 | to build |
| t_c01d2eaf | #871 | audit/gateway/upstream-871 |
| t_bec74c88 | #869 | audit/gateway/upstream-869 |
| t_c05cb52b | #861 | audit/gateway/upstream-861 |
| t_aef6e498 | #840 | audit/gateway/upstream-840 |
| t_291eab6a | #834 | audit/gateway/upstream-834 |
| t_afe5a29a | #782 | audit/gateway/upstream-782 |
| t_c0a324c1 | #301 | to build |
| t_1717a8d8 | #851 | to build |
| t_76252db1 | #836 | audit/gateway/upstream-836 |
| t_b0037579 | #759 | audit/gateway/upstream-759 |
| t_9d67fba1 | #980 | to build |
| t_a08c812a | #617 | audit/hermes_cli/upstream-617 |
| t_61b00b5c | #466 | audit/hermes_cli/upstream-466 |
| t_603de351 | #276 | to build |
| t_d22f7e84 | #728 | audit/scripts_misc/upstream-728 |
| t_a6ed9644 | #1054 | to build |
| t_5fc4c88d | #806 | to build |
| t_cb481e35 | #418 | to build |
| t_cb481e35 | #220 | to build |
| t_cb481e35 | #216 | to build |
| t_cb481e35 | #213 | to build |
| t_cb481e35 | #443 | to build |
| t_00401071 | #571 | audit/gateway/upstream-571 |
| t_6621602f | #902 | audit/plugins/upstream-902 |
| t_dee0dfe1 | #412 | audit/plugins/upstream-412 |
| t_2629830d | nopr:dc4245ab4c | audit/plugins/upstream-dc4245ab4c |
| t_283aa1d2 | #580 | audit/plugins/upstream-580 |
| t_7053f65e | nopr:531f1e8c64 | to build |
| t_048e5927 | nopr:59ed0e41a1 | audit/plugins/upstream-59ed0e41a1 |
| t_58527481 | nopr:5ae50ce919 | to build |
| t_dd0b8b2d | #777 | to build |
| t_dd0b8b2d | #763 | to build |
| t_8a17e1aa | #635 | audit/gateway/upstream-635 |
| t_bb3454b0 | #584 | audit/gateway/upstream-584 |
| t_e4b2e625 | #1024 | to build |
| t_7899d415 | #927 | to build |
| t_15bf9da0 | #662 | audit/cron_tools/upstream-662 |
| t_a9900ff0 | #641 | audit/cron_tools/upstream-641 |
| t_06093ad2 | #618 | audit/cron_tools/upstream-618 |
| t_933b75ff | #615 | audit/cron_tools/upstream-615 |
| t_dda41ad5 | #577, #575 | audit/cron_tools/upstream-575 |
| t_b3cfa839 | #553 | audit/cron_tools/upstream-553 |
| t_05cdacb0 | #543 | to build |
| t_6507eb84 | #399 | audit/cron_tools/upstream-399 |
| t_7829c723 | #314 | audit/cron_tools/upstream-314 |
| t_13bcf452 | #209 | to build |
| t_522e29c9 | #71 | audit/cron_tools/upstream-71 |
| t_05cdacb0 | nopr:4d626ac813 | to build |
| t_e480bf39 | nopr:9a1db22e58 | to build |

## 4. Expected reduction in parity-merge conflicts

Census `conflict_files` is file-level (the PR touches a file named in a sync ledger). A ledger file stops costing a conflict only if NO surviving (KEEP / UPSTREAM-until-merged / UNRESOLVED) row still touches it. Both views:

| ledger | ledger files touched by fork rows | touched by DROP/SUPERSEDED rows | freed (no surviving row touches it) |
|---|---|---|---|
| RESOLUTION-LEDGER.md | 47 | 43 | 7 |
| RESOLUTION-LEDGER-20260807.md | 117 | 85 | 13 |
| RESOLUTION-LEDGER-2026-08-29.md | 123 | 84 | 14 |

Row view: 238/795 ledger-touching rows are DROP/SUPERSEDED; they hold 551/1,727 (32%) of all row×conflict-file incidences. Freed files (all ledgers, deduped): 31 — apps/desktop/src/lib/reasoning-effort.ts, hermes_cli/tools_config.py, locales/ar.yaml, scripts/install.sh, scripts/iso-certify.py, tests/agent/test_compaction_threshold_reresolve.py, tests/agent/test_fallback_announce.py, tests/agent/test_i18n.py, tests/agent/test_redact.py, tests/cron/test_lifecycle_guard_heredoc_data.py, tests/gateway/test_config_env_bridge_authority.py, tests/gateway/test_cron_session_contextvar.py, tests/gateway/test_no_gateway_session_env_writes.py, tests/hermes_cli/test_model_switch_custom_providers.py, tests/hermes_cli/test_web_server.py, tests/run_agent/test_partial_stream_finish_reason.py, tests/run_agent/test_run_agent.py, tests/run_agent/test_run_agent_conversation.py, tests/test_code_skew.py, tests/test_web_server.py, tests/tools/test_cron_subagent_session.py, tests/tools/test_mcp_tool.py, tests/tui_gateway/test_compute_host_phase1.py, tests/tui_gateway/test_iso_certify_seam.py, tui_gateway/compute_host.py, tui_gateway/host_supervisor.py, tui_gateway/methods_session.py, tui_gateway/synthetic_turn.py, web/src/lib/gatewayClient.test.ts, web/src/lib/gatewayClient.ts, website/docs/user-guide/features/browser.md. The god files (gateway/run.py, agent/*helpers, hermes_state.py, cron/scheduler.py) stay conflicted: KEEP rows still touch them, so the win there is fewer hunks, not fewer files.

## 5. D2b registry write-backs owed (docs/sync/fork-features.json)

- entry 14 /undo+/redo fork-permanent → **retire** if Ace approves the ACE-GATED DROP (6 rows, card undo-redo).
- entry 16 /merge fork-permanent → **retire** if Ace approves #231; entry 15 /branch stays (Discord thread half).
- entry 4 messaging+MoA toolsets fork-permanent → **retire** if Ace approves moa-messaging-toolsets; then #342 and the MoA half of #787 go with it.
- entry 11 tool_gate (upstream-intended) → golden update: #37 grace branch removed (7 grace cases).
- entry 12 state ext / platform session search → set `upstream_ref` when #257/#303 is filed.
- entries 22/23/24 (footer / route announce / reasoning announce, upstream-intended) → still outstanding; 90+ gateway KEEP rows ride them. Filing them upstream is the largest remaining burden cut.
- NEW entries owed (KEEP rows with no registry entry, D2b): fork kanban subsystem (hermes_cli auditor: every measured fork-kanban KEEP), blackbox plugin (plugins auditor), cron per-job model pin #411 and approvals-mode-off predicate #786 (cron+tools adversary), Discord thread auto_archive 10080 #710.
- entry 9 restart policy: F1/F2 unit kept; mark the F2 breaker as a trim candidate (0 decisions in 544 logs).

## 6. The three answers Ace asked for

**What still provides value.** 664 rows (334,962 loc) KEEP on a measurement: the restart/resume/admission family (boot_resume_scheduled 1,190, dropbox_resume 1,410, turn_slot_acquire 2,208, restart_notice 455 fires), blackbox cost accounting (19k–47k turns/api-calls priced), relay-lane headers/pricing (16,913 bpx/bpr calls/7d), the footer and route/compaction announce families (registry 3/22/23/24, live config), Discord restart backfill (425 re-injected messages), cron fallback/pins (17 opt-in jobs, 521-job stores). Plus 93 UPSTREAM rows that are needed AND generic — value that should stop being ours.

**What solves problems that no longer exist.** 174 rows are fixed upstream (take theirs at the next sync) and 161 rows DROP: never fired in the window, dormant surfaces (desktop D9 ×37, gemini/yunwu lanes, MoA/send_message tools dormant since July, /undo 2 uses, /merge 0), one-shot June LCM campaign harnesses, and a write-only reaction journal. 24 rows stay UNRESOLVED (silent guards with no log/DB signal — need a canary, not a guess).

**What the maintenance burden actually is (measured).** 515,200 fork lines across 1,116 rows; 795 rows sit on files named in the sync ledgers and 458 on files that conflicted in all three syncs. DROP+SUPERSEDED rows carry 551 of 1,727 (32%) row×conflict-file incidences and 142,058 census loc; but only 31 ledger files are fully freed because KEEP rows still sit on the god files. The real reduction comes in two steps: (1) this audit's DROP/SUPERSEDED (fewer hunks per sync), (2) landing the UPSTREAM rows + registry 22/23/24, which is what empties gateway/run.py of fork hunks.

## 7. Open prerequisites before the sync takes upstream

- mem0: migrate all 9 `mem0.json` from `admin_api_key` to `api_key` before taking upstream SelfHostedBackend (nopr:8b332be03c).
- skew family (8 agent rows): one change with plugins/context_engine/lcm engine.py + drop `compression.skew_floor` from 4 configs.
- #225: re-point the cron consumer; delete `model.announce_recovery` from 13 configs.
- #351 revert must not land before the sync takes tests/home_io_guard.py (cron+tools adversary).
- #88 dependent: tools/terminal_tool.py:179-181 uses is_cron_session(); rewire at sync.
- Skill edits owed with DROPs: lcm-context-engine SKILL.md:874/902, hermes-client-source-attribution, livesync-eyes-on-regression (#268/#272), trivial-pr-fast-path + greploop (#151), blackbox-turn-telemetry perclass ref (#87/#105).
- Ref-namespace note: `audit/final` (this docs branch) blocks a ref named `audit/final/…`; the desktop revert is `audit/final-revert-desktop-retired`.

## 8. Slice cards (step 7)

203 cards, all `parents=[t_03e35f0e]` derived-from, all in triage: `lead/created_ids.json`. Each of the 217 non-auto DROP/UPSTREAM
rows has its own card (`lead/card_ids.json` + `lead/split_ids.json`; FINAL.md `card` column). Where several rows land in one
revert branch or one upstream port, each row still has its own card, and the card names the sibling card that carries the
shared branch. Only two spec-sanctioned exceptions share a card: `auto` tests/docs rows join the card of the code they test
(step 3: "a tests-only row whose code got DROP joins that revert"), and the 37 `auto-desktop-retired` rows share one card
t_899f0539 for the single combined branch `audit/final-revert-desktop-retired` (step 3).
