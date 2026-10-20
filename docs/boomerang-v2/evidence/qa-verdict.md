# Boomerang v2 milestone QA verdict (Argus; t_230d37a3, re-check t_df1e3b88)

QA VERDICT: **PASS-WITH-CAVEATS**  Readiness: **SHIP**

All 15 ACs now pass. F-1 is fixed on the shipped path. This re-check re-graded only the five rows the first pass left open: AC-3, AC-4 and AC-4b (FAIL from F-1), and AC-9 and AC-11 (PENDING-SMOKE). The other 10 rows are unchanged from the first verdict (`b681179d`).

## Re-check (t_df1e3b88, 2026-10-20)

- **Trees under test.**
  - Live runtime `~/.hermes/runtime/hermes-agent` at `d34dee569e8f`.
  - Verdict branch base fork/main `a98c8a3b4a37`.
  - Both contain #2144 `05071447` (F-1 fix), #2147 `c34c07e7` (evidence trimming) and #2152 `e077c0ef` (real-path AC-4/4b): `git merge-base --is-ancestor` rc=0 for each.
  - #2154 `ea6b06df` (smoke.md) is on fork/main. It is docs and the smoke harness only.
- **Evidence.** This pass's artifacts are committed beside this file in `docs/boomerang-v2/evidence/qa-output/`, named `qa-output/` in the re-graded rows below. The first pass's artifacts are outside the repo in `~/.hermes/kanban/worktrees/argus/t_230d37a3/qa-output/`, named `qa-output/` in the 10 unchanged rows.
- **Read-only on live.** No config flips, no gateway actions, no board writes. I used stores and logs only. The one network call was a read-only Discord GET of the origin channel.

### F-1 re-check: RESOLVED

- **Probe.** `qa-output/inv1_probe.py <tree> fork <out>` is the same oracle that reproduced F-1. Run against the live runtime tree, its output is `qa-output/f1-recheck-runtime-fork.json` (stdout `f1-recheck-runtime-fork.stdout`).
  - The child's first request now has messages `[user "The Kestrel canary is at 12 percent.", assistant "Outcome: done…", user "[BOOMERANG FORK — … Your task now: verify the canary …]"]`.
  - After `cache_control` is normalised, the child's first message equals the parent's.
  - `system`, `tools`, `tool_choice`, `model` and `max_tokens` are byte-equal to the parent's request. The only difference is `extra_headers`, which carries the pin `x-hermes-pin-sub: sub-vps-16`.
  - Negative control: the first pass's `inv1-head-fork.json` on `876dc00c` had messages `[user "verify the canary"]`, with 0 "Kestrel" and 0 frame.
- **#2147 trimming.** The same probe's `delegate_task` result has 0 "Kestrel" and 0 "BOOMERANG FORK" in it, and `tool_trace=[]`. The inherited rows feed the request only.
- **Live, Aegis.** In `~/.hermes/profiles/aegis/state.db`:
  - Parent `20261019_222303_762e2b`, row 992510 at 22:23:22, plants `BV2-F1-AD325D`.
  - Child `20261019_222347_460da3` has one user row, 992514, which is the `[BOOMERANG FORK …]` frame. The nonce is not in it.
  - The child's reply, row 992515, is `BV2-F1-AD325D`. The child wrote 215 cache tokens.
  - The nonce appears in no other session in that DB (count 0).
  - The Aegis agent.log has `boomerang.fork served_by=sub-vps-2 read_share=0.9969 prefix_tokens=69910`.
