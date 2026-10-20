# Boomerang v2 milestone QA verdict (Argus, card t_230d37a3)

QA VERDICT: **FAIL**  Readiness: **HOLD** (the milestone is not done; see F-1)

- Verdict SHA: `876dc00c165b8e082be3d79258242a9e684fee6f` (fork/main, 2026-10-19 20:04 PDT).
- Gate: `milestone-qa-gate.py --resolve t_230d37a3 --db boards/boomerang-v2/kanban.db` reports all_merged=true for #2096 (`8a359be7`) and #2121 (`1a74bbba`). `git merge-base --is-ancestor` gives rc=0 for both against the verdict SHA.
- Feature PRs on that SHA: #2095 P1, #2112 P2, #2121 /boomerang, #2099 P3a, #2123 P3b, #2129, #2130 (all ancestors). Live runtime tree HEAD `5f0c257b80e`; the smoke ran on the deployed `fa2fe81d`.
- Scope: AC-1..AC-15 (card amendment, Apollo 2026-10-19). Out of scope: the live smoke itself (Apollo runs it), any config flip, any gateway lifecycle action.
- Evidence directory (outside the repo): `~/.hermes/kanban/worktrees/argus/t_230d37a3/qa-output/`. It is referred to below as `qa-output/`.

## Blocking finding

**F-1 (Blocker): a fork child built by the real `delegate_task` entry point never receives the parent transcript or the fork frame.** This defeats INV-2, D-14 and the purpose of P2.

- **Code path.**
  - `tools/delegate_tool_fork.py::fork_run_inputs` is the only code that turns `child._fork_history` into the child's `conversation_history` plus the `[BOOMERANG FORK …]` frame turn.
  - Its only production call site is `tools/delegate_tool_child_run.py:859`, inside `_ChildRun.await_child`.
  - `_ChildRun` is never instantiated outside tests. The fork's live supervisor is the whole-function override `tools/delegate_tool.py::_run_single_child` (L2265, comment "upstream's _ChildRun path stays importable").
  - Its `_child_turn` calls `child.run_conversation(user_message=goal, …)` with no `conversation_history` (L2699-2703).
- **Reproduction (independent oracle).** Script: `qa-output/inv1_probe.py`, output `qa-output/inv1-head-fork.json`.
  - The script builds a real `AIAgent` parent with one completed turn, then calls `delegate_task(goal=…, inherit_context=True, inherit_mode="fork", parent_agent=parent)`.
  - Only the request client's `messages.stream` is stubbed: it records the kwargs and raises.
  - Result: the child's first request carries the parent's `system`, `tools`, `model` and the pin `x-hermes-pin-sub: sub-vps-16`.
  - But `messages` is `[user: "verify the canary"]`. There is no parent turn ("Kestrel" is absent) and no `BOOMERANG FORK` frame.
- **Live confirmation.** File: `qa-output/live-fork-evidence.txt`.
  - All 5 production fork children logged in agent.log on 2026-10-19 report `boomerang.fork served_by=sub-vps-16 read_share≈0.224 prefix_tokens≈70.8k`.
  - The parent session `20261018_130347_87039e56` has a last-turn cache read of 267,817 tokens.
  - In `state.db`, each child session has exactly 1 user row: the auto-handle GOAL. `instr(content,'BOOMERANG FORK') = 0`.
  - Each child wrote about 55k tokens cold (`sessions.cache_write_tokens` 55,695 / 56,797 / 55,876).
