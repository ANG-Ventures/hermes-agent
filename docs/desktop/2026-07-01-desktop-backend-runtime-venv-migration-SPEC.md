# Desktop Backend → Runtime Deploy-Venv Migration — SPEC

**Status:** DRAFT v0.1 — for prd-review
**Owner:** Apollo
**Date:** 2026-07-01
**Parent:** `docs/desktop/2026-07-01-devtree-contention-and-followups-SPEC.md` (Phase B — unblocks AC-5)
**Decisions (Ace, 2026-07-01):** A1 (backend imports code + venv from the RUNTIME tree, like gateways)
· B1 (fix the default in the desktop app + rebuild via `desktop-update.sh`) · B2 (add a **`config.yaml`**
override knob — NOT a raw env var — so a running install can repoint without a rebuild).

## 1. Summary & Goal
The installed Hermes.app spawns its Python backend from the **DEV tree + DEV venv**
(`~/.hermes/hermes-agent` + `.../venv`), while every gateway was already migrated to the **runtime deploy
tree** (`~/.hermes/runtime/hermes-agent` + its venv) in the 2026-07-01 deploy-only split. The desktop backend
was simply never included. **Goal:** point the desktop backend's Python interpreter + import path at the
runtime deploy tree by default, with a `config.yaml` override, so (a) the app runs reviewed FF-only code with
the same skew guarantees as gateways, and (b) `~/.hermes/hermes-agent/venv` becomes a true orphan — closing
the parent spec's AC-5 (delete the 320M dev venv).

## 2. Ground-truth (measured live 2026-07-01)
Backend spawn lives in `apps/desktop/electron/main.cjs`; the env is assembled by
`apps/desktop/electron/backend-env.cjs` (a **pure path-string builder** — it does not decide the tree; the
caller passes `hermesHome`/`venvRoot`/`pythonPathEntries`). Exact call sites:
- **Constants (the fix target):**
  - `main.cjs:340` `ACTIVE_HERMES_ROOT = path.join(HERMES_HOME, 'hermes-agent')`  ← the dev tree
  - `main.cjs:342` `VENV_ROOT = path.join(ACTIVE_HERMES_ROOT, 'venv')`  ← the dev venv
- **Primary spawn:** `createActiveBackend()` (`main.cjs:~2883`) passes
  `pythonPathEntries: [ACTIVE_HERMES_ROOT, ...getVenvSitePackagesEntries(VENV_ROOT)]`, `venvRoot: VENV_ROOT`,
  `root: ACTIVE_HERMES_ROOT` into `buildDesktopBackendEnv`.
- **Existing override precedent:** `resolveUpdateRoot()` (`main.cjs:~1790`) already honors
  `HERMES_DESKTOP_HERMES_ROOT` ("always wins so devs can pin a worktree") + `SOURCE_REPO_ROOT` (dev) vs
  `ACTIVE_HERMES_ROOT` (packaged). So an override mechanism + tree-selection precedent ALREADY exists in this
  file — we extend the same pattern to the *backend* root, not just the *update* root.
- **Live proof of the coupling:** PID 68895 = `~/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main
  serve --host 127.0.0.1 --port 0`, child of `/Applications/Hermes.app`; PYTHONPATH prepends
  `~/.hermes/hermes-agent`. A `tui_gateway.slash_worker` runs as its child on the same venv.
- **Drift already tolerated (removes the main A1 objection):** the installed .app is 0.17.0 built at
  `3fc83b17`, but its live backend runs dev HEAD `c1dfd00de` — hundreds of commits ahead of the .app build —
  and works (Ace is signed in). So the JS↔Python `json-rpc-gateway` protocol already tolerates
  frontend/backend version drift; pointing the backend at the (more stable) runtime tree REDUCES drift risk.
- **Runtime tree facts:** `~/.hermes/runtime/hermes-agent` (fork/main, FF-only via `deploy.sh`), venv at
  `.../venv`, editable self-install, deps pinned. Gateways import from it and are healthy.

