# PRD — Gateway session-state process-global `os.environ` leak (the v3-latch bug class, generalized)

**Status:** DRAFT v0.1 — pre-review
**Author:** Apollo
**Date:** 2026-06-22
**Related:** `PRD-send-message-origin-leak-v3-cron-session-latch.md` (v3 fixed ONE instance of this class — `HERMES_CRON_SESSION`). This PRD addresses the **remaining instances** + the **root-cause class** discovered during v3 closeout.

---

## 0. Ground-Truth (measured against the live tree, 2026-06-22 — read BEFORE the design)

The discovery that motivated this PRD: from a live interactive `#fix-issues` turn, `env | grep` in a `terminal` subprocess showed `HERMES_CRON_SESSION=1` **and** `HERMES_SESSION_ID/CHAT_ID/KEY` belonging to a *different concurrently-active session* (`ee660b`, running an every-2-min `no_agent` cron sweeper) — not my turn's session. The gateway is a **single process** that runs concurrent sessions (asyncio tasks) + an in-process cron tick; any code that writes session state to **process-global `os.environ`** clobbers it for every other concurrent session.

**The session-context module was ALREADY migrated to contextvars for exactly this reason** (`gateway/session_context.py` docstring literally describes the bug). `set_session_vars` (the main per-turn binder) is **contextvar-only**. But residual `os.environ` writes remain.

### A. Live process-global `os.environ["HERMES_SESSION_*"]` WRITERS that run in the gateway process

| Site | Var | When | Notes |
|---|---|---|---|
| `gateway/run.py:15287` | `HERMES_SESSION_KEY` | every turn | comment: "Keep os.environ as fallback for CLI/cron" |
| `gateway/session_context.py:109` (`set_current_session_id`) | `HERMES_SESSION_ID` | unconditional, on every call | called by ↓ |
| `agent/agent_init.py:1089` | `HERMES_SESSION_ID` (via `set_current_session_id`) | agent init | in-gateway during agent construction |
| `agent/conversation_compression.py:986` | `HERMES_SESSION_ID` (via `set_current_session_id`) | on compaction split | in-gateway |

`set_session_vars` itself is clean (contextvar-only). `acp_adapter/server.py:1503,1525` and the `tui_gateway`/`cli`/`hermes_cli` writers run in **separate process models** (ACP server, TUI worker, CLI) — single-session-per-process, so os.environ is correct there and OUT OF SCOPE (see Non-Goals).

### B. The READERS — and why the live blast radius is SMALL (but not zero)

The consequential readers were **already migrated to contextvar-first**, which is why v3 + the live e2e showed *no actual misroute*:

| Reader | Var | Resolution | Risk |
|---|---|---|---|
| `tools/approval.py` `get_current_session_key` | `SESSION_KEY` | approval-contextvar → session-contextvar → os.environ | contextvar-first ✓ |
| `tools/approval.py:159` `_get_session_platform` | `SESSION_PLATFORM` | contextvar (`get_session_env`) → os.environ | contextvar-first ✓ |
| `tools/terminal_tool.py:208` | `SESSION_KEY` | `get_session_env` → os.environ | contextvar-first ✓ |
| `tools/send_message_tool.py` (v1/v2/v3) | platform/chat/cron | `get_session_env`/send-origin | contextvar-first ✓ |
| `tools/kanban_tools.py:124` `_stamp_worker_session_metadata` | `SESSION_ID` | **raw `os.environ.get`** | gated on `HERMES_KANBAN_TASK==task_id` → only true in **dispatcher-spawned worker subprocesses** (separate process, os.environ correct there). In the gateway this early-returns. **Low risk in practice.** |
| `tools/kanban_tools.py:744` (create-task) | `SESSION_ID` | `args.get("session_id") or` **raw `os.environ.get`** | stamps `worker_session_id` on a created task; on the **gateway** path (not a worker subprocess) this could stamp a *clobbered* session id. **The one real wrong-data risk.** |

**Honest severity:** because the dangerous readers (approval gating, send routing) are contextvar-first, the v3-class bleed does **not** currently cause a security misroute or a wrong approval decision. The measured real impacts are: (1) **diagnostic confusion** — the `terminal env|grep` probe lies (already documented in the misrouting skill); (2) **`kanban_tools.py:744` can stamp a wrong `worker_session_id`** on a task created from the gateway while a concurrent cron/session clobbered `HERMES_SESSION_ID` (data-attribution bug, not security). This honesty matters for scoping: this is a **correctness + hygiene + future-proofing** fix, not an active-incident security fix.

