# Desktop Backend → Runtime Deploy-Venv Migration — SPEC (follow-up to dev-tree contention v0.1)

**Status:** DRAFT v0.1 — follow-up spec (Ace approved spec'ing it, 2026-07-01)
**Owner:** Apollo
**Parent:** `docs/desktop/2026-07-01-devtree-contention-and-followups-SPEC.md` (Phase B, decision A+B)

## 1. Summary & Goal
The installed Hermes.app desktop backend spawns its Python from the **DEV tree + DEV venv**, not the runtime
deploy tree. This is the last consumer pinning `~/.hermes/hermes-agent/venv`, which is why Phase B of the
contention spec could NOT delete that venv (it's not an orphan while the app runs). **Goal:** migrate the
desktop backend to import from and run the **runtime deploy tree** (`~/.hermes/runtime/hermes-agent` +
its venv), matching what the gateways already do — after which `~/.hermes/hermes-agent/venv` becomes a true
orphan and Phase B's deletion is safe.

## 2. Ground-truth (measured 2026-07-01, live)
- `apps/desktop/electron/backend-env.cjs` computes the backend's env:
  - `venvRoot` = `<hermesHome>/hermes-agent/venv` (dev venv)
  - `pythonExe` = `<venvRoot>/bin/python`
  - `PYTHONPATH` prepends `<hermesHome>/hermes-agent` (dev tree import root)
  - PATH prepends `<hermesHome>/node/bin` then `<venvRoot>/bin`.
- Live proof: PID 68895 = `~/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main serve --host 127.0.0.1
  --port 0`, parent `/Applications/Hermes.app/Contents/MacOS/Hermes`. A `tui_gateway.slash_worker` runs as
  its child on the same venv.
- The gateways were already migrated to `~/.hermes/runtime/hermes-agent/venv` in the runtime deploy-only
  split (2026-07-01); the desktop backend was simply never included in that migration.
- `backend-env.cjs` derives paths from a `hermesHome` input (functions `normalizeHermesHomeRoot`,
  `buildDesktopBackendEnv`, `buildDesktopBackendPath`) — so the fix is a **path-derivation change**, plus
  deciding where `hermesHome` vs the runtime tree diverge.

## 3. Non-Goals
- NOT changing the gateways (already on the runtime venv).
- NOT changing the desktop UI, auth, or update flow (that's the shipped `desktop-update.sh`).
- NOT deleting the dev venv in THIS spec — that's Phase B of the parent, unblocked once this lands.

## 4. Open Questions (need Ace)
- **OQ-A: does the desktop backend import CODE from the dev tree or the runtime tree?** Gateways import from
  `runtime/hermes-agent` (deploy-only, FF-only). Should the desktop backend do the same (consistent, but the
  desktop app version is pinned by the installed `.app`, which may lag the runtime tree), or keep importing
  the dev tree for code and only move the VENV? Recommendation: **import + venv both from the runtime tree**
  — one deploy surface, same skew guarantees as gateways; the app's own JS is what's version-pinned, the
  Python backend should track the same runtime code every other process runs.
- **OQ-B: is `backend-env.cjs`'s target a NEW build of the app (ship a corrected `.app`), or can it read a
  config/env override at runtime?** If the path is hardcoded at build time, fixing it means a desktop rebuild
  + reinstall via `desktop-update.sh`. If it honors an env/config knob, we can point existing installs
  without a rebuild. Recommendation: **add a runtime override** (`HERMES_BACKEND_VENV` / config key) so
  existing installs migrate without a rebuild, AND fix the default in the next build.

## 5. Proposed approach (pending OQ answers)
1. Make `backend-env.cjs` derive `venvRoot`/`pythonExe`/`PYTHONPATH` from a **runtime-tree root** (default
   `<hermesHome>/runtime/hermes-agent`), overridable by env/config (OQ-B).
2. Unit-test `buildDesktopBackendEnv`/`buildDesktopBackendPath` for the new derivation (the app already has
   `backend-env.test.cjs` — extend it).
3. E2E: launch the desktop backend, assert its `python`/`PYTHONPATH`/`VIRTUAL_ENV` resolve to
   `~/.hermes/runtime/hermes-agent`, and that it signs in (supervised, like the desktop-update e2e).
4. Once no process holds `~/.hermes/hermes-agent/venv` (re-run the Phase-B `lsof`+guard), execute Phase B's
   deletion.

## 6. Acceptance Criteria
- [ ] AC-1: `backend-env.cjs` resolves the backend venv/python/PYTHONPATH to the runtime tree (unit test).
- [ ] AC-2: a freshly-launched desktop backend process runs `runtime/hermes-agent/venv/bin/python` (live
  `ps`/`lsof` proof), not the dev venv.
- [ ] AC-3: the desktop app still signs in and round-trips a turn (supervised e2e).
- [ ] AC-4: after migration, nothing holds `~/.hermes/hermes-agent/venv` (Phase-B guard reports clean once
  deleted); the dev venv is deleted.
- [ ] AC-5: existing installs migrate without a rebuild IF OQ-B chooses the runtime-override path (else a
  rebuild+reinstall via `desktop-update.sh` is documented).

## 7. Sequencing
This is gated behind the parent v0.1 shipping. It touches `apps/desktop/` (a real desktop-app change) → its
own `prd-review-pipeline` pass before build, per house discipline.
