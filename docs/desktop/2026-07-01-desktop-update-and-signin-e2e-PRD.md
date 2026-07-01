# PRD — Hermes Desktop App Update + Supervised Sign-in, folded into the Hermes update runbook

**Status:** ✅ CLOSED — BUILT + SHIPPED (both Macs 0.15.1→0.17.0, signed in) 2026-07-01. (v0.5 spec below; 4 Opus passes → descope → converged → built via prd-plan/subagent-driven-dev.)
**Author:** Apollo · 2026-07-01
**Owner host for the work:** Mac Studio (`192.168.1.18`, this agent's host) + Ace's MacBook Pro (`192.168.1.88`)

## 0. Scope decision (Ace, 2026-07-01) — why this is v0.4, not another v0.3 pass
Three Opus review passes converged on one truth: an **automated, unattended proof that the packaged app's
renderer completes sign-in** is a tower (a named renderer driver, the production-Electron-fuses/remote-debug
risk, Aqua-session gating, and a persisted-cookie-vs-fresh-profile "what does AC-2 even certify" ambiguity)
— all of it existing *only* to make automated renderer-driving trustworthy. **Ace descoped that
requirement:** "forget about that" → **(B) supervised one-time sign-in confirmation.** This PRD keeps
everything cleanly automatable and genuinely valuable, and makes the actual "can I sign in" a **staged,
supervised, one-time human confirmation** — the same shape v0.3's D-5 already accepted for the MBP, now the
whole sign-in story. The tower is gone.

## 0.1 Phase-0 ground-truth (measured live 2026-07-01, evidence `/tmp/desktop-review/phase0-groundtruth.json`)
- **Version skew (the motivating bug), cited:** installed `/Applications/Hermes.app` = **`0.15.1`**
  (`CFBundleShortVersionString`); repo `apps/desktop/package.json` = **`0.17.0`**. Two minor versions stale.
- **Both Macs are non-loopback OAuth/`basic`-password clients (MEASURED on both, not assumed):**
  - Studio dashboard is bound `--host mac-studio-m3u --port 9119` → `/api/status` returns
    `auth_required: true`; provider `basic` (Username & Password); `127.0.0.1:9119` is refused (no loopback
    dashboard). Auth mode is decided by the bind host (`web_server.py`): loopback→no-auth, non-loopback→OAuth.
  - MBP `~/Library/Application Support/Hermes/connection.json` = `mode:remote,
    url:http://mac-studio-m3u:9119, authMode:oauth` (read live over SSH to `.88`). Same backend, same mode.
- **`authModeFromStatus(status)` → `status.auth_required ? 'oauth' : 'token'`** — both Macs resolve `'oauth'`.
- **Studio Hermes gateway is a LaunchAgent** (`~/Library/LaunchAgents/ai.hermes.gateway.plist`, `gui/<uid>`).
- **`runtime_tree_status.py` exists and `deploy.sh` already carries a `--desktop` hook** (from the shipped
  runtime-deploy-only-split) — so the fold-in target is real, not aspirational.

## 1. Summary & Goal
Ace asked: make **updating the Hermes Desktop app** a first-class part of the "update Hermes" runbook on
**both** his Macs, and give him **confidence he can sign in** after an update. Today the desktop app is a
separate electron-builder package that `hermes update` / `git pull` does not touch — Studio's app is
`0.15.1` while the repo is `0.17.0`, two minor versions stale and nobody noticed.

**Goal:** a single documented, idempotent, reversible procedure (a script + skill runbook) that, per Mac:
(a) rebuilds/reinstalls the desktop app from a pinned checkout ref, (b) **proves freshness by build
identity (git SHA / asar hash), not just the marketing version string**, (c) verifies the backend is
actually **signable** (reachable + `basic` provider present), and (d) **stages a supervised one-time
sign-in** — the app is left launched and ready, Ace signs in once, and the run records the confirmed
result. LOUD, honest failure at every gate if it can't.

## 2. Non-Goals
- **Not** an automated/unattended renderer-driving sign-in test (descoped, §0 — this is the whole reason
  v0.4 exists). No CDP/Playwright-Electron driver, no fuses/remote-debug spike, no Aqua-session automation.
- **Not** an auto-updater / Squirrel feed inside the app (matches the `deploy.sh`/`hermes update` deliberate
  posture Ace prefers).
- **Not** changing the desktop app's auth model or dashboard backend auth (consumed from
  `hermes-desktop-remote-backend`, not redesigned here).
