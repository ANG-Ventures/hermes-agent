# Auto-continue taxonomy refactor — progress ledger

Task: `t_0f2327fc`
Base: `fork/main` at `b286e8ceca646906fb34bef60749261d63afca1f`
Authoritative spec: `~/.hermes/plans/2026-07-11_auto-continue-taxonomy-refactor-spec.md`

## Phase 1 — RC-5 evidence pack (tree-grounded, before code)

### 1. Turn-end chokepoint and ordering

- Every normal turn completion reaches `GatewayRunner._release_running_agent_state` from the `_run_agent` unwind (`gateway/run.py:14054-14058`, with the containing turn's unconditional release documented at `gateway/run.py:12501-12511`).
- The chokepoint is `gateway/run.py:18610-18680`. Its actual order is: generation ownership check (`18639-18644`), active-session lease release (`18645-18650`), remove `_running_agents` (`18651`), remove timestamp/task/busy-ack (`18652-18659`), clear boot-resume state (`18660-18674`), then persist the lower active count (`18675-18679`).
- Divergence from spec hypothesis: there is currently no post-release callback and no delivery barrier in this method. The deferred-restart arm hook must run only after the slot/task removals and active-count persistence, and must schedule (not await) the shutdown work so release remains non-reentrant.

### 2. Adapter `send()` acknowledgement semantics

- The common contract is `BasePlatformAdapter.send()` returning `SendResult(success, message_id, error, raw_response)` (`gateway/platforms/base.py:1854-1860`, `gateway/platforms/base.py:2895-2914`). Completion of the coroutine plus `success=True` is therefore the only uniform cross-platform acknowledgement; the API does not promise client display/read acknowledgement.
- Discord awaits each SDK `channel.send()` before collecting the returned message id (`plugins/platforms/discord/adapter.py:2268-2302`) and returns success only after all chunks are accepted (`2329-2337`). This is Discord API acceptance, not user display/read.
- Telegram awaits Bot API `send_message()` (`plugins/platforms/telegram/adapter.py:3686-3716`) and treats generic timeouts as UNKNOWN because the request may have reached Telegram (`3794-3808`). A successful return is Bot API acceptance, not user display/read.
- Streaming delivery is already tracked from real adapter results: `StreamConsumer` sets `final_content_delivered` only after successful send/edit paths (`gateway/stream_consumer.py:178-182`, `680-686`, `757-809`), and the runner consumes those flags before deciding whether to perform the normal final send (`gateway/run.py:22790-22827`).
- Base adapters already expose generation-aware post-delivery callbacks (`gateway/platforms/base.py:4025-4115`), but the tree-grounded implementation review found that they fire from the handler `finally` block even after cancellation/failure (`gateway/platforms/base.py:5348-5385`). They are not delivery acknowledgements. The implementation therefore adds a separate generation-owned callback fired only after `SendResult.success`, while confirmed streaming delivery is recorded by the runner.
- All other platform adapters implement the same `BasePlatformAdapter.send -> SendResult` contract. No adapter exposes a stronger uniform durable/read receipt. Per spec RC-2, `send()` success / confirmed stream delivery is the barrier; timeout means UNKNOWN and restart proceeds with the loss warning.

### 3. `mark_resume_pending` persistence

- `SessionStore.mark_resume_pending` holds the store lock, rejects suspended sessions, mutates the entry, and calls `_save()` synchronously before returning (`gateway/session.py:2191-2218`). No extra flush API is required for ordinary marks.
- `suspend_recently_active` also performs one synchronous `_save()` after its in-memory marking loop (`gateway/session.py:2297-2331`).
- Divergence/constraint: crash injection after in-memory mark but before `_save()` (T9o) cannot be represented by calling the public method as-is. Boot reconciliation needs an explicit internal two-step test seam (mark mutation, crash hook, synchronous save) while production still preserves the public single-call contract.

### 4. F1/F2 breaker and replay-mark sites

- F1/programmatic restart records the initiating session before setting global restart state (`gateway/run.py:8060-8073`).
- C1 recognizes an executed `safe-restart.py` terminal call (not mere mention) and sets the same per-session flag (`gateway/run.py:20006-20032`).
- Clean-turn F2 consumes the breadcrumb unconditionally, ORs it with the in-memory flag, records a replay mark for restart-initiating turns, otherwise clears marks after genuine work (`gateway/run.py:7576-7661`).
- Breadcrumb validation/consumption is per-session, same-boot, TTL-bounded, and single-use (`gateway/run.py:7663-7758`). It currently unlinks while validating, so deferred arm requires a dedicated consume-and-return validation result (or equivalent) rather than a second consume.
- The reusable replay API is `_record_restart_replay_mark(session_key, now=...)` (`gateway/run.py:7523-7564`). It is not request-id-idempotent today. Cross-boot deferred reconciliation therefore needs request-id dedup persisted alongside breaker state before it can satisfy T9e/T9j.
- Drain-time marking records a replay mark before `mark_resume_pending` (`gateway/run.py:6707-6724`), and the hard timeout marks only genuinely still-running agents before interrupting them (`gateway/run.py:10237-10309`).

### 5. #269 resume-request dropbox read/write sites

- External writes are atomic temp+fsync+replace at `gateway/resume_requests.py:51-83`.
- Current reads enumerate only `*.json`, parse, then unlink *before* returning the request (`gateway/resume_requests.py:86-142`). Malformed files become `*.rejected`; stale files are deleted.
- Gateway folding is `_sweep_resume_requests` (`gateway/run.py:8389-8419`). It immediately calls synchronous `mark_resume_pending`.
- Reads occur before tail classification (`gateway/run.py:8211-8218`), again at scheduler entry before candidate enumeration (`gateway/run.py:8446-8457`), and from housekeeping when a `.json` file exists (`gateway/run.py:23016-23030`).
- Divergence from new lifecycle: the existing sweep's same-pass unlink cannot support submitted→armed→claimed→terminal boot-owned cleanup. Existing plain resume requests must retain current behavior; `deferred_restart` needs a typed lifecycle API that does not flow through the destructive tuple sweep.

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
### 6. Boot startup order

- Real startup order after adapters settle is restart notification, `_prepare_auto_resume_decisions`, `_schedule_resume_pending_sessions`, then `_finish_startup_restore` (`gateway/run.py:9315-9345`).
- `_prepare_auto_resume_decisions` currently sweeps the dropbox before snapshot/classification (`gateway/run.py:8199-8218`). `_schedule_resume_pending_sessions` sweeps again before locked candidate enumeration (`gateway/run.py:8421-8462`).
- Therefore boot deferred-restart reconciliation belongs at the start of `_prepare_auto_resume_decisions`, before the existing plain-request sweep and before scheduling. T9k must execute this real order, not call reconciliation in isolation.

### Grounded design decisions / divergences

1. Preserve plain #269 request behavior; add typed deferred-request lifecycle APIs beside it.
2. Reuse the common `SendResult`/post-delivery path as the barrier; do not invent per-platform durable receipts.
3. Add request-id-bearing SELF metadata to `SessionEntry`; current schema has only `resume_pending`, `resume_reason`, and timestamp (`gateway/session.py:715-720`, serialized at `775-783` and restored at `858-863`).
4. Extend the replay store with request-id idempotence; the current API is timestamp-only.
5. The safe-restart script and skill are outside this repository; spec §4 explicitly assigns the skill rewrite to Apollo post-merge. This branch will implement and test the gateway/dropbox contract and list the required external script change in handoff, without mutating the live skill tree.

## Phase 2 — RED/GREEN implementation

- Added explicit taxonomy constants and the authority mapper in `gateway/auto_resume.py`; persisted `resume_kind`, `resume_handoff`, and `resume_request_id` through `SessionEntry` and `SessionStore`.
- Added the typed deferred lifecycle in `gateway/deferred_restart.py:68-434`: atomic request publication, submitted→armed→claimed→consumed/rejected transitions, same-boot breadcrumb validation, epoch-owned `mkdir` CAS, atomic `meta.json` commit publication, committed-only loser coalescing, and boot-only terminal reconciliation.
- Added a real delivery-ack seam at `gateway/platforms/base.py:4098-4139`. It is generation-owned and fires only from the successful final-send path; confirmed streamed delivery directly acknowledges the armed callback before normal-send suppression. Handler-finally callbacks are intentionally not accepted as delivery proof, and ordinary streamed turns retain no confirmation cache.
- Wired boot reconciliation before the prompt/auto branch and candidate snapshot (`gateway/run.py:8254-8311`), release-time arm after running-state persistence (`gateway/run.py:18736-18855`), request-id replay dedupe (`gateway/run.py:7547-7612`), explicit SELF scheduling without SIBLING attempt credit, and `kind=sibling|self` schedule logs.
- Preserved plain #269 requests by making their sweep ignore `kind=deferred_restart` files.
- Added G1 tests in `tests/gateway/test_deferred_restart_taxonomy.py:51-900`, including T9a/b/c/d/j/k/m/n1..n6/o/p/q. T9n1..T9n6 correspond to every fallible post-`mkdir`/pre-signal boundary introduced: initial leader publication, claim persistence, replay mark, SELF mark, all-peer delivery completion, and committed-meta publication. Every boundary is exercised both with a concurrent loser and as the normal single-request case.

RED observations:

- Initial collection failed on missing `RESUME_KIND_SELF`.
- Added delivery/note contracts failed until release-time strong acknowledgement and SELF note wiring existed.
- Combined regression caught the intentional new taxonomy log and an unintended legacy SIBLING-credit bypass; the bypass was narrowed to explicit persisted `resume_kind=self`, preserving existing behavior for legacy reason-only rows.
- Tree review caught that existing post-delivery callbacks are finally callbacks, not send acknowledgements; the strong callback registry above replaced that unsafe assumption.

GREEN evidence via the repository virtualenv runner:

- `scripts/run_tests.sh tests/gateway/test_deferred_restart_taxonomy.py tests/gateway/test_auto_continue_interrupted_turns.py tests/gateway/test_restart_cascade.py -q` → 122 passed.
- `scripts/run_tests.sh tests/gateway/test_resume_requests.py tests/gateway/test_session.py tests/gateway/test_post_delivery_callback_chaining.py tests/gateway/test_restart_cascade.py tests/gateway/test_auto_continue_interrupted_turns.py tests/gateway/test_deferred_restart_taxonomy.py -q` → 249 passed.
- Final post-rebase command across resume requests, sessions, callback chaining, restart cascade, interrupted-turn behavior, and deferred taxonomy → 277 passed; latest deferred lifecycle file → 61 passed after staggered multi-initiator delivery, T9n6 coverage, injected arm-to-owner failures, host-reboot clock reset, real startup scheduling, stale-latch deletion failure, and loser backoff.
- `ruff check` on all changed Python files → all checks passed.
- Static pre-scan: bandit/ruff/semgrep, 0 HIGH/CRITICAL findings; low/medium output is baseline noise from scanning complete large files and pytest assertions.

Mutation evidence (each mutation was restored from the staged implementation immediately after the expected RED):

- Replace strong delivery-ack registration with finally callback → T2/T8 fails because no strong callback is armed.
- Remove SELF mark → T2/T8 fails (`resume_kind` remains `None`).
- Bypass `mkdir` CAS → T9c fails with two restart signals.
- Remove request-id dedupe → T9j trips the breaker one request early.
- Remove boot reconciliation from preparation → T9k fails in prompt mode.

## Phase 3 — adversarial review corrections

- Momus pass 1 identified generation fail-open and boot-reconcile retry ownership. Both were fixed and regression-tested; disputed signal ordering, legacy log mapping, and cross-boot submitted handling were dismissed by the authoritative spec/source pack.
- Pass 2 identified orphaning of a sole failed winner and malformed committed metadata. The coordinator now retries non-owning/injected precommit cancellation and fallible precommit exceptions with bounded backoff while propagating real external task cancellation to shutdown/boot ownership. Committed metadata requires a finite positive numeric `commit_ts` before any loser transition.
- Pass 3 identified an unbounded alternate-order stream-confirmation cache. Tree ordering proves release precedes streaming suppression, so the cache was removed; confirmed stream delivery directly fires an existing strong callback and ordinary delivery retains no state.
- Pass 4 identified cross-request delivery-barrier bypass. The elected leader now waits until every same-boot armed/claimed request has independently acknowledged delivery or timed out before atomically committing; a staggered two-initiator regression proves no early signal.
- Pass 5 identified an arm-to-task ownership gap when the durable `armed` transition succeeded but a later scan or callback-registration step failed. The coordinator now retains the exact armed/claimed request in memory, schedules its owner immediately after the durable arm, and treats callback-registration failure as UNKNOWN delivery while preserving eventual one-signal progress. Injected post-arm scan and callback failures both prove the request remains owned.
- Pass 6 found the same fingerprint at the narrower rename→payload-refresh boundary. Lifecycle state is authoritative in the successfully renamed filename, so ownership is now established immediately after rename and a failed redundant payload refresh is logged without abandoning the recoverable request. The exact post-rename failure is injected for both a sole request and a concurrent peer; both cases produce exactly one signal.
- Pass 7 certified the ownership fingerprint resolved, then found two new classes: persisted monotonic timestamps fail across host reboot, and T9k tested reconciliation/order proxies instead of the startup effect. Deferred intent/commit and boot-start ordering now use wall-clock timestamps, with a simulated monotonic-epoch reset proving the handoff survives. The startup prepare→snapshot→schedule→finish sequence is one binding helper used by `start()` and T9k; the real surviving request is scheduled as SELF in the same boot.
- Pass 8 certified both pass-7 classes resolved, then identified a non-yielding stale-latch deletion retry and fixed 10 ms loser polling. Failed stale deletion now verifies the directory remains and yields with bounded exponential backoff; all uncommitted/torn loser polling uses the same 10 ms→1 s bounded backoff. Injected regressions prove the event loop yields before deletion retry and the loser never falsely coalesces or signals.

NEXT: Commit, push/open the fork PR, wait for green CI, then arm auto-merge and record the CI/merge handoff. External safe-restart writer/skill wiring and the physical T6 rig remain Apollo/Aegis post-merge work per the authoritative sequence.
