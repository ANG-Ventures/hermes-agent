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

## t_4963087b — R2 closeout M1 ledger

- `2026-07-11T02:23:15Z` — Read the full closeout contract before inspecting the
  suite or R2-07 evidence. Confirmed the Hermes worktree was clean.
- `2026-07-11T02:26:17Z` — Completed the required pre-answer anchor check without
  opening any `R2-07-answer.json` result. `git log --follow` shows the R2 suite
  specification was first introduced at
  `1d987517bf363fdf16abde44a386f8cfcdc5f3ed` on
  `2026-07-10T03:47:20-07:00`; `git cat-file` confirms the file does not exist in
  that commit's parent. The two later pre-answer blobs (`698e0e1`, `d90d73e`)
  carry the same sole R2-07 description verbatim:
  `Gateway restart interrupted turn`. The complete row labels it only
  `answer-presence for cited docs`; it does not define the required behavior.
- `2026-07-11T02:26:17Z` — RC-a quality verdict: **STOP Item A**. The only
  git-anchored R2-07 intent is a topic label, not an independent property-level
  statement such as "surface a clarifying question and wait for the user."
  The later uncommitted frozen fixture (created `2026-07-10T09:53:53-0700`)
  contains a query plus lexical predicate ids, but no `intent` or `description`
  field and therefore cannot supply the contract's required independent
  pre-incident anchor. Per M1, no calibration set was authored and the held-out
  R2-07 answer remains unread. Apollo's GO adjudication must be re-examined; the
  `surface-ask` predicate must not be rewritten on this evidence.

## t_4963087b — Item B routing gate

- `2026-07-11T02:32:39Z` — RC4 read timing pinned before implementation.
  `Mem0MemoryProvider.initialize()` calls `_load_config()` once and snapshots
  `mem0_gbrain` into `self._gbrain_cfg` (`plugins/memory/mem0/__init__.py:1098,
  1160-1168`). Query-time code reads that snapshot (`:1459`, `:1642-1658`), not
  `mem0.json`. Therefore a future `audit_mode_for_ids` flip would take effect on
  provider reinitialization / the next gateway restart, not live in the resident
  provider. No instant-rollback claim is valid.
- `2026-07-11T02:32:39Z` — RC-c metric pinned to **hit@3**. The production
  prefetch path passes `mem0_gbrain.prefetch_limit` (default `3`) directly to
  `_gbrain_pointers`, then renders every returned pointer into `## Local Docs
  (gbrain)` (`plugins/memory/mem0/gbrain_recall.py:58`,
  `plugins/memory/mem0/__init__.py:1655-1664`). The explicit `mem0_search` lane's
  limit 5 is not the prefetch injection budget and is not the acceptance metric.
- `2026-07-11T02:32:39Z` — Per-call mode is **not selectable through the actual
  prefetch path**. Hermes sends `tools/call search` over OAuth HTTP
  (`gbrain_recall.py:314-325`). The live gbrain operation explicitly ignores
  `mode` whenever `ctx.remote is not false` (`src/core/operations.ts:538-546`),
  and the HTTP transport dispatches with `remote: true`. A live request with the
  invalid mode `definitely-not-a-mode` succeeded and returned the same top-3 as
  an omitted mode, proving the server used its configured mode (`tokenmax`).
- `2026-07-11T02:32:39Z` — Three-arm live prefetch A/B completed; full rows are
  saved at `/tmp/t_4963087b-routing-ab.json`.

  | arm | rows | baseline | requested audit | byte-identical |
  |---|---:|---:|---:|---:|
  | real ids (`SGR-*`, `proc_*`, `t_*`) | 5 | hit@3 5/5 | hit@3 5/5 | 5/5 |
  | ordinary prose | 5 | — | — | 5/5 |
  | adversarial near-misses | 5 | — | — | 5/5 |

  The five real identifiers were `SGR-EA6EE271`, `SGR-B8AB643D`,
  `proc_c06b8584afe`, `t_70e0d5a1`, and `t_1e5baf38`, each with a corpus-backed
  expected slug. A second local/trusted CLI comparison (where per-call mode is
  genuinely honored) also scored tokenmax 5/5 vs audit 5/5 at hit@3. The id arm
  is a strict tie, so RC3 forbids default-true and the spec's no-op-complexity
  rule says not to ship the detector. No detector or config flag was added;
  consequently there are no detector-fire log lines or rollback surface to
  certify. This is the contract-prescribed no-ship finding, not an implementation
  omission.

- `2026-07-11T02:34:00Z` — Verification: Hermes' canonical wrapper ran
  `plugins/memory/mem0/test_gbrain_recall.py` with **22 passed, 0 failed**.
  gbrain's focused `bun test test/search/per-call-mode.test.ts` ran **5 passed,
  0 failed**, including the explicit remote-mode-ignored contract.
  `git diff --check` passed. No live gateway was restarted or reconfigured.

## t_4963087b — Item A adjudication and closeout

- `2026-07-11T03:18:26Z` — Ace supplied the missing non-circular property
  anchor and adjudicated the held-out answer **PASS**: “prompt the user is
  fine”; preserve-and-prompt conveys the surface-and-ask doctrine. This ruling
  supersedes the RC-a STOP above. Per Ace's instruction, M1 calibration remains
  the protocol for future disputes and was not applied retroactively here.
- `2026-07-11T03:18:26Z` — Reproduced the frozen failure before editing by
  running `r2_answer_presence.py --check-only` against the original six-case
  fixture and captured results: **5/6, FAIL**. R2-07 passed
  `preserve-prompt`, `no-reexecute`, and `no-autocontinue`, but failed
  `surface-ask`; the other five cases passed.
- `2026-07-11T03:18:26Z` — Added Ace's property statement verbatim as the
  R2-07 `intent` field. Rewrote only R2-07's `surface-ask` lexical set to
  accept preserve-and-prompt and property-level prompt/ask equivalents. The
  other predicates and all five non-R2-07 cases are unchanged.
- `2026-07-11T03:18:26Z` — Re-evaluated the same frozen six answer payloads
  with the updated fixture: **6/6, PASS**. R2-07 `surface-ask` matched
  `preserve-and-prompt` and `ask whether to resume`; the five other cases'
  complete verdict/predicate records were byte-identical to the pre-edit
  check-only report. Mutation proof restored the old narrow phrase list in a
  temporary fixture and returned the suite to **5/6, FAIL**, proving the
  rewritten predicate is the load-bearing gate.
- Item B remains closed unchanged: hit@3 tied, so RC3 correctly ships no
  detector, flag, logging, or rollback surface.
