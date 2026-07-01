# Desktop Backend → Runtime Deploy-Venv Migration — PLAN

> **For Hermes:** implement task-by-task, TDD. Spec: `docs/desktop/2026-07-01-desktop-backend-runtime-venv-migration-SPEC.md`
> (APPROVED v1.0, 4 Opus passes, super-pass). All dev-tree work in a WORKTREE; land `main` via
> `~/.hermes/fleet/land-on-main.sh` (2026-07-01 contention split — root is parked). Commit per task.

**Goal:** point the installed desktop app's Python backend at the RUNTIME deploy tree
(`~/.hermes/runtime/hermes-agent` + venv) instead of the DEV tree, with a `config.yaml desktop.backend_root`
override, so the app runs reviewed FF-only code and `~/.hermes/hermes-agent/venv` becomes a deletable orphan
(closes parent AC-5).

**Architecture:** One new resolver (`resolveBackendRoot()` in `main.cjs`, mirroring the existing
`resolveUpdateRoot()`), a targeted (no-YAML-dep) `desktop.backend_root` scalar reader with a PINNED grammar,
a one-line additive `project_root` emit in the backend ready-file (the AC-3 effect gate), then live repoint
(knob) → rebuild (default) → orphan+delete the dev venv. Phases 1 (unit, no live change) → 2 (live knob
proof) → 3 (rebuild both Macs) → 4 (delete dev venv).

**Tech stack:** Electron `*.cjs` (`node:test` + `node:assert/strict`, per-module `*.test.cjs`); Python
`hermes_cli/web_server.py` (ready-file); the shipped `desktop-update.sh` (Tier-1 worktree build) for rebuild.

**Ground-truth (measured, do not re-derive):**
- `main.cjs:340` `ACTIVE_HERMES_ROOT = path.join(HERMES_HOME,'hermes-agent')`; `:342` `VENV_ROOT = join(ACTIVE_HERMES_ROOT,'venv')`.
- Primary spawn `createActiveBackend()` (~`main.cjs:2883`) + the marker-independent path (~`2666`) + the
  Windows unwrap (`~1334`) all pass `pythonPathEntries:[root, ...getVenvSitePackagesEntries(venvRoot)]`,
  `venvRoot`, `root` into `buildDesktopBackendEnv` (pure builder in `backend-env.cjs`).
- Existing precedence precedent: `resolveUpdateRoot()` (`main.cjs:~1790`) honors `HERMES_DESKTOP_HERMES_ROOT`
  > `SOURCE_REPO_ROOT` (dev) > `ACTIVE_HERMES_ROOT`, `.git`-guarded.
- Ready-file: `hermes_cli/web_server.py:_write_dashboard_ready_file` (~14065) currently
  `json.dumps({"port": int(actual_port)})`; `PROJECT_ROOT = Path(__file__).parent.parent.resolve()` (`:45`).
  App reads it in `backend-ready.cjs:readDashboardReadyFile` (~99, `JSON.parse`, reads only `port`).
- No YAML parser in the desktop app (confirmed). Test infra: `node:test` per-module `*.test.cjs`.
- Rebuild/rollback: `desktop-update.sh` `du_backup_app`→`/Applications/.hermes-app-backups/Hermes.app.old-<ts>`,
  `BACKUP_KEEP_N=3`; freshness `du_build_input_sha` keys on `apps/desktop`+workspace-deps+lockfile (fix C).

---

## PHASE 1 — resolver + config read + ready-file emit + unit tests (NO live behavior change)

### Task 1.1 — `parseDesktopBackendRoot(configText)` scalar reader (pinned grammar)
**Objective:** read ONLY `desktop.backend_root` from raw config text with the spec §4.2 grammar; everything
outside the accepted set → return null (auto-resolve).
- Create: `apps/desktop/electron/backend-root-config.cjs` (pure fn `parseDesktopBackendRoot(text)`).
- Test: `apps/desktop/electron/backend-root-config.test.cjs`.
- **Step 1 (RED):** write the named grammar cases FIRST (each asserts the exact expected string or null):
  valid nested → path; `backend_root:` under a different top-level block → null; commented `# backend_root:` →
  null; trailing `# comment` stripped; single/double-quoted → unquoted path; `~`/`$HOME` expansion; empty
  value → null; **multi-doc (`---` before `desktop:`) → null (first doc only)**; **tab-indented key → null**;
  flow-style `desktop: {backend_root: x}` → null; duplicate key → first.
- **Step 2:** run → RED (fn undefined). **Step 3:** implement the indentation-anchored scanner (column-0
  `desktop:`; deeper-indent `backend_root:` before next column-0 key; `#`-strip; quote-strip; `~`/`$HOME`
  expand; reject tabs/flow/multi-doc). **Step 4:** GREEN. **Step 5:** commit.

### Task 1.2 — `resolveBackendRoot()` three-tier precedence
**Objective:** `HERMES_DESKTOP_BACKEND_ROOT` (from config) > runtime tree (if source-root + has venv) > dev
tree fallback; malformed/missing-tree → fall through.
- Modify: `apps/desktop/electron/main.cjs` (add `resolveBackendRoot()` + wire the config read → internal
  `HERMES_DESKTOP_BACKEND_ROOT`; derive `VENV_ROOT`/spawn inputs from it at all three spawn sites).
- Test: extend `apps/desktop/electron/backend-env.test.cjs` (or a new `resolve-backend-root.test.cjs` if
  `resolveBackendRoot` is extracted to a testable pure module — PREFER extracting it so it's unit-testable
  without launching Electron).
