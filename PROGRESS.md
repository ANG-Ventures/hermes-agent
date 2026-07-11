# Auto-continue taxonomy refactor — progress ledger

Task: `t_0f2327fc`
Base: `fork/main` at `55b8a1b002cdc4559df1ab8f604b6ae9da96027e`
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
- Base adapters already expose generation-aware post-delivery callbacks (`gateway/platforms/base.py:4025-4115`), fired by the actual response-delivery path; this is the correct contract-faithful seam for the SELF delivery barrier, rather than a manually set test flag.
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

NEXT: Phase 2 — write RED tests for typed deferred requests, taxonomy logging/notes, release-time arm, and boot reconciliation (T1/T2/T3/T7/T8/T9/T10 IDs), then implement the smallest gateway changes to make them green.
