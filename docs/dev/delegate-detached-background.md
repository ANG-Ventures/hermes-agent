# Detached background delegation (restart-survivable `delegate_task(background=true)`)

Status: rev 4, built (t_eaabba56). Momus: pass-1 BLOCK, pass-2 BLOCK, pass-3 APPROVE-WITH-CHANGES; the pass-3
RCs (RC-E hand-off fencing, RC-F fallback reclassification, RC-G protocol version) are folded below.
Knob: `delegation.detached_background` (default `true`; `false` = today's threads, byte-identical path).

## Problem

Background `delegate_task` units run as threads on `tools/async_delegation.py`'s daemon executor inside
the gateway. A gateway stop kills them mid-run. The fork's durable layer (t_1191e078,
`_build_durable_background_spec` + `recover_async_delegations`) records the intent and, on the next
boot, RE-RUNS the unit from scratch (generation+1, continuation note): work so far is lost and
side effects repeat. Kanban workers survive the same restart because each is a separate
`start_new_session=True` process whose state lives outside the gateway.

## Decision

A background unit that already gets a durable record (gateway session with a routable platform,
`resume_on_restart` on, `_build_durable_background_spec` returned a spec) runs its children in ONE
detached child process per unit instead of on a gateway thread. The gateway executor thread that used
to run the children supervises the process. A restarted gateway ADOPTS a still-running (or finished)
child instead of re-running it.

Detached mode is taken only when the spawn is restart-safe on this host:
`process_registry.restart_safe_gateway_child_argv(..., outlives_parent=True,
require_restart_safe_scope=False)` returns mode `in_process` (not systemd: own session suffices —
the kanban precedent on launchd) or `scoped` (systemd user scope). On `degraded` (systemd without a
user bus) the unit runs as a thread exactly as today (CB-2). Native Windows is always `degraded`: there is no
process-group cancel (`os.killpg`), so `/stop` could not reach the child. A unit with a `fork`/`branch` child
also stays a thread: the fork's live transcript, cache snapshot and served seat cannot cross the process
boundary. So does a unit under a session-isolated terminal sandbox (`terminal.env: docker` +
`container_persistent: false`): children alias the parent's per-session container by its live task id, and
that container is never reused across processes. An explicit `inherit_mode="summary"` persists the built
child's fold (`materialized_prefill_messages`).

Process identity is `(pid, kernel start time)`; a pid without a start time is never signalled (only adopted
when its argv carries the exact `--job <dir>` pair). SIGKILL escalation also reaches group members captured at
SIGTERM time that outlived the leader. A dispatcher standing an un-advertised child down first fences the
attempt in the registry (`stand_down_detached`); if an overlapping boot already adopted it, the child is left
running for that boot. The fence keeps the child's identity on the record (`detached_fenced`) until its death
is confirmed: `claim_recoveries` will not re-dispatch the unit while it is there, and any boot's adoption pass
re-sends the kill (the dispatcher may die between fence and kill). A fence whose outcome stays unknown (the registry raises on every retry) never kills:
the child and its job dir are left running, marked `stand-down-unknown.json`, and the next boot's adoption pass
decides. A live owned record adopts it (marker cleared); a dir no live record owns is an orphan, killed and
removed. The detached worker writes its process checkpoint into its job dir, never the
gateway's `<home>/processes.json`, and refuses `process_manage(action="handoff")`: its in-process parent is a
reconstruction that exits with the worker, so a handed-off process would have no owner and no completion.
File edits (`write_file` / `patch`) hold the per-path lock across processes too (`file_state.lock_path` takes an
advisory `flock` on `<hermes root>/state/file-locks/<sha256(path)>` inside the thread lock), so two detached
units patching one file serialize exactly as two in-process children do.

Out of scope: synchronous delegation (thread), depth>0 (always synchronous already), TUI/CLI
sessions (no durable spec, unchanged), kanban, cross-host.

## Process model

- Spawn site: the unit's runner on the async executor (`_dispatch_admitted._worker` -> runner), after
  `persist_dispatch` + `mark_submitted`, so the attempt id exists.
