# Dev-Tree Contention Hardening + Orphan-Venv + Freshness — Implementation Plan

> **For Hermes:** Implement task-by-task, TDD (RED→GREEN→commit). Serial single-executor (inline or one
> subagent via subagent-driven-development). Spec: `docs/desktop/2026-07-01-devtree-contention-and-followups-SPEC.md`
> (APPROVED v1.0, 2 Opus passes). Every task commits; each phase ends with a smoke-test step.

**Goal:** Stop fleet scripts (starting `desktop-update.sh`) from building on / mutating the shared root
`main` checkout `~/.hermes/hermes-agent`; delete the 320M orphan dev-tree venv with a guard; fix
`desktop-update.sh`'s freshness so a docs-only commit no longer reads the app as stale.

**Architecture:** Five independent fixes shipped in one v0.1. C (freshness) first — smallest, unblocks a
clean re-run. Then Tier-1 (build in a throwaway worktree) + Tier-2 (shared helper + lint). Then B (orphan
venv). Then Tier-3 (serialized `land-on-main.sh` FF-only writer + extend the existing worktree-aware
`pre-commit` tripwire) — **proof-harnesses (refusal/race tests) land BEFORE the live fork-sync cutover.**

**Tech Stack:** Bash (POSIX-ish, `set -euo pipefail`), `bats`-free shell unit tests in the existing
`test_desktop_update.sh` style (LIB-mode source + assert), `flock`, git worktrees, `jq`, Node (electron-builder
config read for RC-7).

**Ground-truth (measured 2026-07-01, do not re-derive):**
- `desktop-update.sh` = 289 lines; LIB-mode `DESKTOP_UPDATE_LIB=1 source`; tests `fleet/test_desktop_update.sh` (43).
- Freshness bug: `du_resolve_ref(){ ...; echo "HEAD"; }` (line 33) → pins bare HEAD.
- Build step: `du_build_and_install` (line 247); on non-HEAD ref it does `git -C "$dev" merge --ff-only` on
  root (lines 253-254) then `npm ci`/`dist:mac` in `$dev/apps/desktop` — THIS is the root-mutation to remove.
- electron-builder build inputs (`apps/desktop/package.json` §build): `files`=`dist/**,assets/**,electron/**,public/**,package.json`;
  `extraResources`=`build/install-stamp.json,build/native-deps,assets/icon.ico`; `beforeBuild/beforePack/afterPack/afterSign`
  = `apps/desktop/scripts/*.cjs`. `npm ci` consumes `package-lock.json`. All under `apps/desktop/` EXCEPT the
  lockfile — confirm its exact path in Task C-0.
- Existing `pre-commit`: `~/.hermes/hermes-agent/.git/hooks/pre-commit`; worktree-aware
  (`THIS_TREE=$(git rev-parse --show-toplevel)`; exits 0 unless `THIS_TREE == $HERMES_HOME/hermes-agent`).
  Extend THIS hook for Tier-3; do not add a rival.
- Fleet health home for the venv guard: `fleet/runtime_tree_status.py` (already inspects dev/runtime trees).
- Fork-sync runbook to rewire (OQ-3): `skills-shared/coding/hermes-fork-pr-contribution/references/fork-sync-process.md`
  (+ `executing-an-upstream-parity-merge.md`).
