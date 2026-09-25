# Shared-checkout admission hold — API and runbook

Code: `gateway/checkout_admission.py`. Tests: `tests/gateway/test_checkout_admission.py`.
Origin: t_e8017c37, a prerequisite for t_9959a74f AC4. It replaces the withdrawn
hermes-home#382 prototype.

## Problem

Several long-lived processes can import code from ONE git checkout. On ACE-AI
these are `gateway:default`, `gateway:clanker` and `serve:clanker` (:9121).
Fetching or merging that checkout while any of them is mid-turn is unsafe. The
withdrawn prototype had four defects:

- Two idle polls plus markers or `ss` do not stop a turn that arrives just
  after the last poll.
- A bounded wait followed by SIGUSR1 interrupts real work.
- `serve` had no ingress hold.
- A PID or status-JSON file does not prove the process is serving.

## Model

| Piece | Where | Semantics |
|---|---|---|
| Hold | `<dir>/hold.json` | `epoch`, `owner`, `mode` (`drain`/`freeze`), `expected` consumers. **No TTL.** Only an explicit `release` by the same owner opens it. It survives failed imports, reloads, restarts and probes. A corrupt hold fails closed: admissions are refused and the verdict is `UNKNOWN`. |
| Gate | one per consumer process | Every ingress calls `admit()`/`check()`, and **each call re-reads the hold**. There is no cached flag, so a freshly restarted process is closed from its first admission. |
| Ack | `<dir>/consumers/<name>.json` | Published every 2 s. The snapshot is taken under the same lock admissions use, and it contains: the hold epoch the process observed, its admission tickets, and its own authoritative work count. |
| Verdict | `evaluate()` | `QUIESCENT` means a freeze hold, every expected consumer fresh (<20 s), a live pid on this host, the current epoch acknowledged, and zero work. Zero work under a drain hold is `DRAINED`. Nonzero work is `BUSY`. Anything missing, stale, corrupt, unacked, from a foreign host, from a dead pid, with a failing work source, or from an unexpected live consumer is `UNKNOWN`. |

The late-arrival guarantee comes from lock ordering, not from poll timing.
Admission (read hold + register) and ack snapshot (read hold + count) are
serialized. So an ack that observed epoch E counts every turn admitted before E
was written, and nothing is admitted after it. Tests covering this:

- `test_admission_read_and_register_is_atomic_against_the_ack` forces the interleaving.
- `test_late_arrival_after_last_idle_poll_{held,control}_arm` covers the two arms.

Modes:

- `drain` refuses new external turns. It admits internal continuations of
  already-accepted work and counts them: restart replays, background-process
  completions, queued follow-ups, auto-continue, `/goal` and loop wakeups.
  Nothing is lost.
- `freeze` refuses everything. `QUIESCENT` under freeze is the **only** mutation
  authority.

Nothing in this module signals, interrupts or kills. `wait` returns `DEFER` on
its deadline. It does not touch t_e253d9d5's shutdown/interrupt behaviour.

## Ingress covered

| Consumer | Ingress | Gate | Work counted by |
|---|---|---|---|
| gateway | every messaging platform → `GatewayRunner._handle_message` | ticket before the session claim, released in the turn's `finally`; internal events admitted under drain | ticket + `_active_work_count()` (agents, cron, API, compaction) |
| gateway | API server `/v1/chat/completions`, `/v1/responses`, `/v1/runs`, session chat (+stream), `/api/jobs/{id}/run`, `/api/cron/fire` | `_draining_response()` → 503 `checkout_held`, in the same non-awaiting block as the pending reservation; the ack snapshot runs on the same loop | `active_agent_work_count()` |
| gateway | in-process cron ticker | `_CronDispatchGate.admit()`; the ticket spans the whole `tick()` dispatch window | ticket + `get_running_job_ids()` |
| serve | every JSON-RPC over stdio **and every new or existing WebSocket** → `handle_request` | `prompt.submit`, `prompt.background` and `prompt.btw` take a ticket before the handler persists anything → error 5075. Background/btw hand the ticket to their worker thread (`_start_counted_thread`) | ticket + running sessions + compaction + cron |
| serve | direct `_run_prompt_submit` callers (auto-resume, queued prompts, notifications, goal/loop) | `check(internal=True)`: admitted under drain; under freeze, deferred before the turn thread starts, with `running` cleared and an error event emitted | `session["running"]` (set by every caller before the call) |

Not covered, by design:

- Standalone `tui_gateway.entry` (TUI child processes) and the Desktop-only
  in-serve cron ticker (`HERMES_DESKTOP=1`) do not install a gate.
- `evaluate()` flags any live consumer record not in `expected`, but a process
  that never installed a gate is invisible to it. Do not run such processes
  from the shared checkout during a cutover.

## Configuration (per profile `config.yaml`)

```yaml
checkout_admission:
  enabled: true
  dir: ""            # empty = <git-common-dir>/checkout-admission (shared by all consumers of the checkout)
  consumers:
    gateway: gateway:clanker
    serve: serve:clanker
```

If a process has this section enabled but misconfigured (no consumer name, or
the directory cannot be resolved), it logs an error and never acknowledges.
The verdict is then `UNKNOWN` and the cutover DEFERS; the fence is never
silently open. The config is read at process start, so a restart is required
to enable it.

## Operator runbook (the cutover itself belongs to t_9959a74f)

Every command is `python -m gateway.checkout_admission [--dir D] <cmd>` and
prints JSON. Exit codes:

| Exit | Meaning |
|---|---|
| 0 | `QUIESCENT` / ok |
| 1 | `DRAINED` |
| 2 | `NOT_HELD` |
| 3 | `BUSY` / `DEFER` |
| 4 | `UNKNOWN` |
| 5 | pin rejected |
| 6 | probe failed |

1. **Pin.** Run `pin-check <sha> --slug ANG-Ventures/<repo> --remote-ref <remote>/main`.
   The SHA must be full 40-hex, present locally, an ancestor of the remote ref,
   and every check run must be `completed`: success, skipped or neutral, with
   at least one success. Anything else stops the procedure.
2. **Drain.** Run `hold --owner <token> --expect gateway:default --expect gateway:clanker --expect serve:clanker --mode drain`.
3. Run `wait --timeout <s>`. Continue only on exit 1 (`DRAINED`). Exit 3 means
   DEFER: keep holding or release; never signal. Exit 4 means find the UNKNOWN
   consumer first.
4. **Freeze.** Run `hold --owner <token> --expect … --mode freeze`. This creates a
   new epoch, and every consumer must re-acknowledge it.
5. Run `wait --timeout <s>`. Continue **only** on exit 0 (`QUIESCENT`).
6. Fetch/merge the pinned SHA. Restart consumers one at a time. The hold stays
   engaged, so each restarted process refuses work from its first admission.
   There is no gap.
7. **Probe each consumer** with `probe <name>`: a fresh record published from
   the consumer's serving loop, and a live pid. For serve, add
   `--url http://127.0.0.1:9121/api/health`. `status` must be `QUIESCENT` with
   the **new** instances; the `instance` field changes on restart.
8. **Release** with `release --owner <token>` only after every probe passes. If
   an import, reload or probe fails, stay held. Nothing times out.

`probe` and `status` only read files and GET a health URL. They never write the
admission directory (`test_probe_is_non_mutating_and_checks_freshness`).
