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
| 1 | AC-7, INV-5, INV-6 | Mints 3 cards on the scratch board (`auto_handle`, origin chat subscribed `notify+wake`), forces `completed` / `blocked --kind needs_input` / (optional) a forced child failure through the kanban CLI, then reads the origin chat and gateway log for `[auto-handled] <card> - <Outcome>`, `kanban notifier: auto-handled <card> disposition=auto`, no auto-handle on the needs_input card, and `warning auto-handle failed <card>: <reason>`. | no |
| 2 | AC-8, INV-7 | Kill switches. Precondition: `delegation.boomerang.auto_handle_enabled: true` and `fork_enabled: true` (or unset). Prints the one-line config diff, waits for the ack, re-reads config, forces an event, asserts `boomerang.fork_ineligible reason=fork_enabled` (fork off) and no auto-handle within one tick (auto-handle off). Then asks for the restore. | **yes: 3 config flips** (`fork_enabled=false`, `auto_handle_enabled=false`, `restore`) |
| 3 | AC-9, INV-8 | Sends a canary request (positive control, below), forces one fork turn (or observes one with `--no-trigger`), waits for `boomerang.fork served_by=<seat>`, then scans the relay wire capture (relay-wire-forensics JSONL, `req.headers` / `req.headerOrder`) for any `x-hermes-*` header. Zero captured records is INCOMPLETE, never PASS. | no |
| 4 | AC-11, 5.7 | Restart drill observer. Forces an event, waits for the auto-handle dispatch, asks Apollo to restart, then waits for a fresh boot line after the dispatch, the reconciler's re-surface line within startup + one tick, `warning auto-handle lost <card> (reason: gateway restart)` in the origin chat, and no late `[auto-handled]` line. It never restarts anything. | **yes: 1 gateway restart** (`restart`) |

The harness never edits config, never restarts the gateway, and refuses any board whose slug
does not match `^bv2-smoke`.

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
`auto_handle_enabled=false`, `restore` (step 2), `restart` (step 4). Start each smoke run with an
empty ack file: a stale token from an earlier run acks immediately.

**Scripted operator (`BV2_ACK_CMD`).** On an ISOLATED scratch gateway (the puppet-lane
`bv2-smoke-lane.sh`, hermes-home) there is no Apollo to ack: set `BV2_ACK_CMD` to an argv prefix
and the harness runs `<cmd> <token>` instead of waiting. The command performs the action on the
gateway UNDER TEST (edit the scratch config, SIGTERM + relaunch the scratch `gateway run`) and
exits 0; the harness then appends the token to the ack file itself, so the evidence reads the same.
A non-zero exit is a FAIL for that ack, never a pass. Dry-run ignores it.

## Live inputs

| Variable | Meaning | Default |
|---|---|---|
| `BV2_HOME_SESSION` | home session stamped on the cards (the test channel's gateway session) | required live |
| `BV2_ORIGIN_CHAT` | Discord test-channel id (REST read, `DISCORD_BOT_TOKEN` from the gateway `.env`, GET only) or a capture file | required live |
| `BV2_SCRATCH_BOARD` | scratch board slug | `bv2-smoke` |
| `BV2_KANBAN_CMD` | kanban CLI prefix | `hermes kanban` |
| `BV2_GATEWAY_LOG` | gateway log | `<root>/logs/gateway.log` |
| `BV2_GATEWAY_ENV` | `.env` holding the bot token | `<root>/.env` |
| `BV2_GATEWAY_PID` | gateway pid file (boot_id before/after, step 4) | `<root>/gateway.pid` |
| `BV2_CONFIG` | config.yaml read by step 2 | `<root>/config.yaml` |
| `BV2_WIRELOG_DIR` | wire capture root (step 3) | `~/.claude-wirelog` |
| `BV2_ACK_FILE` | ack file | `$TMPDIR/bv2-smoke.ack` |
| `BV2_ACK_CMD` | scripted acker argv prefix, run as `<cmd> <token>` (see Ack mechanism) | unset = wait for a human |
| `BV2_SYNC_TIMEOUT` | seconds allowed for `plan.commands.wirelog_sync` (a timeout is exit 124 in the evidence; on the fork's seat any non-zero exit makes step 3 INCOMPLETE) | `600` |
| `BV2_EVIDENCE` | evidence markdown | `docs/boomerang-v2/evidence/smoke.md` |
| `BV2_PLAN` | command/expectation plan | `fixtures/plan.json` |

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

- `commands.set_auto_handle` assumes P4's CLI flag is `hermes kanban edit <id> --auto-handle`.
  The spec only says `kanban_create/update` gain `auto_handle`. If step 1 aborts at
  `set_auto_handle`, fix the argv in the plan.
- `commands.inject_failure` is the AC-7 live fault: a `kanban comment` on the failure card, run
  only after its auto-handle child is dispatched. The comment is a board event by another session
  past the claim cursor, so the verdict executor refuses it (`write refused: state moved`,
  `gateway/kanban_auto_handle.py::_moved`) and `_fail` posts the warning AND re-surfaces the wake
  into the home session, which stays live. Step 1 then asserts the warning reason, a
  `inbound message: ... msg='[kanban] Task <card>` turn in the gateway log, and no
  `no live home for <card>` line. The old fault (`edit --session <bogus>`) proved only the warning.
  If the child finishes before the comment lands the check is INCOMPLETE; give the card a slow
  body with `commands.s1_fault_hold`. `null` = SKIP live.
- `expect.resurface_log_regex` is a guess (5.7 names `deliver_wake` and the chat line, not a log
  line). Tune it to the line P3 emits.

## Tests

`tests/tools/dev_probes/test_boomerang_v2_smoke_harness.py` runs every step in `--dry-run` on the
fixtures, breaks each contract once to prove the step goes red, and runs step 1 `--live` against
a fake kanban CLI that plays the gateway.