---

## 1. Summary & Goal

**Goal:** Eliminate process-global `os.environ` writes of per-session state in the **gateway process**, so concurrent sessions (and the in-process cron tick) can never clobber each other's session identity — closing the bug *class* that v3 fixed one instance of. Make the raw `os.environ` honest (so the diagnostic stops lying), and fix the one direct-reader (`kanban_tools`) that can act on a clobbered value.

**Why now:** v3 fixed `HERMES_CRON_SESSION`; the closeout re-verification surfaced that the *same class* persists for `HERMES_SESSION_ID`/`HERMES_SESSION_KEY`. Fix the class while the context is warm and the pattern (v3) is fresh.

This PRD is structured as **north-star (full class migration)** + **v0.1 cut (the two highest-value, lowest-risk fixes the user approved: #2 gateway-aware session-id/key writes, #3 kanban direct-reader)**.

---

## 2. Non-Goals

- **NOT** touching the **CLI / TUI-worker / ACP-server / oneshot** os.environ writes (`hermes_cli/*`, `tui_gateway/*`, `cli.py`, `acp_adapter/server.py`, `hermes_cli/oneshot.py`). Those are **single-session-per-process** entrypoints where os.environ is the correct, intended mechanism — migrating them is pure risk with no benefit. (Exception: if the audit finds one of these is *also* imported/run inside the gateway process, it gets pulled in — Phase 0 confirms none are.)
- **NOT** changing boot-time gateway config writes (`HERMES_QUIET`, `HERMES_EXEC_ASK`, `HERMES_MAX_ITERATIONS`, `HERMES_TIMEZONE`, media/display settings in `gateway/run.py:1264-1469`). Those are set **once at gateway startup** and are process-wide *by design* — not per-session, no clobber.
- **NOT** removing the `os.environ` **fallback reads** in `get_session_env`/`get_current_session_key`. The contextvar→os.environ→default chain stays (it's what makes CLI/cron/tests work); we only stop the gateway from *writing* the global.
- **NOT** a new env var or config surface.
- **NOT** re-opening v3 (its `HERMES_CRON_SESSION` fix stays; this is additive).

---

## 3. Constitution / Invariants

- **I1 (the core fix): no gateway code path writes per-session state to process-global `os.environ`.** The set of vars: `HERMES_SESSION_ID`, `HERMES_SESSION_KEY` (and, already done in v3, `HERMES_CRON_SESSION`).
  - *Why it matters:* process-global writes in a multi-session process are the entire bug class.
  - *Closeout proof:* `grep` shows the gateway-reachable writers (`gateway/run.py:15287`, `set_current_session_id`'s os.environ line, the two callers) no longer write os.environ when running in the gateway; a test that simulates two concurrent sessions in one process and asserts neither sees the other's `HERMES_SESSION_ID` in `get_session_env`.

- **I2 (CLI/cron/test back-compat preserved): single-process entrypoints (CLI, cron standalone, ACP, TUI worker) still get session state via os.environ where they rely on it.** The contextvar carries the value in-gateway; the os.environ fallback-read still resolves for non-gateway processes.
  - *Why it matters:* `set_current_session_id` exists *because* the CLI rotates sessions in-process and tools read `get_session_env("HERMES_SESSION_ID")` with an os.environ fallback. Breaking that regresses `/new`/`/resume`/`/branch` + compression-split session tracking on the CLI.
  - *Closeout proof:* the CLI session-rotation path still updates the resolvable session id (via contextvar in-process OR os.environ when not in a gateway); existing CLI/session tests stay green.

- **I3 (kanban worker attribution unchanged): a dispatcher-spawned kanban worker (separate process, `HERMES_KANBAN_TASK` set) still stamps its OWN `worker_session_id` correctly.**
  - *Why it matters:* the kanban fix (#3) must not break the legitimate worker-subprocess path (where os.environ IS correct).
  - *Closeout proof:* a test with `HERMES_KANBAN_TASK` set + a session id present asserts the worker stamps its own id; a gateway-path test (no `HERMES_KANBAN_TASK`, concurrent clobber) asserts it reads the *contextvar* session id, not the clobbered global.

- **I4 (no behavior change to v3 / send routing / approval gating): the contextvar-first readers keep resolving identically.** This PRD removes *writes*, not the read chain.
  - *Closeout proof:* v3 tests + send_message_origin + cron_approval suites stay green; a live bare `send_message` still routes to the current channel.

---

## 4. Resolved Decisions

- **D-1 — `set_current_session_id` becomes gateway-aware (chosen for #2).** It currently writes `os.environ["HERMES_SESSION_ID"]` unconditionally + the contextvar. Change: write the **contextvar always**, and write **os.environ only when NOT in a concurrent-gateway process**. The gateway-detection signal: reuse the existing one the codebase already trusts for this exact distinction (the gateway sets `_HERMES_GATEWAY=1` / `HERMES_GATEWAY_SESSION`; the session-context module + v3 already branch on "is this a live gateway turn"). Pick the signal Phase-0 confirms is reliably set in the gateway and unset in CLI/cron-standalone.
  - *Rejected — remove the os.environ write entirely:* would regress CLI session rotation (I2). The CLI genuinely needs the global because it has no per-task contextvar isolation across `/new`.
  - *Rejected — leave it, only fix readers:* doesn't make the raw os.environ honest, leaves the diagnostic lying, and leaves `kanban:744` exposed on the gateway path. Half-fix.
- **D-2 — `gateway/run.py:15287` (`HERMES_SESSION_KEY` per-turn write) gets the same gateway-aware treatment OR is dropped.** Phase-0 determines: is anything that reads `HERMES_SESSION_KEY` *via raw os.environ* reachable only outside the gateway? If the only gateway-reachable readers are contextvar-first (confirmed: `get_current_session_key`, `terminal_tool:208` both are), the gateway write is **pure pollution** and can be dropped in-gateway (contextvar `set_session_vars` already set it). Decide by grep, not assumption.
- **D-3 — kanban `:744` (#3): read the contextvar, not raw os.environ.** Change `args.get("session_id") or os.environ.get("HERMES_SESSION_ID")` → `args.get("session_id") or get_session_env("HERMES_SESSION_ID")`. `get_session_env` is contextvar-first with os.environ fallback, so the worker-subprocess path (I3) is preserved (no contextvar there → falls to its correct per-process os.environ) AND the gateway path reads the right session's contextvar. `:124` (`_stamp_worker_session_metadata`) is gated on `HERMES_KANBAN_TASK` so it's worker-subprocess-only; migrate it too for consistency (same `get_session_env` swap) but it's lower-risk.
- **D-4 — north-star (#4) is its own roadmap, not v0.1.** The full "audit every process-global `HERMES_*` writer and migrate the whole class" is captured below as the north-star with a phase table, but v0.1 ships only the gateway session-id/key + kanban fixes (the measured-real-impact slice). Broader migration ships per-trigger.

---

## 5. Architecture / Design

### The pattern (mirrors v3 exactly)
v3's fix: a process-global flag → task-isolated contextvar, set/cleared per scope, readers use a context-aware helper. This PRD applies the same shape to the residual session-id/key writes:
- **Writes:** gateway-aware — contextvar always; os.environ only in single-session-per-process contexts.
- **Reads:** already contextvar-first (no change), plus the one kanban raw-reader migrated to `get_session_env`.

### Edits (v0.1 cut)
1. **`gateway/session_context.py`** — `set_current_session_id`: gate the `os.environ["HERMES_SESSION_ID"]` write behind a "not in concurrent gateway" check; always set the contextvar. Add a small helper `_is_concurrent_gateway()` (or reuse the existing signal) — Phase-0 picks the exact predicate.
2. **`gateway/run.py:15287`** — drop or gate the per-turn `os.environ["HERMES_SESSION_KEY"]` write (D-2; the contextvar is already set by `_set_session_env`).
3. **`tools/kanban_tools.py:744` + `:124`** — `os.environ.get("HERMES_SESSION_ID")` → `get_session_env("HERMES_SESSION_ID")` (contextvar-first, os.environ fallback preserves the worker-subprocess path).
4. **Tests** — concurrent-session isolation test; kanban gateway-vs-worker test; CLI back-compat assertion.

### North-star (#4) — full class migration roadmap
| Version | What ships | Trigger | Maps to |
|---|---|---|---|
| **v0.1** (this build) | gateway `HERMES_SESSION_ID`/`KEY` writes made gateway-aware + kanban direct-reader migrated | now (approved) | §6 Phases 1-3 |
| v0.2 | audit + migrate any remaining gateway-reachable per-session os.environ writer found by an enforcement grep test | if Phase-0/closeout grep finds a writer beyond the 4 mapped | §6 Phase 4 (audit) |
| v0.3 | a CI lint/test that FAILS if a new `os.environ["HERMES_SESSION_*"] =` write is added to a gateway-reachable module | if a regression reintroduces the class | future |

---

## 6. Implementation Phases

- **Phase 0 — ground-truth the gateway-detection predicate + reader reachability (REQUIRED, ~5 probes, no code).** Confirm: (a) which signal (`_HERMES_GATEWAY`, `HERMES_GATEWAY_SESSION`, or the session-context "in a gateway turn" check) is reliably set in the running gateway and unset in CLI + cron-standalone; (b) every raw-os.environ reader of `HERMES_SESSION_KEY`/`ID` reachable from the gateway is contextvar-first (already 90% confirmed) so the write can be safely dropped/gated; (c) `set_current_session_id`'s callers in-gateway.
  - *Verify with:* live `execute_code` in the gateway prints the candidate signals; `grep` of every reader; record results in a "Phase-0 ground-truth" block. If a predicate is unreliable or a raw reader is found, the design adjusts before code.

- **Phase 1 — `set_current_session_id` gateway-aware (#2 core).** Contextvar always; os.environ gated.
  - *Unit/script check:* in a simulated gateway context, `set_current_session_id("S1")` then assert `get_session_env("HERMES_SESSION_ID")=="S1"` AND `os.environ.get("HERMES_SESSION_ID")` is **unchanged** (not clobbered). In a non-gateway (CLI) context, assert os.environ IS updated (back-compat).
  - *E2E/integration (REQUIRED — concurrency):* two concurrent asyncio tasks each bind their own session via the real `_set_session_env` + `set_current_session_id`; assert each task's `get_session_env("HERMES_SESSION_ID")` returns ITS OWN id (not the other's), and a `terminal`-style `os.environ` snapshot is not cross-contaminated.
  - *Negative/adversarial:* a cron tick (in-gateway) running concurrently with an interactive turn — the interactive turn's `get_session_env("HERMES_SESSION_ID")` must not return the cron session's id.
  - *Verify with:* `pytest tests/gateway/test_session_id_isolation.py -o 'addopts=' -q` → pass.

- **Phase 2 — drop/gate `gateway/run.py:15287` SESSION_KEY write (#2).**
  - *Unit/script check:* a gateway turn binds session_key via contextvar; assert `get_current_session_key()` resolves correctly WITHOUT the os.environ write; assert os.environ is not polluted with a per-turn key.
  - *Negative/adversarial:* CLI/cron path (no contextvar) still resolves session_key via its own os.environ.
  - *Verify with:* `pytest tests/tools/test_approval*.py -o 'addopts=' -q` (the approval session-key readers) stay green + new isolation assertion.

- **Phase 3 — kanban direct-reader migration (#3).**
  - *Unit/script check:* `_create_task` with no `args["session_id"]` + a bound contextvar `HERMES_SESSION_ID` stamps the contextvar value; with neither, stamps None.
  - *E2E/integration (REQUIRED — the wrong-data path):* simulate gateway concurrency — task created in session A while session B's id is in os.environ (clobbered); assert the created task stamps A's id (from contextvar), not B's.
  - *Negative/adversarial (I3):* a worker subprocess (`HERMES_KANBAN_TASK` set, NO contextvar, session id only in os.environ) still stamps its own id via the os.environ fallback.
  - *Verify with:* `pytest tests/tools/test_kanban*.py -o 'addopts=' -q` → pass.

- **Phase 4 — north-star audit (v0.2, deferred unless Phase-0 finds more).** An enforcement test/grep that asserts no gateway-reachable module writes `os.environ["HERMES_SESSION_*"]` outside the sanctioned single-process entrypoints.
  - *Verify with:* a `test_no_gateway_session_env_writes` grep-test (allowlist the CLI/TUI/ACP/oneshot files); fails if a new violator appears.

---

## 7. Security, Privacy, Ops, Observability

- **Security posture (honest):** this is **not** an active security-incident fix — the dangerous readers (approval gating, send routing) are already contextvar-first, so the bleed does not currently cause a misroute or wrong approval. It IS a correctness/hygiene fix that (a) closes a latent footgun (any *future* raw-os.environ reader would inherit the clobber), (b) fixes a real data-attribution bug (`kanban:744` wrong `worker_session_id`), (c) makes the diagnostic honest.
- **Ops/rollback:** code change to the live editable install; deploy = fork PR merge + **gateway restart** (privileged §7 — PAUSE for Ace's go). Rollback = revert + restart.
- **Observability:** after fix, `terminal env|grep HERMES_SESSION_*` reflects only the true (or empty) per-process state; the misrouting skill's "cross-check execute_code" caveat can note the diagnostic is reliable again for non-clobbered vars.

---

## 8. Risks & Mitigations

- **R1 — gateway-detection predicate is wrong → CLI session rotation regresses (I2) OR gateway still writes os.environ.** *Mitigation:* Phase-0 confirms the predicate empirically (set in gateway, unset in CLI/cron) before coding; Phase-1 has explicit both-directions tests (gateway: os.environ untouched; CLI: os.environ updated).
- **R2 — a raw-os.environ reader I didn't find acts on the dropped SESSION_KEY write.** *Mitigation:* Phase-0 greps every reader; the only gateway-reachable one (`terminal_tool:208`) is contextvar-first. The grep is the gate, not my memory.
- **R3 — kanban worker-subprocess path breaks (I3).** *Mitigation:* `get_session_env` keeps the os.environ fallback, so the worker (no contextvar) resolves identically; Phase-3 negative test proves it.
- **R4 — scope creep into the CLI/TUI/ACP writers.** *Mitigation:* hard Non-Goal; those are single-session-per-process by design. Phase-4 audit allowlists them explicitly.
- **R5 — over-claiming severity.** *Mitigation:* §7 + Ground-Truth state plainly this is hygiene/correctness, not an active security fix. Don't sell it as a vuln patch.

---

## 9. Open Questions

- **OQ1 (Phase-0 resolves):** exact gateway-detection predicate — `_HERMES_GATEWAY`, `HERMES_GATEWAY_SESSION`, or a `session_context` helper? Need one reliably TRUE in-gateway and FALSE in CLI + cron-standalone.
- **OQ2 (Phase-0 resolves):** can `gateway/run.py:15287` SESSION_KEY write be **dropped** entirely in-gateway (D-2), or must it be gated like SESSION_ID? Depends on whether any non-contextvar reader needs it. Grep answers it.

---

## 10. Acceptance Criteria

- [ ] **AC1 (I1):** Two concurrent in-process sessions don't see each other's `HERMES_SESSION_ID` via `get_session_env`. Evidence: `pytest tests/gateway/test_session_id_isolation.py::test_concurrent_sessions_isolated` (RED if the unconditional os.environ write remains).
- [ ] **AC2 (I1):** `grep` shows no gateway-reachable per-turn/per-session `os.environ["HERMES_SESSION_ID"|"HERMES_SESSION_KEY"] =` write (the 4 mapped sites are gated/dropped). Evidence: grep output + the Phase-4 enforcement test.
- [ ] **AC3 (I2):** CLI session rotation still resolves the active session id. Evidence: existing CLI/session tests green + a non-gateway `set_current_session_id` test asserting os.environ IS updated.
- [ ] **AC4 (I3 — #3):** A task created from the gateway under concurrency stamps the correct (contextvar) `worker_session_id`, not a clobbered global; a worker subprocess still stamps its own. Evidence: `tests/tools/test_kanban_session_attribution.py` (both arms).
- [ ] **AC5 (I4):** v3 + send_message_origin + cron_approval suites stay green; a live bare `send_message` routes to the current channel post-deploy. Evidence: pytest + staged live send.
- [ ] **AC6 (honest scope):** §7 and Ground-Truth state the severity accurately (hygiene/correctness + one data-attribution bug, not an active security misroute). Evidence: inspection.

---

## Appendix — the v3 precedent
v3 (`HERMES_CRON_SESSION`) is the proven template: process-global flag → `_VAR_MAP` ContextVar + `is_cron_session()` context-aware reader + set/clear per scope + delegate-boundary rebind. This PRD reuses that exact shape for the residual `HERMES_SESSION_ID`/`KEY` writes. The key difference: the session-id/key **readers are already contextvar-first**, so this is mostly *stopping the writes* + one reader migration — smaller than v3.
