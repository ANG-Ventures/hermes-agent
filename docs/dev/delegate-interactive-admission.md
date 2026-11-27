# Interactive delegation admission

## Contract and scope

A background caller with an existing later-result consumer must never become a
foreground child join because admission failed. The existing session-capability
and wake-session resolution remains authoritative. Finite callers still join;
recovered, already-admitted units still use their synchronous recovery runner.
No new queue, config, cap increase, registry purge, or live restart is included.

## Grounded branch census

`delegate_task` currently joins after durable-spec construction fails, after
`registry_cap`/`registry_error`, and after any other dispatch refusal. These are
new-dispatch failures, unlike the finite-session join before dispatch and the
recovered runner's final synchronous path. `async_delegation._dispatch_admitted`
checks capacity and durable persistence before submitting the worker. Submission
failure removes its own in-memory/SQLite reservation and terminalizes its own
durable attempt; its fallback event must be acknowledged as an inline rejection,
not reported later as a second completion. Detached/kanban launch fallback runs
inside an already-admitted worker and is unchanged.

The incident registry was reported by Apollo as exactly 256 retained terminal
records, not 256 active children. `_prune` stops at the cap while admission rejects
at the cap; retention has not expired. Current main's admission policy prunes
settled history to make one slot at the exact cap, while pending obligations
refuse promptly and remain intact. This fix preserves that policy.

## Implementation decision

One sibling helper closes only the constructed children through the existing
teardown door, finalizes their transcripts as rejected, ends the metrics unit,
and returns `status=rejected`, `mode=background`, `started=false`, a stable
`reason`, `retryable=true`, and a safe next step. It never returns an accepted
handle or executes a child. All three foreground fallback sites use it. Existing
submission-failure outbox acknowledgement is retained with truthful rejection
wording. Pool capacity and backend retirement receive explicit reason codes.

## Verification and rollout

Run `scripts/run_tests.sh tests/tools/test_delegate_admission_rejection.py -q`
on an isolated ci-box checkout. Each failure arm drives real delegate dispatch
with a blocked sentinel child; the parent must return before sentinel release,
with zero executions. The pre-fix run failed all five arms at the bounded parent
return assertion (5 failed in 20.76s). Fixture cleanup releases and joins the
sentinel even on failure. Add success/completion and finite compatibility controls,
then restore old branches as mutation controls. Integration must prove a second
parent request completes before releasing a successful child and one completion
after release, within 60 seconds. Live original-conversation acceptance and
restart authorization belong to Apollo, not this implementation run.

Rollback is a reviewed code revert through normal deployment; preserve all
registry/session data. Existing foreground waits are not retroactively repaired.

## Implementation receipts

ci-box isolated checkout, `HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh`:

- `tests/tools/test_delegate_admission_rejection.py -q`: 14 passed, 0 failed
  (runner wall 19.5s). Includes real loopback HTTP adapter/executor requests on
  one parent, both admission and refusal, plus double delivery through the real
  durable consumer producing one message. Inference alone is replaced by the
  blocked sentinel; this is not a production-chat claim.
- The same file with persistence and finite-outcome controls: 59 passed.
- API delivery contract and capacity-interrupt controls: 21 passed.
- Detached safety and cancellation controls: 60 passed, 1 skipped (169.9s).
- `python evals/delegate_admission_mutation.py`: restored inline execution at the
  shared refusal exit kills all five reason arms, 5 failed, harness exit 0;
  original source restored in `finally`. Initial old-source test: 5 failed.

Earlier development runs exposed a sentinel signature error, a too-short cold
import allowance, and an incorrect completion-event field assertion; these are
corrected, not counted as product regressions. Raw logs including those runs are
attached to task t_11d072bf. PR: ANG-Ventures/hermes-agent#119.

Cleanup follows the existing teardown door (its persistence close already catches
resource-close errors), manifest best-effort writer and metrics publisher. A
failure escaping one cleanup stage cannot prevent the remaining stages or replace
the non-start result: `cleanup_pending` names it. Outbox acknowledgement failure
leaves the existing durable obligation intact, rather than purging it; replay can
therefore still surface the submission failure. No new retry thread or arbitrary
I/O timeout is introduced. Injected exceptions at teardown, manifest, metrics and
acknowledgement all return truthful rejection with the failed stage recorded.

Exact-cap controls use real retained terminal records from other sessions:
pending-only capacity refuses without changing the record; mixed settled/pending
capacity prunes the settled record, admits the new child, and preserves the pending
record and its replay. Main's pre-construction admission checks and turn-stop
ownership remain in force. Post-construction refusal tests explicitly bypass
preflight to exercise the dispatch race, including all five inline-fallback mutants.
Apollo owns independent final review, CI/merge, live deployment and
original-conversation acceptance.
