# Background `delegate_task` units on the kanban worker runtime (option C)

Status: rev 5, SPEC (t_414b4d92). Not built. **Momus: APPROVE-WITH-CHANGES at pass 4**, its one
non-gating RC and lens notes folded mechanically in rev 5 (Momus: "recommend stop"). History: pass 1
BLOCK (CB-1, CB-2, RC-1..5) -> rev 2; pass 2 BLOCK (CB-3, RC-1..7) -> rev 3; pass 3 BLOCK (CB-4,
RC-1..2) -> rev 4; pass 4 APPROVE-WITH-CHANGES -> rev 5. Fold maps in §15. Replaces `tools/delegate_detached.py` and the
`delegation.detached_background` knob (docs/dev/delegate-detached-background.md). Origin: Ace,
#ci-remove-merge-gate 2026-10-21 00:39 PDT, "B now, C as the real fix". Option B (land #2170's
fixes with the knob OFF) is t_7ccdc21b and is not touched here.

## 1. Summary and goal

A background `delegate_task` unit that has a durable record today runs as a thread in the gateway
(or, with the knob ON, in a bespoke detached child). A gateway restart kills the thread; the next
boot re-runs the unit from scratch. The detached child fixed that, but brought its own process
supervision (PID reuse, group scans, fences, orphan dirs) and its own reconstruction of state that
does not cross a process boundary. Five Prism rounds on it did not converge (20, 15, 7+15+2 P0,
then 31 P1 at #2170 e9a3dfd7).

Kanban workers already survive restarts: each is a full agent process (`start_new_session=True`,
restart-safe scope on Linux), its state is in `kanban.db`, and the dispatcher that supervises it
(crash sweep, max-runtime, leftover reap, terminal-worker reap) has run for weeks. This spec moves
the background unit onto that runtime: **one unit = one card on an internal `delegations` board,
spawned immediately through the dispatcher's own spawn function, run by a small runner module that
executes the unit through today's synchronous delegation path and writes the result into the
existing durable registry, from which the gateway delivers it as it does today.**

## 2. Non-goals

- Synchronous `delegate_task` (no change; I1).
- Units without a durable record (CLI/TUI sessions, non-routable platforms, depth > 0): unchanged.
- `inherit_mode="fork"` units: stay in-process (the fork's live transcript and cache seat cannot
  cross a process; same rule as today).
- Steer/list of a running kanban-backed unit's children (same limitation the detached unit has).
- Cross-host placement: a unit always runs on the host that dispatched it.
- Any change to #2170 / t_7ccdc21b.
- Kanban dispatcher changes beyond K1-K6 (§6).

## 3. Measured cost (P0, done on this card)

Method. Studio (macOS 26.7, 32 cores, load1 9.8-17.9 during the runs), runtime tree
`~/.hermes/runtime/hermes-agent` at 29ce82f435, its venv python 3.11. A fake OpenAI-compatible
endpoint (`fake_llm.py`) logs each request's arrival time and answers instantly; when the request
offers `delegate_task` and has no tool result yet, it answers with one
`delegate_task(goal=..., background=false)` call. Home = a copy of the default profile's
`config.yaml` with only `model:` pointed at the fake endpoint and `delegation.{model,provider}`
cleared (plugins, skills, SOUL.md, MCP servers as configured; no `.env`, so credential-gated tools
are absent). Command: `env -i HOME PATH HERMES_HOME=<home> venv/bin/python -m hermes_cli.main
--cli [-t <toolsets>] chat -q "say hi" -Q`, wrapped in `/usr/bin/time -l`. n = 5 per arm.
"slim" = `-t delegation,terminal,file,web,skills,memory,todo` (14 tools); "full" = the profile's
toolsets (56 tools, incl. the gbrain MCP stdio server).

| Quantity | slim | full |
|---|---|---|
| Full agent process: launch -> first model request, median [min-max] | 3.47 s [3.21-4.27] | 4.94 s [4.36-5.94] |
| Same, run with no delegation (control) | 3.93 s [3.50-5.61] | 4.46 s [4.18-8.90] |
| Today's in-process child: parent receives tool call -> child's first model request | 0.48 s [0.38-0.89] | 0.62 s [0.47-0.85] |
| Max RSS of a full agent process | 222.9 MB | 250.6 MB |
| RSS added by an in-process child (with vs without delegation, process max) | +2.3 MB | +4.9 MB |
| Import floor (`import run_agent, model_tools, tools.delegate_tool, tools.async_delegation`), n=5 | 1.24-2.84 s | |

So a kanban-backed unit costs about **3-5 s more start latency and ~220-250 MB more RSS** than an
in-process child. For scale, the units that ran in the last 7 days (150 terminal records across all
profiles' `state/async-delegations.json`) took p10 209 s, p50 650 s, p90 2,024 s, max 7,208 s; 3-5 s
is 0.5-0.8 % of the median unit. Peak day 10-14: 82 units.

Dispatcher latency, from `~/.hermes/kanban.db` events over the last 3 days: claim -> spawned p50 1 s,
p90 9 s, p99 43 s (n=2,210); a card created `ready` -> claimed p50 83 s, p90 1,606 s (n=1,334). The
second number is why §5.2 spawns at dispatch time (K3) instead of waiting for a tick.

Raw measurements (per-run JSON lines, `/usr/bin/time -l` output, the fake endpoint, the harness) are
attached to t_414b4d92 as `t414-boot-cost.tar.gz`.

## 4. Constitution / invariants

- **I1 Synchronous delegation is unchanged.** No line of the `background=False` path changes.
  *Proof:* the build PRs touch no function on the sync path (`git diff` of `_execute_and_aggregate`
  and `_run_single_child` empty); the existing sync tests pass unmodified.
- **I2 Exactly-once delivery per attempt.** The only terminal writer is
  `_store.append_terminal(delegation_id, attempt_id, ...)`, which refuses unless the record is
  `running` on that attempt; the outbox event id is `<id>:terminal:g<gen>`; delivery uses the
  existing claim/ack. *Proof:* AT-1..AT-4 count completion messages in the origin session (== 1),
  then restart again and count again (still 1).
- **I3 No automatic re-run of a kanban-backed unit.** One `spawned` event per attempt. A worker
  that ends without a result is finalized as an error and delivered; the parent decides whether to
  re-delegate. *Proof:* AT-5 counts `spawned` events on the card (== 1) after SIGKILL.
- **I4 No credential is persisted.** The durable spec stays secret-free
  (`_assert_nonsecret_settings`); the runner resolves credentials from its own profile scope.
  *Proof:* existing `_assert_nonsecret_settings` test; AT-8 greps the card body and registry
  record for the fake key.
- **I5 The unit and its TOOLS run as the owning profile, as the gateway's uid, on the dispatching
  host, in the session's directory.** `assignee` = owning profile; `HERMES_HOME` = that profile's
  home (`_default_spawn` already does this via `resolve_profile_env`); `workspace_kind=dir`,
  `workspace_path` = the session's terminal cwd. K3 passes no placement and K1 skips the
  `kanban.local_worker_uid` ssh hop for the `delegations` board, so no `PLACEMENT_ENV` reaches the
  runner and its terminal tools run as local subprocesses, as today's in-process child's do.
  *Proof:* AT-1 records, for a terminal-tool process the unit starts (not the worker): uid ==
  gateway uid, `HERMES_HOME`, cwd, and an ancestry that reaches the worker pid with no `sshd`.
- **I6 Eligibility is an allowlist and fails closed.** A unit takes the kanban runtime only when
  every condition in §5.1 is positively true; anything unknown runs in-process (today's path).
  *Proof:* AT-7 negatives.
- **I7 Kill switch.** `delegation.background_runtime: in_process` sends new units in-process;
  already-running kanban units still finish and deliver (the card-end watcher in §5.4 runs regardless of the
  knob). *Proof:* AT-7 flips the knob mid-unit.
- **I9 A surviving unit is cancelled only on purpose.** Only a session-scoped cancel from the
  unit's own session (`/stop`, session end) or a caller that passes `include_kanban=True` may cancel
  a kanban-backed record. An `all_active` cancel (`interrupt_all(recoverable=False)` from
  `hermes_cli/cli_shutdown.py:135` or a CLI `/stop` at `cli_commands_mixin.py:934`, both reaching
  `cancel_matching` at `async_delegation.py:2385`) never matches one, whatever its `owner_boot_id`,
  neither durably (`cancel_matching(include_kanban=False)`) nor in memory
  (`interrupt_all(include_kanban=False)`, §5.4); gateway shutdown leaves it running. *Proof:* AT-6,
  AT-1 (graceful SIGTERM).
- **I10 Every kanban live record is settled when its unit ends.** A dispatched kanban unit holds
  its capacity slot and its `active_count`/`has_live_for_session` presence from dispatch until the
  card-end watcher settles it, and no longer. *Proof:* AT-11.
- **I8 Test hygiene.** No test signals a bare PID: every cleanup holds a `psutil.Process` handle
  checked against its create time (the class behind cad55e4e4799, 020a022d3c8d, 532e66a4de74).

## 5. Design

### 5.1 Eligibility (dispatch time, in `delegate_tool.py` where `_detached` is decided today)

All of:
1. `_build_durable_background_spec` returned a spec (gateway session, routable platform,
   `resume_on_restart` on). Unchanged predicate.
2. `delegation.background_runtime == "kanban"`.
3. No child has `inherit_mode == "fork"`.
4. Terminal backend is `local` or `ssh` (the filesystem the child sees is the host's or the remote
   host's, not something owned by the gateway process; with I5 the local case is the same uid).
   Docker, modal, daytona, singularity and any plugin backend stay in-process. This is the answer
   to 49a7ba3407bb.
5. A dispatcher serves the delegations board in this process
   (`gateway/kanban_watchers_common.py::_kanban_dispatch_allowed()` true). Without one nothing
   would reap a crashed worker.
6. The session's terminal cwd is an absolute existing directory.

### 5.2 Dispatch (gateway, tool thread)

1. `_dispatch_admitted` persists the durable record as today, with `execution.runtime = "kanban"`
   and a `route` block `{requested_provider, model, explicit_base_url}` taken from the parent:
   `requested_provider` is `parent_agent.requested_provider` (the configured name, e.g. a named
   custom endpoint), never the resolved generic `provider` (answer to 3e4a53f5c897);
   `explicit_base_url` only when the delegation config set one (non-secret, already persisted
   today). Capacity accounting (`max_concurrent_children`, `slot_key`) is unchanged and happens
   before the card exists.
2. Create the card with `kanban_db.create_task` on board `delegations` (created on first use; its
   behaviour comes from the slug constant in K1/K2/K4/K5, never from `board.json`): title
   `deleg <delegation_id>: <goal[:60]>`, `assignee` = owning profile, `workspace_kind="dir"`,
   `workspace_path` = session cwd, `max_retries=1`, `max_runtime_seconds` = NULL (no wall-clock
   cap, exactly as today's in-process unit: `delegation.child_timeout_seconds` is an INACTIVITY cap
   that defaults to none, `delegate_tool_config.py:24-27,148`; a wedged runner process is caught by
   K2's stale-heartbeat path, a wedged child by the sync path's own monitor), `session_id` = origin session id
   (satisfies the home rule), `idempotency_key = attempt_id`, body = one JSON line
   `{"delegation_id", "attempt_id", "profile_home"}`. Under the registry lock, write
   `kanban: {board, task_id}` onto the record.
3. `kanban_db_dispatch.spawn_now(conn, task_id, board="delegations")` (K3): claim, resolve
   workspace, `_default_spawn(placement=None)` (runner branch, K1), set worker pid + start token,
   `spawned` event. This is the dispatcher's own code path for one card, so the env scrub, profile
   home, restart-safe scope (`_restart_safe_worker_argv`), user-bus env (`systemd_user_bus_env`),
   RLIMIT_NPROC, QoS, worker log and identity registration are the same bytes every kanban worker
   gets, minus the local-uid hop (K1, I5).
   Steps 2-4 run inside `_dispatch_admitted._worker`, after its `mark_submitted`, exactly where
   the detached branch calls `_run_detached_unit` today (`async_delegation.py:1296`).
4. If card creation or `spawn_now` fails: archive the card if it exists (archive signals a worker
   whose pid was stamped), then SUPERSEDE the attempt with new store function
   `supersede_attempt(delegation_id, old_attempt_id) -> new_attempt_id`: under the registry lock,
   generation + 1, new `attempt_id`, `execution.runtime = "in_process"`, `kanban` block removed,
   state stays `running`, and NOTHING else from `claim_recoveries`' bump: `redispatch_count` is
   unchanged and no `async_delegation_restarted` outbox event is written (a spawn failure is not a
   restart). The worker then sets `record["attempt_id"] = new_attempt_id` on the record dict it
   shares with `_records`, calls `mark_submitted` on the new id and runs the `runner()` it already
   holds inline; its existing `finally` then finalizes once, reading the re-pointed attempt id (no
   second inline `_finalize` call). A runner that was `Popen`ed before the failure and
   never stamped finds its card's `attempt_id` no longer current at §5.3 step 2 and exits without a
   model call; if it is already past step 2, `append_terminal`'s attempt guard refuses its terminal
   write (I2). Log `async_delegation_kanban_spawn_failed` at WARNING; never silent, never lost.
5. On spawn success the worker sets a local `kanban_spawned = True` flag (the same shape as its
   existing `handed_off` flag), and its `finally` skips `_finalize` when the flag is set: the
   runner owns the terminal write.
   The live record stays in `_records` with `interrupt_fn` = `_cancel_kanban_unit` (§5.5),
   `progress_fn = None` (the runner owns stall detection, §5.3) and `kanban_unit = True`; it leaves
   the active states only through `_settle_kanban_record` (§5.4). The gateway-built children are
   torn down through `_teardown` (they never run), as the detached path does.
6. Return the dispatch payload with `"runtime": "kanban"` and the existing control hint ("steer/list
   are not available for it; /stop still cancels it").

### 5.3 The runner (`hermes -p <profile> delegation-unit`, body in `tools/delegation_unit_worker.py`)

The entry point is a hidden CLI subcommand (argparse `help=argparse.SUPPRESS`), not `python -m`, so
the runner gets the SAME process bootstrap every `hermes -p <profile> chat` worker gets: the profile
override before any import (`hermes_cli/main.py:607` `_apply_profile_override`), the profile
`.env` (`load_hermes_dotenv`, main.py:751) and `agent.process_env_files`
(`_apply_process_env_files`, main.py:3444). `_default_spawn`'s env scrub (`scrub_secrets` under
multiplex or a routed home) and its `strip_overlay` assume exactly that re-sourcing (RC-2). Spawned
by `_default_spawn` with the standard worker env (owner grant pending, board pins,
`HERMES_KANBAN_TASK/RUN_ID/EXIT_FILE`, `HERMES_HOME` = profile home). It:

1. Claims the owner grant for its own pid (`kanban_native_worker` does the same).
2. Reads its card body, then the record from `<profile_home>/state/async-delegations.json` with
   `locked_registry(write=False)` (existing primitive, `async_delegation_store.py:307`). If the
   record is not `running` on the card's `attempt_id`, completes the card with
   `summary="superseded"` and exits 0. This is the runner's only registry read before its terminal
   write.
3. Builds the parent `AIAgent` through the same resolver the CLI uses:
   `resolve_runtime_provider(requested=route.requested_provider, target_model=route.model,
   explicit_base_url=route.explicit_base_url)` (cf. `cli_agent_setup_mixin.py:262-264`; answer to
   ef968436cb9d). Toolsets from `execution.parent_toolsets`/`parent_disabled_toolsets`; prefill
   from the profile's `prefill_messages_file` through the shared loader
   (`cli_config_load._resolve_prefill_messages_file`), the same source the gateway parent's prefill
   came from (answer to 4cd9e879cf0e); `set_session_vars(platform=route.platform, ...)` as the
   recovery runner does; `skip_memory=True`. Children get the profile's own `delegation:` config,
   read by the runner from the same file the gateway read, so a configured
   `delegation.provider/model` applies exactly as in-process. The runner never ADDS a provider
   override derived from the recorded route (the detached reconstruction did, which made
   `_resolve_child_runtime` reset the routing filters); an unpinned unit therefore inherits the
   parent's `providers_allowed`, `providers_ignored`, `provider_data_collection`
   (`tools/delegate_tool_config.py:551`, answer to 12d0c5da2421). The parent never runs a model
   turn; it is the container the synchronous delegation path needs.
4. Runs the unit with `build_recovered_delegation_runner(record, continuation=None, parent,
   source="gateway_kanban")`, i.e. `delegate_task(tasks=source.tasks, background=False,
   _recovery_spec=record)`. Children are threads of the runner, built by the normal child builder
   (roles, skills, images, output_schema, materialized summary prefill). Stall and timeout behaviour
   is the sync path's (`child_timeout_seconds`, its heartbeat monitor).
5. While it runs, one dedicated daemon thread: every 60 s `kanban_heartbeat` (a pure liveness beat,
   independent of child activity, so K2's stale-heartbeat path measures the process, not the
   model); every 5 s one SQLite read of its own card's status. `archived` (the cancel path, §5.5)
   -> hard-interrupt the parent (existing fan-out), write nothing to the registry, exit 0. The
   runner never polls `async-delegations.json`: measured 6.1 MB on the Studio default profile and
   27 ms per `json.load`, so 32 runners at a 2 s poll would hold the registry lock ~0.4 s of every
   second (RC-4).
6. On return: `_store.append_terminal(..., queued_boot_id=None)` (new keyword on the existing
   store function; default keeps today's owner-boot stamp), then the legacy mirror
   (`_persist_completion`), then complete the card with `summary=<status>`. The registry write is
   the delivery truth; the card's end is the gateway's signal to drain (§5.4). A failed card write
   leaves the card to the crash sweep (K2 parks it `blocked`), which the gateway also drains on.
7. Any exception after step 2 -> `fail_attempt(...)` with the traceback tail, card complete, exit 0.
   The runner never exits with a requeue code.

### 5.4 Delivery (gateway)

One delivery path for "owner gateway still up" and "owner gateway restarted". The card is the
signal; the registry stays the delivery truth (RD-3).

- **Card-end watcher.** In the existing idle delivery loop `_async_delegation_watcher`
  (`gateway/run_notifications.py:2054`, 2 s interval), throttled to every 5 s, once per served
  profile under `_each_secondary_ledger`'s scope binding (the launch profile in its own): ONE
  SQLite query on the `delegations` board for this profile's cards (by `assignee`) whose status is
  `done`, `blocked` or `archived` AND that carry no `delegation_drained` event. No registry lock in
  steady state.
- **Targeted drain.** For each such card, in order:
  1. If the record is still `running` on the card's attempt (the runner died before its terminal
     write), finalize with `fail_attempt(..., "unit worker ended without a result: <last 2 KB of
     the worker log>")`. The attempt guard (I2) makes a race with a late runner write harmless.
  2. New store function `enqueue_record_outbox(delegation_id, current_boot_id)` =
     `enqueue_pending_outbox` restricted to ONE record: its pending events whose `queued_boot_id` is
     not this boot are stamped and put on the completion queue, then delivered and acked through the
     existing claim/ack (`claim_event_delivery`, `async_delegation.py:752`). No other producer's
     events are touched.
  3. `_settle_kanban_record(delegation_id)`: if `_records` holds a `kanban_unit` live record for it,
     map the registry state to a live status with a fixed table (`done` -> `completed`,
     `failed` -> `error`, `cancelled` -> `cancelled`; any other state -> no settle, retry next tick)
     and set it and prune through the existing `_finish_finalization` (`async_delegation.py:1706`). This is the
     ONLY path that moves a kanban live record out of the active states, so the capacity slot
     (`_dispatch_admitted`'s `active_slots`), `active_count` and `has_live_for_session` release when
     the unit ends, not at the next restart (pass-2 CB-3).
  4. Once the record's terminal outbox event is `delivered`/`dropped` (or the record is terminal
     with no event, e.g. `cancelled`), append a `delegation_drained` event to the card, then archive
     it (`archive_task`) if it is not already archived. The event, not the card status, is the
     "drained" marker, so a `/stop` that archives a card whose runner already wrote `done` is still
     drained (pass-2 RC-7). A failed step leaves no marker and is retried on the next tick.
  Workspace kind `dir` is never removed by archive: `_cleanup_workspace` returns early for every
  kind but `scratch`/`worktree` (`kanban_db_workspace.py:455`).
- **Boot.** Existing: `enqueue_pending_outbox` once per profile at boot (`gateway/run.py:6012`)
  queues every pending event, including worker-written ones (`queued_boot_id` null). New, in the
  same pass:
  - `claim_recoveries` skips `execution.runtime == "kanban"` records (never re-dispatched, I3).
  - Each `running` kanban record of a served profile gets an observer live record in `_records`
    (status `running`, `kanban_unit = True`, `interrupt_fn = _cancel_kanban_unit`, no supervisor
    thread), so `active_count`, `has_live_for_session` and `/stop` (which archives the card) see it;
    the card-end watcher settles it like any other.
  - Scale-to-zero: `_scale_to_zero_has_live_background_work` (`gateway/run_shutdown.py:365`) counts
    `active_count()`; kanban live records are excluded from that probe (their process is not the
    gateway's, and the next boot picks them up), so a running unit does not keep the gateway awake.
    This is the only `active_count()` reader in `gateway/` besides status display (grep, rev 5).
  - Lost card, unknown is never dead (pass-3 CB-4, pass-4 RC-1). The check runs at boot AND on
    every card-end-watcher tick for the (usually empty) set of `running` kanban records of this
    profile whose card was not returned by the watcher's query; that per-tick pass is the retry
    host. For each, the card read has three outcomes. (1) Read error (busy/locked WAL, I/O error,
    migration race): nothing changes; retried next tick. (2) Row present: normal path. (3) Row
    positively absent (the query succeeded and returned no row): scan the process table for the
    RUNNER: a process whose env carries `HERMES_KANBAN_LINEAGE=<record.kanban.task_id>:*` (K6) AND
    whose cmdline contains the argv pair `delegation-unit` (exact element, never a substring). The
    scan is UNKNOWN if `process_iter` raises or any same-uid process's `environ()` read fails (the
    existing helper's per-process `continue`, `kanban_db.py:17526`, is not used for this check).
    Runner found, or scan unknown: leave the record `running`, retry next tick. Scan succeeded and no
    runner: SIGTERM then SIGKILL every lineage-tagged leftover (create-time checked, I8), then
    `fail_attempt(..., "unit card lost")`, settle and deliver. P2 unit tests: an injected read error
    leaves the record `running` and the runner's later terminal write is delivered; an absent row
    with a live runner is not failed; an absent row with only a tagged non-runner process -> that
    process is reaped and one error is delivered within one tick.
  - Any `delegations` card older than 48 h without a `delegation_drained` event whose record is
    terminal or missing is archived.
- **Legacy SQLite mirror** (`async_delegations` table, `_persist_dispatch` stamps the dispatching
  gateway's `owner_pid`). `_recover_abandoned_delegations` (`async_delegation.py:420`) already skips
  a row whose canonical record is `running`/`recoverable` with `execution.detached`, because "its
  child outlives the owner". The same skip is extended to `execution.runtime == "kanban"`; without
  it a restarted gateway would synthesize an owner-loss notice for a unit that is still running
  (pass-2 RC-6). Once the runner's `append_terminal` + `_persist_completion` land, the existing
  `_canonical_terminal` branch syncs the row as today. AT-1 counts ALL `async_delegation*` messages.
- **`interrupt_all` excludes kanban live records (pass-3 RC-1).** `interrupt_all` gains the same
  keyword, `include_kanban: bool = False`. Unless it is set, records with `kanban_unit = True` are
  removed from `targets` before anything else happens: they get no `recover_on_shutdown`, are not
  in `durable_ids` (so `_mark_recoverable` never flips them to `recoverable`, which would make
  `append_terminal` refuse the runner's result), and their `interrupt_fn` (`_cancel_kanban_unit`)
  is never called. This holds in both modes: gateway shutdown (`recoverable=True`) leaves the unit
  running for the next boot, and an `all_active` stop (`recoverable=False`) leaves it alone in
  memory exactly as `cancel_matching` does durably (I9). Session-scoped `interrupt_for_session`
  still reaches them; it is the only gateway path that cancels a unit. P2 unit test, both modes:
  the kanban record stays `running` in the registry, its card is not archived, and an in-process
  record in the same call is handled as today.

### 5.5 Cancellation and timeout

- `/stop` and session end (`interrupt_for_session`, session-scoped): `cancel_matching` durably
  cancels the matching records (unchanged), then `_cancel_kanban_unit` archives each matching kanban
  record's card. Archive terminates the host-local worker with the dispatcher's identity check
  (pid + start fingerprint); if the worker survives, `reap_terminal_workers` retries every dispatcher
  tick (`kanban_db_dispatch.py:1169`), and the runner's own 5 s card-status read stops it
  independently. A cancelled record gets no completion (append_terminal refuses), as today; the
  card-end watcher settles the live record and marks the card drained. This path reads only the
  registry and `kanban.db`, so it works from any boot (answer to b9657c44e8dc).
- **Other processes' `all_active` cancels skip kanban records (I9, CB-1).** `cancel_matching`
  gains an explicit keyword `include_kanban: bool = False`. With `all_active=True` it skips every
  record with `execution.runtime == "kanban"` unless `include_kanban=True`, independent of
  `owner_boot_id` and of owner-boot liveness. No existing caller passes it, so neither
  `interrupt_all` from a one-shot's shutdown nor a CLI `/stop` can reach a kanban unit, and a
  process whose `current_boot_id()` fails to `""` (`async_delegation.py:134`) cannot either
  (pass-2 RC-4). Session-scoped selectors (`session_key`, `parent_session_id`,
  `origin_ui_session_id`) still match kanban records, which is how `/stop` in the unit's own chat
  cancels it.
- Timeout: none of its own, exactly as an in-process unit today. Inside the runner, the sync path's
  optional inactivity cap `child_timeout_seconds` and its heartbeat monitor apply to each child; a
  runner process that stops beating is reclaimed by the dispatcher's stale-heartbeat path, which K2
  turns into `blocked`, and the card-end watcher finalizes it.
- Worker crash (OOM, SIGKILL): the dispatcher crash sweep reaps the worker's session and the
  run-identified escapees (`_reap_worker_session`, `_reap_run_env_escapees`), books the run, and
  with `max_retries=1` + K2 the card goes `blocked`; the card-end watcher delivers the error. The
  unit's tool processes are found by the escapee sweep only through K6: `scrub_kanban_env`
  (`agent/delegation_context.py:186`) strips `HERMES_KANBAN_TASK/RUN_ID` from every delegated
  child's subprocess env, and the local terminal backend starts each command in a new session
  (`tools/environments/local.py:1046`), so neither the session reap nor the env match sees them
  today.

### 5.6 The `delegate_task` tool contract

Unchanged for the model: same arguments, same `dispatched` payload shape (plus `"runtime"`), same
completion message format. Behavioural differences a user can see, all already true of the detached
unit: no steer/list for the unit; a unit started before `hermes gateway stop` with no later boot
finishes and its result waits in the outbox (48 h replay horizon); after a worker crash the unit is
not re-run (I3; today's restart recovery re-runs it, but restarts no longer kill the unit).

## 6. Kanban changes this needs (and nothing else)

Store changes outside kanban, for completeness: `append_terminal(queued_boot_id=)`,
`enqueue_record_outbox`, `supersede_attempt`, and `cancel_matching(include_kanban=)` (all in
`tools/async_delegation_store.py`); `_settle_kanban_record`, `interrupt_all(include_kanban=)` and
the `_recover_abandoned_delegations` skip (in `tools/async_delegation.py`).

All "delegations board" behaviour keys on one code constant, the slug `delegations`. `board.json`
is not consulted, so writing keys into another board's `board.json` changes nothing.

- **K1 Runner selection.** `_default_spawn`, for the `delegations` slug only: argv =
  `[*_resolve_hermes_argv(), "-p", <profile>, "delegation-unit"]`, no `--skills`, no prompt, no
  `--toolsets`, and the `kanban.local_worker_uid` placement block is skipped (I5); the env also
  pins `HERMES_KANBAN_HOME` to the dispatcher's `kanban_home()` (§8 row 10); otherwise same env,
  log, scope, limits. This argv is final: it is decided after, and replaces, every other argv
  source in `_default_spawn` (the native foreign-lane branch `foreign_lane.worker_command`,
  `model_override`/`--provider`, `--reasoning`, `--skills`, `--toolsets`), so a profile with a
  foreign lane never has its unit card worked by that lane (pass-2 RC-2). Shape mirrors the
  native-worker branch (`kanban_db.py::_native_worker_argv`).
- **K2 No respawn.** For the `delegations` slug, every path that would return a running card to
  `ready` (crash, rate-limited exit, cohort death, stale-heartbeat reclaim, max-runtime) sets it
  `blocked` instead, with the original outcome in the event.
- **K3 `spawn_now(conn, task_id, *, board)`.** The per-card body of `dispatch_once`'s spawn step,
  extracted (not copied) so both callers share it. Calls `_default_spawn(placement=None)`, skips the
  planner, the load gate and the per-tick budget; still takes the board's tick lock.
- **K4 Budget isolation.** The `delegations` board is excluded from
  `count_running_tasks_other_boards` and from the load gate's admission count, so background units
  never consume kanban card capacity (capacity is `delegation.max_concurrent_children`, enforced
  before the card exists).
- **K5 Create without the overlap gate.** `create_task` on the `delegations` board skips
  `kanban_overlap.find_overlaps` (no OVERLAPS comments, no 10-minute `overlap_hold`).
- **K6 Lineage tag for escapee reaping.** `_default_spawn` sets
  `HERMES_KANBAN_LINEAGE=<task_id>:<run_id>` on every worker (all boards). It is not in
  `KANBAN_ENV_KEYS`, so `scrub_kanban_env` keeps it on delegated children's subprocesses. It carries
  no authority: nothing may read it as ownership (a lint fails on any reader outside the reaper).
  `_run_env_escapees_detailed` (`kanban_db.py:17495`) matches it as well as TASK/RUN_ID, still
  bounded by the run's birth window. Side effect on ordinary boards: tool processes started by a
  worker's in-process delegate children are now reaped with the worker's other leftovers, the same
  policy its own processes already get.

The board is visible in `hermes kanban boards list` and the dashboard on purpose (debuggable,
no new hiding surface).

## 7. Retiring `tools/delegate_detached.py` and `delegation.detached_background`

State today (measured): main has the module with the knob default OFF (#2160); no profile config
on the Studio sets the knob; the default profile's registry holds 18 records that ever ran detached
(10-20, while the knob was ON on #2170 E2E), all `done`; no other profile has any.

1. P2 adds `delegation.background_runtime` (`in_process` | `kanban`, default `in_process`).
2. P3 flips the default to `kanban` after the E2E drills.
3. P4 deletes `tools/delegate_detached.py`, its tests, the adoption/fence/stand-down code in
   `async_delegation.py` / `async_delegation_store.py` (`claim_detached_adoptions`,
   `stand_down_detached`, `set_detached_block`, `clear_detached`, `_run_detached_unit`,
   `_adopt_detached`, `_install_detached_supervision`, `_detached_recovery_runner`), the
   `detached_background` default and getter, docs/dev/delegate-detached-background.md, and fixes
   the `tools/AGENTS.md` "Durability" line and the `cron/AGENTS.md` "process-local" line in the
   same PR. Precondition, recorded in the PR: on every fleet host, zero `running`/`recoverable`
   records carrying a `detached` block in any profile's registry (one `jq` per host, output pasted).
   A record that still carries one after P4 is failed at boot with
   "detached runtime retired; re-run the delegation" and its pid logged, never signalled.
4. #2170 (option B) fixes inside `delegate_detached.py` are deleted with the module; its
   `tools/file_state.py` work stays (§8 rows 10, 25, 29).

## 8. The 31 open P1s at #2170 e9a3dfd7

Verdicts: **gone** = the code is deleted and nothing in this design replaces it with home-made
supervision; **kanban** = the job moves to existing dispatcher code (cited); **design** = still
applies, answer here; **N/A** = test of a deleted module.

| # | key | where | verdict | answer |
|---|---|---|---|---|
| 1 | cad55e4e4799 | tests/.../test_delegate_detached_safety.py:907 | N/A | file deleted (P4); I8 for new tests |
| 2 | 020a022d3c8d | same :1169 | N/A | same |
| 3 | eb6ac2c14494 | delegate_detached.py:325 PID reuse, no heartbeat bound | gone / kanban | `_reap_worker_session` reaps only with both birth bounds known (kanban_db.py:17383) |
| 4 | e6da1be9848f | async_delegation.py:2090 factory closes over loop config | gone | the gateway never reconstructs a unit; the runner reads its own profile |
| 5 | 1ec6308a1929 | async_delegation_store.py:1383 dead leader released beside live tool | gone | no re-dispatch exists (I3); survivors reaped by the crash sweep |
| 6 | 3e4a53f5c897 | delegate_tool.py:3615 named provider identity | design | persist `requested_provider` name; runner resolves it like the CLI (§5.2.1, §5.3.3); AT-8 |
| 7 | 9493f3573397 | async_delegation.py:1597 job identity deleted before tool death | gone / kanban | no job dir; the run row keeps pid + fingerprint until `reap_terminal_workers` confirms |
| 8 | 532e66a4de74 | tests :1215 | N/A | file deleted; I8 |
| 9 | 1eb632eda041 | delegate_detached.py:313 PID reuse | gone / kanban | as #3 |
| 10 | 3a5ef8faf345 | file_state.py:73 lock root differs across processes | design | lock root = `kanban_home()/state/file-locks` in every process. K1 pins `HERMES_KANBAN_HOME=<dispatcher's kanban_home()>` into the runner env (step 1 of `kanban_home()`, `kanban_db.py:1166`). Today `_default_spawn` pins only `HERMES_KANBAN_DB`/`WORKSPACES_ROOT`, so a gateway on a custom `HERMES_HOME` and a runner under the native `profiles/` root would derive different roots. Lands with B's file_state change or in P2; AT-10 |
| 11 | 7b5584535443 | delegate_detached.py:723 members.json overwrite | gone | |
| 12 | 5c13226e14d2 | delegate_detached.py:674 user-bus env | kanban | `_default_spawn` applies `systemd_user_bus_env(env)` (kanban_db_dispatch.py:8261) |
| 13 | ef968436cb9d | delegate_detached.py:527 provider without target model | design | `target_model=route.model` (§5.3.3); AT-8 |
| 14 | 49a7ba3407bb | delegate_tool.py:3708 sandbox reuse | design | eligibility allowlist: `local`/`ssh` only (§5.1.4); AT-7 |
| 15 | 4bbe470b37c9 | delegate_detached.py:303 unrelated termination | gone / kanban | as #3 |
| 16 | 5b4ac7caedae | delegate_detached.py:309 unknown membership | gone | |
| 17 | 7a2264e3050c | async_delegation.py:2082 orphan dirs on unknown scan | gone | no orphan dirs or stand-down markers |
| 18 | f1ca3da23dc9 | delegate_detached.py:633 no upper bound | gone / kanban | as #3 |
| 19 | f127e6d3bb64 | delegate_detached.py:710 survivor signalling | kanban | the reaper signals each process group of the session, not members as leaders |
| 20 | 891000acec8b | async_delegation.py:2017 retry timers vs shutdown | gone | no retry timers, no hand-off; supervision is in `kanban.db`, not in a boot |
| 21 | b9657c44e8dc | async_delegation.py:1457 cancel when identity unknown | kanban | archive + `reap_terminal_workers` retry each tick + runner self-poll (§5.5); AT-4 |
| 22 | 48aaef48a772 | async_delegation.py:2075 freeze profile config | gone | as #4 |
| 23 | 27ccc94e9643 | delegate_detached.py:333 uncaptured survivors, fence | gone | no fences, no re-dispatch |
| 24 | a2561fce2f02 | delegate_detached.py:253 zombie leader reuse | gone | |
| 25 | 3cebba6972e4 | file_state.py:174 forget_task check-and-unlink race | design | not in the deleted module; OFF-reachable, B (t_7ccdc21b) fixes it; P3 flip waits for it |
| 26 | 90f22da2db3f | delegate_detached.py:284 reused group | gone | |
| 27 | b1177741eb00 | async_delegation.py:2026 orphan identity deleted | gone | |
| 28 | 4cd9e879cf0e | delegate_tool.py:3588 child prefill | design | runner loads the profile prefill through the shared loader (§5.3.3); AT-8 |
| 29 | cb9d31ea6f8e | file_state.py:128 stale sibling-edit refusal across processes | design | units are separate processes under C too: the cross-process writer record (B) is a P3 precondition; AT-10 |
| 30 | 12d0c5da2421 | delegate_tool.py:3984 routing restrictions | design | the runner adds no route-derived provider override; unpinned children inherit the parent's routing (§5.3.3); AT-8 |
| 31 | 566a2590230e | delegate_detached.py:213 surviving members before redispatch | gone | no re-dispatch (I3) |

Totals: gone or kanban 20, design 8, N/A 3.

## 9. Phases

- **P0 - measure (done).** §3. Raw data attached to the card.
- **P1 - kanban seams K1-K6.** No delegation wiring; nothing user-visible except K6's wider reap.
  - *Unit:* K1 argv for the `delegations` slug, and no placement even with
    `kanban.local_worker_uid` set; K1 argv unchanged when the assignee profile sets
    `foreign_lane.worker_command`, and when the card carries a model override and reasoning effort; writing `runner`/`respawn` keys into another board's
    `board.json` changes nothing; K2 each requeue path parks `blocked` on the `delegations` board
    (crash, rate-limited, cohort, stale, max-runtime: one parametrized test per path, red on base);
    K3 `spawn_now` produces the same `spawned`/`placement` event payload keys as a tick spawn; K4
    count excludes the board; K5 no overlap comment; K6 an escapee whose env carries only the
    lineage tag is reaped, one born outside the run's window is not, and the reader lint fails on a
    planted reader.
  - *E2E:* scratch `HERMES_HOME` + `HERMES_KANBAN_SANDBOX=1`: create a card on the `delegations`
    board whose runner subcommand is stubbed to sleep then complete; `spawn_now`; SIGKILL it; one
    dispatcher tick -> card `blocked`, one `spawned` event.
  - *Negative:* a normal-board spawn's argv and env diff against base is exactly the added
    `HERMES_KANBAN_LINEAGE` key (the test captures `Popen` args on base and head); requeue
    behaviour unchanged (existing dispatcher tests unmodified and green in CI); archiving a `dir`
    card whose worker was killed leaves `workspace_path` and its contents byte-identical (tree hash
    before and after).
  - *Verify with:* `scripts/run_tests.sh tests/hermes_cli/test_kanban_delegation_runner_board.py`
    -> all pass; CI green on the PR.
- **P2 - runner, dispatch, delivery, cancel** behind `delegation.background_runtime`
  (default `in_process`).
  - *Unit:* eligibility table (each §5.1 condition false -> in-process); dispatch writes the
    `kanban` block and `route` with `requested_provider`; runner superseded/cancelled/exception
    paths; `supersede_attempt` makes a stale card's runner exit at step 2 with zero model calls;
    spawn failure -> the fallback thread runs exactly once under the NEW attempt id, finalizes, and
    the record has zero `async_delegation_restarted` events and an unchanged `redispatch_count`;
    targeted drain queues one record's `queued_boot_id=None` event exactly once per boot and leaves
    another record's pending event (stamped by another live boot) untouched; card-end watcher vs a
    late runner write (second writer refused); `_settle_kanban_record` moves the live record out of
    `_ACTIVE_STATES` on done, error and cancel; `/stop` archiving a card whose runner already wrote
    `done` still delivers once and writes `delegation_drained`; `claim_recoveries` skips kanban
    records; `_recover_abandoned_delegations` emits no owner-loss notice for a running kanban record
    with a dead `owner_pid`; `cancel_matching(all_active=True, owner_boot_id=X)` and
    `(all_active=True, owner_boot_id="")` both skip a kanban record and still cancel an in-process
    one; `include_kanban=True` cancels it.
  - *E2E:* AT-1, AT-3, AT-4, AT-5, AT-6, AT-7, AT-8, AT-10, AT-11 on the Studio against a scratch gateway
    (`hermes gateway run`, scratch home, the fake endpoint from P0 extended to answer the child).
  - *Negative:* I4 grep; I6 negatives; a unit whose record is cancelled before the runner reads it
    never runs a model call (fake endpoint log shows zero child requests).
  - *Verify with:* `scripts/run_tests.sh tests/tools/test_delegation_unit_worker.py
    tests/tools/test_async_delegation_kanban_runtime.py` -> all pass; the AT receipts below.
- **P3 - drills on both hosts, then flip the default.** AT-1, AT-3..AT-6, AT-9 and AT-11 on the Studio,
  AT-2 on ace-ai. Preconditions: B merged (rows 25, 29). Last commit flips the default to `kanban`.
- **P4 - retire the detached runtime** (§7).

## 10. Acceptance tests

Each AT has a hard timeout of 15 min, a cleanup trap that archives scratch cards and kills scratch
gateways by recorded (pid, create time), and lands raw pids, timestamps and log lines in the card
handoff. "Tick" = `kanban.dispatch_interval_seconds`, default 60 s.

- **AT-1 Studio SIGTERM mid-unit.** Scratch gateway, knob `kanban`, fake endpoint whose child
  first issues one `terminal` call running `sleep 100`, then answers after 120 s. Dispatch; record
  the worker's (pid, create time) and the `sleep` process's uid, cwd, `HERMES_HOME` and ancestry.
  SIGTERM the gateway; start a new one. Pass: the worker's (pid, create time) is unchanged across
  the restart and its ppid is 1 or launchd; the `sleep` runs as the gateway's uid with an ancestry
  that reaches the worker pid with no `sshd` (I5); the origin session receives exactly one
  `async_delegation*` message of any type (result, restart notice or owner-loss notice) for the
  unit, and a third restart after delivery adds none. Negative control: same run with knob `in_process` -> the unit is
  re-dispatched (generation 1), proving the restart reached it.
- **AT-2 ace-ai `systemctl --user restart` mid-unit.** Same as AT-1 with a `systemd --user`
  scratch gateway unit. Pass adds: the worker's cgroup is its restart-safe scope, not the gateway
  unit's (`/proc/<pid>/cgroup`).
- **AT-3 Restart while the unit finishes.** (a) Stop the gateway; let the unit finish while it is
  down (registry shows a pending outbox event with `queued_boot_id` null); start the gateway ->
  delivered once. (b) Restart; the unit finishes after the new boot's startup scan -> delivered by
  the card-end watcher within 10 s of the card's `completed_at`, once.
- **AT-4 /stop after a restart.** Dispatch, restart the gateway, `/stop` in the origin chat. Pass:
  card `archived`, worker (pid, create time) gone within 120 s (2 ticks), registry `cancelled`, no
  completion message, fake endpoint receives no child request after the stop timestamp + 6 s.
- **AT-5 Worker SIGKILL.** The unit's child starts, through the `terminal` tool, a command that
  traps SIGTERM and sleeps (the local backend gives it a new session; `scrub_kanban_env` strips
  TASK/RUN_ID from its env). SIGKILL the runner. Pass: within 120 s the card is `blocked`, exactly
  one `spawned` event, the tool process is gone (create-time checked, found through K6), and one
  error completion carrying the worker log tail is delivered. Negative control: with K6's matcher
  disabled the tool process survives (proves the stimulus reached the gap).
- **AT-6 Another process's cancel cannot reach a surviving unit (I9).** Start a kanban unit; stop
  the gateway. On the same profile: (a) run `hermes -z "hi"` (its shutdown runs
  `interrupt_all(reason="CLI shutdown")` -> `cancel_matching(all_active=True,
  owner_boot_id=<its boot>)`); (b) start a CLI session, have it start its OWN background
  delegation first (the CLI `/stop` returns before `interrupt_all` when it has nothing running,
  `cli_commands_mixin.py:927`), then `/stop`. Stimulus proof: for each, the CLI process's own
  in-process record carries a `cancel_attribution` stamped by that call (registry read). Then start
  the gateway. Pass: the kanban record is still `running` after (a) and after (b), the worker
  (pid, create time) is unchanged, and the unit delivers exactly once. Negative control: the
  in-process record of a stopped gateway's unit IS cancelled by (a) (today's behaviour preserved).
- **AT-7 Eligibility and kill switch.** CLI session, docker backend with `container_persistent:
  false`, a fork child, dispatcher disabled, knob `in_process`: each runs in-process (no card
  created). Flipping the knob to `in_process` while a kanban unit runs: that unit still delivers.
- **AT-8 Route parity, under multiplex.** Gateway with `gateway.multiplex_profiles` on; the unit is
  dispatched from a SECONDARY profile whose provider key exists only in that profile's `.env` (the
  scrubbed-env case). That profile has a named custom endpoint selected by `/model`, a model whose
  `api_mode` depends on the model, `provider_routing.only/ignore/data_collection` set, and a
  `prefill_messages_file`. Pass: the runner's resolved `(provider name, base_url, api_mode)` equals
  the gateway parent's; the child's request to the fake endpoint carries the prefill and the routing
  preferences; the fake API key string appears in neither the card nor the registry.
- **AT-9 Cost.** n = 10 units on the Studio, same harness as §3. Pass: every unit's dispatch payload
  says `"runtime": "kanban"` and has its own card with one `spawned` event; dispatch -> child's
  first model request p50 <= 8 s and p90 <= 15 s; runner max RSS <= 300 MB. Control: the
  in-process path in the same run (expected ~0.5 s).
- **AT-10 Sibling edit across processes.** (a) The parent session writes a file; a kanban unit
  patches a stale view of it -> refused with the sibling-write error, as an in-process child is
  today. (b) Two concurrent kanban units read the same file; unit A patches it; unit B then applies
  a stale V4A delete -> refused (row 29's case). Runs WITHOUT `HERMES_KANBAN_SANDBOX` (which makes
  `kanban_home()` ignore `HERMES_KANBAN_HOME`), against a scratch gateway whose `HERMES_HOME` is
  outside the native root and a unit from a `profiles/` profile, so the row-10 pin is what makes
  both processes use one lock directory.
- **AT-11 Slots are released (I10).** One gateway, `max_concurrent_children = 3`, five SEQUENTIAL
  kanban units (each dispatched after the previous one delivered), then three CONCURRENT ones and a
  fourth while they run. Pass: all five sequential units return `"runtime": "kanban"` and create a
  card; `active_count()` returns to 0 within 10 s after each delivery; of the concurrent four, three
  are kanban and the fourth gets today's capacity response. Negative control: with
  `_settle_kanban_record` disabled the fourth sequential unit runs synchronously.

## 11. Security, ops, observability

- Credentials: none persisted (I4); the worker's env is built by the dispatcher's scrub
  (`build_subprocess_env` + `strip_launch_profile_env`), and the runner re-sources its profile's
  `.env` through the CLI bootstrap (§5.3), the same as every worker.
- Privilege: tools run as the gateway's uid, as today's in-process child's do (I5). The
  `kanban.local_worker_uid` boundary is a kanban-card policy; extending it to background units is a
  separate decision, not part of C.
- Logs: worker stdout/stderr in the board's `logs/<task>.log`; registry lifecycle events
  `kanban_spawned`, `kanban_card_ended`; gateway log lines `async_delegation_kanban_spawn_failed`,
  `async_delegation_kanban_drained delegation_id= event_id=`.
- Rollback: knob to `in_process` (I7). Reverting P2 code is safe only once no record has
  `execution.runtime == "kanban"` in `running`; the revert PR carries that `jq` count.

## 12. Risks

- **R1 Boot cost under load.** 3-5 s measured at load1 10-18; the slow tail at higher load is
  unmeasured. AT-9 thresholds catch it.
- **R2 Memory.** 82 units/day peak; with `max_concurrent_children` 32 the worst case is 32 x
  ~250 MB = 8 GB on a 512 GB Studio. Smaller hosts: the same cap applies; no new limit.
- **R3 `kanban.db` write load.** One card + ~1 heartbeat/min per unit; at the 82/day peak this is
  small next to the board's 2,210 spawns in 3 days.
- **R4 Survivors that leave the session AND drop the lineage tag** (a tool that clears its whole
  env) are not found by the crash sweep. Same residual every kanban worker has; K6 closes the common
  case (delegate children's tools), AT-5 proves it.
- **R7 Wedged-but-beating runner.** If the runner's main thread wedges while its heartbeat thread
  keeps beating, nothing reclaims it and the unit runs uncapped. Same as today's in-process unit
  with no `child_timeout_seconds` (the sync path's per-child stall monitor still applies). Accepted.
- **R5 B slips.** P3 waits on B's file_state fixes (rows 25, 29). P1-P2 do not.
- **R6 Registry size.** The Studio default profile's registry is 6.1 MB (116 records) and every
  locked write rewrites it. C adds per unit: 1 dispatch write, 1 terminal write, 1 targeted drain,
  1 ack; the same order as today's in-process unit. No steady-state polling
  of the registry (§5.3.5, §5.4).

## 13. Open questions (RULED by Ace, 2026-10-21 02:45 PDT, msg 1562401396303597598)

Both ruled as recommended: **Q1 = (a) never re-run**; **Q2 = (a) bypass**. Recorded in §14 (RD-6, RD-7).

- **Q1 No automatic re-run after a worker crash (I3).** Today's restart recovery re-runs a killed
  unit from scratch with a continuation note. Under C a restart does not kill the unit, so re-run
  would only fire on a crash (OOM, SIGKILL, quota exit), and it repeats side effects. Options: (a)
  never re-run, deliver the error and let the parent re-delegate; (b) re-run once with the
  continuation note, which brings back the "survivor still running beside the re-run" class (#5,
  #23, #31). Recommendation: (a).
- **Q2 Admission.** Background units bypass the kanban load gate and card budget (K3, K4); the
  delegation cap is the only limit, as today. Option (b): count them against kanban capacity, which
  makes a background unit queue behind cards on a busy Studio. Recommendation: (a) bypass.

## 14. Resolved decisions

| id | decision | why |
|---|---|---|
| RD-1 | One internal board `delegations`, not per-profile boards | assignee already carries the profile; one board for the dispatcher to sweep |
| RD-2 | The runner reuses the synchronous delegation path, not a custom child loop | sync semantics (roles, skills, images, schema, timeout, heartbeat) are tested; I1 |
| RD-3 | Delivery truth stays in `async-delegations.json`; the card carries only process lifecycle | one outbox, one claim/ack, same code for owner-up and owner-restarted |
| RD-4 | Spawn at dispatch (K3), not on the next tick | ready -> claimed p50 83 s on the live board |
| RD-5 | Board stays visible | debuggable; no new hiding surface |
| RD-6 | No automatic re-run of a kanban-backed unit after a worker crash (I3); deliver the error, the parent re-delegates | Ace 2026-10-21 02:45 (Q1 = a) |
| RD-7 | Background units bypass the kanban load gate and card budget (K3, K4); `delegation.max_concurrent_children` is the only limit | Ace 2026-10-21 02:45 (Q2 = a) |
| RD-7 | PROPOSED pending Q2: bypass kanban admission | §13 |
| RD-8 | Runner tools run as the gateway uid, not `kanban.local_worker_uid` (pass-1 CB-2) | parity with today's in-process child; the knob is unset on the Studio and ace-ai anyway |
| RD-9 | Board behaviour keys on the slug constant, not `board.json` (pass-1 lens) | a writable file must not switch a board to a different runner |

## 15. Fold map (rev 1 -> rev 2)

| Pass-1 item | Fold |
|---|---|
| CB-1 dead-owner `all_active` cancel | I9; §5.5 rule in `cancel_matching` (rev 3: explicit `include_kanban` keyword, see pass-2 RC-4); AT-6 with negative control. Grounded: the callers are `cli_shutdown.py:135` and `cli_commands_mixin.py:934`, via `interrupt_all(recoverable=False)` -> `async_delegation.py:2385` |
| CB-2 local-uid ssh hop | Grounded: `kanban.local_worker_uid` is unset in the Studio root config and on ace-ai; the code path is real regardless. I5 now covers tools; K1 skips the hop for the slug; K3 passes `placement=None`; AT-1 asserts the tool process's uid and ancestry; RD-8 |
| (found while grounding CB-2) delegate children's tools are invisible to the escapee reaper | K6 lineage tag; §5.5; AT-5 with negative control; R4 |
| RC-1 fallback double-execution | §5.2.4 `supersede_attempt` before the thread starts |
| RC-2 runner bootstrap | §5.3: entry is a hidden `hermes -p <profile> delegation-unit` subcommand (shares `_apply_profile_override`, `.env`, `process_env_files`); AT-8 under multiplex from a secondary profile |
| RC-3 `dir` workspace removal | Cited `kanban_db_workspace.py:455`; P1 negative test (tree hash before/after) |
| RC-4 5 s registry drain | Removed. Card-end watcher reads SQLite; targeted `enqueue_record_outbox` per ended card; runner polls its card, never the registry; registry measured 6.1 MB / 27 ms per load (R6) |
| RC-5 card archive owner | §5.4: the card-end watcher archives after ack; retry next tick; boot pass archives >48 h cards |
| Lens: heartbeat independence | §5.3.5 dedicated thread, pure liveness beat |
| Lens: unit-vs-unit sibling edit | AT-10(b) |
| Lens: AT-4 tick interval | 120 s, tick defined in §10 |
| Lens: `board.json` writable by anyone | K1/K2/K4/K5 key on the slug constant; RD-9 |
| Lens: normal-board spawn diff | P1 negative test diffs argv/env against base |
| (self-check) rev 1 row 10 claimed the kanban root was already pinned into workers | Wrong: only `HERMES_KANBAN_DB` is. K1 now pins `HERMES_KANBAN_HOME` |

### Rev 2 -> rev 3 (Momus pass 2)

| Pass-2 item | Fold |
|---|---|
| CB-3 live record never settled (slot leak, silent sync fallback) | I10; §5.2.5 the worker returns without `_finalize` and tags `kanban_unit`; §5.4 step 3 `_settle_kanban_record` through `_finish_finalization` is the only exit; boot observer records settle the same way; AT-11 (sequential beyond the cap, with negative control); AT-9 asserts a card per unit |
| RC-1 fallback keeps the old attempt id | §5.2.4: `supersede_attempt` returns the new id; the worker re-points `_records`, `mark_submitted`s and finalizes on it; no `redispatch_count` bump, no restart event; P2 unit test |
| RC-2 native foreign-lane branch wins over K1 | K1 argv is final over the native branch and every override; P1 unit test with `foreign_lane.worker_command` |
| RC-3 `max_runtime_seconds` shape | Grounded: `child_timeout_seconds` is an inactivity cap, default none (`delegate_tool_config.py:24-27,148`). Card gets no wall-clock cap, same as today's unit; the runner's stale heartbeat is the process backstop |
| RC-4 empty `owner_boot_id` sentinel | Grounded: `current_boot_id()` returns `""` on failure (`async_delegation.py:134`). Replaced with `include_kanban: bool = False`; no existing caller sets it; P2 tests both sentinels |
| RC-5 AT-6 CLI `/stop` leg was a non-stimulus | Grounded: `cli_commands_mixin.py:927` returns early. AT-6(b) starts its own background unit first and proves the call via `cancel_attribution` |
| RC-6 legacy mirror owner-loss notice | Grounded: `_recover_abandoned_delegations` already skips running detached records (`async_delegation.py:449`); the skip is extended to `runtime == "kanban"`; P2 unit test; AT-1 counts all `async_delegation*` messages |
| RC-7 "not yet drained" marker | §5.4 step 4: a `delegation_drained` card event; P2 race test (`/stop` vs a runner that already wrote `done`) |
| Lens: sync-capacity fallback logged at info | The kanban spawn-failure fallback logs WARNING (§5.2.4); the existing capacity fallback is unchanged and out of scope |
| Lens: conftest scrub of `HERMES_KANBAN_HOME` | Grounded: `tests/conftest.py:166` already captures `HERMES_KANBAN_HOME` before sandboxing; no change |
| Lens: AT-10 under sandbox ignores the pin | AT-10 runs without `HERMES_KANBAN_SANDBOX`, gateway home outside the native root |
| Lens: cut `restamp_kanban_owner` | Cut (I9 no longer reads owner liveness) |
| Residual: running record whose card is lost | §5.4 boot: fail with "unit card lost" and deliver |

### Rev 3 -> rev 4 (Momus pass 3)

| Pass-3 item | Fold |
|---|---|
| CB-4 boot fails a live unit on an unreadable card row | §5.4 Boot: three-outcome read; read error -> retry next tick; positively absent -> lineage-tag process scan (K6); fail only when the scan succeeds and finds nothing; two P2 tests |
| RC-1 `interrupt_all` reaches kanban live records (both modes) | Grounded: `interrupt_all` targets every `_ACTIVE_STATES` record except `detached_unit` (`async_delegation.py:2343-2388`). §5.4: `interrupt_all(include_kanban=False)` drops `kanban_unit` records from targets, `durable_ids` and `recover_on_shutdown`; I9 names both halves; P2 test for both modes |
| RC-2 status mapping | §5.4 step 3 fixed table `done->completed`, `failed->error`, `cancelled->cancelled`, else no settle |
| Lens: "return without `_finalize`" vs `finally` | §5.2.5 `kanban_spawned` flag, same shape as `handed_off` |
| Lens: wedged-but-beating runner | R7, accepted (parity with today) |

### Rev 4 -> rev 5 (Momus pass 4, APPROVE-WITH-CHANGES; terminal, no further pass)

| Pass-4 item | Fold |
|---|---|
| RC-1(a) absent-row retry had no host | §5.4: the card-end watcher re-checks running kanban records whose card it did not return, every tick |
| RC-1(b) scan matched escapees, not the runner | runner = lineage tag AND argv element `delegation-unit`; tagged leftovers without a runner are killed (I8), then the unit fails and delivers; P2 test |
| RC-1(c) scan failure indistinguishable from not-found | scan is UNKNOWN on `process_iter` raise or any same-uid `environ()` failure |
| Lens: observer record needs `interrupt_fn` | §5.4 boot: `interrupt_fn = _cancel_kanban_unit` |
| Lens: double `_finalize` in fallback | §5.2.4: single finalize via the existing `finally` reading the re-pointed attempt id |
| Residual: shutdown waits on `active_count()` | Grounded: the only gateway reader is the scale-to-zero suspend probe; kanban records excluded from it (§5.4 boot) |
