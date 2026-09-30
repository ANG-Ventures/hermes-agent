# delegate_task child lifecycle (late-completion path)

Status: design of record for `tools/delegate_tool.py` `_ChildLifecycle` and
`_start_late_completion`. Written for card t_1268c9bf after three Prism
rounds on the same path (#1535: 6 P1s, #1542: 6 P1s, #1549: 5 P1s). Each
round found another path where an accepted steer was dropped or a child was
torn down under a live turn. The code tracked state in scattered locals
(`stop`, `hung`, `finalizer_pending`, `active_turn`) and each fix patched one
path. This document makes the state machine explicit, and the code follows
it through one transition function.

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
| `timed_out_running` | `correct` | `correcting` | late thread | the correction turn's future becomes the live turn |
| `timed_out_running` | `finish` | `late_completed` | late thread | close steering, drain registry text into the steer ledger |
| `correcting` | `finish` | `late_completed` | late thread | same |
| `timed_out_running` | `stall` | `reaped` | late thread | same |
| `correcting` | `stall` | `reaped` | late thread | same |
| `late_completed` | `persist` | `persisted` | late thread | write the record (once) |
| `reaped` | `persist` | `persisted` | late thread | write the record (once) |
| `persisted` | `amend` | `persisted` | the live turn's exit callback | rewrite the one record with steer text found after `persist` (ledger source 4), or clear `steer_fate_unknown`; re-nudge the parent only for new text |
| `persisted` | `teardown` | `torn_down` | the live turn's exit callback, or the late thread when no turn ran off-thread | `child.close()` and relay unregister; refused while any turn is live |

`running` has no exit edge for a child that finishes inside the wait. That
child never gets a lifecycle object: the owner's normal path handles it.
The object is created at the `timeout` edge, by the owner thread.

### Concurrency

Three threads call `fire()`: the owner (`timeout`), the late thread (every
edge up to `persist`), and the live turn's worker thread through the exit
callback (`amend`, `teardown`). `_ChildLifecycle` serializes every
transition and every ledger read or write under one lock. The exit callback
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
silently. `fire()` holds the lifecycle lock only for the state change and the
ledger update. The record write and `parent.steer()` run outside the lock,
and neither calls back into the lifecycle.

Released outside the state machine, at the `finish`/`stall` decision and
unchanged from #1535: the credential lease, the `_active_subagents` entry and
the parent's `_active_children` link. A stuck child must not hold its lease
(the #1535 ceiling contract), and these handles hold no transcript data.

Parent exit is not a state. A parent that finishes, goes idle or is
interrupted does two things. It propagates an interrupt to the child, so the
live turn exits and the next edge is `finish`. Or its `steer()` refuses the
nudge, which the durable record already covers. Neither changes the edge
set.

## Invariants

**I1: an accepted steer is never dropped on any path, provided the live
turn exits.** A steer that
`steer_subagent` accepted (returned True) ends in exactly one of two places:
it was consumed by a turn (appended to a tool result, so the child saw it),
or its text appears in the durable record's `missed_steer`. Accepted text
can be in four places, and the steer ledger (`_ChildLifecycle.steer`)
collects all four:

1. the child's pending queue at steer closure: drained by `finish`/`stall`
   under the registry lock;
2. the first turn's finalizer return (`result["pending_steer"]`): taken
   when the first turn's result is read, before any code rebuilds `result`;
3. the correction turn's finalizer return, when it finishes normally:
   `_apply_output_schema` merges it into `result["pending_steer"]`. Source 2
   was popped from `result` before the correction turn started, so after the
   merge that field holds the correction turn's text only. The ledger also
   drops exact duplicates;
4. the finalizer return of a turn that was still live at the stop decision.
   It is read when that turn's future completes, and read exactly once per
   future (the ledger records which futures it has harvested). If the turn
   completes inside the bounded drain, the text goes into the first write.
   In every other case the exit callback reads it. That callback is
   registered unconditionally after `persist`, so it covers a turn that
   finished between drain expiry and registration, as well as one that
   finishes later. If the read finds new text, the callback fires `amend`
   (same file, same `late_result_id`) and then `teardown`.

What each consumer sees after an `amend`:

- `action='list'`, the in-memory `_late_results` mirror, and the record
  file: the amended entry. The file is rewritten atomically; a memory-only
  record (the disk write failed) is replaced in memory.
- the parent: a second nudge naming the missed steer. It is sent even for
  an absorbed handle, because the absorbing unit's completion event has
  already carried the pre-amendment entry. That event is not re-sent; this
  gap is accepted, and the durable record plus the nudge cover it.
- `handle.entry`: replaced, for any later `_join_late_result` reader.

A steer accepted after closure is impossible: closure sets
`accepting_steer=False` under the lock `steer_subagent` holds.

Residual case, named here: the real finalizer drains pending steer before
its post-turn hooks run. If a hook then blocks forever, the future never
completes, source 4 is never read, and source 1 found an empty queue. The
child's record is already `timeout`, so the parent knows the work failed. The
record carries `steer_fate_unknown: true` whenever the live turn outlived the
stop drain, meaning a steer may be pending that the record does not show
yet. When that turn exits, the exit callback fires `amend` whether or not it
found new text, because the fate is now known either way. The amendment
clears the flag. The parent is re-nudged only when there is new text.

**I2: no teardown while any turn of that child is live.** `teardown` checks
the live-turn future. If the future is not done, `teardown` is not fired
inline. It is registered as the future's done-callback and runs when the
turn exits, in a copy of the owner's context. The drain before `persist`
stays bounded (`_LATE_STOP_DRAIN_SECONDS`, 5 s), so delivery of the result
is never held hostage by a stuck turn. Only persistence teardown waits.
A turn that never exits leaks its tool resources until process exit, which
is logged at WARNING together with a process-wide count of deferred
teardowns (`_deferred_teardowns`), so accumulation is observable. Closing
its SessionDB under it would lose transcript writes, and that is the bug
being fixed. The drain bound is `min(ceiling, 5)`, which is 5 s at every
configurable `child_timeout` (floor 60 s); the constant says so where it is
defined.

**I3: every terminal child has exactly one durable record under its owning
profile.** `persist` is legal once (from `late_completed` or `reaped`). It
writes `<late_dir>/<late_result_id>.json`, where `late_dir` was captured on
the spawning thread (the owner's context-local home). An I1 amendment
rewrites that same file atomically (tmp + `os.replace`) and never creates a
second one.

Failure semantics. "Durable" means the file when the disk write succeeds.
If the write fails, `_record_late_result` keeps a memory-only record and
logs a WARNING. If that record is later evicted by the in-memory cap, it
logs the entry at ERROR. I3 then degrades to "exactly one record, in memory,
loudly reported"; it never degrades to zero records silently. The retention
sweep unlinks files older than 7 days. An amendment that arrives later than
that (a turn stuck for a week) recreates the file, which still makes one
record.

## Mapping from the old code

| Old implicit state | New |
|---|---|
| `stop is None` and `error is None` after first supervision | still `timed_out_running`, then `correct` or `finish` |
| `stop in {"hung","wall"}` | `stall` from `timed_out_running` |
| `stop in {"retry_hung","retry_wall"}` | `stall` from `correcting` |
| `finalizer_pending` local | ledger source 2 |
| `active_turn["future"]` dict | `_ChildLifecycle.live` |
| `active_turn["future"].result(timeout=5.0)` then unconditional `_release_child_resources` | bounded drain (ledger source 4) then `persist`, then `teardown` (deferred if live) |

## Findings closed by construction (#1549 @ 064c531a)

| Finding | Why the machine rules it out |
|---|---|
| `:3491` drained correction turn's steer discarded | ledger source 4: a completed drain's result is read, not dropped |
| `:3638` teardown after an expired drain | I2: `teardown` is refused while the future is live and deferred to its exit |
| `:3478` drain targets the finished first turn | `live` is set by `correct`, so the drain always targets the running turn |
| `:3434` and `:3494` first-turn `pending_steer` lost on correction stall | ledger source 2 is read at the first turn's completion, before any rebuild |

## Test obligations

- One test per #1549 finding, RED on the head where the finding was raised.
- An interleaving property test drives seeded random orders of `timeout`
  (first-turn hang), `steer`, `schema-fail`, `correction-timeout`,
  `late-finish` and `parent-exit` through the real `_run_single_child` and
  late thread against stub agents. It asserts I2 and "at most one record"
  (I3) at every step, and I1 plus "exactly one record" (I3) at the terminal
  state.
- A named test for the window between drain expiry and callback
  registration: the live turn finishes during `persist`, with a
  finalizer-drained steer. It asserts the record's `missed_steer`, and it is
  RED on 064c531a.
- The exit callback reaches `teardown` in the owner's context on both
  paths: the immediate one (turn already done, callback on the late thread)
  and the worker-thread one (turn exits later).
- Stub fidelity. The stubs model the two real behaviours the findings
  depend on. The finalizer drains pending steer into the turn's return
  value before the turn exits. `close()` closes a persistence handle, and a
  write to it after close is recorded as a violation.
- A model-level test enumerates every `(state, event)` pair and asserts only
  the edges above are legal. It also asserts that `teardown` is refused
  while the live future is not done.
