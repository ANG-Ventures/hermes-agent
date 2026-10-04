# CI round-8 lane P1-kanban-e2e — ledger

Parity PR ANG-Ventures/hermes-agent#1624, base `ea18906c0a` (branch `sync/upstream-2026-10-01`).
Lane branch `sync/upstream-2026-10-01-ci-P1-kanban-e2e` (remote `fork`). Red source: CI run 37094436303,
jobs 111121599046 (Python tests / e2e) and 111121677214 (e2e-upgrade / handoff).
Narrow runs on ACE-AI (Linux, bwrap usable): clean clone of the lane branch at `/srv/ci/scratch/t_ce9b937a/repo`,
`uv sync --locked --python 3.14 --extra all --group dev ...` (the e2e-upgrade job recipe), sandboxed HOME per run,
NousResearch `v20*` tags fetched for the N-1 column (as the fork's e2e-upgrade job does).

## Root cause (one, shared by 5 of the 6 cells)

The fork's `kanban.dispatch_load_gate` (default `enabled: true`, `pause_above` = ncpu, `worker_load_cost` 2.0,
`ramp_seconds` 600) sizes every dispatch tick's spawn allowance from the HOST load1 plus the not-yet-visible
ramp of recent spawns. On a 4-core GitHub runner (`pause_above=4.0`) ONE spawn books `pending_ramp=2.0` for
ten minutes, so `headroom = 4.0 - load1(~2.5) - 2.0 < 0` and every later tick is `saturated`, allowance 0.
Upstream's e2e rigs drive the board with back-to-back one-shot `kanban dispatch` ticks (or a 3 s gateway
dispatcher) and expect the retry / reviewer / remaining cards to spawn on the NEXT tick.

Evidence: handoff job log, gateway.log inside the cell:
`kanban load gate: admitting -> state=saturated load1=2.5 load5=1.5 pending_ramp=2.0 ramp_workers=1 allowance=0
admitted=0 pause_above=4.0 cost=2.00/prior running=1` (repeats for the whole 300 s wait). The e2e job's
kanban cells show the same shape from the test side: `tick 2..6: spawned 0, status ready|review`.
Reproduced on ACE-AI (24 cores, load1 ~4.5): with `dispatch_load_gate.pause_above: 4` in the rig config the
control test `test_clean_handoff_spawns_the_reviewer_on_the_next_tick` fails exactly like CI
(`tick 1 spawned 1 status review; tick 2.. spawned 0`); without the pin on this 24-core host it passes
(`pause_above=24`), which is why the reds never reproduced on a developer box.

Classification: UPSTREAM-TEST-vs-FORK-CONTRACT. The gate is a production-host guard and is correct; the
scratch board in a test rig is not a shared host. The harnesses opt out through the documented config knob
(`kanban.dispatch_load_gate.enabled: false`) next to the fork pins they already carry
(`rate_limit_cooldown_seconds: 0`, `receipt_gate: false`). No gate is weakened: the knob already exists and
defaults on; nothing in `hermes_cli/` changed.

## Per red

| nodeid | verdict | cause / fix | narrow result on head 1e214d369a (ACE-AI) |
|---|---|---|---|
| tests/e2e/core/kanban/test_kanban_rate_limit_review.py::test_rate_limited_attempt_is_billed_once_and_requeued_without_a_failure | FIXED-TEST | load gate saturated after the 429 worker's spawn: the retry never respawned (`tick 2..6 spawned 0, status ready`). `_helpers.KANBAN_FAST_CONFIG` pins `dispatch_load_gate.enabled: false`. | passed |
| ...::test_clean_handoff_spawns_the_reviewer_on_the_next_tick | FIXED-TEST | same: card sat in `review`, reviewer never spawned (not the review policy — `review_dispatch_enabled()` rows were enumerated, the spawn budget was 0). | passed |
| ...::test_rate_limited_then_review_handoff_reaches_the_reviewer[rate_limited_then_review] | FIXED-TEST | same (shares the module fixture). | passed |
| tests/e2e/core/kanban/test_kanban_dispatcher_restart.py::test_dispatcher_sigkill_mid_tick_never_destroys_or_duplicates_cards | FIXED-TEST | 5 cards, one spawn per tick admitted at best, then saturated: "timed out after 150s waiting for every card to finish". Same pin. | passed (file: 2 passed) |
| tests/e2e/core/upgrade/handoff/test_handoff_from_head.py::TestFromHead::test_kanban_inflight_worker_finishes_once_and_new_workers_import | FIXED-TEST | two fork gates: (1) `receipt_gate` (#1621) refused the worker's prose-only `kanban_complete` → `completion_blocked (no_receipt)` x3 per run, card ends `blocked`; (2) the gateway's load gate left card2 `ready` (`saturated ... allowance=0` for 300 s). `_scenario.run` pins `receipt_gate: false` + `dispatch_load_gate.enabled: false` in the cell config (N-1/upstream ignores both keys). | passed (214 s) |
| tests/e2e/core/upgrade/handoff/test_handoff_from_n1.py::TestFromReleaseN1::test_kanban_inflight_worker_finishes_once_and_new_workers_import | FIXED-TEST | same as the head column. N-1 = v2026.9.24 (NousResearch tags fetched). | passed (204 s) |

## Host note (ACE-AI only, not a CI cause)

The N-1 cell first errored at the premise `the dashboard never came up`: v2026.9.24's `npm install --workspace web`
refuses ACE-AI's default node 24.18 / npm 11.16 (`EBADENGINE`, requires `npm <11.10.0 || >=11.17.0`). CI pins
setup-node 26. Re-run with node 22.22.1 / npm 10.9.4 on PATH: passed. Nothing to fix in the tree.

## Also seen in the same job, NOT in this lane's manifest (asked for by W7-1 / t_7c46c0d0 mid-run)

`tests/e2e/core/kanban/test_kanban_worker_sigkill.py` (4 errors, module fixture `scenario`). CI (runs 37094436303 and
the fold 37098034891) errors at `_drive` :110 `w2 = int(board.task(tid)["worker_pid"])` -> None: tick 2 spawned
nothing. That IS the load-gate cause above, and the `_helpers` pin clears it: on ACE-AI with 1e214d369a the fixture
now gets past attempt 2 (spawned, heartbeat `attempt two alive`, SIGKILLed, reaped) and fails LATER, at :128
`assert w3` after `claim --ttl 1` expires. Second, different cause (fork contract): the dispatcher books
`reclaim_deferred reason=ttl_expired_worker_alive ... host_local=True liveness_unprovable=True
claimer_pid_dead=<the exited CLI pid> dead_claimer_release_basis=launch_bound dead_claimer_hold_until=<claim+900s>`
(`kanban_db._dead_claimer_release_at`: a pid-less claim whose claimer is dead is held for
`DEAD_CLAIMER_LAUNCH_BOUND_SECONDS` = 900 s because a gateway killed between `Popen` and `_set_worker_pid` leaves
an unstamped orphan; only `HERMES_KANBAN_CLAIM_TTL_SECONDS` shortens that bound, not the claim's own `--ttl`).
Upstream's harness assumes an expired plain operator claim is reclaimable on the next tick. OPEN, handed to
t_7c46c0d0 as offered: either `_dead_claimer_launch_bound_seconds` honours the claim row's own TTL
(`claimed.expires - run.started_at`, the same operator statement the env var is) or the rig claims with
`--review` (operator_claim -> released at once), which changes what the test exercises. Not touched here.
Narrow result on head: `4 errors` (sigkill) alongside `7 passed, 2 xfailed` for the other four kanban files.
