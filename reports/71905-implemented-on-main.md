# #71905 — implemented on main

Disposition: Apollo approved retirement as `implemented_on_main`; no runtime re-port or push is required.

Verified upstream base: `cedf4a3d78675283fa93e4e6ea2d6212bf414667`.

The original narrowed symptom cannot occur through the tested ingress: `/restart` is handled off-turn and creates no agent turn or transcript row to replay; unrelated active work in the same session remains resumable.

## Recorded evidence

Prior worker run 437 exercised actual `_handle_message('/restart')` → `request_restart` → timeout drain → disposable `SessionStore` reopen → startup recovery, with the session ContextVar unbound. The five cases were:

- Idle slash probe: zero agent calls, zero slash transcript rows, zero unrelated-work schedules.
- Busy slash probe: zero agent calls, zero slash transcript rows, one unrelated-work schedule.
- `tests/gateway/test_restart_drain.py::test_restart_command_while_busy_requests_drain_without_interrupt`
- `tests/gateway/test_restart_resume_pending.py::test_drain_timeout_marks_resume_pending`
- `tests/gateway/test_restart_resume_pending.py::test_auto_resume_runs_agent_exactly_once_through_full_path`

Both probe cases also assert zero schedules on an immediate second recovery call.

Recorded result: `5 passed, 7 warnings in 13.96s`; exit 0. Raw evidence was read back during closeout; these tests were not rerun during the documentation-only closeout.

Evidence archive: `.test-home/restart-premise-evidence.zip` contains `receipt.txt`, `probe_restart.py`, `run_probe.py`, and `probe.log`. The receipt preserves exact invocation and base verification. The dependency-only `-I -S` runner bypassed editable `.pth` files and asserted workspace-local import origins for `gateway.run`, `gateway.run_shutdown`, `gateway.slash_commands`, and `gateway.session`; execution used sanitized environment and disposable state.

Limits: adapters and the agent placeholder were fake; this proves routing, persistence, and scheduling, not model or supervisor execution. No failing behavior was reproduced, and no RED/GREEN mutation claim is made. Apollo superseded the original causal-attribution implementation/mutation acceptance with this evidence-only disposition and owns upstream closure.