- All work lands on `main` via a worktree (this plan file itself will land via `land-on-main.sh` once it exists;
  until then, commit in the current session's normal flow).

---

## PHASE C — Freshness false-stale fix (smallest, unblocks a clean re-run)

### Task C-0: Enumerate + confirm the COMPLETE build-input path set (BLOCKING GATE, RC-7)

**Objective:** Mechanically determine every path whose change changes the built app, so the pin can't
under-report stale. Phase C does not proceed until this is confirmed on disk.

**Files:**
- Create: `fleet/desktop-build-inputs.sh` (a tiny helper that PARSES electron-builder config, not a hand-list)
- Test: `fleet/test_desktop_build_inputs.sh`

**Step 1 — Write the enumeration helper (mechanical parse):**
```bash
#!/usr/bin/env bash
# Emit, one per line, the git-relative paths that are build inputs for `npm run dist:mac`.
# MECHANICAL (Nit-2): parse apps/desktop/package.json .build.files/.extraResources/.beforeBuild etc.
# + the lockfile npm ci consumes. Re-run this whenever deps change; never hand-maintain the list.
set -euo pipefail
dev="${1:-$HOME/.hermes/hermes-agent}"; pj="$dev/apps/desktop/package.json"
# files globs + extraResources.from + build hook scripts, all relative to apps/desktop/
jq -r '
  ([.build.files[]?] // [])
  + ([.build.extraResources[]? | if type=="object" then .from else . end] // [])
  + ([.build.beforeBuild, .build.beforePack, .build.afterPack, .build.afterSign] | map(select(. != null)))
' "$pj" | sed 's#^#apps/desktop/#'
# the lockfile npm ci reads (path confirmed in this task; emit whichever exists)
for lk in apps/desktop/package-lock.json package-lock.json; do [ -f "$dev/$lk" ] && echo "$lk"; done
```

**Step 2 — Confirm the lockfile location on disk:**
Run: `ls -1 ~/.hermes/hermes-agent/package-lock.json ~/.hermes/hermes-agent/apps/desktop/package-lock.json 2>/dev/null`
Record which exists — that path enters the pin set. (If root, a root-lockfile change must flip stale.)

**Step 3 — Write the completeness test (RED):**
```bash
# test_desktop_build_inputs.sh — assert the enumeration is non-empty, includes apps/desktop, includes the
# real lockfile, and that a KNOWN bundled path (apps/desktop/electron) is present; a bundled-but-unpinned
# path (negative) is caught by asserting the parse covers .build.files rather than a hardcoded subset.
out="$(bash fleet/desktop-build-inputs.sh)"
echo "$out" | grep -q '^apps/desktop/electron/' || { echo FAIL missing electron; exit 1; }
echo "$out" | grep -qE 'package-lock\.json$' || { echo FAIL missing lockfile; exit 1; }
```

**Step 4 — Run RED → implement → GREEN:**
Run: `bash fleet/test_desktop_build_inputs.sh` → expect FAIL (helper absent) → add helper → expect PASS.

**Step 5 — Commit:**
```bash
git add fleet/desktop-build-inputs.sh fleet/test_desktop_build_inputs.sh
git commit -m "feat(desktop-update): mechanical build-input enumeration (RC-7 Phase-1 gate)"
```

### Task C-1: `du_build_input_sha` — pin to last build-input commit, not bare HEAD

**Objective:** Replace the freshness key so a docs-only commit doesn't mark the app stale.

**Files:**
- Modify: `fleet/desktop-update.sh` (add `du_build_input_sha`; use it where `du_resolve_ref`→`du_ref_sha` feeds identity)
- Modify: `fleet/test_desktop_update.sh`

**Step 1 — RED test (in test_desktop_update.sh, LIB-mode):**
```bash
# In a temp git repo with apps/desktop/foo + a later docs-only commit:
# du_build_input_sha <dev> must equal the apps/desktop commit, NOT HEAD.
DESKTOP_UPDATE_LIB=1 source fleet/desktop-update.sh
sha="$(du_build_input_sha "$tmp")"
[ "$sha" = "$apps_commit" ] || { echo "FAIL: got $sha want $apps_commit"; exit 1; }
```

**Step 2 — Run RED:** `bash fleet/test_desktop_update.sh` → new test FAILs (`du_build_input_sha` undefined).

**Step 3 — Implement:**
```bash
# du_build_input_sha <repo_dir> [ref] -> last commit touching the build-input set (freshness key).
du_build_input_sha(){
  local dev="$1" ref="${2:-HEAD}"; local -a paths
  mapfile -t paths < <(bash "$(dirname "${BASH_SOURCE[0]}")/desktop-build-inputs.sh" "$dev")
  git -C "$dev" log -1 --format=%H "$ref" -- "${paths[@]}" 2>/dev/null
}
```
Then repoint the identity gate (line ~182) to use `du_build_input_sha "$dev" "$resolved"` as the pinned key.

**Step 4 — GREEN + negative:** `bash fleet/test_desktop_update.sh` → all pass, incl. a negative test that an
`apps/desktop/` change DOES flip the pin.

**Step 5 — Commit:**
```bash
git add fleet/desktop-update.sh fleet/test_desktop_update.sh
git commit -m "fix(desktop-update): freshness keys on last build-input commit, not bare HEAD (C, AC-1/AC-13)"
```

### Task C-2: Live smoke — Studio reads "already current" after the docs-only HEAD

**Objective:** Prove fix C on the live box (installed `3fc83b17`, HEAD now a docs-only commit).

**Step 1 — Run PLAN-only (no --apply):**
Run: `bash ~/.hermes/fleet/desktop-update.sh --host self`
Expected: `GATE identity: PLAN — already current` (NOT "stale"), because last build-input commit == installed.

**Step 2 — Evidence row + commit** (if any code touched): capture the PLAN output into the closeout notes.

---

## PHASE Tier-1 — Build in a throwaway worktree (the concrete contention fix)

### Task T1-1: `du_build_and_install` builds in an ephemeral worktree, never root

**Objective:** Move the build off the root checkout; root `main` HEAD+status byte-identical before/after.

**Files:**
- Modify: `fleet/desktop-update.sh` (`du_build_and_install`, lines 247-257)
- Modify: `fleet/test_desktop_update.sh`

**Step 1 — RED test:** stub the actual `npm ci`/`dist:mac` (via `BUILD_INSTALL_CMD` DI hook already present),
run an `--apply`-shaped call through the real worktree machinery in a temp repo, assert:
`git -C "$tmp" rev-parse HEAD` and `git -C "$tmp" status --porcelain` are identical before and after, and the
reflog shows no `checkout`/`reset` on `main`.

**Step 2 — Run RED:** current code does `git -C "$dev" merge --ff-only` on root for a non-HEAD ref → the
before/after reflog assertion FAILs.

**Step 3 — Implement:** replace the in-root checkout/merge with a throwaway worktree:
```bash
# inside du_build_and_install, for the real (non-DI) path:
local wt; wt="$HOME/.hermes/worktrees/desktop-build-$(date +%s)-$$"
trap 'git -C "$dev" worktree remove --force "$wt" 2>/dev/null || true' RETURN
git -C "$dev" worktree add --detach "$wt" "$ref" -q || return 1
( cd "$wt/apps/desktop" && npm ci ) || return 1
( cd "$wt/apps/desktop" && npm run dist:mac ) || return 1
# ...backup old app, ditto new from "$wt/apps/desktop/release", xattr, etc. (unchanged, but sourced from $wt)
```
(Prune stale `desktop-build-*` worktrees at start: `git -C "$dev" worktree list | awk '/desktop-build-/{print $1}' | xargs -r -n1 git -C "$dev" worktree remove --force`.)

**Step 4 — GREEN:** `bash fleet/test_desktop_update.sh` all pass, incl. AC-7 (no leftover worktree) and AC-14
(induced build failure leaves root pristine — force `npm ci` to fail, assert HEAD+status unchanged).

**Step 5 — Commit:**
```bash
git add fleet/desktop-update.sh fleet/test_desktop_update.sh
git commit -m "fix(desktop-update): build in throwaway worktree, never mutate root main (Tier-1, AC-2/3/7/14)"
```

### Task T1-2: MBP arm uses the same worktree isolation (retire the stash dance)

**Objective:** The `--host mbp` SSH path builds in its own throwaway worktree on `.88`; the hotfix-stash
dance is removed (nothing to stash if root is never checked out).

**Files:** Modify: `fleet/desktop-update.sh` (mbp branch), `fleet/test_desktop_update.sh`

**Step 1 — RED test:** DI-stub the SSH exec; assert the mbp command string builds in a worktree path and
contains no `git stash`. **Step 2 — RED. Step 3 — implement. Step 4 — GREEN.**

**Step 5 — Commit:** `git commit -m "fix(desktop-update): MBP arm builds in worktree over SSH, drop stash dance (Tier-1, R-2)"`

### Task T1-3: Live smoke — `--apply` on Studio leaves root pristine

**Step 1:** capture `git -C ~/.hermes/hermes-agent rev-parse HEAD` + `git status --porcelain` + `git reflog -5`.
**Step 2:** `bash ~/.hermes/fleet/desktop-update.sh --host self --apply` (Studio already current → identity gate
should PLAN "already current" and NOT rebuild; to actually exercise the build path, use `--force` on a throwaway).
**Step 3:** re-capture; assert HEAD unchanged, status clean, no `main` reset in reflog; `git worktree list` clean.
**Evidence row.**

---

## PHASE Tier-2 — Shared helper + root-mutation lint

### Task T2-1: Extract `with_ephemeral_worktree` into `fleet/dev-tree-guard.sh`

**Objective:** Reusable `with_ephemeral_worktree <dev> <ref> <cmd...>`; `desktop-update.sh` adopts it.

**Files:** Create `fleet/dev-tree-guard.sh` + `fleet/test_dev_tree_guard.sh`; Modify `fleet/desktop-update.sh`.

**Step 1 — RED test:** `with_ephemeral_worktree "$tmp" HEAD 'pwd > "$OUT"'` runs cmd inside a worktree, removes
it after (success AND failure), and root HEAD is unchanged. **Step 2 RED → 3 implement → 4 GREEN.**

**Step 5 — Commit:** `git commit -m "refactor(fleet): shared with_ephemeral_worktree helper (Tier-2)"`

### Task T2-2: Root-main-mutation lint over fleet/ + scripts/

**Objective:** A grep-based lint flags any `fleet/`/`scripts/` script doing `git -C <dev-tree> (checkout|reset|merge|commit)`
without going through the helper/writer; returns 0 offenders after adoption.

**Files:** Create `fleet/lint-root-main-mutation.sh` + `fleet/test_lint_root_main_mutation.sh`.

**Step 1 — RED test:** a fixture script with a raw `git -C ~/.hermes/hermes-agent merge` is flagged (exit≠0);
a script using `with_ephemeral_worktree`/`land-on-main.sh` is clean. **Step 2 RED → 3 implement → 4 GREEN.**

**Step 5 — GREEN over the real tree:**
Run: `bash fleet/lint-root-main-mutation.sh` → **0 offenders** (after `desktop-update.sh` adopted the helper).
Fix any real offender the lint surfaces before commit.

**Step 6 — Commit:** `git commit -m "feat(fleet): root-main-mutation lint, 0 offenders (Tier-2, AC-4)"`

---

## PHASE B — Orphan-venv removal + guard

### Task B-1: Guard — flag a reappeared dev-tree-root venv (detection-only, RC-6)

**Objective:** Add a check that warns (→ #alerts) if `~/.hermes/hermes-agent/venv` reappears — matching the
dev-tree ROOT specifically, not any nested worktree venv.

**Files:** Modify `fleet/runtime_tree_status.py` (add a `--check-orphan-venv` path) + its test.

**Step 1 — RED test:** with a fake `~/.hermes/hermes-agent/venv` dir present, the check returns non-zero /
emits a warning; absent → clean; a `worktrees/*/venv` is IGNORED. **Step 2 RED → 3 implement → 4 GREEN.**

**Step 5 — Commit:** `git commit -m "feat(fleet): dev-tree-root orphan-venv guard, root-specific (B, AC-6)"`

### Task B-2: Re-prove nothing holds the venv, then delete

**Step 1 — re-prove (LIVE):**
```bash
lsof +D /Users/alexgierczyk/.hermes/hermes-agent/venv 2>/dev/null | head    # expect empty
grep -rl 'hermes-agent/venv' ~/Library/LaunchAgents ~/.hermes/fleet ~/.hermes/scripts 2>/dev/null \
  | grep -vE '\.bak|\.pyc|scripts-bak|deploy-split'                          # expect no LIVE refs
du -sh /Users/alexgierczyk/.hermes/hermes-agent/venv                        # ~320M
```
**Step 2 — delete:** `rm -rf /Users/alexgierczyk/.hermes/hermes-agent/venv`
**Step 3 — verify (AC-5):** `ls ~/.hermes/hermes-agent/venv` → absent; all 6 gateways `state=running` (deploy
venv unaffected); the B-1 guard now reports clean.
**Step 4 — Commit** (evidence only; nothing to code-commit unless the guard log path changed).

---

## PHASE Tier-3 — Serialized `land-on-main.sh` writer + root-checkout tripwire

> **Proof-harnesses first (high-invariant phase):** build + prove the writer refusals, the tripwire, and the
> race BEFORE the live fork-sync cutover (T3-6). The cutover is the last, reversible step.

### Task T3-1: `land-on-main.sh` — flock + under-lock fetch/FF, dirty/non-FF refusal

**Objective:** The single sanctioned FF-only writer of root `main`.

**Files:** Create `fleet/land-on-main.sh` + `fleet/test_land_on_main.sh`.

**Step 1 — RED tests (temp repos):**
- dirty root → exit 3, `main` unmoved;
- non-FF ref → exit 4, `main` unmoved, NO reset;
- clean FF ref → `main` fast-forwarded, sentinel set during the merge.

**Step 2 — RED. Step 3 — implement:**
```bash
#!/usr/bin/env bash
set -euo pipefail
dev="${DEV_TREE:-$HOME/.hermes/hermes-agent}"; ref="${1:?usage: land-on-main.sh <branch|ref>}"
exec 9>"$dev/.git/main-land.lock"
flock -w 60 9 || { echo "land-on-main: could not acquire lock in 60s" >&2; exit 5; }   # R-5: bounded, loud
[ -z "$(git -C "$dev" status --porcelain)" ] || { echo "land-on-main: root dirty; refusing" >&2; exit 3; }
git -C "$dev" fetch --all -q                                    # UNDER the lock (D-5.2)
cur="$(git -C "$dev" rev-parse HEAD)"; tgt="$(git -C "$dev" rev-parse "$ref^{commit}")"
base="$(git -C "$dev" merge-base HEAD "$ref")"
[ "$base" = "$cur" ] || { echo "land-on-main: $ref is not a fast-forward of main; refusing (no reset)" >&2; exit 4; }
LAND_ON_MAIN=1 git -C "$dev" merge --ff-only "$ref" -q         # sentinel scoped to THIS process only
echo "land-on-main: main -> $tgt"
```

**Step 4 — GREEN.** **Step 5 — Commit:**
`git commit -m "feat(fleet): land-on-main.sh serialized FF-only writer (Tier-3, AC-9)"`

### Task T3-2: Race test — serialize two lands (AC-8)

**Step 1 — RED test:** two `land-on-main.sh` backgrounded against the same repo → both serialize; `main` ends
FF-advanced; reflog shows FF, no reset. **Step 2 RED → 3 (behavior already correct from flock) → 4 GREEN.**
**Step 5 — Commit:** `git commit -m "test(fleet): land-on-main serializes concurrent lands (Tier-3, AC-8)"`

### Task T3-3: Concurrency-realism — land ∥ ephemeral build ∥ external advance (AC-12, RC-3/BL-2)

**Step 1 — RED test:** run `land-on-main.sh` concurrent with `with_ephemeral_worktree` AND a simulated
`origin/main` advance (push to a bare origin, then land). Assert: root `main` ends FF-advanced OR loud-refused
(never reset); the build worktree is unaffected. **Step 2 RED → 3 (fetch-under-lock handles it) → 4 GREEN.**
**Step 5 — Commit:** `git commit -m "test(fleet): land ∥ build ∥ external-advance realism (Tier-3, AC-12)"`

### Task T3-4: Extend the existing worktree-aware pre-commit tripwire (commit half)

**Objective:** Refuse a commit authored in the ROOT checkout without the writer's sentinel; worktrees exempt.

**Files:** Modify `~/.hermes/hermes-agent/.git/hooks/pre-commit` (extend, keep the existing runtime-guard
block + worktree-awareness). Test: `fleet/test_precommit_root_guard.sh` (drives the hook via `git commit` in
temp clones that mirror the hook).

**Step 1 — RED test:** a raw `git commit` in the root checkout (no `LAND_ON_MAIN`) → refused (exit≠0);
`LAND_ON_MAIN=1 git commit` → allowed; a commit in a worktree (`~/.hermes/worktrees/*` AND a `~/Projects/wt*`
path, AC-15) → allowed. **Step 2 — RED. Step 3 — implement:** append to the hook, after the existing block:
```bash
# Tier-3 root-checkout working-surface tripwire (accident-guard; --no-verify & non-adoption bypass by design).
if [ "$THIS_TREE" = "$RUNTIME_TREE" ] && [ "${LAND_ON_MAIN:-0}" != "1" ]; then
  cat >&2 <<'MSG'
✋ dev-tree root is not a working surface (Tier-3).
Commit in a worktree, or land via ~/.hermes/fleet/land-on-main.sh.
(Break-glass: git commit --no-verify — you own the contention risk.)
MSG
  exit 1
fi
```
**Step 4 — GREEN** (all incl. AC-15 both worktree locations).
**Step 5 — Commit** (the hook is inside the dev-tree `.git`, NOT version-controlled — commit a COPY to
`fleet/hooks/pre-commit` as the source-of-truth + an installer, and reference it):
```bash
git add fleet/hooks/pre-commit fleet/install-hooks.sh fleet/test_precommit_root_guard.sh
git commit -m "feat(fleet): pre-commit root-checkout tripwire, worktree-exempt (Tier-3, AC-10/15)"
```

### Task T3-5: Checkout-guard half + rollback-uncaged assertion (Nit-1)

**Objective:** Refuse a `checkout` moving root off `main`; PROVE a raw FF-only fork-sync from a parked-on-`main`
root is NOT caged (the break-glass survives).

**Files:** `fleet/hooks/post-checkout` (or a wrapper — git has no `pre-checkout`; enforce via the writer +
lint + a `post-checkout` warn), `fleet/test_checkout_guard.sh`.

**Step 1 — RED test:** (a) `git -C root checkout <branch>` off `main` → flagged; (b) `git -C root merge
--ff-only fork/main` while parked on `main` → SUCCEEDS (no checkout needed, no commit object authored → neither
guard fires). **Step 2 RED → 3 implement → 4 GREEN** (AC-11 rollback path proven uncaged).
**Step 5 — Commit:** `git commit -m "feat(fleet): checkout guard + proof raw FF fork-sync stays uncaged (Tier-3, Nit-1)"`

### Task T3-6: LIVE cutover — rewire fork-sync + agent doc-land to `land-on-main.sh` (OQ-3, do LAST)

**Objective:** Make `land-on-main.sh` the sanctioned path; keep raw FF as documented break-glass.

**Files:** Modify `skills-shared/coding/hermes-fork-pr-contribution/references/fork-sync-process.md`
(+ `executing-an-upstream-parity-merge.md`): replace the raw `git merge --ff-only fork/main` step with
`bash ~/.hermes/fleet/land-on-main.sh fork/main`, and add a "break-glass: raw FF" note.

**Step 1 — sequence check:** confirm T3-1..T3-5 all GREEN first (RC-4).
**Step 2 — rewire the runbook** (grep to prove no un-annotated raw `merge --ff-only fork/main` remains, AC-11).
**Step 3 — install the hooks live:** `bash fleet/install-hooks.sh` (copies `fleet/hooks/*` into the dev-tree
`.git/hooks/`, preserving the existing runtime-guard block).
**Step 4 — live land proof:** land this plan doc via `land-on-main.sh` (or the next real doc-land); reflog shows
FF, root never left `main`. **Rollback:** revert the runbook line — raw FF still works.
**Step 5 — Commit:** `git commit -m "feat(fleet): fork-sync + agent lands route through land-on-main.sh (Tier-3, OQ-3, AC-11)"`

---

## CLOSEOUT — smoke matrix + acceptance mapping

### Task Z-1: Full smoke matrix (one evidence row each)

Run each end-to-end with real input; capture a row:
- **C:** `desktop-update.sh --host self` → `identity: PLAN — already current` after docs-only HEAD.
- **Tier-1:** `--apply --force` throwaway build → root HEAD+status unchanged, no worktree left.
- **Tier-2:** `lint-root-main-mutation.sh` → 0 offenders.
- **B:** `ls ~/.hermes/hermes-agent/venv` → absent; 6 gateways running.
- **Tier-3:** race test green; raw root commit refused; worktree commit (both locations) allowed;
  `land-on-main.sh fork/main` FFs; raw FF break-glass still works.

### Task Z-2: Map all 15 ACs to evidence, run prd-closeout, share result

- Fill the spec's AC-1..AC-15 checkboxes with evidence pointers.
- `prd-closeout` on the spec; update mem0 with the durable fact (dev-tree lands go through `land-on-main.sh`;
  root checkout is not a working surface; `desktop-update.sh` builds in a throwaway worktree).
- Re-share the spec link (verdict → shipped).

### Task Z-3: Skill updates

- `git-worktree-isolation` — add the dev-tree root-checkout-is-not-a-working-surface + `land-on-main.sh` pattern.
- `live-artifact-update-runbook` — note `desktop-update.sh` now builds in a worktree (root untouched).
- `hermes-fork-pr-contribution` — fork-sync goes through `land-on-main.sh` (raw FF = break-glass).
