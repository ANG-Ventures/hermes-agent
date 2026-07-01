# Desktop Backend → Runtime Deploy-Venv Migration — SPEC

**Status:** APPROVED v1.0 — 4 Opus passes, super-pass (0 blockers, 0 required changes). Ready for prd-plan.
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

### 4.2 The `config.yaml` override knob (B2) — RESOLVED (pass-1 BL-1/BL-2)
**Ground-truthed in review:** the Electron app has **NO YAML parser** (zero `yaml`/`js-yaml` in
`apps/desktop/package.json` deps/devDeps; the only `.yaml` refs in `electron/*.cjs` are a MIME extension map).
So a full YAML read in JS would mean adding a YAML dependency to Electron — rejected (avoidable weight). And
there is **no existing "emit resolved backend root" CLI path** (`config` has `path`/`env-path` but nothing for
this) — so option (ii) requires building a *new* emitter, which reintroduces a chicken-egg (which interpreter
runs the emitter that tells you which interpreter to use?).

**Chosen design — minimal targeted scalar read in JS, no YAML dep, no bootstrap interpreter (BL-1+BL-2):**
- `config.yaml`'s `desktop.backend_root` is a single **scalar string** under a known key. The Electron side
  reads ONLY that one value with a tiny targeted parser (a bounded line/regex scan for `backend_root:` under a
  `desktop:` block — NOT a general YAML parser, no new dependency). Empty/absent → auto-resolve (4.1).
- This **sidesteps the bootstrap chicken-egg entirely** (BL-2): choosing the backend root no longer requires
  running any Python — the JS reads the file directly before spawning anything. No interpreter parses config
  to decide which interpreter to use.
- The value, once read, becomes the highest-precedence input to `resolveBackendRoot()` (via the internal
  `HERMES_DESKTOP_BACKEND_ROOT` bridge var — internal only, user-facing docs reference `config.yaml`
  exclusively, per the house rule).
- Precedence: `config.yaml desktop.backend_root` > runtime tree (default, 4.1) > dev tree (fallback).
- Robustness: a malformed/partial config read must FAIL SAFE to auto-resolve (never crash the spawn); a set
  value that points at a non-existent tree logs a warning and falls through to auto-resolve (so a typo can't
  brick the app).
- **Parser grammar (pass-2 BL-1 — pinned, NOT "whatever the regex matches"):** the scan is
  indentation-anchored, not a bare token grep:
  1. Find a top-level (column-0) `desktop:` key; only `backend_root:` nested under it (deeper indent, before
     the next column-0 key) is accepted. A `backend_root:` under any OTHER block is IGNORED.
  2. Strip a trailing `#` comment; a line whose first non-space char is `#` is a comment → ignored (a
     commented-out `# backend_root:` never matches).
  3. Accept the scalar unquoted, single-, or double-quoted; trim surrounding whitespace; expand a leading
     `~`/`$HOME`. Empty value → auto-resolve.
  4. First matching `backend_root:` under `desktop:` wins (duplicate keys → first).
  5. **Flow-style (`desktop: {backend_root: /x}`), multi-document YAML (a `---` document marker — the scan
     stops at the FIRST `---` and only reads the first document), tab-indented `backend_root:` (YAML forbids
     tabs; a tab-indented key → REJECT, never silently read as un-nested and pick up a sibling block's key),
     and any shape the scanner can't unambiguously read → REJECT → auto-resolve** (fail-safe, not a guess).
     The accepted grammar is a *defined* set; everything outside it deterministically falls to auto-resolve.
  Each of these is a NAMED Phase-1 unit-test case (§6/AC-1): different-block false key, commented-out line,
  trailing-comment, quoted path, `~`-expansion, flow-style, **multi-document `---`, tab-indented**, duplicate
  — each asserting the RIGHT tree (or auto-resolve), never just "doesn't crash."
- This lets a RUNNING install repoint (set key → relaunch app) and prove AC-2 BEFORE any rebuild.

### 4.3 Build + ship (B1)
- The default change (4.1) only takes effect in a NEW build. Rebuild + reinstall both Macs via the shipped
  `~/.hermes/fleet/desktop-update.sh --host self|mbp --apply` (already builds in a throwaway worktree, Tier-1).
- Version does not change (0.17.0), so freshness keys on the build-input commit (fix C) — the `apps/desktop`
  change flips the pin to stale → rebuild fires correctly. **Confirmed (pass-2):** `desktop-update.sh`'s
  `du_build_input_sha` (fix C, shipped) keys on `desktop-build-inputs.sh`'s output = `apps/desktop` + resolved
  `file:` workspace deps + root lockfile — so a change under `apps/desktop/` DOES advance the pin regardless of
  the unchanged version string; the rebuild will fire (not a version-string no-op).

## 5. Open Questions
- **OQ-1 → RESOLVED (pass-1 BL-1):** neither (i) full-YAML-in-JS nor (ii) new-CLI-emitter — the app has no
  YAML parser and no emitter exists, and (ii) reintroduces a bootstrap chicken-egg. **Chosen: JS reads the
  single `desktop.backend_root` scalar with a bounded targeted parse (no dep, no interpreter), §4.2.**
- **OQ-2 → RESOLVED (pass-1 RC):** broken/missing runtime venv → fall back to the dev tree so the app stays
  usable, BUT the fallback is a **monitored condition, not a boot-log-into-the-void**: surface it via the
  app's notify/UI path (a "running fallback dev backend" indicator) AND a boot-log line, so a silent stale-code
  regression can't hide. AC-5 asserts both surfaces.
- **OQ-3 → RESOLVED (Ace, decision):** delete the dev venv in THIS spec's Phase 4 (this spec is what makes it
  a true orphan), after AC-2 proof, and flip the parent's AC-5 to done.