- **Live, default profile, with a negative control from the same day.** Fork children of `20261018_130347_87039e56` in `~/.hermes/state.db` and `~/.hermes/logs/agent.log`:
  - 22:26:41 `20261019_222634_921ff8`: read_share 0.2242, 59,588 cache write, first user row = the bare auto-handle GOAL, no frame. The 22:21 deploy of `05071447` ran with `--no-restart` (`var/bv2-smoke/deploy2144c.log`), and the gateway restarted only at 22:27. So this child ran the OLD code.
  - 22:59:27 `…225910_d3f0c8`: 0.9845. 23:06:37 `…230616_54025d`: 0.9979. 00:03:54 `…000336_66a346`: 0.9983, 408,697-token prefix.
  - In every one of these three, the first user row starts `[BOOMERANG FORK`.

### AC-11 adjudication: I agree with Apollo (PASS)

I checked it against four independent stores, not the harness:

1. **The child was cut mid-run.** Child `20261020_000336_66a346` was dispatched at 00:03:34.759. Its last transcript row is 00:04:22 (a tool result), `ended_at` is null and it made no handoff. The old gateway was force-exited at 00:04:28 (`Shutdown watchdog fired after 50s`). The new boot logged `Gateway running` at 00:05:02.703.
2. **Re-surface on the first post-boot reconcile.** `gateway.log` shows the reconciler line `t_1b09baf2 lost reason=gateway restart resurfaced delivered=True` at 00:05:30.235. `kanban owner-wake` logged 10 ms earlier, and in `gateway/kanban_watchers.py` L3372-3386 `owner_wake.tick` runs immediately before `reconcile` in the same notifier tick body.
   - Inference from the code (`kanban_auto_handle.py::reconcile` L1045-1065): the claim was open and its child was not in the emptied in-process registry. So any earlier completed tick would have re-surfaced it.
   - No `reconcile failed` warning is logged. 00:05:30 is therefore the first tick to reach the reconciler, which is "within one tick" by construction.
   - The harness FAIL is its own 25 s window. The line came 27.5 s after boot on a loaded host.
3. **The claim ledger agrees.** In `~/.hermes/gateway/kanban_auto_handle.json`, `bv2-smoke-r14|t_1b09baf2|4` has `state=terminal outcome=lost reason="gateway restart" resurfaced=true resurface_delivered=true`.
4. **The wake reached the home session exactly once.** `state.db` session `20261018_130347_87039e56` has exactly 1 user row `[kanban] Task t_1b09baf2 completed…` (row 2166658, stamped 00:05:30). The session was mid-turn, so the queued wake was persisted after that turn. This is normal surfaced-wake behaviour.
5. **Exactly one ⚠️ line and no late completion.** `qa-output/ac11-discord-origin-read.json` counts every message in the origin channel 1561469136796782633 from 00:03 to 00:30 PDT (script `qa-output/ac11_discord_read.py`): 1 × `⚠️ auto-handle lost t_1b09baf2 (reason: gateway restart)` at 00:05:30.497, and 0 × `[auto-handled] t_1b09baf2`.

## Findings carried from the first pass

- **F-1 (Blocker):** RESOLVED by #2144. See above.
- **F-2 (Major, 3 host-load tests red on main):** #2150 (`af07f462`, "cpu-axis host contention exempt for auto-handle too; #2129's tests red on main") touches that test file and the gate. I did **not** re-run it in this pass, which was scoped to the five rows. It is open until someone reads CI on the #2150 head.
- **F-3 (Minor, no live `fork_ineligible reason=warm` line):** not re-checked. It stands.
- **F-4 (Minor, INV-1 self-comparison test):** unchanged. The AC-1 base-vs-head differential covers it.

## AC table

