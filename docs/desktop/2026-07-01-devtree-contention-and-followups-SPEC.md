# SPEC — Dev-tree contention hardening + desktop-update follow-ups

**Status:** DRAFT v0.1 — for Opus review → plan. Author: Apollo · 2026-07-01
**Origin:** loose ends from the desktop-update build closeout (Ace: "do all 3 [contention tiers], spec them
all out; spec out removing [the orphan venv]; spec out fixing [the Studio false-stale]").

## 1. Summary & Goal
Four related fixes, all born from the desktop-update `--apply` run mutating the **shared root dev checkout**
`~/.hermes/hermes-agent`:
- **A. Contested-checkout hardening (3 tiers, ALL in v0.1 per Ace 2026-07-01)** — stop any fleet script
  (starting with `desktop-update.sh`) from building on / committing to the shared root `main` checkout, which
  is the branch ~30 worktrees fork from and multiple agents touch. Tier-1 (build-in-worktree) + Tier-2
  (shared helper + lint) + Tier-3 (serialized FF-only `land-on-main.sh` writer + root-checkout working-surface
  guard).
- **B. Orphan-venv removal** — the 320M `~/.hermes/hermes-agent/venv` recreated during the desktop build,
  used by nothing (gateways run the runtime deploy venv).
- **C. Freshness false-stale fix** — `desktop-update.sh` keys freshness on the bare repo HEAD, so a
  docs-only commit marks the app "stale" though `apps/desktop/` bytes didn't change.

**Goal:** the desktop update (and any future live-artifact build) never mutates root `main`; freshness
reflects the app's real source; and the one-off orphan venv is gone with a guard so it can't silently
return.

## 2. Non-Goals
- **NOT** making the dev tree "deploy-only" like the runtime tree — the dev tree legitimately hosts active
  development (30 worktrees fork from `main`; PRs merge to `main`; fork-syncs land on `main`). Root `main`
  MUST keep advancing. **Tier-3 does NOT make root `main` "unwritable" or the root checkout structurally
  uneditable — a `--no-verify`-bypassable git hook provably cannot cage that (see `git-worktree-isolation`).
  Tier-3's honest guarantee is narrower: (a) SERIALIZE the local writers that adopt `land-on-main.sh` (no
  two racing FF/reset), and (b) DETECT (tripwire, not prevent) an in-place root-checkout mutation by a
  caller that didn't adopt it.** Automated callers that bypass the writer remain a contention source — the
  same bug class — so Tier-2's lint + the tripwire are how we *find* them, not a cage. (See D-5.)
- **NOT** changing the runtime deploy-only split (already shipped, separate system).
- **NOT** touching the ~30 existing feature worktrees or how PRs land (they keep forking from `main`).

## 3. Ground-truth (measured 2026-07-01, before design)
- **Root cause is self-inflicted, not a sibling:** the dev-tree reflog shows `checkout main→main` (04:19:51)
  → `reset to origin/main` (04:19:52) → `checkout 3fc83b17` (04:19:53), all within 2s — that was
  `desktop-update.sh`'s own `du_build_and_install` git dance during my `--apply`, NOT another agent.
- **The worktree-isolation pattern is already the norm:** `git worktree list` shows ~30 worktrees under
  `~/.hermes/worktrees/` + `~/Projects/wt*`, each a feature/fix branch. The gap is only that **root `main`
  itself is used as an ad-hoc build/commit surface.**
- **`fork-drift-check.py` is READ-ONLY** (fetch + rev-list; never merges/resets) — confirmed; it is NOT a
  contention source.
- **No git hook or desktop script recreates the venv:** dev-tree `.git/hooks` has only `pre-commit`;
  `apps/desktop/scripts/*.cjs` contain zero `venv`/`python -m venv` refs; no `post-checkout`/`post-merge`
  hook. So the 320M venv's 04:19:53 recreation was a transient side-effect of the earlier accidental
  bare-run build chain — it will NOT self-regenerate. `lsof +D` shows nothing holds it open.