## 6. Implementation Phases (for prd-plan)
- **Phase 1 — resolver + config read + ready-file emit + unit tests (no live behavior change).** Add
  `resolveBackendRoot()` (three-tier precedence) + the targeted `desktop.backend_root` scalar read (grammar
  §4.2); extend `_write_dashboard_ready_file` to emit `project_root` (AC-3 mechanism); unit tests for
  `buildDesktopBackendEnv`/`buildDesktopBackendPath` derivation AND the config-read parser (the named grammar
  cases: different-block false key / commented-out / trailing-comment / quoted / `~`-expansion / flow-style /
  duplicate / missing-tree → fail-safe) across all precedence tiers. Also assert the tier-3 (no runtime tree)
  AND broken-runtime-venv fallbacks BOTH hit the same surfaced-condition path (AC-5 / pass-2 residual).
- **Phase 2 — live repoint WITHOUT rebuild (B2 proof, effect-gated).** Set `desktop.backend_root` on the
  running Studio install, relaunch, prove the backend PID runs `runtime/hermes-agent/venv/bin/python`
  (live `ps`/`lsof`) AND that a hermes module imported by the live backend has `__file__` under
  `~/.hermes/runtime/hermes-agent` (the effect gate, BL-3), THEN sign-in + turn round-trip (supervised).
- **Phase 3 — rebuild + reinstall (B1).** `desktop-update.sh --apply` both Macs; prove the DEFAULT (no knob)
  resolves the runtime tree post-rebuild (same effect gate); supervised sign-in both Macs.
- **Phase 4 — orphan the dev venv + close AC-5.** Re-prove nothing holds `~/.hermes/hermes-agent/venv`
  including the primary backend AND its `tui_gateway.slash_worker` children AND any transient child
  (`lsof +D` + `ps` for all descendants, BL-3/AC-6), `rm -rf`, guard reports clean; flip parent AC-5 to done.

## 7. Acceptance Criteria
- [ ] AC-1: `resolveBackendRoot()` returns the runtime tree by default when it exists; falls back to the dev
  tree when it doesn't; `config.yaml desktop.backend_root` overrides both; malformed/missing-tree value
  fails safe to auto-resolve (unit tests, all tiers + fail-safe). **Also assert the DERIVED
  `getVenvSitePackagesEntries(venvRoot)` + `pythonPathEntries` point at the RUNTIME venv (pass-3), not just
  that `root` resolved** — a correct `root` with a stale `venvRoot` would still mis-spawn; both spawn sites
  (`main.cjs:~1334`, `~2874`) derive from the resolved pair.