- Argv: `[sys.executable, "-P", "-m", "tools.delegate_detached", "--job", <job_dir>]` (`-P`: the workspace cwd never shadows `tools`) through the helper above.
- `Popen(start_new_session=True, stdin=DEVNULL, stdout/stderr -> <job_dir>/child.log, cwd=<workspace_hint
  if it is a dir, else None>)`.
- Env: `environments/local.py::served_profile_child_env(target_home=<owning profile home>,
  inherit_credentials=True)`, then `cron/scheduler_worker_env.py::pin_hermes_tree_on_pythonpath` (this
  runtime tree + PM's committed dependency generation) and the `_HERMES_DETACHED_WORKER_BOOT` marker: the
  child's `main()` consumes it and runs `pm.environments.activate_dependencies` (lease + activate) before its
  first third-party import, the same boot as the cron external worker (`cron/worker_bootstrap.py`).
- Job dir, keyed by ATTEMPT (RC-3): `<profile_home>/state/detached-delegations/<attempt_id-safe>/` (0700):
  `spec.json` (input), `child.json` (child writes `{pid, create_time}` as its FIRST act), `heartbeat.json`
  (child, every 10 s), `result.json` (child, atomic rename), `child.log`.
- Spawn window (RC-C): the gateway writes `spawn.json` `{attempt_id, intent_at}` BEFORE `Popen`, and
  `pid.json` `{pid, create_time}` (plain atomic write, no lock) immediately after. The child writes
  `child.json` as its first act. Recovery treats "spawn.json but no pid file" as unknown and scans the
  process table for a process whose argv contains the exact element pair `--job <job_dir>` (exact element
  equality on a unique path, never a substring) before deciding the child is dead.
- Registry record: `execution.detached = true` at persist time; additive `detached` block
  `{pid, create_time, job_dir, attempt_id, spawned_at}` written under the registry lock right after `Popen`.

## Inheritance (must match today's child)

`spec.json` = the durable record already persisted for restart recovery (`source`, `execution`,
`route`, `profile`; secret-free by `_assert_nonsecret_settings`) + `continuation` (null on a first run)
+ `live_delegation_id` (the id the dispatch payload advertised; RC-4).

The child executes it through the SAME reconstruction restart recovery uses. The body of
`gateway/run.py::_recover_async_delegations_once._runner_factory` moves to
`tools/delegate_detached.py::build_parent_and_runner(record, continuation, delegation_cfg, resolvers)`;
the gateway passes its own `_resolve_runtime_agent_kwargs*` (patch seams kept) and the child passes the
same functions imported from `gateway.run`. It builds the parent `AIAgent` from the persisted execution
(model, provider re-resolved by `credential_ref` from the profile's own config/secrets, toolsets,
reasoning, fallback chain, provider routing, depth, workspace) and calls
`build_recovered_delegation_runner(record, continuation, parent, source=...)`, which runs
`delegate_task(tasks=..., background=False, _recovery_spec=record)`. Per-task skills, role,
inherit_context + materialized prefill, output_schema, images stay in `source.tasks` and go through the
normal child builder. Call-level inputs delegate_task applies outside the task dicts (top-level `images`,
`output_schema` and `skills`) are written into each persisted task, so the reconstruction sees them. Children are always built with `skip_memory/skip_context_files` (`_build_child_agent`),
so the recovery parent's skip flags change nothing the child sees.

RC-1 fixes in the shared runner: a `None` continuation appends nothing; the session source is a
parameter (`gateway_detached` on first run, `gateway_recovery` on re-dispatch).

Approval: decided by the bound session vars (`set_session_vars(platform=route.platform, ...)`, same call
the recovery runner makes) + `_run_single_child`'s per-thread subagent approval callback
(`delegation.subagent_auto_approve`: deny by default). Today's in-gateway children never route an approval
to the user's chat either; the child process runs the same code with the same bound platform and inherits
`HERMES_EXEC_ASK` from the gateway env. Tools that push through the gateway: a leaf child has no
`send_message`/`clarify` (blocked toolset); an orchestrator child keeps `delegate_task` only (depth>0 is
synchronous). MCP: the child runs `mcp_startup._discover_mcp_tools_without_interactive_oauth()` before
building the agent.

Parent-level toolsets (RC-D): today `execution.toolsets` is read from child 0 (already role-stripped).
The spec adds `execution.parent_toolsets` / `execution.parent_disabled_toolsets` read from the DISPATCHING
parent (`enabled_toolsets`, `disabled_toolsets`); reconstruction prefers them when present (older records
keep the old field). Per-task role stripping then happens in the normal child builder, so a mixed-role
batch resolves each child exactly as the thread path does.

Parity gate (RC-2/RC-D test): for a mixed-role batch fixture (task 0 leaf, task 1 orchestrator), the
per-task child config built by the thread path and by the detached reconstruction (enabled/disabled
toolsets, model, provider, role, skills, prefill) is equal.

## Liveness (CB-1)

`progress_fn` for a detached unit reads `heartbeat.json`: `{token, in_tool, ts}` where the child computes
`token` from its children's `get_activity_summary()` exactly like `_batch_progress` does in-process. The
stall monitor semantics are unchanged (frozen token past threshold -> interrupt -> grace -> stalled). A
missing heartbeat file before the first write returns `(None, False)` with the dispatch time as the
token's age origin. Bootstrap (imports, MCP discovery, agent build) writes a phase token per step
(`("bootstrap", step)`) so the idle ceiling (450 s) measures each step, not the whole bootstrap. The gateway still builds `_child_agents` (inherit_context prefill needs the live
parent), and closes them through `_teardown` right after the spawn succeeds; they never run.

## Result delivery

The supervisor (original gateway or adopter) polls the pid (psutil, pid + create_time) every 1 s, reaps
its own Popen child, reads `result.json`, and finalizes through `_finalize(..., attempt_id=...)` ->
`_store.append_terminal` -> JSON outbox -> `completion_queue`, the existing durable late-result path.
Missing/invalid `result.json` after exit, while this gateway owns the unit -> `status=error` with the last
2 KB of `child.log` (same as a thread that crashed today).

## Restart / adoption

- Shutdown (`interrupt_all(recoverable=True)`): live detached records are NOT interrupted and get no
  `recover_on_shutdown`; they ARE durably marked `recoverable` like every durable record today (RC-A). The
  old supervisor's late finalize is then refused by `append_terminal` (state != running), and the
  adopter owns the result. Because the adopter keeps the same attempt id, `append_terminal` alone does not
  fence the old supervisor (RC-E): shutdown also marks the live record `detached_handed_off`, drops its
  `interrupt_fn`/`progress_fn`, and the supervisor's wait loop exits on that flag without finalizing.
- Boot: `recover_async_delegations` first calls `_store.claim_detached_adoptions(current_boot_id,
  owner_alive, probe)` for records with `execution.detached` that are `recoverable` (any owner: the
  shutdown handoff, so an overlapped restart adopts at once), or `running` with an owner boot that is dead
  and not this boot. Pid identity = registry `detached` block, else `child.json` in the current
  attempt's job dir (RC-3: gateway died between Popen and the registry write):
  - pid alive (create_time match), or `result.json` present -> adopt: `owner_boot_id=<this boot>`,
    `state=running`, lifecycle `adopted`, same attempt id + generation (no redispatch, no retry budget).
  - pid dead and no `result.json` -> drop the `detached` block (lifecycle `detached_lost`) and leave the
    record to `claim_recoveries` in the SAME boot: normal re-dispatch with continuation, retry budget spent
    as today (CB-2). The re-dispatch spawns a new detached child for the new attempt.
  - record already `cancelled`/terminal with a live pid -> SIGTERM the group (orphan rule). The `detached` block
    stays until the pid is confirmed dead, so every later boot re-sends the kill.
  - age cap: a detached record older than `ACTIVE_STALE_SECONDS` -> SIGTERM the group if alive, fail the
    record `stale_record` (same cap `claim_recoveries` applies today).
  Adoption runs regardless of `resume_on_restart` AND of `detached_background` (RC-B: the child is already
  running; flipping the kill switch must not strand it).
- The adopter installs the same live record the dispatcher has (CB-3): `interrupt_fn` = group SIGTERM
  (pid + create_time checked) with non-blocking SIGKILL escalation, `progress_fn` = heartbeat reader,
  `_started=True`, and arms the stale monitor. A wedged adopted child is therefore stalled, signalled and
  force-finalized exactly like a dispatcher-owned one; `/stop` after adoption kills the group.
- `claim_recoveries` skips records that still carry a `detached` block after adoption.
- `_recover_abandoned_delegations` (SQLite mirror "owner exited, outcome unknown") skips rows whose
  canonical record is a running/recoverable detached unit: the adopter owns the outcome.
- `hermes gateway stop` with no later boot leaves a detached child running with no consumer; its
  result is delivered by the next boot (48 h outbox replay horizon). Semantic change from today, called out.

## Cancel / steer

- `/stop`, session end, `interrupt_all(recoverable=False)`: durable cancel first (unchanged), then the
  live record's `interrupt_fn` = SIGTERM to the child's process group, SIGKILL after 30 s on a timer
  thread (never blocks the `/stop` caller; injected clock in tests). The child's SIGTERM handler interrupts its parent agent (existing fan-out), writes
  `result.json` with what it has, exits. A cancelled record gets no completion (append_terminal refuses,
  same as threads today).
- Steer/list of detached children: not supported (another process). The dispatch payload omits
  `subagent_ids`/`control_hint` for a detached unit and says `/stop` still works. Called out in the PR.

## Cleanup

The supervisor removes the job dir after finalize. Boot removes job dirs whose attempt has no
running/recoverable record and no live pid.

## Kill switch / revert

Fallback (RC-F): a unit whose spawn is refused or fails runs in-process and its record is rewritten to
`execution.detached = false`; a record is treated as live-detached only while it carries a `detached` block,
and a re-dispatch spawns a detached child only when the current `detached_background` flag allows it.

Protocol (RC-G): `spec.json`, `heartbeat.json` and `result.json` carry `schema_version`. An unknown version
reads as no progress (heartbeat) or an explicit error result naming `child.log` (result); a child given an
unknown spec writes an error result and exits.

`delegation.detached_background: false` -> new units run as threads exactly as before; already-running
detached children are still adopted and delivered. Revert procedure (RC-B): flip the flag, wait until no
`running`/`recoverable` registry record carries a `detached` block (`<profile>/state/async-delegations.json`),
then revert the PR; base code would re-dispatch a live detached record. Catch:
spawn failure logs `async_delegation_detached_spawn_failed` and falls back to the in-process thread run
for that unit (never silent, never lost).

## Acceptance (engineered, not soaked)

1. Red-on-base test: owning process "restarts" (shutdown hook + new boot id) mid-run. Base: the unit is
   interrupted/marked recoverable and re-dispatched (generation 1). Change: the child pid survives, the new
   boot adopts it (generation 0, no redispatch) and delivers its result.
2. Negative tests: stall heartbeat (frozen vs advancing token), dead child with no result -> redispatch,
   Popen-to-registry crash window (pid.json only; spawn.json only + live argv match) -> adopt, cancelled
   record + live pid at adoption -> SIGTERM, adopted unit frozen heartbeat -> stalled + SIGTERM, `/stop`
   after adoption -> group SIGTERM, recoverable record with a live owner -> adopted, flag off + live
   detached record -> adopted, SIGTERM->SIGKILL escalation on an injected clock, degraded scope -> thread
   path, mixed-role parity.
3. Live E2E on a scratch gateway (real `hermes gateway run`, scratch `HERMES_HOME`): background delegation
   whose child runs >2 min; SIGTERM the gateway mid-run; start a new gateway; child pid unchanged and still
   running; its result lands in the origin session. Raw pids, timestamps and log lines in the handoff.
   macOS => this exercises the `in_process` spawn mode only; `scoped` (Linux systemd) is covered by unit
   tests of the mode branch, not by this E2E.
