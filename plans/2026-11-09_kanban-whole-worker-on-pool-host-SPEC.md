# Kanban: run the whole worker on the pool host (KWLB v1.0) vs the cheap gate fix

Card t_4a046fb0 (origin: Apollo session 20261108_093540_fa4b70b9; Ace 2026-11-08 22:40 "get kanban
work off the Mac Studio now"). Status: SPEC, measured. Momus pass is requested on a separate card.

This document answers the card's three asks. Section 1 is the measurement, and it changes the
premise. Section 2 is the cheap fix (step 2). Section 3 is the whole-worker design (step 1). Section 4 compares them.

## 0. Verdict

- The cheap fix is **already on main**. Since remote-first (#2148, t_82b4db80; live
  `kanban.placement.default: remote`), `pins_only` is always False, so `_route_wait` never holds a
  portable row `local_full` while a pool host has a slot, whatever the Studio band is. A red-first test was
  added (`test_saturated_studio_sends_portable_to_free_pool_host`). It is green on unmodified main,
  and the mutation `pins_only -> True` turns it red. No production diff is needed.
- Every `local_full` hold in 14 days is a **Studio-bound** card (route `local`), never a portable
  one: 7,188 holds on 2,309 cards from 2026-10-26 to 2026-11-09, route=portable **0**, checked on
  the root board and on every `kanban/boards/*/kanban.db`. The Studio gate holding them is correct.
  They cannot run anywhere else as written.
- Running the whole worker on the pool host (KWLB v1.0) moves **about 0 CPU**. Measured on the
  Studio: a remote-placed worker loop costs 0.007 cores and about 330 MB RSS. The v1.0 trigger
  already ruled in the KWLB PRD §6 (Ace, 2026-10-03: "Studio worker-loop CPU share exceeds 20% of
  ncpu") is at **0.06%**. Do not build v1.0 now.
- What does move Studio load is **fewer Studio-bound cards** (section 4.2). Most of the Studio's
  busy cores are not kanban workers at all.

## 1. Measurement (Studio, 2026-11-09 02:10-02:30 PST, mac-studio-m3u, 32 cores)

### 1.1 CPU by owner, cputime deltas (`ps time=` before/after, not decayed `%cpu`)

Script: `t4a_cpu_attr.py` (card attachment). A worker = an `hermes_cli.main ... --skills
kanban-worker` process plus all of its descendants. "Remote loop" = the worker has an `ssh` child
(its tools run on a pool host). Three windows were measured. Here is the 121 s window:

| owner | workers / procs | cores | RSS GB |
|---|---|---|---|
| other (non-fleet: claude CLIs, fseventsd, pyright, VM services, ...) | 1745 | 6.96 | 123.6 |
| fleet scripts (`scripts/...`: wirelog sync, prism_holds, lints, subs-bake, ...) | 235 | 3.18 | 16.1 |
| gateway (`gateway run`, incl. dispatcher) | 9 | 1.82 | 14.6 |
| OrbStack VM | 11 | 1.55 | 104.4 |
| **kanban workers, local** | 12 | **0.65** | 6.6 |
| **kanban workers, remote loop** | 3 | **0.02** | 1.0 |
| total busy | | 14.2 of 32 | |

The other windows: 60 s (2 remote, 0.00 cores; 14 local, 0.28 cores) and 90 s (16 workers, 0.65
cores total). `vm.loadavg` was 43-75 the whole time while busy cores were 13-17. On macOS, load
counts every runnable and uninterruptible thread, and there were about 1,700 processes. So
load1 / 32 overstates CPU saturation by roughly 3x.

### 1.2 Where workers ran (placement events, root board)

Last 24 h of spawns: remote 506 (remote-policy 359; worktree_port pins 140; explicit pin 7) vs local 245.
The local ones, by rule: `workspace_kind` 114, `host_studio` 53, `local_files` 53,
`requires:studio-state` 11, `studio_needs` 6, `default` 5, `requires:browser-session` 2, access 1.
Running now: 7 remote, 8 local (by rule: workspace_kind 4, local_files 2, host_studio 1,
studio-state 1).

### 1.3 `local_full` holds per hour (root board, distinct cards per hour)

| PST hour (11-08) | 12 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 | 23 | 11-09 00 | 01 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| holds | 56 | 99 | 137 | 141 | 184 | 155 | 113 | 205 | 264 | 280 | 273 | 284 | 279 |
| cards | 31 | 43 | 44 | 53 | 60 | 58 | 62 | 86 | 101 | 102 | 99 | 97 | 99 |

All rows are route `local`. Breakdown of the 149 distinct cards held in the last 8 h, by body
pin and workspace: `host: studio` line 89 (10 of them with a pool `origin_host`), unpinned with a
`worktree` workspace 26, unpinned scratch with a workspace path set 13, unpinned scratch with no
workspace 6 (e.g. t_610596ed, assignee `apollo`, which the pool does not serve), `host: any` with a
continuation origin 6, `requires:studio-state` (soft body match) 5, explicit
`host: mac-studio-m3u` 2, `host: ci-box.` (malformed pin) 1, `studio_needs` 1. The `binding` field
is `gate` (load gate saturated) on 73% of holds (1,405) and `quota` (allowance spent) on the rest (517).

The card's premise ("holds every card, including portable ones, with held: local_full") matches
the code path that existed before #2148. It does not match any event in the last 14 days.

## 2. Step 2, the cheap fix: status

The card asks: gate pool-bound spawns on the TARGET host's band, not the Studio's. On main:

- `_route_wait` (`kanban_db_dispatch.py:5925`) holds a `portable` row `local_full` only when
  `plan.pins_only`. `plan_pool` (`gateway/kanban_gate_tick.py:387`) sets `pins_only` False whenever
  `kanban.placement.default` is `remote`, and it is (config, and `placement_default()` forces it,
  test `test_plan_pool_reads_the_flag_per_tick`).
- A portable row then reaches `_portable_goes_remote` (`:5956`), which returns True whenever
  `default_remote`. `take()` judges the TARGET host: slots, band, pressure, unified admission
  (`kanban_worker_pool.py:1972`). The Studio allowance (`local_budget`) only decides the
  last-resort local spawn (`_last_resort_allowed`).
- The total budget (`spawn_budget`: max_spawn, max_in_progress, memory, `--max`) still counts remote
  spawns. That is intentional (the loop runs on the Studio and holds RSS there), and live it is
  unset or high: tick_gate `max_in_progress: null` on this card's own placement event.

Shipped: `tests/hermes_cli/test_kanban_placement_route_first.py::
test_saturated_studio_sends_portable_to_free_pool_host`. The test sets up band `spilling`, allowance 0,
not paused, and one free pool host. The portable card spawns `remote-policy` on ace-ai, and only the
`host: studio` card waits `local_full`. It is green on main. The mutation (force `_route_wait` to hold portable
rows) gives `assert [] == [(id, 'ace-ai')]`.

Scope split with t_f60f502f (#153, tick lock): this card does not change how remote starts are
charged, so the `_hold_lock` `others` subtraction edit named in that split is **not triggered**.

## 3. Step 1, the whole worker on the pool host (KWLB v1.0): design

The design is recorded so the trigger can be acted on. Do NOT build until the trigger in 4.3 fires.

### 3.1 What stays on the Studio

- The board: `kanban.db` (all boards), its writers, and the dispatcher (claim, reclaim, TTL,
  placement ledger). One writer process. SQLite over a network FS is rejected (PRD §6 v1.0).
- The review/merge lanes: review rows are local-only today (`spillover is None` guard; reviews
  never offered to the pool), along with the lander, fleet-merge and the merge queue.
- Model credentials of the subscription lanes (claude-dtlr/apr bridges, cpa). They are bound to
  Studio-local bridges. A remote loop calls them over the LAN (3.3).
- Gateway notifications, home-chat delivery, and the survivor capture writer.

### 3.2 What moves

The worker process: `python -P -m hermes_cli.main -p <profile> --cli --skills kanban-worker
...` runs as `kanbanw@<host>` from the pool's existing install (`~/repo`, fleet-gh-cred). Its
tools run locally on that host, so the `ssh` terminal backend is no longer used for a placed run.

### 3.3 Channels (the existing ones, extended)

| Need | Today (tools-remote) | v1.0 |
|---|---|---|
| kanban_* tool writes (heartbeat, comment, complete, block, review) | in-process SQLite on Studio | **dispatcher RPC**: an authenticated local-net endpoint on the Studio gateway that accepts the same verbs `tools/kanban_tools.py` calls, keyed by a per-run token minted at spawn (the existing pid-anchored owner grant becomes a token grant). The worker's `kanban_*` tools pick the RPC client when `KANBAN_RPC_URL` is set. Same validation, same events, one writer. |
| Liveness | dispatcher checks pid + start token locally | RPC heartbeat with the run token. The dispatcher reclaims on missed heartbeats (the existing `dispatch_stale_timeout_seconds`) plus an ssh `kill -0` probe through the pool's existing probe channel. pid checks become host-qualified. |
| Spawn / kill | `Popen` on the Studio | `ssh kanbanw@host` with a detached launcher that writes `pid start_token` back on stdout. `kill` goes over the same channel. |
| Model calls | Studio process -> Studio bridges on localhost | remote process -> Studio bridge over the LAN (bridges already listen for fleet clients; see claude-relay-client-onboarding). No credential is copied to the host. |
| Worker log | Studio file | host file, tailed back by the existing probe on completion |
| Survivor capture | Studio `preserve()` | on-host `git bundle` + `kanban_attach` over RPC |

### 3.4 Failure modes it adds

- Studio RPC down: every remote worker loses its board. It must block locally and retry, never
  exit silently. The run is booked `reclaimed`, not `crashed`.
- Network partition: a host can keep working with no heartbeats. The run token must be fenced
  (the dispatcher rejects a write whose run_id is no longer current) before any reclaim respawns it.
- Version skew: the host's `~/repo` must match the Studio runtime's RPC schema. Gate the spawn on a
  version probe.

### 3.5 Cost

The RPC endpoint and client, token grants, remote spawn/kill/liveness, fencing, the bundle path, and
host version gating. Most of these touch `kanban_db_dispatch.py`, which already has four other open
cards (collision warning on this card). Rough size: several PRs, 2-3 weeks of review.

## 4. Comparison: which moves more load

### 4.1 Whole worker (step 1)

It moves the 0.007 cores and about 330 MB per remote-placed worker that the loop costs today. At the
measured 3-7 concurrent remote workers that is **at most 0.05 cores and about 2.3 GB RSS**. Most of
the loop's work is waiting on the model API. It also does not touch the 245 local spawns per day,
because those are local for a capability reason, not because of the agent loop.

### 4.2 Cheap fix (step 2) and the levers that do move load

Step 2 as specified is already live, and it moves nothing new. The levers that move kanban work
off the Studio, largest first (from 1.2 and 1.3):

1. **`host: studio` lines on minted cards** (53 spawns/day, 89 of 149 held cards). Most come from
   the minting session, not from a capability need. Lint them at create time, the same way the
   auto-pin comment already explains `requires:`.
2. **Studio-bound workspaces** (`workspace_kind` 114/day): a card created with `--workspace
   dir:/Users/...` or a Studio worktree. v0.4 (worktree on the pool host via `kanban clone`) covers
   hermes-agent and hermes-home. Repoint the minting defaults to it.
3. **Soft body matches** (`requires:studio-state` matched the word "kanban.db" in this card's own
   body). It is soft, so lower it to `warn` for prose matches.

Each of these is a separate card. None is a change to the load gate.

But the Studio's busy cores are mostly not kanban workers. Of 14.2 busy cores, local workers are
0.65. Fleet scripts take 3.2, the gateway 1.8 and the OrbStack VM 1.6. Getting "kanban work off the
Studio" will not bring load1 under the gate's line by itself.

### 4.3 Trigger for building step 1

Unchanged from PRD §6 v1.0: Studio worker-loop CPU over 20% of ncpu (6.4 cores), measured by
`t4a_cpu_attr.py`-style cputime deltas over at least 10 minutes, or a ruling from Ace. Today:
0.02 cores remote, 0.65 total.

## 5. Live proof (card step 3)

| metric | card's "before" (11-08 23:20-23:35) | now (11-09 02:10-02:30, same main, no new deploy) |
|---|---|---|
| worker processes on Studio | 22 (4 doing local work) | 15-18; 3-7 remote loops, 11-14 local |
| Studio load1 | 46.9 | 43-75 (busy cores 13-17 of 32) |
| `local_full` holds / hour (root board) | 273 (23:00 hour) | 284, 279 (00, 01 hours) |
| of which portable | not measured | 0 (14-day window: 0 of 7,188) |

Nothing changed between the two columns, because nothing needed to change for portable cards. The
`local_full` rate is the Studio-bound queue waiting on a saturated Studio. It falls only when the
levers in 4.2 land or the Studio's non-kanban load drops.
