# Boomerang v2 smoke harness

One command for the deploy-gated smoke card (t_989cde1b), built ahead of the deploy so that card
is a run, not a build:

```bash
python3 tools/dev_probes/boomerang_v2_smoke/run.py --step all --live
```

Coded against the contract in `~/.hermes/plans/2026-10-18_boomerang-fork-autowake-spec.md`
(sections 5.4, 5.5, 5.7, 10), not against the P1-P4 branches. Stdlib only. Dev probe, not a
tool: `tools/dev_probes/` has no `__init__.py`, so tool discovery never imports it.

| Step | Acceptance | What it does | Needs Apollo |
|---|---|---|---|
| 1 | AC-7, blocked_assess, INV-6 | Mints 3 cards on the scratch board (level `boomerang wake`, origin chat subscribed `notify+wake`, set by `edit --boomerang-wake`), forces `completed` / `blocked --kind needs_input` / (optional) a forced child failure through the kanban CLI, then reads the handled receipt (see *Handled receipt* below) for `boomerang handled <card> (<kind>): <summary>` (the gateway's handback line), the gateway log for `kanban notifier: boomerang-wake <card> disposition=auto`, for the needs_input card a `boomerang handled <card> (blocked_assess): ...` receipt and `disposition=auto row=blocked_assess` and its claim (state file or `.terminal.jsonl` archive under `BV2_WAKE_STATE`) terminal with an outcome in the gateway's `HANDLED_OUTCOMES` (a failed assessment logs the same row and receipt; since #104/#309 every `blocked` event is boomerang-woken to assess the block, `gateway/kanban_wake_disposition.py` row 1; the old INV-5 "needs_input must surface" no longer holds), and (fault) a `boomerang-wake superseded <card>: card moved to <status> before the wake ran` log line with NO `warning boomerang-wake failed <card>` line in the origin chat (INCOMPLETE when the fault-card claim closed handled instead: that row cannot pass since #425, see *Before the live run*). | no |
| 2 | AC-8, INV-7 | Kill switches. Precondition: no `<root>/state/boomerang-wake.disabled` stop file and `fork_enabled: true` (or unset). Prints the one-line change, waits for the ack, re-reads config / the stop file, forces an event, asserts `boomerang.fork_ineligible reason=fork_enabled` (fork off) and no boomerang wake within one tick (stop file present). Then asks for the restore. | **yes: 3 changes** (`fork_enabled=false`, `boomerang_wake=disabled` = touch the stop file, `restore`) |
| 3 | AC-9, INV-8 | Sends a canary request (positive control, below), forces one fork turn (or observes one with `--no-trigger`), waits for `boomerang.fork served_by=<seat>`, then scans the relay wire capture (relay-wire-forensics JSONL, `req.headers` / `req.headerOrder`) for any `x-hermes-*` header. Zero captured records is INCOMPLETE, never PASS. | no |
| 4 | AC-11, 5.7 | Restart drill observer, two arms, one gateway restart each. **Redispatch arm** (retry unspent, t_870c1fba): forces an event, waits for the boomerang-wake dispatch, asks for the restart, then waits for a fresh boot line after the dispatch, the old boot's INFO `kanban boomerang-wake: <card> child interrupted by gateway shutdown; claim left open` line (the drain timed out with the child mid-call, t_abf5cae7; absent = INCOMPLETE; see *Step 4: making the drain interrupt the child* below) with ZERO `warning boomerang-wake failed <card>` lines, the INFO `kanban boomerang-wake: <card> redispatched after gateway restart (retry 1/1) delegation=<new>` line within the first-tick window, the `<key>#restartN` claim of delegation `<new>` terminal in `BV2_WAKE_STATE` with an outcome in the gateway's own `HANDLED_OUTCOMES` (imported from `gateway/kanban_boomerang_wake.py`; `surfaced` counts, `failed`/`lost`/none in the window FAIL; the evidence names the outcome), the handled receipt `boomerang handled <card> (<kind>): <summary>` or the chat's `[boomerang-wake] child surfaced <card> (...)` after the restart, and ZERO `warning boomerang-wake lost <card>` lines. **Fallback arm** (retry pre-spent: the harness writes `restart_spent[<card>]=1` (the restart retry; `retries` is untouched) into `BV2_WAKE_STATE` once the child is `running`): `lost reason=gateway restart, retry spent resurfaced delivered=True` in the log within the first tick, `warning boomerang-wake lost <card> (reason: gateway restart, retry spent)` in the origin chat, no redispatch, no late `boomerang handled <card>` line. It never restarts anything. | **yes: 2 gateway restarts** (`restart`, once per arm) |

The harness never edits config or the stop file, never restarts the gateway, and refuses any board whose slug
does not match `^bv2-smoke`. Its one state write is step 4's fallback arm: `restart_spent[<card>]=1` (restart retry) into
`BV2_WAKE_STATE` (required live; never defaulted, so it cannot touch a fleet gateway's file).

## Step 4: making the drain interrupt the child (t_409de482)

How the shutdown interrupt reaches a boomerang child, read from the code on main 921449502b:

- The drain (`gateway/run_shutdown.py::_drain_active_agents`) waits on session turns
  (`_running_agents`), cron jobs, api_server runs and deferred agent workers. A boomerang child is
  none of these: `run_entry` runs it with `asyncio.to_thread(delegate_task, ...)`. With an idle home
  session the drain ends at once (`drain took 0.00s, timed_out=False, active_at_start=0`) and the
  process exit kills the child. That is the hard-kill path, not the interrupt #283 fixed.
- Only a drain TIMEOUT interrupts anything: `_stop_interrupt_remaining_work` calls
  `_interrupt_running_agents`, which hard-interrupts each agent in `_running_agents`. Nothing
  interrupts a boomerang child directly (no walk of `delegate_tool_registry._active_subagents`).
- The interrupt reaches the child **through its parent**: `InterruptControlMixin.interrupt()` fans
  out to the agent's `_active_children`, and `delegate_task` attached the child there
  (`_attach_child`). The parent `resolve_parent` returns is the home session's cached agent (fork
  mode) or `snapshot_parent(cached)`, whose `copy.copy` + `vars().update` shares the SAME
  `_active_children` list; a home turn that reuses the cached agent therefore carries the child.
  A summary child built by `_db_parent` (no cached agent) has a private parent no one interrupts:
  it can only be killed, never interrupted.
- So the redispatch arm holds a home-session turn open across the restart. After the dispatch it
  waits for the child's session id on the claim, then for that child's first `API call #N` line in
  `agent.log` (tagged `[<child session>]`), then sends `plan.commands.s4_hold_turn` (the lane makes it
  a puppet `--say` asking for a foreground `sleep` longer than the drain) and waits until
  `gateway_state.json` `active_agents` is non-zero. Only then does it ask for the restart. The drain
  now times out on the home turn, its interrupt fans out to the child mid-call, and `run_entry` sees
  `status=interrupted` while `_draining` is set.
- The hold must be a turn of the HOME session (a turn in another session times the drain out, but its
  interrupt never reaches this child), so the retry child forks a home transcript that ends with the
  interrupted hold. On 11-12 12:18 the hold named the card and the retry child took "run sleep 120 ...
  reply S4-HOLD-DONE" for the card's deliverable (`request_changes` on the done fixture card). The hold
  therefore never names the card: `s4_hold_turn` gets `{chat}` only, a `{card}` in it FAILs the arm
  unsent, and the lane's message says it is harness scaffolding that later turns must ignore.
- Without `s4_hold_turn` in the plan the arm records INCOMPLETE up front. If the hold turn is held
  but the shutdown line is still absent, read the old boot's `drain done ... timed_out=` line and the
  `PHASE=drain_timeout_turn` lines: a timed-out drain with no shutdown line is a #283 defect (the
  interrupt did not reach the child, or the claim closed as failed), not a harness gap.

## Handled receipt (hermes-agent#439)

Since #439 (t_8ab5557d) the gateway writes `boomerang handled <card> (<kind>): <summary>` to the
fleet agent log (`<fleet root>/state/agent-logs/<UTC day>.jsonl`, a row with `producer: boomerang-wake`),
not to the origin chat, unless `<HERMES_HOME>/state/boomerang-handled-line-chat.enabled` exists. The
fleet root is the nearest ancestor of `HERMES_HOME` holding `scripts/lib/agent_inbox.py` (the
writer's own lookup; `NOTIFY_AGENT_LOG_DIR` overrides, `BV2_AGENT_LOG_DIR` overrides both); the knob
is home-local. On the puppet lane the scratch gateway needs a fleet root to write the row at all:
run the lane with `PUPPET_LANE_SCRATCH=<dir>` where `<dir>/scripts/lib/agent_inbox.py` exists, and point
`BV2_AGENT_LOG_DIR` at `<dir>/state/agent-logs` (with no fleet root the gateway posts the line to the
chat, which still counts). The harness follows the same knob (`BV2_HANDLED_KNOB`):

- **Knob absent (default):** a `boomerang-wake` agent-log row since the step started passes; no
  chat line is expected. A chat line still counts (the gateway posts it when no agent log is
  available, or a surfaced turn was refused). The blocked_assess receipt and the negative checks
  (step 4's late line) read both. The evidence marks an agent-log hit `[agent-log] ...`.
- **Knob present:** the line is required in the origin chat; agent-log rows are ignored.

Only `boomerang handled` rows are read from the agent log; every warning (`failed`, `lost`) is
still a chat line. The dry-run fixtures keep the pre-#439 transcript, so the fixture knob
(`fixtures/fake_boomerang-handled-line-chat.enabled`) is present; `fixtures/fake_agent_logs/` holds
the same receipts as agent-log rows for the knob-absent tests.

## Ack mechanism

When a step needs Apollo it prints:

```text
>>> APOLLO ACTION (...):
    config.yaml: delegation.boomerang.fork_enabled: true -> false
>>> then: echo fork_enabled=false >> $TMPDIR/bv2-smoke.ack
```

Apollo makes the change (config via the normal config writer; restart via `safe-gateway-restart`
with its single-use approval), then appends the token as a whole line to the ack file. The
harness polls the file (default `$TMPDIR/bv2-smoke.ack`, override `BV2_ACK_FILE`,
`--ack-timeout` default 1800 s). Tokens, in order: `fork_enabled=false`,
`boomerang_wake=disabled`, `restore` (step 2), `restart` twice (step 4). Live, an ack counts only
when it is a NEW line since the request, so arm 1's `restart` never answers arm 2.

**Scripted operator (`BV2_ACK_CMD`).** On an ISOLATED scratch gateway (the puppet-lane
`bv2-smoke-lane.sh`, hermes-home) there is no Apollo to ack: set `BV2_ACK_CMD` to an argv prefix
and the harness runs `<cmd> <token>` instead of waiting. The command performs the action on the
gateway UNDER TEST (edit the scratch config or touch/rm its stop file, SIGTERM + relaunch the scratch `gateway run`) and
exits 0; the harness then appends the token to the ack file itself, so the evidence reads the same.
A non-zero exit is a FAIL for that ack, never a pass. Dry-run ignores it.

## Live inputs

| Variable | Meaning | Default |
|---|---|---|
| `BV2_HOME_SESSION` | home session stamped on the cards (the test channel's gateway session) | required live |
| `BV2_HANDLED_KNOB` | the gateway's `boomerang-handled-line-chat.enabled` knob file (see *Handled receipt*) | `<HERMES_HOME>/state/boomerang-handled-line-chat.enabled` (dry-run: the fixture, present) |
| `BV2_AGENT_LOG_DIR` | fleet agent log dir (`<UTC day>.jsonl`) read for the handled receipt when the knob is absent | `<fleet root>/state/agent-logs` (see *Handled receipt*; dry-run: `fixtures/fake_agent_logs`) |
| `BV2_ORIGIN_CHAT` | Discord test-channel id (REST read, `DISCORD_BOT_TOKEN` from the gateway `.env`, GET only) or a capture file | required live |
| `BV2_SCRATCH_BOARD` | scratch board slug | `bv2-smoke` |
| `BV2_KANBAN_CMD` | kanban CLI prefix | `hermes kanban` |
| `BV2_GATEWAY_LOG` | gateway log | `<root>/logs/gateway.log` |
| `BV2_GATEWAY_ENV` | `.env` holding the bot token | `<root>/.env` |
| `BV2_GATEWAY_PID` | gateway pid file (boot_id before/after, step 4) | `<root>/gateway.pid` |
| `BV2_WAKE_STATE` | the scratch gateway's `gateway/kanban_boomerang_wake.json`; step 4's fallback arm writes `restart_spent[<card>]=1` (restart retry) here | required live for the fallback arm (SKIP without it) |
| `BV2_CONFIG` | config.yaml read by step 2 | `<root>/config.yaml` |
| `BV2_STOP_FILE` | boomerang-wake stop file read by step 2 | `<root>/state/boomerang-wake.disabled` |
| `BV2_WIRELOG_DIR` | wire capture root (step 3) | `~/.claude-wirelog` |
| `BV2_ACK_FILE` | ack file | `$TMPDIR/bv2-smoke.ack` |
| `BV2_ACK_CMD` | scripted acker argv prefix, run as `<cmd> <token>` (see Ack mechanism) | unset = wait for a human |
| `BV2_SYNC_TIMEOUT` | seconds allowed for `plan.commands.wirelog_sync` (a timeout is exit 124 in the evidence; on the fork's seat any non-zero exit makes step 3 INCOMPLETE) | `600` |
| `BV2_EVIDENCE` | evidence markdown | `docs/boomerang-v2/evidence/smoke.md` |
| `BV2_PLAN` | command/expectation plan (`commands.s4_hold_turn`: argv, `{chat}`/`{card}` filled, that sends the step-4 hold turn) | `fixtures/plan.json` |

`<root>` is the root Hermes home (a profile `HERMES_HOME` resolves to its root). Timing:
`--tick-seconds` (notifier tick, default 5), `--slack-seconds` (default 10), `--timeout` (max wait
for a child to finish, default 600).

Step 3 positive control (AC-9): before the scan the harness POSTs one `max_tokens: 1` request to
`canary_url` (default the apr relay `:18810`, `BV2_CANARY_URL` overrides) carrying
`x-hermes-lane-src: platform=bv2canary<nonce>;...` and `x-hermes-qa-canary: <nonce>`, then runs
`commands.wirelog_sync` for the seat that served it. No hop passes an unknown `x-hermes-*` header
(the relay forwards `content-type`/`anthropic-version`/`anthropic-beta` plus its own route-id and
lane-src, `claude_pool_relay.py:881,1322`; apx's outbound allowlist drops the rest,
`proxy.js:755-766,951`), so `x-hermes-qa-canary` can never reach a capture. `x-hermes-lane-src`
is the one fleet header the box records (as the record's top-level `lane_src`), so a record with
`lane_src.platform == <nonce>` proves a fleet header reached the capture point, and its
`req.headers` is scanned like the fork's. Canary seen + clean = PASS, canary missing = INCOMPLETE
(blind), any `x-hermes-*` name in `req.headers` = FAIL.

Step 3: if the fork is served by a remote seat, its capture is on that box. Run
`wirelog-fleet.py sync` and point `BV2_WIRELOG_DIR` at `~/.claude-wirelog-fleet`, and run
`wirelog-coverage-check.py` first (the harness prints both).
Live, the harness runs `commands.wirelog_sync` for the fork's seat itself; if that pull exits
non-zero, times out, or no sync command is configured, the check `fork seat ... capture pulled`
is INCOMPLETE: a partial pull can miss the fork request while other clean records from the seat
remain, so the header scan cannot be a PASS.

## Evidence

Each step replaces its own section of `docs/boomerang-v2/evidence/smoke.md` (markers
`<!-- bv2-smoke:stepN:begin/end -->`): commands run, raw output, a check table with
PASS / FAIL / SKIP / INCOMPLETE. Exit code: 0 all PASS, 1 any FAIL, 3 nothing failed but something
was SKIP/INCOMPLETE, 2 usage.

## Before the live run: check `fixtures/plan.json`

Two contract gaps are parameters, not code:

- `commands.set_boomerang_wake` is `hermes kanban edit <id> --boomerang-wake` (t_7be18632). If
  step 1 aborts at `set_boomerang_wake`, fix the argv in the plan.
- `commands.inject_failure` is the AC-7 live fault: a `kanban comment` on the failure card, run
  only after its boomerang-wake child is dispatched. The comment is a board event by another session
  past the claim cursor, so the verdict executor refuses it (`write refused: state moved`,
  `gateway/kanban_boomerang_wake.py::_moved`). That is a supersede (t_ce8bd439): `_fail` logs one
  `boomerang-wake superseded` line (to #logs, never the origin chat) AND re-surfaces the wake
  into the home session, which stays live. Step 1 then asserts that line, no chat warning, a
  `inbound message: ... msg='[kanban] Task <card>` turn in the gateway log, and no
  `no live home for <card>` line. The old fault (`edit --session <bogus>`) proved only the warning.
  **Since #425 this row cannot pass on the plan's fault** (Apollo ruling
  `t_9f0d174a-superseded-row-out-of-scope`): `_moved_noise` drops an unattributed `kanban comment`
  (actor session NULL), so a write verdict is not refused, and a `surface` verdict returns before
  the freshness check anyway. When the fault card's claim closes with a handled outcome the row reads
  INCOMPLETE with that outcome, never FAIL, and the re-surface row is INCOMPLETE too. `null` = SKIP live.
- `expect.resurface_log_regex` is a guess (5.7 names `deliver_wake` and the chat line, not a log
  line). Tune it to the line P3 emits.

## Tests

`tests/tools/dev_probes/test_boomerang_v2_smoke_harness.py` runs every step in `--dry-run` on the
fixtures, breaks each contract once to prove the step goes red, and runs step 1 `--live` against
a fake kanban CLI that plays the gateway.