- **Not** code-signing with an Apple Developer cert (electron-builder ad-hoc sign stays; clear quarantine).
- **Not** Windows/Linux desktop packaging (both Ace targets are macOS; `dist:win`/`dist:linux` stay a
  roadmap row).

## 3. Constitution / Invariants
- **INV-1 (no silent stale install — keyed on BUILD IDENTITY, not the version string):** after the runbook
  runs on a Mac, the installed `/Applications/Hermes.app` must match the pinned build by a **content/commit
  identity** — the build's git SHA (embedded at build time) and/or the `app.asar` content hash — AND the
  human-readable `CFBundleShortVersionString` equals the ref's `apps/desktop/package.json` version. *Why the
  SHA:* two different builds of `0.17.0` are identical to a version-string check, so a code change with no
  version bump would install stale and report "already current" — exactly the *silent* stale install INV-1
  forbids. *Closeout proof:* the report shows installed SHA/asar-hash == pinned build's, on both Macs.
- **INV-2 (sign-in is proven by a SUPERVISED human confirmation, honestly recorded — never faked):** the
  update is "done" for a Mac only when EITHER (a) Ace completes a one-time sign-in in the freshly-installed
  app and **the agent** records that confirmation to a durable record (D-7), OR (b) the run records an
  explicit **PENDING-SUPERVISED-SIGNIN** state for that Mac. **The `desktop-update.sh` SCRIPT only ever
  writes PENDING** — it cannot block on Ace's out-of-band "signed-in" reply; the CONFIRMED transition is
  written by the *agent* (Apollo) when Ace attests, to the D-7 record. The runbook may automate the
  *precondition* — the backend is signable (`/api/status` reachable with the correct Host header +
  `auth_required:true` + a `basic` provider in `/api/auth/providers`) — but a green precondition is
  **explicitly NOT** a sign-in proof and must never be reported as one. Under `deploy.sh --desktop`
  (non-interactive) there is no attester, so that path is **PENDING-by-construction** — honest, not a
  failure. *Why:* SOUL §8 — a backend `200` that leaves the app unusable is a fake green; the honest proof
  is the human doing the one thing he'd do anyway, once, recorded as an artifact rather than assumed.
- **INV-3 (no credential in logs/repo/artifact):** no dashboard password is handled by the runbook at all in
  the supervised model (Ace types it into the app himself). The backend-signable precondition uses only
  unauthenticated `/api/status` + `/api/auth/providers` probes. *Closeout proof:* gitleaks-clean; no
  password read, piped, logged, or committed anywhere in the runbook.
- **INV-4 (build reproducibility):** the rebuild syncs to the pinned `--ref` and runs `npm ci` (not `npm
  install`) at the correct monorepo workspace root, so the packaged app matches the checkout that was built.
  *Closeout proof:* runbook uses `npm ci`; a dirty lockfile is surfaced, not silently installed over.
