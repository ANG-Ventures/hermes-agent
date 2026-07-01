# SPEC — Dev-tree contention hardening + desktop-update follow-ups

**Status:** DRAFT v0.1 — for Opus review → plan. Author: Apollo · 2026-07-01
**Origin:** loose ends from the desktop-update build closeout (Ace: "do all 3 [contention tiers], spec them
all out; spec out removing [the orphan venv]; spec out fixing [the Studio false-stale]").

## 1. Summary & Goal
Four related fixes, all born from the desktop-update `--apply` run mutating the **shared root dev checkout**
`~/.hermes/hermes-agent`:
- **A. Contested-checkout hardening (3 tiers)** — stop any fleet script (starting with `desktop-update.sh`)
  from building on / committing to the shared root `main` checkout, which is the branch ~30 worktrees fork
  from and multiple agents touch.
- **B. Orphan-venv removal** — the 320M `~/.hermes/hermes-agent/venv` recreated during the desktop build,
  used by nothing (gateways run the runtime deploy venv).
- **C. Freshness false-stale fix** — `desktop-update.sh` keys freshness on the bare repo HEAD, so a
  docs-only commit marks the app "stale" though `apps/desktop/` bytes didn't change.

**Goal:** the desktop update (and any future live-artifact build) never mutates root `main`; freshness
reflects the app's real source; and the one-off orphan venv is gone with a guard so it can't silently
return.

## 2. Non-Goals
- **NOT** migrating the whole dev tree to a runtime-style deploy-only split in v0.1 (that's Tier-3, spec'd
  as a roadmap item requiring its own review — it's a large architectural change to how ALL fleet coding
  works, not a desktop-update fix).
- **NOT** changing the runtime deploy-only split (already shipped, separate system).
- **NOT** touching the ~30 existing feature worktrees or how PRs land.

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
- **D-1 (Ace): do all 3 contention tiers, spec them all.** Tier-1 (build-in-worktree) ships in v0.1 as the
  concrete fix; Tier-2 (shared helper + lint) ships in v0.1; Tier-3 (dev-tree deploy-only split) is spec'd
  as a roadmap item with its own review gate (large, architectural).
- **D-2: Tier-1 uses a throwaway `git worktree`, not the root checkout.** `du_build_and_install` creates a
  detached worktree at the pinned ref under `~/.hermes/worktrees/desktop-build-<ts>`, runs `npm ci` +
  `dist:mac` there, installs, and `git worktree remove`s it — root `main` is never checked out, reset, or
  committed to. The MBP arm does the same over SSH (its own throwaway worktree), so the MBP hotfix-stash
  dance is no longer needed.
- **D-3: freshness keys on the last commit that touched the app's build inputs**, not bare HEAD:
  `git log -1 --format=%H -- apps/desktop package-lock.json` (the paths whose change actually changes the
  built app). Docs-only commits no longer mark the app stale.
- **D-4: orphan-venv removal is a one-shot with a standing guard.** `rm -rf ~/.hermes/hermes-agent/venv`
  after re-proving nothing holds it + no gateway references it; then a cheap guard (a line in the existing
  `runtime_tree_status`/fleet health check, or `.gitignore` confirmation) that flags if a venv reappears in
  the dev tree (it shouldn't — gateways use the runtime venv).

## 6. Implementation Phases

- **Phase 1 — C: freshness false-stale fix (smallest, unblocks a clean re-run).**
  - *Unit/script:* `du_resolve_ref`/identity resolves the pin to the last `apps/desktop`-touching commit;
    a docs-only commit on top does NOT flip identity to stale.
  - *E2E:* on the live Studio (installed `3fc83b17`), after the docs-only `0dd61f6b8` HEAD, `desktop-update.sh
    --host self` reports **"already current"** (not "stale"), because the last `apps/desktop` commit is
    still the installed one.
  - *Negative:* a real `apps/desktop/` change flips it to "stale → would rebuild."
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
- **Phase 5 (ROADMAP, own review) — A-Tier3: dev-tree deploy-only split.**
  - Make root `main` read-only-for-agents the way the runtime tree is: nobody commits to root `main`
    directly; every change (even docs) goes through a worktree + FF, mirroring the runtime deploy-only
    split. Requires: a decision on the fork-sync landing branch, a sanctioned FF-only writer for root
    `main`, and possibly a `flock` on `.git` for concurrent-mutation safety (Tier-3b). **Not built in
    v0.1** — spec'd here, gated on Ace's go after v0.1 lands.

## 7. Security / Ops
- All v0.1 changes are reversible: Tier-1/2 only change *where* the build runs (worktree vs root); C only
  changes the freshness pin; B is a delete of a gitignored orphan with a pre-delete safety re-prove.
- The `flock` (Tier-3b) is deferred with Tier-3; not needed once Tier-1 stops the only known writer.

## 8. Risks & Mitigations
- **R-1: a throwaway worktree leaks disk if cleanup is skipped.** *Mitigation:* `trap 'git worktree remove
  --force' EXIT` + a prune of stale `desktop-build-*` worktrees on each run (keep-none).
- **R-2: the MBP over-SSH worktree build needs the same isolation.** *Mitigation:* D-2 applies the worktree
  pattern to the MBP arm too; the hotfix-stash dance is retired (nothing to stash if root `main` is never
  checked out).
- **R-3: freshness-on-apps/desktop-commit misses a change made OUTSIDE apps/desktop that the build bundles**
  (e.g. a shared root `package-lock.json`). *Mitigation:* D-3 includes `package-lock.json` in the pin paths;
  if the build bundles more, add those paths — grep `apps/desktop` build inputs to confirm the set.
- **R-4: Tier-3 is large and could contend with the runtime split's assumptions.** *Mitigation:* it's a
  roadmap item with its own review, explicitly out of v0.1.

## 9. Open Questions
- **OQ-1:** Tier-3 — is a dev-tree deploy-only split worth it, or does Tier-1+2 (nobody's scripts touch root
  `main`; humans/agents use worktrees by convention) suffice? (Recommendation: ship v0.1 = Tier-1+2+B+C,
  then decide Tier-3 from whether contention recurs — it may be fully solved by Tier-1.)
- **OQ-2:** should the freshness pin also include the root `package-lock.json` / any monorepo-shared build
  input, or only `apps/desktop`? (Recommendation: include `apps/desktop` + `package-lock.json`; confirm the
  exact build-input set by reading `dist:mac`'s dependency graph.)

## 10. Acceptance Criteria
- [ ] **AC-1 (C):** after a docs-only HEAD advance, `desktop-update.sh --host self` reports "already current"
  when the installed SHA == last `apps/desktop` commit. Evidence: the live PLAN output.
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

## 11. Roadmap
| Version | Ships | Trigger |
|---|---|---|
| v0.1 | Tier-1 (build-in-worktree) + Tier-2 (helper+lint) + B (orphan venv) + C (freshness) | now |
| v0.2 (Tier-3) | dev-tree deploy-only split (root `main` read-only-for-agents) + optional `.git` flock | contention recurs after v0.1, or Ace's go |