- **RED:** tiers — runtime present → runtime; runtime absent → dev; config override → override; config points
  at missing tree → auto-resolve (fail-safe). **AC-1 site-packages assert:** the derived
  `getVenvSitePackagesEntries(venvRoot)` + `pythonPathEntries` resolve under the RUNTIME venv, not just
  `root`. **GREEN. Commit.**

### Task 1.3 — ready-file emits `project_root` (AC-3 mechanism)
**Objective:** the backend publishes the tree it's running from so the app can effect-gate it.
- Modify: `hermes_cli/web_server.py:_write_dashboard_ready_file` → `json.dumps({"port": int(actual_port),
  "project_root": str(PROJECT_ROOT)}, ...)`.
- Modify: `apps/desktop/electron/backend-ready.cjs:readDashboardReadyFile` → also surface `project_root`.
- Test: extend `apps/desktop/electron/backend-ready.test.cjs` (reads `project_root`); a Python test asserting
  the ready-file JSON includes `project_root` (find the existing web_server test module; if none, a targeted
  pytest that calls `_write_dashboard_ready_file` against a tmp path and checks the JSON).
- **RED → implement (one-line additive each side) → GREEN → commit.**

### Task 1.4 — AC-5 fallback surface (tier-3 absent-tree AND broken-venv → same surfaced condition)
**Objective:** when resolution falls back to the dev tree (either no runtime tree OR broken runtime venv),
surface it on BOTH a boot-log line AND the app notify/UI path — one shared code path, two triggers.
- Modify: `main.cjs` (the fallback branch in `resolveBackendRoot()` emits the condition).
- Test: both triggers hit the same surfaced-condition function (unit, no live app).
- **RED → implement → GREEN → commit.**

### Task 1.5 — Phase-1 mechanics gate
Run `apps/desktop` node test suite (`npm run test:desktop:platforms` or the targeted `node --test`
electron suite) — all green including the new files. Row of evidence. Commit if any fixup.

---

## PHASE 2 — live repoint WITHOUT rebuild (B2 proof, effect-gated) — SUPERVISED

### Task 2.1 — set the knob + relaunch + prove
- Set `desktop.backend_root: ~/.hermes/runtime/hermes-agent` in the live `config.yaml` (Studio).
- **Do NOT relaunch mid-`deploy.sh`** (R-6): confirm no deploy in flight first.
- Relaunch Hermes.app; prove via live `ps`/`lsof`: backend PID runs
  `~/.hermes/runtime/hermes-agent/venv/bin/python`, PYTHONPATH → runtime tree (AC-2).
- **Effect gate (AC-3):** read the ready-file `project_root` — assert it's under `runtime/hermes-agent`;
  MISSING/empty = FAIL (the pre-Task-1.3 backend would fail this — expected until relaunch on new code path;
  since Phase 2 uses the CONFIG knob on the EXISTING installed app, the ready-file emit only exists after a
  rebuild — SO: for Phase 2 the effect proof is `ps`/`lsof` + PYTHONPATH; the `project_root` gate applies to
  Phase 3's rebuilt app. Note this ordering explicitly.)
- Supervised: Ace signs in + round-trips a turn on the repointed backend.
- **Rollback proof:** clear the knob → relaunch → backend back on dev tree (AC-4 inverse, AC-7 knob half).
- Evidence rows. (No commit — live-config change, not code.)

---

## PHASE 3 — rebuild + reinstall (B1) — SUPERVISED

### Task 3.1 — rollback precondition gate (AC-7) BEFORE overwrite
- ditto the current `/Applications/Hermes.app` to a throwaway path; restore it back +
  `xattr -dr com.apple.quarantine` + launch-verify; **ABORT the rebuild if restore fails.**

### Task 3.2 — rebuild both Macs
- `bash ~/.hermes/fleet/desktop-update.sh --host self --apply` (Studio), then `--host mbp --apply`.
- Freshness (fix C) fires because `apps/desktop` changed (Tasks 1.2-1.4) even though version stays 0.17.0.

### Task 3.3 — prove the DEFAULT (no knob) resolves runtime
- With `desktop.backend_root` UNSET, relaunch; `ps`/`lsof` → runtime venv; ready-file `project_root` under
  runtime tree (AC-3 effect gate, now fail-closed on missing). Supervised sign-in both Macs (AC-3).
- Evidence rows.

---

## PHASE 4 — orphan the dev venv + close parent AC-5

### Task 4.1 — re-prove nothing holds the dev venv (ALL descendants)
- `lsof +D ~/.hermes/hermes-agent/venv` empty; `ps` shows no backend / `slash_worker` / transient child on
  it (BL-3/AC-6). If the desktop backend is now on runtime, the only holders were it + its children — all
  gone after the Phase-3 relaunch.
- Grep no LIVE plist/script refs (excluding backups) — already clean from parent v0.1.

### Task 4.2 — delete + verify
- `rm -rf ~/.hermes/hermes-agent/venv`; `runtime_tree_status.py --check-orphan-venv` → clean (AC-6).

### Task 4.3 — flip parent AC-5 + closeout
- Edit parent spec `2026-07-01-devtree-contention-and-followups-SPEC.md` AC-5 `[~]`→`[x]` (venv deleted).
- `prd-closeout` this spec: map AC-1..AC-7 to evidence, re-share the link (verdict → shipped), mem0 the fact
  (desktop backend now on runtime venv; dev venv deleted).

---

## Smoke matrix (Phase-1 gate before any live step)
Every new unit file green + the electron mechanics suite green, one row each:
- `backend-root-config.test.cjs` (grammar cases) · `resolve-backend-root` (tiers + site-packages) ·
  `backend-ready.test.cjs` (project_root) · python ready-file test · fallback-surface test ·
  `test:desktop:platforms` full green.
