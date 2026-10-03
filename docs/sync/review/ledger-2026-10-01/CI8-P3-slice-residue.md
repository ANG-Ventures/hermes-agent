# CI8 lane P3-slice-residue (card t_b6b70fb3)

Base `ea18906c0a` (sync/upstream-2026-10-01, round-8 fold). Lane branch
`sync/upstream-2026-10-01-ci-P3-slice-residue` on `fork`. Red source: CI run 37094436303.
All three reds were green in round 7 (run 37081194693) and nothing in round 8 touched
`tools/`, `tui_gateway/compute_host.py` or `hermes_state*`; the question was flake vs real.

Repro host: ace-ai (24 cores, load1 ~4), `/srv/ci/scratch/t_b6b70fb3/repo` = clean clone of the lane
branch, uv venv python 3.14.4, serial `pytest -p no:randomly`, sandboxed HOME/HERMES_HOME. Mac re-proof
through `e45-pt-P3-slice-residue.sh` (test-gate, sandboxed HOME): 5 passed, 2 skipped (linux-only cells).

| red | class | evidence | fix |
|---|---|---|---|
| tests/tools/test_process_registry_list_exit.py::test_list_reconciles_real_exit_without_consuming_owned_result (slice 7, job 111121599277) | LOAD-FLAKE -> FIXED-TEST | Base: 15/15 green on ace-ai. CI stdout: `list` took 9e-05 s, reader alive, probe died at `completion_queue.get(timeout=2)`. Instrumented `_finish_reader`: 0.20-0.25 s idle, all of it `transform_process_output` (cold `hermes_cli.lifecycle.invoke_hook` plugin import inside `_redact_process_result`; second call 0.0006 s). An 8-way shard stretched that first-publish import past 2 s. Fork's reader finish path (`_reader_finish_requested` -> break -> `_finish_reader` -> `_move_to_finished`) is correct. | Join the reader (10 s hang guard, below the fixture writer's 15 s deadline), assert it exited, `get_nowait()` the event: the invariant is "the reader publishes the owned completion before it exits". RED: reader ignoring the finish request -> `reader did not finish after list requested it`; reader finishing without publishing -> `_queue.Empty`. 10/10 green after. |
| tests/tui_gateway/test_compute_host.py::test_compute_host_line_json_hello_and_shutdown (slice 2, job 111121599297) | LOAD-FLAKE -> FIXED-TEST | Base: 15/15 green on ace-ai. Cold `python -m tui_gateway.compute_host` emits hello after 0.88-0.92 s idle (5 runs); `-X importtime`: `tui_gateway.server` 0.92 s, of which `hermes_cli.auth_constants` 0.48-0.55 s = `get_version_info()` running 8 git subprocesses at import (upstream c13ea774e6; chain tools.environments.local -> local_env_policy `_build_provider_env_blocklist` -> hermes_cli.auth -> auth_zai_kimi -> auth_constants). 2 s bound at ~2x the idle boot. Not a merge regression (upstream carries the same import; fork/main had `__version__` here). | `hello` gets a 30 s hang guard (`_HELLO_TIMEOUT_S`); the warm frames after it keep 2 s. RED: hello emission removed -> `assert 'hb' == 'hello'` (heartbeat arrives instead). Code-side import cost left alone: out of lane scope, note for the orchestrator below. |
| tests/hermes_state/test_hermes_state_compression_busy_retry.py::test_append_is_never_blocked_by_a_foreign_compression_lock (slice 3, job 111121599328) | LOAD-FLAKE -> FIXED-TEST | `assert 0.6995 < 0.5`. Base: 15/15 green; first append on a fresh SessionDB 0.045 s idle, later ones 0.002 s. `append_message` never consults compression_locks (#75316 contract), so the 0.70 s was cold first-write setup on the shard, not a lease wait. | All four stopwatch bounds in the file -> `lease_waits` witness (monkeypatch `SessionDB._sleep_before_write_retry` to record and refuse). RED: appends fencing on the lock again -> `Failed: append waited on a compression lease: SessionCompressionInProgressError(...) (waits=[5.0])` on both append tests; lost lease raised as the transient subclass -> `assert [5.0] == []` on the fail-fast test. 4 passed. |

Final proof on head `1295236f55`: ace-ai serial loop 10/10 `7 passed` over the three files; mac 5 passed, 2 skipped.

Commits: `8c01647cf8` (busy-retry witness), `1295236f55` (list-exit witness + compute-host hang guard). Tests only; no production code changed.

Note for the orchestrator (not fixed here, outside the three-nodeid scope): importing `tools.process_registry` or
`tui_gateway.server` pays ~0.5 s of git subprocesses via `hermes_cli.auth_constants.CODEX_OAUTH_USER_AGENT`
(`get_version_info()` at import). Upstream-inherited; a lazy UA would cut every CLI/compute-host cold start.
