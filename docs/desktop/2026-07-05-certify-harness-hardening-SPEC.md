# SPEC: `iso-certify.py` — incident-regime certify harness hardening

- **Status:** v0.1 (authored 2026-07-05)
- **Owner:** Apollo
- **Origin:** During the turn-isolation live certify, the harness wedged the live dashboard
  **twice** — not by exercising a product bug, but by its OWN defects: (1) on kill/exit it left
  ~20 orphaned server-side turn processes (compute_host + slash_workers) hammering the box, which
  exhausted whole-machine CPU and starved the serving loop; (2) killing the client did not cancel
  the server-side turns it had submitted. A test harness that wedges the system under test is
  worse than no harness — it manufactures false incidents and destroys trust in the measurement.

## 1. Summary & Goal

Harden `/tmp/iso-certify.py` (the ws-driven load+probe harness that certifies AC-4 loop liveness)
so that **it can never leave orphaned load or wedge the target**, and so its numbers are
trustworthy. It is a test tool, not production code — but it drives the LIVE dashboard, so its
safety bar is real. Promote it from `/tmp` to a committed, reviewed location so it's reusable for
the session.list PRD's certify too.

## 2. Non-Goals

- NOT a general load-testing framework — single-purpose: seed heavy sessions, drive concurrent
  turns/reads, probe the serving plane, clean up, report a PASS/FAIL against AC-4 thresholds.
- NOT changing what AC-4 measures (loop liveness: REST p99 < 1s, ws list latency, 0 stalls, 0
  drops) — only making the measurement SAFE and self-cleaning.

## 3. Invariants (the safety contract — these are why it exists)

- **INV-1 (no orphaned load — the load MUST die when the harness dies).** Every server-side
  turn/session the harness starts is cancelled or reaped on ANY exit path: normal completion,
  `--duration` timeout, `KeyboardInterrupt`, exception, AND external `kill`. *Proof:* after a
  `kill -9` of the harness mid-run, `ps` shows ZERO harness-attributable compute_host/slash_worker
  processes within a bounded settle window; a test asserts this.
- **INV-2 (disposables are always deleted).** Every seeded session carries a unique marker title
  (`apollo-certify-DISPOSABLE-*`); cleanup deletes ALL of them on exit, and a final sweep by
  marker catches any the id-race missed. *Proof:* post-run, a marker query returns 0.
- **INV-3 (the harness never itself wedges the target).** Concurrency is bounded and moderate
  (default ≤ 4 turn-drivers, not 6+ unbounded); the harness backs off if the target's health
  probe starts failing (a circuit breaker: if N consecutive probes fail, STOP the load and report,
  don't pile on). *Proof:* a run against a deliberately-slow target ends by backing off, not by
  driving it to 100% CPU.
- **INV-4 (honest, separated metrics).** Report ws-list latency and REST loop-liveness latency
  SEPARATELY (they measure different things — a slow list with a live loop is a PASS for isolation,
  a FAIL for list-speed). The AC-4 gate keys on loop liveness (REST p99 + zero stalls), not list
  latency. *Proof:* the report prints both; the gate expression references the loop-liveness metric.

## 4. Resolved Decisions

- **D-1 (interrupt-on-exit, belt AND suspenders).** Each turn-driver thread, in a `finally`,
  fires `session.interrupt` for its sid; the main path, before deleting disposables, interrupts
  all seeded sids. Already partially implemented 2026-07-05 — formalize + test it.
- **D-2 (a SIGKILL can't run `finally` — so also make the SERVER reap on client disconnect).**
  Client-side interrupt covers graceful exit, but `kill -9` skips `finally`. The durable backstop
  is that the compute host / gateway should cancel a turn when its submitting ws client
  disconnects — this is a PRODUCT gap the harness incident exposed. Filed as a cross-reference to
  the turn-isolation PRD (add an AC: "turn is cancelled when its submitting client disconnects").
  The harness can't fully self-protect against its own `kill -9` without this; document the
  residual honestly.
- **D-3 (circuit breaker).** A dedicated health-probe thread; if it sees ≥ 5 consecutive failures
  or p99 > 3s sustained, it sets `stop` — the harness aborts its own load rather than wedging the
  target. This is the single most important fix: it makes the harness fail SAFE.
- **D-4 (bounded concurrency + generous per-turn timeout).** Default 4 drivers (not 6);
  per-turn recv timeout 120s (real turns with 429 retries run long — the original 90s caused the
  WebSocketTimeoutException cascade). Concurrency is a `--sessions` arg but capped at a sane max.
- **D-5 (promote out of /tmp).** Move to `scripts/certify/dashboard_loop_certify.py` in the repo,
  committed + a small unit test for the cleanup/interrupt/circuit-breaker logic (mockable).

## 5. Implementation Phases

- **Phase 1 — Safety (INV-1, INV-3): interrupt-on-exit + circuit breaker.**
  - *Unit:* mock ws; assert every seeded sid gets `session.interrupt` on normal exit, on
    exception, and on `stop`-set; assert the circuit breaker sets `stop` after 5 mocked probe
    failures.
  - *E2E:* run against the live dashboard with `--duration 60 --sessions 3`; then `kill -9` it
    mid-run; assert (out-of-band script) harness-attributable turn procs reap within 30s.
  - *Negative:* point it at a deliberately-throttled target; assert it backs off (stop set) rather
    than driving CPU to 100%.
  - *Verify with:* `pytest scripts/certify/test_dashboard_loop_certify.py` + the live kill-9 test.
- **Phase 2 — Cleanup completeness (INV-2) + honest metrics (INV-4).**
  - *Unit:* marker-sweep deletes all `apollo-certify-DISPOSABLE-*` even when the per-id delete
    races; report prints ws-list and REST-loop metrics separately.
  - *E2E:* full run leaves 0 disposables (marker query) and prints a two-metric report.
  - *Verify with:* post-run marker query == 0.

## 6. Risks & Mitigations

- **R1 — `kill -9` still orphans (INV-1 can't be fully met client-side).** Mitigated by D-2 (the
  product-side disconnect-cancel), which is the real fix; until that ships, the harness documents
  "do not `kill -9`; use the circuit breaker / `--duration`" and its own SIGTERM handler interrupts.
- **R2 — circuit breaker false-trips on a healthy-but-slow target and under-measures.** Tune the
  threshold (5 consecutive fails / sustained p99>3s) above normal jitter; the breaker's job is to
  catch a WEDGE, not slowness.

## 7. Acceptance Criteria

- [ ] WHEN the harness is `kill -9`'d mid-run, harness-attributable turn processes reap within 30s.
      Evidence: out-of-band `ps` script post-kill returns 0 (assuming D-2 product fix; else SIGTERM path).
- [ ] WHEN the target's health probe fails 5× consecutively, the harness STOPS its load and reports.
      Evidence: `test_circuit_breaker` + live throttled-target run.
- [ ] Post-run, zero `apollo-certify-DISPOSABLE-*` sessions remain. Evidence: marker query == 0.
- [ ] The report prints ws-list latency and REST loop-liveness SEPARATELY; the gate keys on loop
      liveness. Evidence: report output + gate expression inspection.