- **Freshness sensitivity:** bare `HEAD = 0dd61f6b8` but `last commit touching apps/desktop/ = 44ddc552f`.
  A docs-only commit advances HEAD without changing app bytes → keying on bare HEAD over-reports "stale."
- **The runtime deploy venv is what gateways use** (`~/.hermes/runtime/hermes-agent/venv`); the dev-tree
  venv is not imported by any gateway (verified in the deploy-split closeout).
- **The dev tree is an ACTIVE-dev repo, unlike the runtime tree (decides Tier-3's shape):** `git log main`
  shows real PR merges landing on `main` (`#56330`, `#56325`) + fork-syncs + agent doc-commits; 30
  worktrees fork from `main`. So `main` MUST keep advancing — Tier-3 cannot make it read-only; it can only
  make the *root checkout* never be a working surface. There is NO autocommit on the `hermes-agent` repo
  (the `[home-autocommit]` hook is on the separate `~/.hermes` repo); the only mutators of root `main` are:
  (a) `git merge --ff-only fork/main` fork-syncs, (b) PR merges, (c) ad-hoc agent commits/checkouts (the
  contention source). A `pre-commit` hook already exists (runtime-tree deploy-only guard, worktree-aware).
- **`flock` is an established fleet pattern** (`codex-token-keeper.py`, `autocommit-hermes-home.sh`,
  `dns-resilience-snapshot.py`) — reusable for Tier-3's serialized writer.

## 4. Layer Analysis — where each fix must live (§5A-style)
| Concern | Layer | What it CAN fix | What it CANNOT fix | Touches shared root main? |
|---|---|---|---|---|
| A-Tier1: desktop build isolation | `desktop-update.sh` `du_build_and_install` | build in a throwaway worktree, never touch root `main` | other scripts that touch root `main` | removes the touch |
| A-Tier2: convention + tooling | a `fleet/dev-tree-guard.sh` helper + docs | give every script a "build-in-worktree" helper + a lint that flags root-main mutation | can't force adoption (no pre-commit that works — proven) | no |
| A-Tier3: dev-tree deploy-only split | new architecture (roadmap, own review) | make root `main` read-only like the runtime tree | large blast radius; needs Ace sign-off | restructures it |
| B: orphan venv removal | one-shot `rm -rf` + a guard | delete the 320M orphan; guard re-creation | n/a | no (venv is gitignored) |
| C: freshness false-stale | `desktop-update.sh` `du_resolve_ref`/identity | key freshness on last `apps/desktop/` commit | n/a | no |

**The one thing that must live in each layer:** Tier-1 in the script (only the script's own git dance
caused the touch); Tier-3 in a new architecture (only restructuring the tree makes root `main` unwritable);
C in the freshness helper (only it decides the pin).

## 5. Resolved Decisions
- **D-1 (Ace): do all 3 contention tiers, Tier-3 INCLUDED IN THIS BUILD (not roadmap).** Ace's call
  2026-07-01: "that may happen, you just did it — let's do tier 3 now." Rationale: the contention is
  demonstrated (my own `--apply` did it), and other agents touch root `main`, so whack-a-mole (Tier-1 only)
  is insufficient. All of Tier-1/2/3 + B + C ship in v0.1.
- **D-2: Tier-1 uses a throwaway `git worktree`, not the root checkout.** `du_build_and_install` creates a
  detached worktree at the pinned ref under `~/.hermes/worktrees/desktop-build-<ts>`, runs `npm ci` +
  `dist:mac` there, installs, and `git worktree remove`s it — root `main` is never checked out, reset, or
  committed to. The MBP arm does the same over SSH (its own throwaway worktree), so the MBP hotfix-stash
  dance is no longer needed.
- **D-3: freshness keys on the last commit that touched the app's build inputs**, not bare HEAD.
  Per D-6 (OQ-2 resolved), the pin paths are `apps/desktop` **+ `package-lock.json`**, confirmed against
  `dist:mac`'s actual dependency graph in Phase 1. Docs-only commits no longer mark the app stale.
- **D-4: orphan-venv removal is a one-shot with a standing guard.** `rm -rf ~/.hermes/hermes-agent/venv`
  after re-proving nothing holds it + no gateway references it; then a cheap guard (a line in the existing
  fleet health check) that flags if a venv reappears in the dev tree.
- **D-5: Tier-3 = SERIALIZE-the-adopters + DETECT-the-rest — NOT a structural cage and NOT a runtime-style
  deploy-only tree.** Honest guarantee (per BL-1 fold): the writer serializes *local writers that adopt it*;
  the hook is a *tripwire that detects, not prevents* in-place root-checkout mutation (`--no-verify` and
  simple non-adoption both bypass it — a git hook provably cannot cage this). It measurably shrinks
  contention (the only known writer — `desktop-update.sh` — and the fork-sync path both adopt it) but does
  NOT eliminate the capability for a rogue/unported caller. Residual: any automated caller that doesn't
  route through `land-on-main.sh` is the same contention bug class; Tier-2's lint is how we find them.
  Concrete mechanism:
  1. **The root checkout stays parked on `main`; adopting callers never `checkout` it onto a working branch
     or commit in place.** All work — including doc/spec commits like this one — happens in a worktree, then
     lands on `main` via the single sanctioned writer.
  2. **`fleet/land-on-main.sh <branch|ref>` serialized FF-only writer:** acquires a non-blocking `flock`
     (bounded wait, LOUD fail on timeout — never an unbounded block; R-5) on
     `~/.hermes/hermes-agent/.git/main-land.lock`, then — **UNDER THE LOCK** — `fetch`es, re-checks
     FF-ability against the just-fetched `origin/main`/target, refuses a dirty root (exit 3) and a non-FF
     (exit 4) **loudly without ever resetting**, and on success FF-merges into `main`. The fetch+FF-check
     MUST be under the lock (not before acquiring it) so a server-side advance observed mid-op can't race a
     stale FF. **Local-writer serialization only** — see D-5.5 on PR merges.
  3. **Extended root-checkout tripwire (extend the existing worktree-aware `pre-commit`; add a `pre-checkout`-
     equivalent guard):** refuses (a) a commit authored in the root checkout without the writer's sentinel,
     and (b) a `checkout` moving the root off `main`. Worktrees are EXEMPT (existing hook is worktree-aware).
     Bypassable (`--no-verify`) by design — it's the tripwire half of D-5, not a cage.
  4. **`desktop-update.sh` (Tier-1) and every fleet script that advances `main` route through
     `land-on-main.sh`** instead of a raw `git merge`/`checkout`/`reset` on root.
  5. **Sentinel threat model (RC-5):** the env sentinel the writer sets is *forgeable* (`SENTINEL=1 git
     commit` bypasses it) — it gates *accidental* raw commits, NOT adversarial ones. It is an
     accident-guard, not a security control. It MUST be scoped so a worktree commit spawned inside a
     `land-on-main.sh`-invoked build cannot inherit it and mask a real violation (unset it before invoking
     any child build; the build runs in a worktree which is exempt anyway).
  6. **PR merges are OUT-OF-BAND (RC-2, BL-3):** a GitHub PR merge advances `origin/main` server-side; local
     root `main` moves only on the next `fetch`+FF. `land-on-main.sh` therefore serializes **local writers
     against each other**, NOT against GitHub. The writer treats an `origin/main` advance as out-of-band:
     it `fetch`es under the lock and FFs onto whatever the server now has, failing loudly (never resetting)
     if that makes the requested land non-FF. "PR merges route through the writer" is FALSE and was removed.
- **D-6 (Ace, OQ-2 resolved): freshness pin = `apps/desktop` + `package-lock.json`.** Confirm the exact
  build-input set by reading `dist:mac`'s dependency graph in Phase 1; if the build bundles more shared
  inputs, add those paths (grep-confirmed).

## 6. Implementation Phases

- **Phase 1 — C: freshness false-stale fix (smallest, unblocks a clean re-run).**
  - *BLOCKING GATE (RC-7):* before writing the pin, **enumerate and grep-confirm the COMPLETE build-input
    path set** by reading `dist:mac`'s actual dependency graph (`apps/desktop/package.json` `build`/`files`/
    `extraResources`, `electron-builder` config, and the workspace deps `dist:mac` bundles). The pin MUST
    cover every path whose change changes the built app. This is the correctness crux: a MISSING input makes
    freshness UNDER-report stale (ships nothing when it should rebuild) — strictly worse than over-reporting.
    Phase 1 does not proceed until the set is confirmed on disk. **Enumeration must be MECHANICAL, not
    hand-listed (Nit-2):** parse `electron-builder`'s `files`/`extraResources` + resolve workspace deps
    programmatically and record the method, so a future dep addition re-runs the same enumeration rather
    than trusting the v0.1 snapshot. (Caveat, stated honestly: this catches *known* build inputs; an unknown
    unknown outside the parsed graph can't be tested for — the mechanical re-run is the mitigation.)
  - *Unit/script:* `du_resolve_ref`/identity resolves the pin to the last commit touching the confirmed
    build-input set (`apps/desktop` + `package-lock.json` + any additions from the gate); a docs-only commit
    on top does NOT flip identity to stale.
  - *E2E:* on the live Studio (installed `3fc83b17`), after the docs-only `0dd61f6b8` HEAD, `desktop-update.sh
    --host self` reports **"already current"** (not "stale"), because the last build-input commit is
    still the installed one.
  - *Negative:* a real `apps/desktop/` change flips it to "stale → would rebuild."
  - *Negative (RC-7, build-input completeness):* a change to a bundled-but-initially-unpinned input (e.g. a
    workspace dep `dist:mac` bundles) MUST be caught by the enumeration step — i.e. the confirmation gate
    fails if such a path exists outside the pin. Prove the enumeration is complete, not just that the two
    obvious paths work.
  - *Verify with:* `bash ~/.hermes/fleet/desktop-update.sh --host self` → `GATE identity: PLAN — already current`.
- **Phase 2 — A-Tier1: build in a throwaway worktree (the concrete contention fix).**
  - *Unit/script:* `du_build_and_install` creates `~/.hermes/worktrees/desktop-build-<ts>` at the pinned
    ref, builds there, removes it; root `main` `git rev-parse HEAD` + `git status` are byte-identical
    before and after an `--apply` run (proven by capturing both).
  - *E2E:* a real `--apply` on Studio leaves root `main` HEAD unchanged (the reflog shows no `main`
    checkout/reset), app still installs correctly.
  - *Negative:* worktree cleanup runs even on build failure (trap/finally); a leftover
    `desktop-build-*` worktree is pruned on next run.
  - *Verify with:* capture `git -C ~/.hermes/hermes-agent rev-parse HEAD` + reflog before/after `--apply`;
    assert HEAD unchanged and no `main` reset in reflog.
- **Phase 3 — A-Tier2: shared build-in-worktree helper + root-main-mutation lint.**
  - *Unit/script:* extract the worktree-build into `fleet/dev-tree-guard.sh` (reusable
    `with_ephemeral_worktree <ref> <cmd>`); a lint (`grep`-based) flags any `fleet/`/`scripts/` script that
    does `git -C <root-dev-tree> (checkout|reset|merge|commit)` without going through the helper.
  - *Verify with:* the lint run over `~/.hermes/fleet` + `~/.hermes/scripts` returns 0 offenders after
    `desktop-update.sh` adopts the helper.
- **Phase 4 — B: orphan-venv removal + guard.**
  - *Unit/script:* re-prove `lsof +D ~/.hermes/hermes-agent/venv` empty AND no gateway plist/script
    references it; then `rm -rf`; add a health-check line that warns if a `venv` reappears in the dev tree.
  - *E2E:* after removal, all 6 gateways still healthy (they use the runtime venv — unchanged); a fleet
    health run is clean.
  - *Verify with:* `ls ~/.hermes/hermes-agent/venv` → absent; gateways `state=running`.
- **Phase 5 — A-Tier3: serialized FF-only writer for root `main` + root-checkout working-surface guard (D-5, IN SCOPE).**
  - **Sequencing (RC-4):** land the writer + refusal gates (AC-8/AC-9) and the tripwire (AC-10) FIRST; do the
    live fork-sync-path cutover (E2E below, AC-11) LAST, only after those pass. Rollback for the cutover:
    revert the one runbook line — raw `git merge --ff-only fork/main` still works as the fallback path.
    **The rollback path MUST NOT be caged by the checkout-guard it's meant to bypass (Nit-1):** when root is
    already parked on `main`, `git merge --ff-only fork/main` needs NO `git checkout` and authors no commit
    object in root, so neither the checkout-guard (D-5.3.b) nor the commit-sentinel guard (D-5.3.a) fires —
    assert this explicitly in the rollback runbook (a raw FF-only fork-sync from a parked-on-`main` root
    succeeds under the tripwire).
  - *Unit/script:* `fleet/land-on-main.sh <branch|ref>` acquires the `flock`, then UNDER THE LOCK
    `fetch`es + FF-checks, refuses a dirty root (exit 3), refuses a non-FF (exit 4) without resetting, and on
    success FF-merges into `main` with the sentinel set; two concurrent invocations serialize (the second
    waits for the lock, then FFs on top or no-ops) — proven with two background invocations racing.
  - *Concurrency realism (RC-3, BL-2):* a race test that runs `land-on-main.sh` **concurrent with an
    ephemeral build worktree (`with_ephemeral_worktree`)** AND a **simulated external `origin/main` advance**
    (a pushed commit fetched mid-op) — the demonstrated failure mode was a build/checkout dance racing a ref
    advance, not two lands. Assert: root `main` ends FF-advanced or loud-refused (never reset), the build
    worktree is unaffected, and no interleaving corrupts root `main`.
  - *E2E (do LAST, RC-4):* migrate this repo's own doc-land flow + the fork-sync flow to `land-on-main.sh`; a
    real land advances `main` with the root checkout never leaving `main` (reflog shows no `checkout`-to-branch
    on root).
  - *Negative/adversarial:* (a) a raw `git commit` in the root checkout WITHOUT the writer's sentinel is
    refused by the extended `pre-commit` guard (exit≠0); (b) a `git checkout <branch>` on the root is
    refused; (c) both are EXEMPT in a worktree — prove a worktree commit still succeeds, testing **both**
    worktree locations (`~/.hermes/worktrees/*` AND any `~/Projects/wt*` form, RR-1). Guard is
    `--no-verify`-bypassable (tripwire, not a cage) per the runtime-guard precedent.
  - *Disk-headroom (RR-3):* a failed `npm ci` / near-full-filesystem mid-build MUST still leave root `main`
    pristine (Tier-1's core invariant) — assert HEAD+status unchanged after an induced build failure.
  - *Verify with:* the race test (concurrent land + build + external advance → serialize, `main` FF or
    loud-refuse, no reset); a root-checkout raw-commit attempt → refused; a worktree commit (both locations)
    → succeeds.

## 7. Security / Ops
- All changes are reversible: Tier-1/2 change *where* the build runs (worktree vs root); C changes the
  freshness pin; B deletes a gitignored orphan with a pre-delete safety re-prove; Tier-3's writer/guard are
  additive (the `flock` + hook only *serialize/refuse* — they never rewrite history; `--no-verify` remains
  an escape hatch).
- **Tier-3 serializes LOCAL writers only; it does NOT block PR merges (they're server-side, out-of-band —
  BL-3/D-5.6) or fork-syncs (those route THROUGH `land-on-main.sh` as FF, not forbidden).** The guard
  refuses only *in-place working-surface* mutation of the root checkout, and only as a bypassable tripwire.
- **Tier-3 interaction with the existing `pre-commit` runtime-guard:** extend that same hook (don't add a
  competing one); preserve its worktree-awareness so the 30 feature worktrees are unaffected.

## 8. Risks & Mitigations
- **R-1: a throwaway worktree leaks disk if cleanup is skipped.** *Mitigation:* `trap 'git worktree remove
  --force' EXIT` + a prune of stale `desktop-build-*` worktrees on each run (keep-none). Ensure the trap
  captures build logs BEFORE removing the worktree on the failure path (RR-2).
- **R-2: the MBP over-SSH worktree build needs the same isolation.** *Mitigation:* D-2 applies the worktree
  pattern to the MBP arm too; the hotfix-stash dance is retired (nothing to stash if root `main` is never
  checked out).
- **R-3: freshness pin misses a build input made OUTSIDE `apps/desktop` that the build bundles**
  (e.g. a workspace dep or shared `package-lock.json`). *Mitigation:* D-6 + Phase-1 BLOCKING GATE (RC-7):
  enumerate + grep-confirm the COMPLETE `dist:mac` build-input set on disk before writing the pin; a
  bundled-but-unpinned input fails the confirmation. Under-reporting stale is the dangerous direction.
- **R-4 (Tier-3): the writer/guard could block a legitimate land or fight the existing hook.** *Mitigation:*
  fork-syncs route THROUGH the writer as FF (not forbidden); PR merges are out-of-band and NOT expected to
  route through it (D-5.6); extend the existing worktree-aware `pre-commit` rather than adding a second hook;
  keep `--no-verify` as the escape hatch; prove a worktree commit still succeeds (negative test c).
- **R-5 (Tier-3): a `flock` held by a crashed writer wedges all lands.** *Mitigation:* `flock` auto-releases
  on holder death (kernel-managed, same as the safe-restart single-flight); use a non-blocking acquire with
  a bounded wait + LOUD failure, never an unbounded block.
- **R-6 (Tier-3): can't force human/other-agent adoption of the writer (this is the honest limit, BL-1).**
  *Mitigation:* the guard is a tripwire (refuses raw root commits/checkouts — accident-grade, `--no-verify`-
  and non-adoption-bypassable), `land-on-main.sh` is the easy sanctioned path, and the lint (Tier-2) flags
  any fleet script still doing raw root mutation. Full structural enforcement (removing the capability) is
  explicitly a non-goal / future item — a git hook provably can't cage this (see `git-worktree-isolation`
  "a git hook CANNOT enforce it"). **The spec claims serialization-for-adopters + detection, nothing more.**
- **R-7 (B venv guard, RC-6): a health-check line DETECTS, doesn't PREVENT, a reappeared venv.** *Mitigation:*
  acceptable (detection-only, stated honestly); the guard matches `venv` at the **dev-tree root
  specifically** (`~/.hermes/hermes-agent/venv`), NOT any `venv` under it (worktrees may legitimately create
  their own); on match it routes to the health check's existing alert channel (#alerts) so a 320M reappearance
  actually surfaces, not just logs.

## 9. Open Questions
- **OQ-1 → RESOLVED (Ace, 2026-07-01): do Tier-3 now.** "That may happen, you just did it — let's do tier 3
  now." Tier-3 is in v0.1 scope (Phase 5), mechanism per D-5.
- **OQ-2 → RESOLVED (Ace): freshness pin = `apps/desktop` + `package-lock.json`** (D-6), full build-input
  set confirmed against `dist:mac` in Phase 1.
- **OQ-3 (recommendation, pass-1 reviewer CONCURRED):** `land-on-main.sh` becomes the sanctioned path for
  BOTH fork-syncs and agent/script lands (update the `hermes-fork-pr-contribution` runbook to call it) — a
  manual `git merge --ff-only` bypass reintroduces the exact race. Note (BL-1/D-5.6): this only helps where
  adopted, and PR merges remain out-of-band by nature; the writer serializes *local adopters*, nothing more.
  Ace's call if he wants to keep the manual path as an explicit break-glass.

## 10. Acceptance Criteria
- [ ] **AC-1 (C):** after a docs-only HEAD advance, `desktop-update.sh --host self` reports "already current"
  when the installed SHA == last `apps/desktop`+`package-lock.json` commit. Evidence: the live PLAN output.
- [ ] **AC-2 (A-Tier1):** an `--apply` run leaves root `main` HEAD + `git status` unchanged and the reflog
  shows no `main` checkout/reset. Evidence: before/after capture + reflog grep.
- [ ] **AC-3 (A-Tier1):** the app still installs correctly from the worktree build (version + SHA asserted).
  Evidence: post-install identity gate green.
- [ ] **AC-4 (A-Tier2):** the root-main-mutation lint returns 0 offenders across `fleet/`+`scripts/`.
  Evidence: lint run output.
- [ ] **AC-5 (B):** `~/.hermes/hermes-agent/venv` is absent; all 6 gateways `state=running`; a fleet health
  run is clean. Evidence: `ls` + gateway states.
- [ ] **AC-6 (B guard):** a re-created dev-tree venv is flagged by the health check. Evidence: touch a fake
  `venv/` → health check warns → remove.
- [ ] **AC-7 (worktree cleanup):** no `desktop-build-*` worktree remains after a run (success OR failure).
  Evidence: `git worktree list` clean post-run.
- [ ] **AC-8 (A-Tier3 serialized writer):** two concurrent `land-on-main.sh` invocations serialize (no race,
  no reset); `main` ends FF-advanced. Evidence: race-test output + `main` reflog shows FF, no reset.
- [ ] **AC-9 (A-Tier3 dirty/non-FF refusal):** `land-on-main.sh` refuses a dirty root (exit≠0) and a non-FF
  ref (exit≠0), mutating nothing. Evidence: both refusal runs + unchanged `main`.
- [ ] **AC-10 (A-Tier3 root-checkout guard):** a raw `git commit`/`git checkout <branch>` in the root
  checkout (no writer sentinel) is refused by the hook; the SAME operation in a worktree succeeds. Evidence:
  refused-in-root + succeeds-in-worktree runs.
- [ ] **AC-11 (A-Tier3 land migration):** the fork-sync + agent doc-land flows route through
  `land-on-main.sh` (grep the runbook/scripts); a real land advances `main` without the root leaving `main`.
  Evidence: grep + reflog. **Do LAST (RC-4), after AC-8/9/10 pass; rollback = revert the runbook line.**
- [ ] **AC-12 (A-Tier3 concurrency realism, RC-3/BL-2):** `land-on-main.sh` running concurrent with an
  ephemeral build worktree AND a simulated external `origin/main` advance ends with root `main` FF-advanced
  or loud-refused (never reset), build worktree unaffected. Evidence: race-test output.
- [ ] **AC-13 (C build-input completeness, RC-7):** the Phase-1 enumeration gate confirms the COMPLETE
  `dist:mac` build-input set on disk; a bundled-but-unpinned input fails the confirmation. Evidence:
  enumeration output + a negative check proving an unpinned bundled path is caught.
- [ ] **AC-14 (A-Tier1 disk-headroom, RR-3):** an induced `npm ci`/build failure mid-run leaves root `main`
  HEAD+status pristine. Evidence: before/after capture across a forced-fail build.
- [ ] **AC-15 (A-Tier3 worktree-exempt both locations, RR-1):** a worktree commit succeeds in BOTH
  `~/.hermes/worktrees/*` and a `~/Projects/wt*`-style path (the hook's worktree-detection covers both).
  Evidence: two worktree-commit runs.

## 11. Roadmap
| Version | Ships | Trigger |
|---|---|---|
| v0.1 | Tier-1 (build-in-worktree) + Tier-2 (helper+lint) + **Tier-3 (serialized `land-on-main.sh` writer + root-checkout tripwire)** + B (orphan venv) + C (freshness) | now (Ace: do Tier-3 now) |
| future | remove the raw-shell/`git` capability from agent profiles entirely (the structural cage a git hook can't provide) | if the tripwire proves insufficient |
