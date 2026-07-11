# t_37659d3c progress

## Diagnosis (2026-07-10)

Observed outbound message `1525268439529164851` snowflake-decodes to
`2026-07-10T15:32:14.101-07:00`.

The reported send did **not** use cron auto-delivery and did not enter
`GatewayRunner._run_agent`:

- `cron/scheduler.py:3298-3328` constructs `AIAgent(platform="cron")` directly.
- `cron/scheduler.py:3376-3382` runs it in a copied-context worker thread.
- `cron/scheduler.py:3031-3039` correctly resolved and bound the stored origin as
  `HERMES_CRON_AUTO_DELIVER_*` (`discord:1523978409129021484`).
- The persisted cron transcript (`state.db`, session
  `cron_73830b66a04e_20260710_151618`) shows the model wrote
  `~/.hermes/scripts/redispatch-graph-research.sh` with an explicit foreign
  `--origin discord:1525251294728556615`, then launched it at 15:32:09.
- `~/.hermes/scripts/dispatch-agent.sh:49-60,88` sends its ACK to that explicit
  argument. The wrong-thread message timestamp is the ACK timestamp.
- The scheduler's real final delivery ran later and logged at 15:33:59:
  `Job '73830b66a04e': delivered to discord:1523978409129021484`.
- No `Agent executor context mismatch` warning fired because cron correctly
  bypasses the gateway wrapper; there was no gateway executor binding to compare.

Therefore hypotheses 1 and 4 are false. Hypothesis 2 describes the architecture
but not the defect. Hypothesis 3 is confirmed only for a nested process launched
by the cron agent: that process accepted an explicit target invented from task
context instead of the job's stored origin. Core cron delivery itself routed
correctly.

Root cause in the repo: `_build_job_prompt` at `cron/scheduler.py:2407-2419`
tells cron agents that final output is auto-delivered, but gives no contract for
nested subprocesses that emit ACK/heartbeat/status messages. The authoritative
per-job target exists in worker ContextVars, but the model is never told to use
`HERMES_CRON_AUTO_DELIVER_PLATFORM/CHAT_ID/THREAD_ID` rather than infer or
hardcode an ID from referenced work.

## Planned regression and fix

1. Add a worker-level regression in `tests/cron/test_scheduler.py` that runs
   `run_job` with a foreign chat ID in task text and a different stored origin,
   captures the actual worker prompt plus worker ContextVars, and asserts the
   prompt identifies the ContextVar-backed target as authoritative and forbids
   inferring/hardcoding a target from task content.
2. Observe RED on current `fork/main`.
3. Bind a scheduler-owned delivery-target instruction into the prompt from the
   same `delivery_target` object used to populate `HERMES_CRON_AUTO_DELIVER_*`.
   This preserves the intentional separation between cron execution identity
   and delivery identity from commit `dbafa083b5`.
4. Mutation-check by removing the binding and re-running the regression.
5. Run the targeted cron scheduler and gateway/session-context suites.

No live cron mutation, gateway restart, or push is part of this task.

## Implementation and verification

- Added `_bind_cron_delivery_target_hint()` in `cron/scheduler.py`. `run_job`
  invokes it only after resolving the concrete target and binding the same target
  into `HERMES_CRON_AUTO_DELIVER_*`. Cron execution identity remains blank as
  required by `dbafa083b5`.
- The hint JSON-encodes the authoritative target and tells nested status helpers
  to read the three existing cron delivery variables instead of inferring IDs
  from task content. Jobs with no resolved target are unchanged.
- Added a worker regression with `FOREIGN_CHAT` in task text and a distinct stored
  origin. It asserts both the actual worker ContextVars and the authoritative
  target instruction.
- Strengthened the transport regression to contaminate ambient
  `HERMES_SESSION_*` values and assert `_send_to_platform` receives the stored
  origin chat ID and thread ID.

Observed test evidence:

- RED before implementation:
  `scripts/run_tests.sh tests/cron/test_scheduler.py -q` -> `226 passed, 1 failed`;
  the new worker test failed because the authoritative target was absent.
- GREEN after implementation:
  the same command -> `227 passed, 0 failed`.
- Mutation check: removed the single prompt-binding call and reran the same file
  -> `226 passed, 1 failed`; restored the call and reran -> `227 passed, 0 failed`.
- Related gateway/send coverage:
  `scripts/run_tests.sh tests/gateway/test_session_context_inheritance.py tests/tools/test_send_message_origin.py tests/tools/test_send_message_tool.py -q`
  -> `172 passed, 0 failed`.
- Lint: shared-venv `ruff check cron/scheduler.py tests/cron/test_scheduler.py`
  -> `All checks passed!`; `git diff --check` passed.
- Deterministic pre-scan ran Bandit, Ruff, and Semgrep. Whole-file mode reported
  existing repository/test-file findings; none are on the added production lines.
- Momus review transport could not start because its configured
  `opus-review-direct.py` path is absent under the Daedalus profile. The task is
  therefore handed off `review-required` rather than self-approved.
---

# delegate_task restart-survival build progress

Task: `t_50d839af`
Spec: `~/.hermes/plans/2026-07-10_delegate-restart-survival-spec.md`
Momus review: `~/.hermes/plans/2026-07-10_delegate-restart-survival-momus-p1.md`

## Status