- **Why the evidence packs missed it.** The INV-2 unit test (`tests/tools/test_delegate_tool_fork_mode.py::_child_first_request`) and the AC-4/AC-4b harness (`tools/dev_probes/boomerang_v2_p2_live.py` L137-141) both call `fork_run_inputs` themselves, then call `run_conversation`. They never go through `_run_single_child`. Both tests pass on a path production does not take.
- **Inference (not separately reproduced).** `/boomerang <task>` now defaults to fork (D-1, #2121). It lands in the same `delegate_task` path, so its children start with no inherited context at all, where v1's fold carried a summary.
- **Owner:** Daedalus (fix). Apollo decides whether `delegation.boomerang.fork_enabled=false` should be set meanwhile; the summary fold does inherit context. Argus has not changed any config.

## Other findings

**F-2 (Major): 3 tests in the feature's own file fail on current main and pass on the deployed tree.**
- The tests are in `tests/gateway/test_kanban_auto_handle_dispatch.py`:
  - `test_host_load_alone_does_not_stop_the_auto_handle_child`
  - `test_failed_dispatch_under_host_load_stays_a_notify`
  - `test_unflagged_card_under_host_load_is_unchanged`
- They fail on `876dc00c` (`qa-output/run2-p3.log`, `qa-output/run3-head-hostload.log`) and pass on `fa2fe81d` (`qa-output/run3-deployed-hostload.log`, 3 passed).
- **Cause.** #2114 (`5c0ca14cc1b`, the wake gate now reads cpu_busy instead of load1) changed the gate these #2129 tests drive with `loadavg=71`. The `(notify: host load 71)` suffix and the stays-a-notify path no longer occur.
- **Open question.** Is this a stale test or a behaviour regression? The owner decides.
- **CI.** None of the feature commits has any check-run; only the `fleet/attribution` status exists. Main went red without a CI signal.

**F-3 (Minor): no live `boomerang.fork_ineligible reason=warm` line exists.**
- No `reason=warm` line appears in `agent.log`..`agent.log.3` (from 2026-10-19 17:15 on) or in `gateway.log*`.
- The one live `fork_ineligible` line I read, at 20:4x before it rotated out of agent.log, was:
  `2026-10-19 16:41:21,098 INFO tools.delegate_tool: boomerang.fork_ineligible reason=fork_enabled parent_session=20261018_130347_87039e56`
  The same line is in `~/.hermes/var/bv2-smoke/smoke.md` step 2, check 4.
- The `warm` downgrade is proven only by unit tests (AC-10).

**F-4 (Minor, test gap): the PR's own INV-1 test is a head-vs-head self-comparison.**
- `test_no_inherit_mode_builds_identical_kwargs_to_explicit_none` compares two head builds against each other. It is not a byte-identity check against the pre-feature code.
- My base-vs-head differential (AC-1) covers the gap.
- AC-15(b)'s `comment_can_hold` clause is not asserted in the test file. I checked it live (AC-15 row).

## AC table

| AC | Verdict | Artifact | Note |
|---|---|---|---|
| AC-1 (INV-1) | **PASS** | `qa-output/run1-inv1-p2.log` (test_boomerang_inherit_context.py 20 passed); `qa-output/inv1-{base,head}-{plain,inherit_true,inherit_false}.json` | v1 suite: 0 commits since base `ffdaa7d6` (`git log ffdaa7d6..876dc00c -- tests/tools/test_boomerang_inherit_context.py` is empty). Real `delegate_task` child first request, base `ffdaa7d6` vs head `876dc00c`, normalised for ids/timestamps: plain sha `24acc8db…` = `24acc8db…`; inherit_context=True `88a13837…` = `88a13837…`; inherit_context=False is equal too. No pin header in any of them. |
| AC-2 (P1) | **PASS** | `docs/boomerang-v2/evidence/p2-cache-trials.jsonl` (16/16 pin == served_by == parent seat, recomputed); `qa-output/run1-inv1-p2.log` (test_delegate_tool_fork_pin.py 9 passed); `qa-output/inv1-head-fork.json` (pin present) vs `inv1-head-inherit_true.json` (no pin) | Caveat: the evidence is response `x-pool-served-by` rows, not relay usage-ledger `sub_key` rows. On the real entry point the pin is applied correctly (F-1 does not affect the pin). |
| AC-3 (P2, INV-2) | **FAIL** | `qa-output/inv1-head-fork.json`; `qa-output/live-fork-evidence.txt` | F-1. On the real path the child prefix holds 0 parent turns. The unit test passes only because it calls `fork_run_inputs` itself. No live wire capture exists (smoke step 3 INCOMPLETE). |
| AC-4 (P2 eval) | **FAIL** | `docs/boomerang-v2/evidence/p2-cache-trials.jsonl`; `qa-output/live-fork-evidence.txt` | The artifact is internally valid: 16/16 recomputed, min read_share 0.99938, prefix 325,650. But the harness injects `fork_run_inputs` itself (F-1). The 5/5 production forks read 0.224. The claim does not hold on the shipped path. |
| AC-4b (P2 cost) | **FAIL** | `docs/boomerang-v2/evidence/p2-cost-pairs.jsonl` | 5/5 fork ≤ fold, recomputed from usage with the README ratios. Same harness bypass as AC-4. Production forks pay ~55k cold write with zero parent context. |
| AC-5 (INV-4 gate) | **PASS** | `docs/boomerang-v2/evidence/inv4-probes/results.jsonl` | Recomputed: 20 probes, 5 prefixes (55k–497k), recall 19/20 ('7F3A'), disavowal 0/20 under the fixed regex; the one miss (500k #3) was `stop_reason=tool_use`. The gate is a raw-wire framing probe and stands on its own. Because of F-1, production children never receive this framing. |
| AC-6 (INV-5) | **PASS** | `qa-output/run2-p3.log` (test_kanban_wake_disposition.py 192 passed: every kind 11, kind×payload 110, owner-wake reasons 6, needs_input 27) | Live corroboration: smoke step 1 checks 3–4 (needs_input card not auto-handled). |
| AC-7 (INV-6) | **PASS** (caveat) | `~/.hermes/var/bv2-smoke/smoke.md` step 1 (PASS, 16:32–16:34); gateway.log 16:33:15 `auto-handled t_78080b3d disposition=auto` | Caveat: in the forced-failure arm, ⚠️ was posted but the gateway.log line at 16:33:28 says `no live home for t_dcfcc7f0; wake not re-surfaced`. The injected home did not exist, so the "re-surfaced wake" half is proven only in the unit test `test_child_crash_posts_warning_and_resurfaces_the_wake` (passed). |
| AC-8 (INV-7) | **PASS** | smoke.md step 2 (PASS, 16:40–16:42; `fork_ineligible reason=fork_enabled` at 16:41:21; auto_handle_enabled=false gave no auto-handle in 20 s); `qa-output/run2-p3.log` `test_kill_switch_flipped_between_ticks_next_tick_surfaces` passed | Config restored to true/true (step 2 check 10). |
| AC-9 (INV-8) | **PENDING-SMOKE** | smoke.md step 3 | Last recorded run 18:50–18:57: INCOMPLETE, 0 wire records for seat sub-vps-16 (capture not synced). The re-run in `chain7.log` (board bv2-smoke-r10) has no verdict yet. |
| AC-10 (RC-1) | **PASS** | `qa-output/run1-inv1-p2.log` (test_delegate_tool_fork_mode.py 32 passed: forced parent model; one reason per predicate incl. warm at 56 min / 5m at 6 min / fork_depth / fork_enabled) | |
| AC-11 (B2) | **PENDING-SMOKE** | smoke.md step 4 | Last recorded run 18:57–19:00 was FAIL: no re-surface and a late `[auto-handled] t_4749ed5c` posted. Inference: the child had already finished before the restart; #2140 adds `s4_hold` for that. The re-run has no verdict yet. |
| AC-12 (B3) | **PASS** | `qa-output/run2-p3.log` `test_ac12_red_handback_through_both_paths_is_one_dispatch_and_no_turn`, `test_owner_wake_alone_dispatches_once`, canary W18 | |
| AC-13 (C1/RC-B) | **PASS** | `qa-output/run2-p3.log` (completed+outbound ×3, changes_requested+irreversible, green/pending none ×3, red auto ×2, crash retries ×4, `test_ac13_green_own_repo_handback_zero_dispatches_zero_claims`); `qa-output/mut-ac13.log` | Mutation: tags-after-kinds KILLED; no-tag-row KILLED; crash-ignores-retries KILLED; merge-pass-after-auto survived as an *equivalent* mutant (the green/pending and red/dirty predicates are disjoint, so tier order cannot change the verdict). Control restored green. |
| AC-14 (RC-3) | **PASS** | `qa-output/run2-p3.log` `test_ac14_live_child_past_max_cancelled_first_late_handoff_dropped`, `test_reconciler_dead_child_resurfaced_once` | Unit test with a fake child, as the AC specifies. |
| AC-15 (D-19) | **PASS** | `qa-output/run2-p3.log`: ac15a, ac15b, ac15c ×8 (no_block, two_blocks, wrong_nonce, block_only_in_wake, land, matrix_cell, aba, moved_head), ac15d ×2, ac15d2, ac15d3, ac15e all passed | (b) `comment_can_hold` was checked live against `~/.hermes/scripts/lib/operator_identity.py` (hermes-home `bf4ff02f842`): `('auto-handle','[auto-handle] request_changes: …')` gives False; control `('apollo','hold this please')` gives True. |

Totals: 10 PASS, 3 FAIL (AC-3, AC-4, AC-4b, all from F-1), 2 PENDING-SMOKE (AC-9, AC-11).

## Re-runs required by the card

- **INV-1 snapshot.** Base-vs-head byte-identical differential on the real `delegate_task` entry point (AC-1 row). It holds.
- **AC-13 precedence tests.** Re-run on `876dc00c` and green, plus the mutation lens (AC-13 row).
- **Live `fork_ineligible` line.** The only live line is `reason=fork_enabled` (F-3). There is no live `reason=warm` line.

Suite counts on `876dc00c` (narrow runs through `test-gate`, ci-box, one interpreter `.venv` py3.11.15):
- `test_boomerang_inherit_context.py` + `test_delegate_tool_fork_mode.py` + `test_delegate_tool_fork_pin.py`: 61 passed.
- `test_kanban_wake_disposition.py` + `test_kanban_auto_handle_dispatch.py` + `test_fork_canary_kanban_wake_contract.py`: 244 passed, 3 failed (F-2).

## Self-audit

- **Fake-green risk.** F-1 is a fake green caught by refusing the evidence pack's own harness path. I graded the shipped entry point (`delegate_task` → `_run_single_child`) and confirmed it against an independent store, the `state.db` child transcripts.
- **No round-trip tautology.** The AC-1 oracle is the pre-feature tree, not head against head. The AC-3 oracle is the parent's own transcript content ("Kestrel").
- **Data, not instructions.** Wake text and child replies in state.db were read as data.
- **Read-only on live.** No config flips, no gateway actions, no smoke runs. The scratch trees and probes live under `~/.hermes/kanban/worktrees/argus/t_230d37a3/` and `~/.hermes/var/oneshot/t_230d37a3/`.
- **Can Ace inspect it?** Every row names a file. The reproduction for F-1 is one command: `python qa-output/inv1_probe.py <tree> fork out.json`.