## 3. Non-Goals
- NOT changing gateways (already on the runtime venv).
- NOT changing desktop UI / auth / the update flow itself (that's the shipped `desktop-update.sh`).
- NOT introducing a raw `HERMES_*` behavioral env var (house rule: `.env` = secrets only; behavioral config
  lives in `config.yaml`). The override is a `config.yaml` key, bridged to an internal env var if the JS side
  needs one.
- NOT deleting the dev venv in THIS spec's build — that's the parent AC-5, executed in closeout once AC-2
  (backend on runtime venv) is proven and nothing holds the dev venv.

## 4. Design
### 4.1 Backend root resolution (A1)
Introduce a single resolver, `resolveBackendRoot()`, mirroring `resolveUpdateRoot()`'s precedence but for the
*backend* interpreter/import source:
1. `HERMES_DESKTOP_BACKEND_ROOT` (internal env, highest — dev/test pin; bridged from config, see 4.2).
2. The **runtime deploy tree** `~/.hermes/runtime/hermes-agent` **if it exists and is a hermes source root**
   (`isHermesSourceRoot` + has a `venv`). ← new DEFAULT.
3. Fallback to `ACTIVE_HERMES_ROOT` (the dev tree) — preserves today's behavior on installs that have no
   runtime tree (CLI-only-never-split users, fresh installs before a `deploy.sh`).

`VENV_ROOT` and the `pythonPathEntries`/`root` passed to `buildDesktopBackendEnv` derive from
`resolveBackendRoot()` instead of the hardcoded `ACTIVE_HERMES_ROOT`. `createActiveBackend()` and the
marker-independent path (`main.cjs:~2666`) both switch to the resolved root.

### 4.2 The `config.yaml` override knob (B2)
- Config key: **`desktop.backend_root`** (string path; default empty = auto-resolve per 4.1).
- The Python side already reads `config.yaml`; the DESKTOP (Electron/JS) side reads it at spawn time. Two
  viable wirings — decide in review (OQ-1):
  - **(i)** JS reads `config.yaml` directly (it already parses YAML for other keys?) and sets
    `HERMES_DESKTOP_BACKEND_ROOT` internally; user-facing docs point ONLY to `config.yaml`.
  - **(ii)** a tiny `hermes` CLI subcommand / existing config-read path emits the resolved root the desktop
    consumes.
- Precedence: explicit `desktop.backend_root` > runtime tree (default) > dev tree (fallback).
- This lets a RUNNING install repoint (set key → relaunch app) and prove AC-2 BEFORE any rebuild.

### 4.3 Build + ship (B1)
- The default change (4.1) only takes effect in a NEW build. Rebuild + reinstall both Macs via the shipped
  `~/.hermes/fleet/desktop-update.sh --host self|mbp --apply` (already builds in a throwaway worktree, Tier-1).
- Version does not change (0.17.0), so freshness keys on the build-input commit (fix C) — the `apps/desktop`
  change flips the pin to stale → rebuild fires correctly.

## 5. Open Questions (for review)
- **OQ-1 (wiring, 4.2):** does the Electron main process already read `config.yaml`, or must we add a
  config-read (option i vs ii)? Recommendation: whichever avoids a second YAML parser in JS — if the app
  already reads config, extend it; else emit the resolved root from the Python config layer the app already
  shells for the backend. Ground-truth in Phase 1.
- **OQ-2 (fallback safety):** if the runtime tree exists but its venv is broken/mid-deploy, should the backend
  fall back to the dev venv or FAIL LOUD? Recommendation: fall back to dev tree with a logged warning (an
  app that won't start is worse than one on a slightly-staler-but-working venv), but surface the fallback in
  the boot log so it's diagnosable.
- **OQ-3 (AC-5 timing):** delete the dev venv in THIS spec's closeout, or leave it to the parent? Rec: delete
  here (this spec is what makes it a true orphan) after AC-2/AC-4 proof, and flip the parent's AC-5 to done.

## 6. Implementation Phases (for prd-plan)
- **Phase 1 — resolver + unit tests (no live change).** Add `resolveBackendRoot()` + config override; unit
  tests for `buildDesktopBackendEnv`/`buildDesktopBackendPath` derivation from each precedence tier (extend
  `backend-env.test.cjs` + a new `main`-level test). Ground-truth OQ-1 here.
- **Phase 2 — live repoint WITHOUT rebuild (B2 proof).** Set `desktop.backend_root` on the running Studio
  install, relaunch, prove the backend PID now runs `runtime/hermes-agent/venv/bin/python` (live `ps`/`lsof`)
  and the app still signs in + round-trips a turn (supervised). This proves the knob before any rebuild.
- **Phase 3 — rebuild + reinstall (B1).** `desktop-update.sh --apply` on both Macs; prove the DEFAULT (no
  knob) now resolves the runtime tree post-rebuild; supervised sign-in both Macs.
- **Phase 4 — orphan the dev venv + close AC-5.** Re-prove nothing holds `~/.hermes/hermes-agent/venv`
  (Phase-B `lsof` + guard), `rm -rf`, guard reports clean; flip parent AC-5 to done.

## 7. Acceptance Criteria
- [ ] AC-1: `resolveBackendRoot()` returns the runtime tree by default when it exists; falls back to the dev
  tree when it doesn't; `desktop.backend_root` overrides both (unit tests, all three tiers).
- [ ] AC-2: a freshly-launched desktop backend runs `~/.hermes/runtime/hermes-agent/venv/bin/python` with
  PYTHONPATH → runtime tree (live `ps`/`lsof`), NOT the dev venv — proven first via the config knob
  (Phase 2) then via the default post-rebuild (Phase 3).
- [ ] AC-3: the desktop app signs in and round-trips a turn on the migrated backend (supervised, both Macs).
- [ ] AC-4: `config.yaml desktop.backend_root` overrides the default (set → relaunch → backend on the
  specified tree); empty → auto-resolve. User-facing docs reference `config.yaml` only (no raw env var).
- [ ] AC-5: broken/missing runtime venv → documented fallback behavior (OQ-2) with a boot-log line.
- [ ] AC-6: after migration, nothing holds `~/.hermes/hermes-agent/venv`; it is deleted; the Phase-B guard
  reports clean; the parent spec's AC-5 is flipped to done.

## 8. Risks
- **R-1: an install with no runtime tree (fresh/CLI-only) must still work.** Mitigation: tier-3 fallback to
  the dev tree (AC-1) — the default only *prefers* the runtime tree when present.
- **R-2: the .app build SHA and the runtime-tree code can diverge.** Mitigation: already true today and
  worse (dev churn); the protocol tolerates it (§2); runtime tree is the MORE stable target.
- **R-3: rebuild required for the default to take effect.** Mitigation: the config knob (B2) migrates running
  installs immediately; the rebuild makes it the permanent default.
- **R-4: house-rule violation (raw env var).** Mitigation: the user-facing knob is `config.yaml
  desktop.backend_root`; any internal `HERMES_DESKTOP_BACKEND_ROOT` is a JS-side bridge only, documented as
  internal.

## 9. Review / build discipline
Touches `apps/desktop/` (a real desktop-app change) → its own `prd-review-pipeline` pass before build, per
house discipline. All dev-tree work happens in a worktree; `main` advances via `land-on-main.sh` (2026-07-01
contention split). This spec doc itself lands via that flow.