- Rebased this worktree onto `fork/main` before implementation and again after
  upstream advanced; `fork/main` is an ancestor of the feature commit.
- Added a profile-scoped, owner-only durable registry at `$HERMES_HOME/state/async-delegations.json`.
- Added persist-before-submit, submission telemetry, attempt fencing, dead-boot claiming, a two-relaunch breaker, restart/terminal outbox replay, durable acknowledgement, retention/cap enforcement, and record integrity checks.
- Added a shared live/resume adapter in `tools/delegate_tool.py`; recovery reconstructs the original single/batch unit with continuation instructions and re-resolves current credentials under the originating profile.
- Wired one recovery pass per gateway boot, including degraded boots with no
  connected adapters. Restart and terminal events continue through
  `process_registry.completion_queue` and the existing
  `_async_delegation_watcher`; no second drain or conversation-history mutation
  was added.
- Wired bounded best-effort `recoverable` marking on gateway shutdown. Dead-owner recovery of a still-`running` record is the guaranteed fallback.
- Wired `/stop`, `/new`, parent, and session cancellation to terminally cancel
  durable work before signalling live children, including detached work with no
  resident foreground agent.
- Added `delegation.resume_on_restart: true` to both config default surfaces and documented it.
- Added a loud registry-cap log and `sync_fallback_registry_cap` observability counter; unpersistable work degrades to synchronous execution.

## Momus required changes folded

- **RC-1 folded:** a dead boot's claimed-but-never-submitted generation is reconciled without incrementing `redispatch_count`; only durable `submitted_at` telemetry consumes one of the two replacement launches. Named test: `test_rc1_claimed_but_never_submitted_does_not_count_attempt`.
- **RC-2 folded:** restart and terminal outbox entries both replay after the queuing boot dies. A restart event from an older generation becomes terminal `dropped(superseded)` rather than remaining pending. Named test: `test_rc2_pending_restart_event_replays_and_superseded_event_drops`.
- **RC-3 folded:** clean-shutdown `recoverable` marking uses a bounded lock timeout and is explicitly best-effort. Recovery treats a dead owner's `running` record equivalently.
- **RC-4 folded:** all anchors were re-grounded against current `fork/main`; event delivery extends the existing `process_registry.completion_queue`, gateway watch drain checkpoint, and `_async_delegation_watcher`.

## Re-grounded anchor map

| Surface | Current anchor |
|---|---|
| Child construction / execution | `tools/delegate_tool.py:1146` `_build_child_agent`; `:2010` `_run_single_child` |
| Live/resume adapter | `tools/delegate_tool.py:2663` `_build_durable_background_spec`; `:2798` `build_recovered_delegation_runner`; `:2847` `delegate_task` |
| In-memory dispatch / recovery | `tools/async_delegation.py:153` single dispatch; `:443` batch dispatch; `:706` recovery; `:851` outbox replay; `:930` cancellation/shutdown; `:975` session cancellation |
| Durable registry | `tools/async_delegation_store.py:35` path; `:126` lock; `:303` persist; `:368` submit telemetry; `:435` terminal; `:540` durable cancellation; `:652` claim; `:811` replay; `:847` acknowledgement |
| Gateway boot/profile scope | `gateway/run.py:1805` `_profile_runtime_scope`; `:7560` `_current_boot_id`; `:8424` startup |
| Gateway fresh-turn delivery | `gateway/run.py:17772` injection; `:17852` once-per-boot recovery; `:18030` existing async-delegation watcher; `:18722` explicit session cancellation |
| Shared completion rail | `tools/process_registry.py:173` `completion_queue`; `:2080` `format_process_notification` |
| Boot identity | `gateway/status.py:142` process start; `:182` current boot ID; `:187` liveness; `:443` boot ID construction |
| Config defaults | `hermes_cli/config.py:976` `DEFAULT_CONFIG`, delegation at `:2278`; CLI fallback at `cli.py:499` |

## Verification log

Canonical runner: `scripts/run_tests.sh`.

- Initial RED: `tests/tools/test_async_delegation_persistence.py` — 13 failed because durable APIs did not exist.
- Core GREEN: persistence + existing async delegation — 34 passed.
- Broad focused pass: 5 files / 348 tests passed.
- Gateway recovery/routing pass: 2 files / 34 tests passed.
- Expanded persistence pass: 21 tests passed.
- Shared delegate resume adapter pass: 3 tests passed.
- Final post-rebase broad focused pass: 12 files / 506 tests passed, 0 failed.
- Exact Momus gate:
  `test_rc1_claimed_but_never_submitted_does_not_count_attempt` and
  `test_rc2_pending_restart_event_replays_and_superseded_event_drops` — 2 passed.
- Shared-venv `compileall` passed for all changed runtime modules.
- Ruff passed for all changed Python files.
- `git diff --check` passes after removing two Markdown hard-break spaces from
  this progress file.
- Final diff-aware Bandit/Ruff/Semgrep pre-scan found one production finding:
  Semgrep flagged owner-only `0o700` directory permissions as more permissive
  than `0o644`; this is a scanner false positive because `0o700` grants no
  group/other access. Remaining added-line findings are test-only assertions and
  fixed `/tmp` fixture values.
- Independent review was not obtained: the configured Momus transport path is
  absent and the standing-profile fallback timed out after 500 seconds. Final
  disposition remains `review-required`.