- [ ] AC-2: a freshly-launched desktop backend runs `~/.hermes/runtime/hermes-agent/venv/bin/python` with
  PYTHONPATH → runtime tree (live `ps`/`lsof`), NOT the dev venv — proven first via the knob (Phase 2) then
  via the default post-rebuild (Phase 3).
- [ ] **AC-3 (EFFECT gate, BL-3 + pass-2 BL-2 — concrete mechanism):** the backend's existing dashboard
  ready-file writer (`hermes_cli/web_server.py:_write_dashboard_ready_file`, currently emits `{"port": N}`) is
  extended to ALSO emit `"project_root": str(PROJECT_ROOT)` (`PROJECT_ROOT = Path(__file__).parent.parent` —
  the tree the backend is running from). The app's `backend-ready.cjs` `readDashboardReadyFile` asserts
  `project_root` is under `~/.hermes/runtime/hermes-agent` on the migrated backend. This one-line additive
  emit is a **Phase-1 deliverable** (not "e.g."). A silent dev-tree fallback fails this gate. Sign-in + turn
  round-trip is required IN ADDITION. **Fail-closed (pass-3): a MISSING or empty `project_root` in the
  ready-file = gate FAIL, not skip** — an un-rebuilt/older backend that never emits the key must NOT pass by
  omission (that would resurrect the proxy-gate problem).
- [ ] AC-4: `config.yaml desktop.backend_root` overrides the default (set → relaunch → backend on the
  specified tree); empty → auto-resolve. User-facing docs reference `config.yaml` only (no raw env var).
- [ ] AC-5: broken/missing runtime venv → fall back to the dev tree AND surface it on BOTH the app notify/UI
  path and a boot-log line (monitored condition, not a silent log).
- [ ] AC-6: after migration, nothing holds `~/.hermes/hermes-agent/venv` — the primary backend, its
  `slash_worker` children, and any transient descendant all re-proven off it — it is deleted; the Phase-B
  guard reports clean; the parent spec's AC-5 is flipped to done.
- [ ] AC-7 (rollback, pass-2/3 — cite the real backup, gate the restore): the knob revert is
  `desktop.backend_root=""` + relaunch (tested by AC-4's inverse). The post-rebuild revert reinstalls the prior
  `.app` from the EXISTING keep-last-3 backup: `desktop-update.sh` writes `du_backup_app` →
  `/Applications/.hermes-app-backups/Hermes.app.old-<ts>` (`BACKUP_DIR_DEFAULT`, `BACKUP_KEEP_N=3`, both
  shipped). **The restore direction is a Phase-3 PRECONDITION GATE (pass-3): before the rebuild overwrites the
  live app, ditto the current app to a throwaway path, restore it back + `xattr -dr quarantine` +
  launch-verify, and ABORT the rebuild if that restore fails** — the rollback is exercised on the real target
  first, not assumed.

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
- **R-5 (pass-1): AC-3 as a proxy gate.** Mitigation: AC-3 is now an EFFECT gate (imported-module `__file__`
  under the runtime tree), so a silent dev-tree fallback cannot pass it.
- **R-6 (pass-1): concurrency — repointing a running install while a `deploy.sh` lands on the same runtime
  tree.** Mitigation: `deploy.sh` is FF-only + serialized; the desktop backend imports at spawn (frozen in
  `sys.modules`), so a concurrent FF doesn't move code under a running backend — but Phase 2 should not
  relaunch the app mid-`deploy.sh`. Note it in the plan; low-risk given FF-only semantics.

## 9. Review / build discipline
Touches `apps/desktop/` (a real desktop-app change) → its own `prd-review-pipeline` pass before build, per
house discipline. All dev-tree work happens in a worktree; `main` advances via `land-on-main.sh` (2026-07-01
contention split). This spec doc itself lands via that flow.
