# delegate_task child lifecycle (late-completion path)

Status: design of record for `tools/delegate_tool.py` `_ChildLifecycle`,
`_SteerLedger`, the teardown door `_teardown`, and `_start_late_completion`.
Written for card t_1268c9bf after three Prism rounds on the same path
(#1535: 6 P1s, #1542: 6 P1s, #1549: 5 P1s), then revised for card t_27ff68e8
after #1573 drew 9 more (7 real). Every round found another path where an
accepted steer was dropped or a child was torn down under a live turn.

Round 5 changed the approach. #1573 kept I1 and I2 by preserving a flag or
draining a slot at each site, so every new path was a new place to forget.
They now hold by construction, through two mechanisms:

- **I1, the steer ledger.** One append-only record per child. A steer is
  written to it when `steer_subagent` accepts it, and only its consumer (the
  agent loop writing the text into a tool result) settles it. No other path
  has to preserve anything, because no other path can clear it.
- **I2, the teardown door.** One function, `_teardown(child, reason)`, is the
  only way to close a delegated child. It defers while the child's run or any
  of its turns is live. An AST contract test
  (`tests/tools/test_delegate_teardown_door.py`) fails on any new call site
  that closes a child outside it.

## Scope

The lifecycle below starts when `_run_single_child`'s wait hits
`child_timeout` while the child is still working, i.e. when it returns a
`timed_out_running` entry and hands the child to `_start_late_completion`.
Children that finish inside the wait never enter it; the normal path in
`_run_single_child` owns them and is out of scope, apart from the entry edge.

## Actors

| Actor | Thread | Owns |
|---|---|---|
| owner | the `delegate_task` caller in `_run_single_child` | `running` and the `timeout` edge |
| late thread | `delegate-late-<sid>`, runs in a copy of the owner's context | every edge from `timed_out_running` to `persisted` |
| turn worker | executor thread running a child turn (first turn, correction turn) | nothing; it only signals "turn exited" via its future |
| steerer | any thread calling `steer_subagent` | nothing; acceptance is linearized with steer closure under `_active_subagents_lock` |
| parent | the parent agent | nothing; `parent.steer()` is a best-effort nudge, never a store |

## States

| State | Meaning |
|---|---|
| `running` | Owner is waiting on the first turn. Steer is accepted. |
| `timed_out_running` | Owner returned `timed_out_running`; the late thread supervises the first turn under the hang and wall ceilings. Steer is accepted. |
| `correcting` | The first answer failed `output_schema`; the one correction turn is running under the same ceilings. Steer is accepted. |
| `late_completed` | The live turn exited on its own (finished, raised, or was interrupted). Steer is closed. |
| `reaped` | A ceiling fired (hang or wall, first turn or correction turn). Steer is closed; the child is interrupted, its subtree reaped, and the live turn is drained for a bounded time. |
| `persisted` | The one durable late record exists under the owning profile's late dir. |
| `torn_down` | Persistence/tool resources are closed (`child.close()`, relay-session unregister). Terminal. |

## Transitions

Exactly these edges exist. `_ChildLifecycle.fire(event)` is the only way to
change state; any other `(state, event)` pair raises `IllegalTransition`.

| From | Event | To | Owner | Side effects performed by the transition |
|---|---|---|---|---|
| `running` | `timeout` | `timed_out_running` | owner | none (the late thread is started afterwards) |
| `timed_out_running` | `correct` | `correcting` | late thread | the correction turn's future becomes the live turn; `corrections` += 1 (I3) |
| `timed_out_running` | `finish` | `late_completed` | late thread | close steer acceptance |
| `correcting` | `finish` | `late_completed` | late thread | same |
| `timed_out_running` | `stall` | `reaped` | late thread | same |
| `correcting` | `stall` | `reaped` | late thread | same |
| `late_completed` | `persist` | `persisted` | late thread | write the record (once) |
| `reaped` | `persist` | `persisted` | late thread | write the record (once) |
| `persisted` | `amend` | `persisted` | the live turn's exit callback | re-derive `missed_steer` from the ledger and clear `steer_fate_unknown` in the one record |
| `persisted` | `teardown` | `torn_down` | the live turn's exit callback, or the late thread when no turn ran off-thread | records that the late thread has handed the child to the door. The door itself closes it, now or when the last hold goes |

`running` has no exit edge for a child that finishes inside the wait. That
child never gets a lifecycle object: the owner's normal path handles it.
The object is created at the `timeout` edge, by the owner thread.

### Concurrency

Three threads call `fire()`: the owner (`timeout`), the late thread (every
edge up to `persist`), and the live turn's worker thread through the exit
callback (`amend`, `teardown`). `_ChildLifecycle` serializes every
transition under one lock. The ledger and the door each have their own
lock and never call back into the lifecycle. The exit callback
is registered only after `persist` returns, so it cannot interleave with the
first write. `concurrent.futures` runs a callback registered on an
already-finished future immediately, on the registering thread. So there is
exactly one post-`persist` path for every live turn, whether it finished
before or after registration.

The callback runs in a fresh copy of the owner's context
(`contextvars.copy_context()` taken on the late thread, then `.copy()` per
run). It never re-enters `owner_context`. That context is entered by the
late thread, and re-entering it would raise `RuntimeError`, which
`concurrent.futures` swallows, and `amend`/`teardown` would be skipped
silently. `fire()` holds the lifecycle lock only for the state change. The
record write and `parent.steer()` run outside the lock, and neither calls
back into the lifecycle. A deferred door close runs on whichever thread
releases the last hold (usually the turn's worker), in a copy of the
context of the thread that requested the teardown.

Released outside the state machine, at the `finish`/`stall` decision and
unchanged from #1535: the credential lease, the `_active_subagents` entry and
the parent's `_active_children` link. A stuck child must not hold its lease
(the #1535 ceiling contract), and these handles hold no transcript data.

Parent exit is not a state. A parent that finishes, goes idle or is
interrupted propagates an interrupt to the child, so the live turn exits and
the next edge is `finish`, and its `steer()` may refuse the nudge, which the
durable record already covers. A parent that is closed or evicted
(`AIAgent.close()`, `release_clients()`) hands the child to the door, which
defers until the child is released. None of these changes the edge set.

## Invariants

**I1: an accepted steer is never dropped on any path.** A steer that
`steer_subagent` accepted (returned True) is either delivered (written into a
tool result the child's model reads) or named in `missed_steer` of the
child's completion entry or durable late record.

Mechanism, `_SteerLedger` (one per registered child, `child._steer_ledger`):

- `steer_subagent` appends an `accept` entry under the registry lock, BEFORE
  calling `agent.steer()`, so a delivery that happens the instant the text
  lands in the child's slot finds its entry. If `agent.steer()` refuses, the
  entry is marked `withdrawn`.
- The only consumer is the agent loop: `apply_pending_steer_to_tool_results`
  and the pre-API injection call `note_steer_delivered(agent, text)` after the
  text is in a tool result. That calls `child._steer_delivery_sink`, which
  settles the matching entries. Matching is by content, longest first, each
  entry consuming its own span. The agent joins pending steers with newlines
  and can put a drained batch back behind newer text, so order is not
  relied on.
- `missed()` is "accepted and not delivered", in acceptance order, with
  duplicates kept (#1573 :3057). Every completion path reads it: the normal
  path's success, failure and exception branches (through
  `_close_and_read_missed`), the late path's first write, and its amendment.
- Nothing else touches an entry. The finalizer's drain into
  `result["pending_steer"]`, `clear_interrupt()` (the finalizer's reset, and
  `_return_interrupted`'s), and the closure drain in
  `_close_subagent_steering` only empty the agent's in-memory slot. None of
  them can lose a ledgered steer (#1573 :3035, :3045, :3623). The
  `pending_steer` result field is no longer read by delegate_task.
- Durable copy: one JSON line per operation (`accept`, `withdraw`,
  `deliver`) in the owning profile's delegation live dir: next to the
  child's live transcript (`<transcript>.steer.jsonl`), else in
  `live/steer_<sid>_<hex>/steer.jsonl`, which the live-dir retention prune
  also covers. The path is resolved on the spawning thread. Memory is
  authoritative; a failed write is logged once at WARNING.

A steer accepted after closure is impossible: closure (`finish`/`stall`, or
the normal path's completion) sets `accepting_steer=False` under the lock
`steer_subagent` holds.

`steer_fate_unknown: true` now means the record was written while a turn of
the child was still live. That turn may still deliver a ledgered steer, so
the record can over-report `missed_steer`, but never under-report it. When
the turn exits, whether it returned or raised, the exit callback fires
`amend`. That re-derives `missed_steer` from the ledger, clears the flag and
rewrites the same file (#1573 :3862). No parent re-nudge is needed: the set
can only shrink after `persist`.

**I2: no teardown while the child's run or any of its turns is live.**
Mechanism, the door (`_teardown`, with its deferred half `_release_hold`):

- Holds. `_run_single_child` takes a run hold (`_hold_run`) before its
  first turn. Every turn is held from submit until it exits:
  `_submit_turn` (executor turns: the first turn, supervised correction
  turns) and `_inline_turn` (correction turns run on the calling thread).
- `_teardown(child, reason)` closes (`_close_child_persistence`: `child.close()`
  plus relay unregister) only when nothing holds the child. Otherwise it
  records one pending close with a copy of the caller's context, logs a
  WARNING and counts it in `_deferred_teardowns`. The pending close runs
  exactly once, when the last hold goes. `owner=True` is the run's own end:
  `_release_child_resources` on the normal path, and the late thread after
  `persist`. It releases the run hold first. A second request after the
  close is a no-op.
- Parent-driven closes go through the same door. `_run_single_child` stamps
  `child._owner_teardown`. `AIAgent.close()` and `AIAgent.release_clients()`
  hand each active child to `_close_delegated_child(child, reason)`, which
  calls that door. A recursive close of a delegated orchestrator therefore
  reaches its still-running late grandchildren through their own doors
  (#1573 :3564, :4963).
- The AST contract fails if `_close_child_persistence` is referenced outside
  the door, if `tools/delegate_tool.py` calls `.close()`/`.release_clients()`
  anywhere but `_close_child_persistence` (DB handles excepted), or if any
  loop over an agent's `_active_children` in the scanned modules closes a
  child without first handing it to `_close_delegated_child`.

The drain before `persist` stays bounded (`_LATE_STOP_DRAIN_SECONDS`, 5 s),
so delivery of the result is never held hostage by a stuck turn. Only
persistence teardown waits. A turn that never exits leaks its tool resources
until process exit. That is logged at WARNING with the `_deferred_teardowns`
count, so accumulation is observable. This now also applies to a
normal-path child abandoned at `child_timeout` (hung or never started),
which before #1573 round 5 was closed under its still-running worker. The
drain bound is `min(ceiling, 5)`, which is 5 s at every configurable
`child_timeout` (floor 60 s).

**I3: every terminal child has exactly one durable record under its owning
profile, and it carries its attempt count.** `persist` is legal once (from
`late_completed` or `reaped`). It writes `<late_dir>/<late_result_id>.json`,
where `late_dir` was captured on the spawning thread (the owner's
context-local home). An amendment rewrites that same file atomically (tmp +
`os.replace`) and never creates a second one. `schema_retries` is the
machine's `corrections` counter (incremented by the `correct` edge), not a
local. `_apply_output_schema` runs at most one correction turn, so
`correcting` has no `correct` edge (#1573 :3000/:3590 were false positives).

Failure semantics. "Durable" means the file when the disk write succeeds.
If the write fails, `_record_late_result` keeps a memory-only record and
logs a WARNING. If that record is later evicted by the in-memory cap, it
logs the entry at ERROR. I3 then degrades to "exactly one record, in memory,
loudly reported"; it never degrades to zero records silently. The retention
sweep unlinks files older than 7 days. An amendment that arrives later than
that (a turn stuck for a week) recreates the file, which still makes one
record.

## Mapping from the old code

| Old (#1573) | New (round 5) |
|---|---|
| `_ChildLifecycle._steer` list with four "ledger sources" (closure drain, first-turn finalizer, correction finalizer, harvest of a late-exiting future) | `_SteerLedger`, written at acceptance, settled only by delivery |
| text dedup in `add_steer` | none: entries are per acceptance |
| `harvest()` reading a future's `pending_steer`, swallowing a raise | deleted: the ledger does not need the turn's return value |
| `_apply_output_schema` merging the correction turn's `pending_steer` | deleted |
| normal path: `result["pending_steer"]` + closure drain | `_close_and_read_missed` (ledger) |
| `fire("teardown")` refused while `live` is not done; late-path-only deferral | `_teardown` door: run hold + turn holds; every close routes through it |
| `AIAgent.close()`/`release_clients()` calling `child.close()` directly | `_close_delegated_child` -> `child._owner_teardown` -> door |
| `schema_retries` local | `_ChildLifecycle.corrections` |

## Findings closed by construction

#1549 @ 064c531a:

| Finding | Mechanism |
|---|---|
| `:3491` drained correction turn's steer discarded | ledger: settled only by delivery |
| `:3638` teardown after an expired drain | door: the live turn holds the child |
| `:3478` drain targets the finished first turn | `live` is set by `correct`; door holds on the running turn |
| `:3434`, `:3494` first-turn steer lost on correction stall | ledger |

#1573 (pre-merge 25a555f6, post-merge f9d4df5a). One test each in
`tests/tools/test_delegate_round5_findings.py`, RED on f9d4df5a:

| Finding | Verdict | Mechanism |
|---|---|---|
| `:3000` / `:3590` repeated corrections, untracked retry | false positive: at most one correction turn | proof test (green on both heads) |
| `:3862` fate cleared when the exiting future raises | real | ledger: fate re-derived without the future's result |
| `:3623` interrupted-turn exit empties the slot | real | ledger |
| `:3035` / `:3045` steer between finalizer drain and reset | real | ledger |
| `:3057` lossy dedup | real | ledger: one entry per acceptance |
| `:3564` parent-driven close of a live child | real | door via `_owner_teardown` |
| `:4963` recursive ancestor cleanup | real | door, reached by the recursion |

## Test obligations

- One test per finding, RED on the head where it was raised.
- An interleaving property test (`test_interleavings_preserve_i1_i2_i3`)
  drives seeded random orders of `timeout`, `steer`, `schema-fail`,
  `correction-timeout`, `late-finish`, `parent-exit`, `future-raise`,
  `interrupted-exit`, `parent-close` (real `AIAgent.close`) and
  `ancestor-cleanup` (the door on the orchestrator, recursing through the
  real `AIAgent.close`). The child's steer slot, `clear_interrupt` and
  `_return_interrupted` are the real `AIAgent` code, and delivery goes
  through the real `note_steer_delivered`. It asserts I2 and "at most one
  record" at every step. At the terminal state it asserts exactly one
  record carrying the correction count, and I1 exactly:
  `missed_steer == accepted - delivered`.
- Door unit tests: the run and turn holds defer the close, the close runs
  exactly once on the last release, and the deferred counter returns to its
  baseline.
- Ledger unit test: duplicates are kept, a withdrawn entry is not missed,
  matching is longest-first, and the durable operation log is complete.
- The AST contract test (`test_delegate_teardown_door.py`) is RED on
  f9d4df5a, naming its four bypasses (two door references, two run_agent loops). It includes killer mutations: a new
  door bypass in each of the three rule shapes must be reported.
- A model-level test enumerates every `(state, event)` pair and asserts only
  the edges above are legal.