- **INV-5 (idempotent + reversible via a durable, RETENTION-BOUNDED backup, NOT Trash):** re-running on an
  already-current Mac (same build identity, INV-1) is a safe no-op ("already current", skip rebuild unless
  `--force`); the old app is moved to a **versioned backup dir outside Trash** (`/Applications/.hermes-app-backups/
  Hermes.app.old-<ts>`), never `rm`-ed, and pruned **keep-last-N** (default N=3, ~300MB each) so repeated
  updates don't silently fill `/Applications`. A restore **re-clears quarantine** (`xattr -dr
  com.apple.quarantine`) and **launch-verifies beyond process-exists** (app process up AND its window/main
  renderer reaches ready — not a bare `pgrep`, which can be a white/broken renderer). *Why not Trash:* it
  auto-empties (30-day), per-user `~/.Trash` differs under a non-interactive SSH `HOME`, and Gatekeeper
  re-quarantines a Trash-restored app. *Closeout proof:* second run prints "already current"; the pre-swap
  backup exists; only the last N are retained; a rehearsed restore launch-verifies clean.

## 4. Resolved Decisions
- **D-1: Both Macs are non-loopback OAuth/`basic`-password clients (MEASURED on both, §0.1).** Studio →
  `mac-studio-m3u:9119`; MBP → same over Tailscale. Uniform `'oauth'` mode. No loopback/token special case.
  *(The v0.1 "Studio=local/token" premise was refuted by measurement; the MBP premise is now measured too,
  not assumed — closes the last "assumed backend" gap.)*
- **D-2: Sign-in is SUPERVISED, not driven (the descope).** The runbook stages the freshly-installed app
  (launches it, or leaves it ready + prints exactly what to do) and Ace signs in once; the run records
  CONFIRMED or PENDING-SUPERVISED-SIGNIN. No renderer driver, no packaged-app automation. The automatable
  precondition (backend signable) runs first and gates the staging, but is never reported as the proof.
- **D-3: Freshness is keyed on the embedded git SHA (asar hash is a post-install integrity check ONLY,
  NOT the pre-build skip key — folds pass-3 B2″ + pass-4 change-1).** The build embeds its source git SHA;
  the runbook compares the installed app's embedded SHA to the pinned `--ref`'s SHA (`git rev-parse <ref>`
  — free, no build needed) for the "already current" skip and INV-1. The **`app.asar` sha256 is used only
  AFTER a build**, as a post-install integrity check that the installed bytes match what was just built —
  it CANNOT be the pre-build skip key (you can't know the target build's asar hash without building it, and
  electron-builder output isn't guaranteed byte-reproducible). Version-string equality is a human-readable
  secondary. **Bootstrap note:** the currently-installed `0.15.1` app predates the SHA-embed and carries no
  embedded SHA, so the *first* run's identity gate falls back to the version-string secondary (0.15.1 ≠
  0.17.0 → correctly caught); SHA-based "already current" only holds once a SHA-embedded build is installed.
- **D-4: Runbook is a script + skill, driven per-Mac.** One `desktop-update.sh --host <self|mbp> [--ref
  <git-ref>] [--force]`; for the MBP it SSHes to `192.168.1.88`. Mirrors the fleet-rollout discipline in
  `safe-gateway-restart` (back up → change → gate → prove). The build+install+freshness+backend-precondition
  steps are fully **unattended**; only the final sign-in is supervised (D-2).
- **D-5: The deploy ref is explicit, not a moving `HEAD` (folds pass-2 RC-6).** `--ref` defaults to the
  deploy tree's current `fork/main` tip (via `runtime_tree_status.py`); the version/identity gate compares
  against **that pinned ref's** build, so "already current" is judged against a fixed target.
- **D-6: Fold into the update runbook + `deploy.sh --desktop`.** The deliverable updates the `hermes-agent`
  skill (desktop-update step + pointer) and extends `hermes-desktop-remote-backend`; `deploy.sh --desktop`
  (hook already present) calls the same `desktop-update.sh` — single implementation, two entry points. Run
  under `deploy.sh`, a hard FAIL (build/install/precondition) routes LOUD to #alerts; a
  PENDING-SUPERVISED-SIGNIN is a quiet, honest "waiting for Ace" status, not a failure.
- **D-7: CONFIRMED is a durable artifact written by the AGENT, not the script (folds pass-4 change-2 + 4).**
  The `desktop-update.sh` script writes a per-Mac record `~/.hermes/state/desktop-signin/<host>.json`
  (`{ref, sha, version, backend, precondition, signin_state, ts}`) with `signin_state=PENDING-SUPERVISED-SIGNIN`
  on every run — a shell script cannot block on Ace's out-of-band "signed-in" reply. When Ace attests he
  signed in, **Apollo (the agent)** flips that record to `CONFIRMED` (with the attest timestamp). Under
  `deploy.sh --desktop` (non-interactive) the record stays PENDING by construction — honest. **MBP launch is
  degraded (change-4):** launching a GUI app over a non-interactive SSH session hits the same Aqua/window-
  server dependency the descope did NOT remove (it removed *driving* the renderer, not *needing an Aqua
  session to show it*). So for the MBP the script does **install + quarantine-clear + record PENDING with a
  "launch & sign in on the MBP" instruction** — it does NOT assert it left the app foregrounded. On Studio
  (this host, logged-in Aqua session) it can `open` the app for Ace directly.

## 5. Architecture / Design
```
desktop-update.sh --host <self|mbp> [--ref <git-ref>] [--force]
  ├─ 0. resolve target (self = Mac Studio local; mbp = ssh alexgierczyk@192.168.1.88);
  │        resolve --ref (default: deploy tree fork/main tip via runtime_tree_status.py) [D-5]
  ├─ 1. PRECONDITION GATES (fail fast, before the long build):
  │        repo checkout + node/npm toolchain present on target; monorepo workspace root resolved;
  │        free disk >= electron-builder output need;
  │        BACKEND SIGNABLE: read this Mac's connection.json backend -> curl /api/status (correct Host
  │          header) => auth_required:true, and /api/auth/providers contains `basic`.
  │          FOR --host mbp THIS CURL RUNS ON .88 OVER THE SSH SESSION [change-3] (exercises the MBP's own
  │          Tailscale/MagicDNS/Host-allowlist path — a Studio-side curl would prove Studio's reachability,
  │          not the MBP's) -- LOUD w/ cause
  ├─ 2. sync checkout to --ref (git FF), npm ci at workspace root (INV-4)
  ├─ 3. IDENTITY GATE (INV-1/INV-5) -- AFTER the FF, against `git rev-parse --ref` (free, no build) [D-3]:
  │        installed app's EMBEDDED GIT SHA == pinned ref's SHA & !--force
  │          -> "already current", skip rebuild, jump to step 6 staging
  │        (first run: 0.15.1 app has no embedded SHA -> version-string fallback 0.15.1 != ref -> not current)
  ├─ 4. npm run dist:mac -> release/mac-arm64/Hermes.app (embeds source git SHA at build time)
  ├─ 5. install over old (INV-5): quit app; mv old -> /Applications/.hermes-app-backups/Hermes.app.old-<ts>;
  │        prune backups keep-last-N (N=3); ditto new -> /Applications; xattr -dr com.apple.quarantine
  │        assert: installed embedded-SHA == pinned ref's SHA (INV-1); app.asar sha256 == just-built asar
  │          (post-install integrity check ONLY, not a pre-build key); version string == ref (secondary)
  ├─ 6. node --test the desktop electron suite (test:desktop:platforms) -- mechanics (unattended)
  ├─ 7. SUPERVISED SIGN-IN STAGING (INV-2, D-2/D-7): write ~/.hermes/state/desktop-signin/<host>.json with
  │        signin_state=PENDING-SUPERVISED-SIGNIN. Studio (live Aqua): `open` the freshly-installed app for
  │        Ace + print exactly what to do ("sign in; reply `signed-in`/`still-broken`"). MBP (SSH, no Aqua):
  │        install+quarantine-clear + print "launch & sign in on the MBP" -- do NOT assert foregrounded
  │        [change-4]. The SCRIPT only ever writes PENDING; the AGENT flips it to CONFIRMED on Ace's attest.
  └─ 8. report: per-mac table (preconditions, embedded-SHA, version, mechanics, sign-in [CONFIRMED|PENDING]).
           Under deploy.sh (non-interactive): hard FAIL -> LOUD #alerts; PENDING (always, no attester) -> quiet.
```

## 6. Implementation Phases

- **Phase 0 — Ground-truth (DONE 2026-07-01, §0.1; evidence `/tmp/desktop-review/phase0-groundtruth.json`).**
  Measured on BOTH Macs: version skew; non-loopback OAuth/`basic` on Studio AND MBP (connection.json read
  live); Studio gateway is a LaunchAgent; `runtime_tree_status.py` + `deploy.sh --desktop` exist.
- **Phase 1 — build identity + precondition gates + rebuild/reinstall (INV-1/-4/-5), Studio first.**
  - *Build-identity plumbing:* confirm/where-needed add the source git SHA embedding at `dist:mac` build
    time (electron-builder `extraMetadata` or a generated `build-info.json` inside the asar), and the
    read-back path (installed app's embedded SHA + `shasum -a 256` of `app.asar`). This is the D-3 mechanism.
  - *Unit/script:* precondition gates (toolchain/disk/workspace-root/backend-signable) fail fast BEFORE the
    build; `npm ci && npm run dist:mac` exits 0; **identity gate runs AFTER the git FF** against the pinned
    `--ref`; post-install embedded-SHA/asar-hash == pinned build's; version string == ref (secondary).
  - *Negative/adversarial:* a dirty lockfile STOPS the run; a same-version-different-SHA build is detected as
    NOT current (the B2″ case); a missing precondition fails LOUD at the gate.
  - *Verify with:* `bash desktop-update.sh --host self` → identity table equal; old app in the backup dir.
- **Phase 2 — supervised sign-in staging (INV-2/-3), Mac Studio.**
  - *Unit/script:* after install, the run launches the app and prints the exact one-time instruction;
    records CONFIRMED (on Ace's attestation) or PENDING-SUPERVISED-SIGNIN. No password is handled by the
    script (INV-3). Backend-signable precondition already passed in Phase 1.
  - *Verify with:* `bash desktop-update.sh --host self` → "sign-in: PENDING (app staged)" then, after Ace
    signs in and confirms, the recorded state flips to CONFIRMED.
- **Phase 3 — MacBook Pro (remote client) over SSH.**
  - *Unit/script:* `--host mbp` SSHes to `.88`, runs the precondition gates (incl. its measured
    `mac-studio-m3u:9119` backend), rebuilds/installs, asserts build identity — **all unattended**.
  - *Supervised sign-in:* same as Studio — the app is staged on the MBP; Ace signs in there once. (No
    Aqua-session automation, no `launchctl asuser` renderer drive — that was the descoped tower.)
  - *Negative/adversarial:* MagicDNS off / Host-allowlist 400 / NordVPN full-tunnel → the precondition gate
    fails LOUD with the specific cause (`hermes-desktop-remote-backend` ladder), before the long build.
  - *Verify with:* `bash desktop-update.sh --host mbp` → identity table + sign-in PENDING/CONFIRMED.
- **Phase 4 — fold into the update runbook + skills + `deploy.sh --desktop` + alert routing.**
  - *Unit/script:* `hermes-agent` skill gains a "desktop app update" step pointing at the script;
    `hermes-desktop-remote-backend` gains the backend-signable precondition; `deploy.sh --desktop` calls the
    same `desktop-update.sh`; a hard FAIL under `deploy.sh` routes LOUD to #alerts, PENDING stays quiet.
  - *Verify with:* `grep -c desktop-update.sh` in the runbook skill ≥ 1; a forced hard-FAIL emits an #alerts
    post; a PENDING run does not.

## 7. Security / Privacy / Ops
- **No credential handled by the runbook** (INV-3) — the supervised model means Ace types his password into
  the app himself; the script only runs unauthenticated `/api/status` + `/api/auth/providers` probes. This
  is strictly *less* credential surface than the descoped stdin-password design.
- Backups: old app → versioned dir `/Applications/.hermes-app-backups/` with timestamp (INV-5), NOT Trash;
  restore re-clears quarantine and launch-verifies.
- Ops posture matches the harness deploy: **deliberate**, operator-run, idempotent, reversible; the rebuild
  does not restart any gateway (app-only). Under `deploy.sh --desktop`: hard FAIL → LOUD #alerts; a
  PENDING-SUPERVISED-SIGNIN is an honest quiet "waiting for Ace," never dressed up as a pass or a fail.

## 8. Risks & Mitigations
- **R-1: the MBP can't reach the backend** (MagicDNS off / Host-allowlist 400 / NordVPN full-tunnel).
  *Mitigation:* the backend-signable precondition gate (before the build) runs the
  `hermes-desktop-remote-backend` diagnostic ladder and fails LOUD with the specific cause; correct Host
  header + reachability checked up front. MBP backend measured live (§0.1) so the target is known.
- **R-2: build carries new code but the version string didn't bump** → a version-only check would call it
  "current." *Mitigation:* INV-1/D-3 key on the **embedded git SHA** (the asar hash is a post-install
  integrity check only, not the pre-build skip key — pass-4 change-1), so same-version drift is detected
  (folded pass-3 B2″).
- **R-3: build-identity embedding doesn't exist yet in `dist:mac`.** *STATUS 2026-07-01: ALREADY BUILT —
  R-3 largely dissolved.* Ground-truthing for the plan found `apps/desktop/scripts/write-build-stamp.cjs`
  runs as part of `npm run build` (before `dist:mac`) and writes `build/install-stamp.json`
  (`{schemaVersion, commit:<40-char git SHA>, branch, builtAt, dirty, source}`), shipped into the packaged
  app via `extraResources` → `/Applications/Hermes.app/Contents/Resources/install-stamp.json`, and read back
  by `electron/main.cjs` (packaged path → dev fallback), surfaced as `installStamp`. **So the SHA embed +
  read-back the PRD assumed I'd build already exists** — the runbook just `cat`s the installed stamp's
  `commit` and compares to `git rev-parse <ref>`. Verified live: the installed app's stamp is `6351c7ed`
  (2026-06-25, stale ancestor of HEAD) — the identity gate fires correctly. Residual: the `dirty` flag
  handling (a locally-built `dirty:true` stamp is a valid-but-non-reproducible identity) is a plan detail,
  not a blocker.
- **R-4: rebuild over SSH needs the Mac awake + a long build.** *Mitigation:* background the build, poll a
  sentinel, keep the host awake (existing skill guidance); preconditions fail fast so a doomed run doesn't
  burn the build first.
- **Rollback:** `xattr -dr com.apple.quarantine` + `ditto /Applications/.hermes-app-backups/Hermes.app.old-<ts>
  /Applications/Hermes.app`, then verify the restored app launches. (Not Trash — INV-5.)

## 9. Open Questions
- **OQ-1 → RESOLVED (measured):** both Macs non-loopback OAuth/`basic` (Studio + MBP connection.json read
  live, §0.1). No assumed backend remains.
- **OQ-2 → RESOLVED: sign-in is SUPERVISED (Ace, 2026-07-01).** The unattended-renderer-drive requirement is
  dropped; Ace signs in once per Mac after an update and the run records it. This is the load-bearing scope
  decision that collapses the v0.3 tower.
- **OQ-3 → RESOLVED: freshness keyed on build identity (SHA/asar hash), not the version string.**

## 10. Acceptance Criteria
- [ ] **AC-1 (INV-1, build identity):** after `desktop-update.sh --host self` and `--host mbp`, each Mac's
  installed app **embedded git SHA == the pinned `--ref`'s SHA** (the pre-build skip + INV-1 key), the
  post-install `app.asar` sha256 matches the just-built asar (integrity check), AND
  `CFBundleShortVersionString` == the ref's `package.json` version (human-readable secondary). Evidence: the
  report table shows the installed embedded-SHA equal to `git rev-parse <ref>` on both Macs (a
  same-version-different-SHA build is caught). *(First run caveat: the pre-embed `0.15.1` app has no embedded
  SHA → version-string fallback catches `0.15.1 ≠ ref`; SHA-based "already current" holds from the next
  SHA-embedded build on.)*
- [ ] **AC-2 (INV-2, supervised sign-in — honestly recorded, script vs agent):** the **script** writes
  `signin_state=PENDING-SUPERVISED-SIGNIN` to `~/.hermes/state/desktop-signin/<host>.json` on every run; the
  **agent** flips it to **CONFIRMED** (with attest ts) only when Ace attests he signed in. The
  backend-signable precondition is reported separately and **never** as the sign-in proof. Under
  `deploy.sh --desktop` the state is PENDING by construction (no attester) — honest, not a fail. Evidence:
  the per-Mac record showing the script wrote PENDING and (when applicable) the agent's CONFIRMED flip; a
  run with an unsignable backend fails the precondition, not a fake sign-in pass.
- [ ] **AC-3 (INV-3, no credential handled):** gitleaks-clean; the runbook reads/pipes/logs no password
  anywhere (supervised model). Evidence: scan + a grep of the script showing only unauthenticated probes.
- [ ] **AC-4 (INV-5, reversible + retention-bounded):** a second run on a current Mac (same embedded SHA)
  prints "already current" and skips the rebuild; after a real update the versioned backup exists, **only
  the last N=3 are retained** (older pruned), AND a rehearsed restore (quarantine re-clear) **launch-verifies
  beyond process-exists** (renderer reaches ready). Evidence: idempotency output + backup-dir listing showing
  ≤N + restore launch-verify.
- [ ] **AC-5 (INV-4):** the rebuild uses `npm ci` at the correct workspace root; a dirty lockfile STOPS the
  run (no silent `npm install`). Evidence: the run log.
- [ ] **AC-6 (mechanics):** `test:desktop:platforms` node suite green on each Mac post-install. Evidence:
  `node --test` run.
- [ ] **AC-7 (precondition gates):** a missing precondition (no toolchain / insufficient disk / unsignable
  or unreachable backend / wrong Host) fails LOUD at the gate **before** the build starts. Evidence: a
  forced-missing-precondition run exits at the gate with the specific cause.
- [ ] **AC-8 (alert routing, D-6):** run under `deploy.sh --desktop`, a hard FAIL posts LOUD to #alerts; a
  PENDING-SUPERVISED-SIGNIN stays quiet (honest, not a failure). Evidence: a forced hard-FAIL produces an
  #alerts message; a PENDING run does not.
- [ ] **AC-9 (MBP precondition runs in the MBP's network context, pass-4 change-3):** for `--host mbp`, the
  backend-signable `curl` executes **on `.88` over the SSH session** (not from Studio against the MBP URL),
  so it exercises the MBP's own Tailscale/MagicDNS/Host-allowlist path. Evidence: the run log shows the curl
  ran MBP-side; a MBP-only network fault (reachable from Studio, not from MBP) fails the gate.

## 11. Roadmap (post-v0.5)
| Ships | Trigger |
|---|---|
| unattended renderer-drive sign-in proof (the descoped v0.3 tower — CDP/Playwright + fuses/Aqua spike) | Ace ever wants hands-off sign-in verification; v0.3 is filed in git history as the reference design |
| Windows/Linux desktop packaging arm (`dist:win`/`dist:linux`) | a non-Mac client needs it |
| in-app auto-update feed | Ace wants hands-off desktop updates |

## 12. Review Log
- **Pass-1 (Opus, BLOCK) → v0.2.** B1 (curl arm bypasses the app) → renderer-level proof; B2 (Studio auth
  shape) → Phase-0 measured non-loopback OAuth, OQ-1 refuted → both-Macs OAuth; B3 (unattended GUI over SSH)
  → session-gated; B4 (Trash rollback) → versioned backup dir. Plus version-gate-after-FF, stdin creds,
  precondition gates.
- **Pass-2 (Opus, BLOCK) → v0.3.** B1′ (jsdom escape hatch + spawn≠drive) → removed the escape hatch, added
  a renderer-driver spike gate; B2′ (throwaway identity) → measured: `basic` has no account lockout (per-IP
  10/60s), single credential → fresh-profile wrong-cred arm; RC-4 (MBP backend) → Phase-3 gate; RC-5
  (strings-grep overclaim) → corrected; RC-6 (`--ref`); RC-7 (MBP cred source).
- **Pass-3 (Opus, BLOCK) → DESCOPE (v0.4), Ace's call.** Pass-3 confirmed all pass-2 blockers resolved but
  surfaced B1″ (persisted-cookie-vs-fresh-profile: the automated sign-in test either fake-greens on a stale
  cookie or proves only capability) and B2″ (version-string freshness misses same-version SHA drift). B1″
  was the **recurrence signal** — the same "what does 'prove sign-in' mean" problem-class relocated three
  passes deep, with a real feasibility residual (can the ad-hoc-signed packaged Electron renderer even be
  driven; fuses/Aqua). Per `descope-when-review-recurses`, the move was to name the load-bearing requirement
  (*unattended automated renderer-drive*) and offer Ace a scope fork rather than grind a 4th pass. **Ace
  chose (B): supervised one-time sign-in.** The tower (driver spike, fuses, Aqua gating, profile-subject
  ambiguity, all of v0.3's INV-2/D-2/D-6/Phase-0.5/AC-2/AC-3/AC-11) is removed. **B2″ folded cleanly** into
  INV-1/D-3 (build-identity keying) — kept because it's a real correctness fix independent of the descope.
  Also measured live this pass (closing pass-3 residuals): MBP `connection.json` (oauth ✓), `launchFresh()`
  isolates via its own temp userData ✓, Studio gateway is a LaunchAgent ✓, `runtime_tree_status.py` +
  `deploy.sh --desktop` exist ✓.
- **Pass-4 (Opus, lean re-review of v0.4 → APPROVE-WITH-CHANGES = CONVERGED, v0.5).** Confirmed the descope
  *dissolved* B1″ (a human certifies → no renderer to drive, no profile-subject ambiguity, no debug-port RCE
  window) rather than relocating it, and B2″ is genuinely folded. The 5 required changes were the small
  threat-model corrections the descope skill predicts (not a new tower), all folded here: (1) SHA-embed is
  load-bearing for the pre-build skip, asar hash is post-install-integrity-only (D-3/AC-1); (2) the *script*
  only writes PENDING, the *agent* attests CONFIRMED to a durable record, `deploy.sh` path is
  PENDING-by-construction (INV-2/D-7/AC-2); (3) MBP precondition curl runs on `.88` over SSH, not from
  Studio (§5 step 1/AC-9); (4) launch-verify beyond `pgrep` + MBP launch degraded to install+instruct, no
  foregrounded claim over SSH (INV-5/D-7); (5) keep-last-N=3 backup retention (INV-5). Spec SHRANK v0.3→v0.4
  (+223/−336) then folded lean — convergence, not ballooning.
- **Next:** `prd-plan` on the approved v0.5 (Phase 1's first task = the git-SHA-embed plumbing, the one
  load-bearing unknown; everything else is bounded scripting against measured facts).