| AC | Verdict | Artifact | Note |
|---|---|---|---|
| AC-1 (INV-1) | **PASS** | `qa-output/run1-inv1-p2.log` (test_boomerang_inherit_context.py 20 passed); `qa-output/inv1-{base,head}-{plain,inherit_true,inherit_false}.json` | v1 suite: 0 commits since base `ffdaa7d6` (`git log ffdaa7d6..876dc00c -- tests/tools/test_boomerang_inherit_context.py` is empty). Real `delegate_task` child first request, base `ffdaa7d6` vs head `876dc00c`, normalised for ids/timestamps: plain sha `24acc8db…` = `24acc8db…`; inherit_context=True `88a13837…` = `88a13837…`; inherit_context=False is equal too. No pin header in any of them. |
| AC-2 (P1) | **PASS** | `docs/boomerang-v2/evidence/p2-cache-trials.jsonl` (16/16 pin == served_by == parent seat, recomputed); `qa-output/run1-inv1-p2.log` (test_delegate_tool_fork_pin.py 9 passed); `qa-output/inv1-head-fork.json` (pin present) vs `inv1-head-inherit_true.json` (no pin) | Caveat: the evidence is response `x-pool-served-by` rows, not relay usage-ledger `sub_key` rows. On the real entry point the pin is applied correctly (F-1 does not affect the pin). |
| AC-3 (P2, INV-2) | **PASS** *(re-graded; was FAIL)* | `docs/boomerang-v2/evidence/qa-output/f1-recheck-runtime-fork.json`; Aegis `state.db` rows 992510/992514/992515; `~/.hermes/state.db` fork children 22:59/23:06/00:03 | On the real `delegate_task` → `_run_single_child` path, the child's first request = the parent transcript ("Kestrel" turn + reply) + the `BOOMERANG FORK` frame. system/tools/model are byte-equal to the parent's. Live: the Aegis child recalled `BV2-F1-AD325D`, which existed only in the parent's earlier turn. Negative control: the 22:26 child on the pre-restart (old) code got the bare goal at read_share 0.224. |
| AC-4 (P2 eval) | **PASS** *(re-graded; was FAIL)* | `docs/boomerang-v2/evidence/p2-cache-trials-realpath.jsonl`; `p2-realpath-README.md`; `tools/dev_probes/boomerang_v2_p2_live.py` L136-145 | Recomputed from usage: 16/16 trials, `mode=fork`, pin == served_by == parent seat `sub-vps-10`, prefix 326,884 tokens, min read_share **0.99938** (gate ≥ 0.85), 23:14–23:18. The harness now calls `delegate_tool._run_single_child` (verified in source). Caveat: it builds the child with `_build_child_agent` directly, so `delegate_task`'s eligibility gate is not on this path. Production corroborates on the full path: 4 post-fix forks read 0.9845–0.9983. |
| AC-4b (P2 cost) | **PASS** *(re-graded; was FAIL)* | `docs/boomerang-v2/evidence/p2-cost-pairs-realpath.jsonl` | Recomputed every `cost_input_equiv` from `usage_total` with the README ratios (read 0.1, 1h write 2, 5m write 1.25, output 5). All 10 arms match the rows exactly. Fork ≤ fold 5/5: 87,825 vs 179,687; 105,222 vs 196,885; 84,005 vs 178,461; 70,311 vs 160,773; 146,014 vs 220,471. Every fork arm has `effective_mode=fork`, first read_share ≥ 0.9990; every fold arm is `summary`. Caveat: the `red_review` fork arm answered "couldn't find the test file" while the fold arm answered. Both used 3 requests, so the saving is not from doing less. AC-4b grades cost, not answer quality. |
| AC-5 (INV-4 gate) | **PASS** | `docs/boomerang-v2/evidence/inv4-probes/results.jsonl` | Recomputed: 20 probes, 5 prefixes (55k–497k), recall 19/20 ('7F3A'), disavowal 0/20 under the fixed regex; the one miss (500k #3) was `stop_reason=tool_use`. The gate is a raw-wire framing probe and stands on its own. Because of F-1, production children never receive this framing. |
| AC-6 (INV-5) | **PASS** | `qa-output/run2-p3.log` (test_kanban_wake_disposition.py 192 passed: every kind 11, kind×payload 110, owner-wake reasons 6, needs_input 27) | Live corroboration: smoke step 1 checks 3–4 (needs_input card not auto-handled). |
| AC-7 (INV-6) | **PASS** (caveat) | `~/.hermes/var/bv2-smoke/smoke.md` step 1 (PASS, 16:32–16:34); gateway.log 16:33:15 `auto-handled t_78080b3d disposition=auto` | Caveat: in the forced-failure arm, ⚠️ was posted but the gateway.log line at 16:33:28 says `no live home for t_dcfcc7f0; wake not re-surfaced`. The injected home did not exist, so the "re-surfaced wake" half is proven only in the unit test `test_child_crash_posts_warning_and_resurfaces_the_wake` (passed). |
| AC-8 (INV-7) | **PASS** | smoke.md step 2 (PASS, 16:40–16:42; `fork_ineligible reason=fork_enabled` at 16:41:21; auto_handle_enabled=false gave no auto-handle in 20 s); `qa-output/run2-p3.log` `test_kill_switch_flipped_between_ticks_next_tick_surfaces` passed | Config restored to true/true (step 2 check 10). |
| AC-9 (INV-8) | **PASS** *(re-graded; was PENDING-SMOKE)* | `docs/boomerang-v2/evidence/qa-output/ac9-wire-rescan-sub1.json` (script `ac9_wire_scan.py`); `docs/boomerang-v2/evidence/smoke.md` step 3 | My own re-scan of `~/.claude-wirelog-fleet/sub-1.jsonl` for 05:58–06:07Z: 30 records, 0 with an `x-hermes-*` name in headers/headerOrder/rawNames/outHeaders. The fork child's 3 API calls join to records by exact usage: call #1 cache 335,531 = `916331-6965`; calls #2/#3 cache 340,821 = `6967`/`6970`. Each carries 21 headers, none `x-hermes-*`. Whole-file grep of the 2.3 GB capture: 0 `x-hermes` occurrences. Caveat (no positive control): no capture has ever shown an `x-hermes-*` header, so the capture point's ability to see one is inferred, not proven. Other custom `x-*` headers (`x-app`, `x-stainless-*`, `x-claude-code-session-id`) are captured. |
| AC-10 (RC-1) | **PASS** | `qa-output/run1-inv1-p2.log` (test_delegate_tool_fork_mode.py 32 passed: forced parent model; one reason per predicate incl. warm at 56 min / 5m at 6 min / fork_depth / fork_enabled) | |
| AC-11 (B2) | **PASS** *(re-graded; was PENDING-SMOKE)* | `docs/boomerang-v2/evidence/qa-output/ac11-discord-origin-read.json` (script `ac11_discord_read.py`); `~/.hermes/logs/gateway.log` 00:05:30.235; `~/.hermes/gateway/kanban_auto_handle.json` claim `bv2-smoke-r14\|t_1b09baf2\|4`; `~/.hermes/state.db` row 2166658; `docs/boomerang-v2/evidence/smoke.md` step 4 | The child was in flight (last row 00:04:22, no handoff) when the gateway was force-exited at 00:04:28. After the 00:05:02 boot, the first reconcile (00:05:30) re-surfaced the wake once: 1 home-session wake row and 1 ⚠️ line in the origin channel. 0 late `[auto-handled]` through 00:30. Claim terminal `outcome=lost resurface_delivered=true`. The harness FAIL is its own 25 s window; I agree with Apollo's PASS (see adjudication above). |
| AC-12 (B3) | **PASS** | `qa-output/run2-p3.log` `test_ac12_red_handback_through_both_paths_is_one_dispatch_and_no_turn`, `test_owner_wake_alone_dispatches_once`, canary W18 | |
| AC-13 (C1/RC-B) | **PASS** | `qa-output/run2-p3.log` (completed+outbound ×3, changes_requested+irreversible, green/pending none ×3, red auto ×2, crash retries ×4, `test_ac13_green_own_repo_handback_zero_dispatches_zero_claims`); `qa-output/mut-ac13.log` | Mutation: tags-after-kinds KILLED; no-tag-row KILLED; crash-ignores-retries KILLED; merge-pass-after-auto survived as an *equivalent* mutant (the green/pending and red/dirty predicates are disjoint, so tier order cannot change the verdict). Control restored green. |
| AC-14 (RC-3) | **PASS** | `qa-output/run2-p3.log` `test_ac14_live_child_past_max_cancelled_first_late_handoff_dropped`, `test_reconciler_dead_child_resurfaced_once` | Unit test with a fake child, as the AC specifies. |
| AC-15 (D-19) | **PASS** | `qa-output/run2-p3.log`: ac15a, ac15b, ac15c ×8 (no_block, two_blocks, wrong_nonce, block_only_in_wake, land, matrix_cell, aba, moved_head), ac15d ×2, ac15d2, ac15d3, ac15e all passed | (b) `comment_can_hold` was checked live against `~/.hermes/scripts/lib/operator_identity.py` (hermes-home `bf4ff02f842`): `('auto-handle','[auto-handle] request_changes: …')` gives False; control `('apollo','hold this please')` gives True. |

Totals: **15 PASS, 0 FAIL** (first pass: 10 PASS, 3 FAIL, 2 PENDING-SMOKE). Re-graded rows: AC-3, AC-4, AC-4b, AC-9, AC-11.

Note on the AC-5 row (unchanged text): its last sentence, "production children never receive this framing", described F-1 and no longer holds after #2144. Production children now do receive the frame (AC-3).

## Disagreements and caveats

1. **AC-11 harness window (#2154).** I agree the 25 s window was wrong. But `--first-tick-seconds` defaults to 90 s, so the harness check now accepts anything up to 90 s. I graded "within one tick" from the tick-order argument (owner-wake → reconcile in one tick body, and no earlier tick could have skipped the open claim), not from that window. A tighter oracle would be to log the notifier tick sequence number with the reconcile line.
2. **AC-9 has no positive control.** The capture has never shown an `x-hermes-*` header, so "the capture would see one" is inferred from other `x-*` headers being captured. A one-request canary would close this: send a harmless `x-hermes-qa-canary` through a lane that does not strip it, and confirm the capture records it.
3. **AC-4/4b harness entry.** It now goes through `_run_single_child` (the F-1 seam) but builds the child with `_build_child_agent`, not `delegate_task`. The eligibility gate is covered by AC-10 unit tests and by the live forks, not by this pack.
4. **Deploy without restart leaves old code live.** The 22:26 child ran pre-fix code 5 minutes after `05071447` was "deployed". This is the expected `--no-restart` behaviour, and the deploy log says so. Recording it because it is the negative control in AC-3.

## Self-audit

- **Fake-green risk.**
  - AC-3 uses the same probe that caught F-1, on the live runtime tree, plus a store-level recall test whose nonce exists only in the parent turn. A green here needs the parent transcript in the child request.
  - AC-4/4b rows were recomputed from raw usage, not from the `pass` or `fork_le_fold` fields.
  - AC-9 and AC-11 were re-derived from the wire capture, the claim ledger, `state.db` and the Discord channel, not from smoke.md.
- **No round-trip tautology.** The oracles are the parent's own content ("Kestrel", `BV2-F1-AD325D`), the pre-fix child at 22:26 as a negative control, provider usage joined to wire records, and the Discord API.
- **Data, not instructions.** The drill card body and the child transcripts were read as data.
- **Real build tested.** The live runtime `d34dee56`. The verdict file is branched from current fork/main `a98c8a3b`.
- **Can Ace inspect it?** Every re-graded row names a committed file. To reproduce F-1 or its fix: `python docs/boomerang-v2/evidence/qa-output/inv1_probe.py <tree> fork out.json`, then check `child_requests[0].messages` for the "Kestrel" turn and the `BOOMERANG FORK` frame.
