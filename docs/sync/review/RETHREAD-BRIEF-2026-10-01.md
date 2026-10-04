# RE-THREAD BRIEF: parity sync 2026-10-01, round 4 (fork deltas into upstream-extracted siblings)

Orchestrator card: **t_e45c8c8d**. Worktree: `/Volumes/fleet-scratch/workspaces/default/t_e45c8c8d/wt`,
branch `sync/upstream-2026-10-01`. The 2-parent merge is COMMITTED (merge commit `0c16eec702`, parents
`b6c8bfe8ab` fork-side + `612d8e44a2` upstream). There is no MERGE_HEAD now. `fork/main` baseline
checkout for comparisons: `/Volumes/fleet-scratch/workspaces/default/t_e45c8c8d/base` (read-only).

## The problem you are fixing
Upstream decomposed god-files (run_agent.py, conversation_loop.py, gateway/run.py, gateway/session.py,
hermes_state.py, cli.py, cron/scheduler.py, auth.py, ...) into sibling modules. The round 1-3 lanes kept
upstream's facades and wrote **FOLLOWUP manifests** listing every fork edit to a method that now lives in a
sibling. Those edits are NOT in the tree yet. Live symptoms (all PASS on fork/main, FAIL merged):
`rewind_to_message() got an unexpected keyword 'require_user_role'`,
`get_messages_as_conversation() ... 'include_timestamp'`,
`_resolve_session_agent_runtime() ... 'persisted_route_lookup'`, 219 fork-canary reds
(test_route_change_turn_contract 69, test_session_model_reset 21, undo/redo ~37, restart ~30, ...).

## You are a LANE worker. Hard limits.
- You own ONLY the target modules listed in your card body. Never edit any other path.
  A manifest item whose target is not yours: append it to your ledger as `XFOLLOWUP <target> <item>` and skip.
- BARRED: `git commit`, `git merge`, `git stash`, `git reset`, `git checkout <ref>`/`switch`, `git push`,
  `git clean`, `git rm`, editing outside the worktree, running a whole suite or a whole test directory.
- Allowed git: `git show <ref>:<path>`, `git log`, `git diff`, `git grep`, `git blame`, `git add <your-file>`.
  Refs: fork tip `b6c8bfe8ab`, upstream `612d8e44a2`, merge-base `26350357d7`.
  If `git add` hits `index.lock`, sleep 2 and retry. Never delete the lock.
- Scratch only under `/tmp/e45-<LANE>/`.

Durable copies of round-3 scratch (fork bodies, deltas): `/Volumes/fleet-scratch/workspaces/default/t_e45c8c8d/followup-src/`
(e.g. `e45-R01-cli-kanban/FOLLOWUP-kanban_parser-fork-build_parser.py`). Paths in manifests that say `/tmp/e45-...` live there now.

## Method (per manifest item)
1. Read the fork delta (the manifest gives `diff base->fork` or line ranges in `b6c8bfe8ab:<old god-file>`).
2. Find the method in its NEW home (the sibling). Upstream may have rewritten it. **Upstream structure wins;
   re-thread the fork BEHAVIOUR into it.** Never paste the fork's old body over upstream's.
3. If upstream already carries the behaviour (absorbed / parallel invention), write `NO-PORT-NEEDED: <evidence>`.
4. If the delta's subject was deleted upstream, find what replaced it (`git log -S'<token>' 26350357d7..612d8e44a2`)
   and port onto the replacement, or ledger `DROPPED: <why>, replacement <x>` only when the fork behaviour is
   genuinely moot. A fork test that asserts the behaviour means it is NOT moot.
5. Sibling modules late-import fork helpers that stayed in the facade (`from <facade> import _helper` inside the
   function) the way upstream's own siblings do. No re-export shims.
6. After each file: `~/.hermes/hermes-agent/venv/bin/python -m py_compile <f>`;
   `~/.hermes/hermes-agent/venv/bin/python -m ruff check --select F821,F811,F823 <f>` (no new findings);
   `git add <f>`.

## Test files
Do not edit test files. A red you believe is a STALE test (the merged contract legitimately changed AND
fork/main's copy of the test passes only because of the old contract) goes to your ledger as
`XFOLLOWUP-TEST <file>::<test> <one-line proposed change>`; the orchestrator applies those.

## Verification (required before you finish)
- Every fork test that exercises your targets must pass. Find them: `git grep -l '<symbol>' -- tests` for the
  symbols you touched, plus the tests the manifest names. Run them ONLY through the host gate, file-scoped:
  ```
  bash /Volumes/fleet-scratch/workspaces/default/t_e45c8c8d/e45-pt.sh <test files...>            # wraps ~/.hermes/scripts/test-gate, sandboxed HOME
  ```
  Binary-classify each red against the baseline checkout (same file under `.../t_e45c8c8d/base`, use
  `E45_DIR=/Volumes/fleet-scratch/workspaces/default/t_e45c8c8d/base bash /Volumes/fleet-scratch/workspaces/default/t_e45c8c8d/e45-ptd.sh <files>`):
  passes on base => your regression, fix the CODE; fails on base too => inherited, ledger it, do not fix.
  A merged-only red whose cause is outside your targets: ledger `XFOLLOWUP`.
- Ledger: `docs/sync/review/ledger-2026-10-01/<LANE>.md`, one row per manifest item:
  `| <target>::<symbol> | PORTED / NO-PORT-NEEDED / DROPPED / XFOLLOWUP | evidence | test(s) |`
  then `git add` it.

## Finish
`kanban_complete(summary=..., metadata={"no_pr": true, "no_pr_reason": "re-thread lane of parity merge t_e45c8c8d; orchestrator lands one PR", "ported": N, "no_port_needed": N, "dropped": N, "xfollowup": N, "tests_pass": N, "tests_fail_inherited": N})`.
If survivor capture refuses (`survivor_unavailable`), `kanban_block(reason='LANE DONE: ...')` and the operator closes it.
Near your context ceiling: stop clean, ledger every item you did NOT reach as `TODO`, and say how many remain.
