# CI round-7 lane N4-e2e-ci-upgrade — ledger

Branch `sync/upstream-2026-10-01-ci-N4-e2e-ci-upgrade` off `b9239a49ed`. Red source: run 37081194693
(jobs 111082224676 unit slice, 111082109862 e2e-upgrade/handoff, 111082109874 e2e-upgrade/core,
111082080241 Windows install+update E2E, 111082078781 Windows-only tests).

Baseline (`fm-base` = fork/main 0410ed1b35): `tests/test_ci_overflow_workflow_contract.py` 50/50 green,
so every red below is merge-caused unless marked INHERITED.

## Root causes (shared across files)

1. **N-1 release tag** (handoff ×8, test_upgrade_path ×7, test_update_network_failure ×1, plus the pm/git
   shards outside this manifest). The fork repo carries upstream release tags only up to `v2026.5.7`
   (`git ls-remote --tags fork`); CI's checkout fetched `+refs/tags/*` from `origin` = the fork, so
   `git describe --tags --match 'v20*'` named `v2026.5.7` (log: `install (v2026.5.7 installer, base
   498bfc7bc12a)`), whose `uv.lock` the job's pinned uv 0.12.13 refuses:
   `error: The lockfile at uv.lock needs to be updated, but --locked was provided`. Reproduced locally
   with uv 0.12.13 on `git archive v2026.5.7 pyproject.toml uv.lock` (rc=1, "addition of global
   exclude newer"); `v2026.8.3`, `v2026.9.14`, `v2026.9.24` lockfiles pass. Round 5 (117c1c249d)
   pointed `describe` at HEAD instead of HEAD~1, which is right but moot when the newer tags are not in
   the checkout. FIX: e2e-upgrade job fetches `+refs/tags/v20*` from NousResearch/hermes-agent (retry
   action, `github.repository != 'NousResearch/hermes-agent'` only).
2. **Idle hang detector** (fork `scripts/run_tests_parallel.py` 294e7450b1: SIGKILL after 240 s of
   silence; upstream's runner has no such detector). Upstream's e2e suites are silent for minutes by
   design (N-1 `uv sync`, sandboxed updater dependency sync, 1.8k-module import sweep, `install.ps1`
   on Windows). Log lines: `(no test progress for 240s -- hang; process tree SIGKILL'd)` ×3 in
   e2e-upgrade, ×6 in Windows install+update (all killed at 268.8-269.1 s); the one file the isolated
   retry rescued ran 200.1 s alone. FIX: `CI_TEST_IDLE_TIMEOUT=1200` on the two jobs (wall ceilings
   3000/2400 s unchanged) and the hermetic runner shell wrapper forwards that knob (it was
   `env -i`-stripped before, so the setting was unreachable from a workflow).
3. **Hosted 4-vCPU Windows lane** (fork has no `windows-latest-32-core`): the two desktop-update
   self-tests use upstream's 60 s subprocess deadline and hit it under 8-file parallelism
   (`subprocess.TimeoutExpired` at 63 s; the cwd file passed on its automatic retry = FLAKY). FIX:
   300 s deadline, assertions unchanged.

## Per file

| file | verdict | detail |
|---|---|---|
| tests/ci/test_flake_quarantine_workflow.py | FIXED-TEST | Upstream's tests.yml runs the Slice verdict checker under `"$HERMES_PYTHON"` (setup-pm) instead of `.venv/bin/python`; the step-bytes harness exported no such var → `bash: line 14: : command not found` (same line in CI log). Harness now exports `HERMES_PYTHON=sys.executable`. 13 passed. |
| tests/ci/test_no_new_source_proxy_asserts.py | FIXED-TEST | 3 fork tests the merge rewrote as structural AST/source censuses (`test_turn_result_carries_footer_display_figure`, `test_turn_binding_publishes_the_late_bind_resolver`, `test_all_six_session_parent_mutation_sites_are_maintenance_adjacent`) carry `# noqa: source-proxy <reason>`; stale baseline key `test_hermes_state_search.py::...::test_sqlite_timeout_is_at_least_30s` deleted (that test is behavioural on the fold). green. |
| tests/ci/test_uv_python_install_retried.py | FIXED-CODE | e2e-upgrade (`tests.yml`) and `live-providers.yml` `uv python install 3.14` wrapped in `./.github/actions/retry` attempts=5 delay=30 (fork #1450 contract). Census floor 7→2: the unit/lint/docker/desktop lanes now take their interpreter from setup-pm and run no `uv python install` step. green. |
| tests/test_ci_overflow_workflow_contract.py (7 nodeids) | FIXED-CODE + FIXED-TEST | (a) `e2e` job gained `&& inputs.e2e` upstream (slow lane): harness ctx carries `inputs.e2e=true`, `E2E_IF_PREDICATE` pinned; (b) `tests-complete` gate: `E2E_REQUESTED=${{ inputs.e2e }}` + `gate(e2e_requested=)` so an unrequested e2e skip is not a failure while unset/true still gates (test covers both); (c) placement bundle ships `scripts/ci/list_os_marked_tests.py` (new static import of run_tests_parallel, `imported by the plan read but not shipped`); (d) `HERMES_TEST_FILE_TIMEOUT: 600` quoted (env scan `TypeError: argument of type 'int' is not iterable`); (e) upstream's new `install-e2e-red.yml` / `stable-release-publication.yml` `workflow_run` get `branches-ignore: [ci-overflow-ledger]` (spec §5.3), `tests/ci/test_stable_release_graph.py` pin updated. 50 passed + blacksmith_plan 25 + stable_release_graph 10. |
| tests/e2e/core/upgrade/handoff/test_handoff_from_n1.py (7) | FIXED-CI | cause 1. Not runnable on macOS (bwrap). |
| tests/e2e/core/upgrade/handoff/test_update_network_failure.py[n1] | FIXED-CI | cause 1. |
| tests/e2e/core/upgrade/test_upgrade_path.py (7) | FIXED-CI | cause 1 (`N-1 venv install from its uv.lock failed`). |
| tests/e2e/core/upgrade/handoff/test_handoff_from_head.py | FIXED-CI | cause 2: `HUNG: no test progress for 240s (7 collected)` on both the pool run and the isolated retry; the file's first scenario installs HEAD via install.sh (silent). |
| tests/e2e/core/upgrade/test_update_userstate_realistic.py | FIXED-CI | cause 2: `killed at 3000s in the pool, 200.1s alone` (reported SLOW; the kill was the idle detector at 255 s, not the wall). |
| tests/e2e/core/upgrade/test_fresh_process_entrypoints.py | FIXED-CODE + FIXED-CI | pool run: cause 2 (247 s idle kill). Isolated retry: `test_every_shipped_module_imports_from_a_clean_first_party_graph` red on `repro_rollback_store` (fork #801 root script runs `git show cea2ef75…` and execs it at import → `CalledProcessError` in a sandbox with no repo). Body moved under `main()` with a `__main__` guard; `gateway/boot_preload.py` already excluded it for the same reason. Verified `python3 -I -c "import repro_rollback_store"` is side-effect free. |
| tests/e2e/core/windows_update/test_{fresh_install_update,gateway_across_update,git_and_holders,interpreter,paths_and_acls,update_spares_other_installs}.py | FIXED-CI | cause 2: all six `TIMED OUT` at 268.8-269.1 s, each with `last test reached` = its first journey cell, i.e. killed inside `install.ps1` (no transcript artifact was uploaded: `No files were found with the provided path: D:\a\_temp/win-update-e2e`, consistent with teardown never running). One shared cause, not six bugs. Windows-only; cannot reproduce here. |
| tests/scripts/desktop_update/test_desktop_update_windows_cwd.py (2) | FIXED-TEST | cause 3 (FLAKY in CI: red at 60 s, green on retry at 165 s total). |
| tests/scripts/desktop_update/test_desktop_update_windows_timestamp.py | FIXED-TEST | cause 3 (`TimeoutExpired` at 63.6 s). |

## Pre-existing on the lane base (not in manifest, not touched)

`tests/gateway/test_boot_preload.py::test_preload_on_real_tree_has_no_process_side_effects` (env_changed
`['PATH']`) and `tests/scripts/test_run_tests_parallel.py::test_known_flag_missing_value_errors_with_usage_instead_of_sweeping`
fail identically on untouched `b9239a49ed` (verified by `git diff > patch; git checkout -- .; run; git apply`).
Neither file exists on fork/main under that path. Reported for whoever owns them.

## Commits

- 6386737cd0 fix(parity 2026-10-01 ci): N4 ci-contract reds (tests.yml wiring, source-proxy ratchet)
- a4c1ccb88f fix(parity 2026-10-01 ci): N4 e2e-upgrade + Windows install E2E reds
- (this commit) test(parity 2026-10-01 ci): N4 Windows desktop-update self-test deadlines + ledger
