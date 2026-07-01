# Desktop Update + Supervised Sign-in — Implementation Plan

> **For Hermes:** Implement task-by-task — inline (serial) or via `subagent-driven-development` (fresh
> subagent per task, two-stage review). Every task is RED → GREEN → commit.

**Goal:** Build `desktop-update.sh` — a per-Mac, idempotent, reversible runbook that rebuilds/reinstalls the
Hermes desktop app from a pinned git ref, proves freshness by the app's **embedded git SHA** (already
shipped in `install-stamp.json`), verifies the backend is signable, and stages a **supervised one-time
sign-in** (script writes PENDING; the agent attests CONFIRMED).

**Architecture:** One bash script (`~/.hermes/fleet/desktop-update.sh`) + a bash test harness
(`~/.hermes/fleet/test_desktop_update.sh`, no framework — plain `assert` funcs, same style as the fleet's
existing `test_deploy_sh.py` shape but in bash since the SUT is bash). The script has pure, unit-testable
helper functions (identity compare, backend-signable probe, backup prune, state-record write) that the
harness drives with fixtures; the orchestration (`main`) is smoke-tested live on Studio. Folds into
`deploy.sh --desktop` (hook exists) in the last phase.

**Tech Stack:** bash, `curl`, `jq` (already used across fleet scripts), `git`, `plutil`/`defaults` (macOS
Info.plist read), `shasum`, `ditto`, `xattr`, `launchctl`. SUT reads the EXISTING `install-stamp.json` —
no app/electron-builder changes needed (R-3 dissolved, see PRD §8).

**Source-of-truth facts (ground-truthed 2026-07-01):**
- Installed stamp: `/Applications/Hermes.app/Contents/Resources/install-stamp.json` →
  `{schemaVersion, commit, branch, builtAt, dirty, source}`. Currently `6351c7ed` (stale).
- Repo desktop version: `apps/desktop/package.json` `version` (0.17.0). Installed app version:
  `/Applications/Hermes.app/Contents/Info.plist` `CFBundleShortVersionString` (0.15.1).
- `dist:mac` = `npm run build` (runs `write-build-stamp.cjs`) → `npm run builder -- --mac`. Output:
  `apps/desktop/release/mac-arm64/Hermes.app`.
- Both Macs: non-loopback OAuth, dashboard `mac-studio-m3u:9119`, `/api/status`→`auth_required:true`,
  `/api/auth/providers`→`basic`. Studio gateway is a LaunchAgent (`gui/<uid>`).
- Deploy tree `--ref` default: `runtime_tree_status.py` reports `fork/main` tip.

**Test-run convention:** `bash ~/.hermes/fleet/test_desktop_update.sh` (self-contained; makes its own
tmpdirs; no network in unit tests — the backend probe is dependency-injected via a `CURL_CMD` override).

---

## Phase 1 — freshness identity + install/backup/rollback (INV-1/-4/-5, AC-1/AC-4/AC-5)

### Task 1: Scaffold the script + test harness (RED first)

**Files:**
- Create: `~/.hermes/fleet/desktop-update.sh`
- Create: `~/.hermes/fleet/test_desktop_update.sh`

**Step 1 — write the failing harness** (`test_desktop_update.sh`):
```bash
#!/usr/bin/env bash
# Unit tests for desktop-update.sh — plain-bash asserts, no framework, no network.
set -uo pipefail
SUT="$(cd "$(dirname "$0")" && pwd)/desktop-update.sh"
PASS=0; FAIL=0
assert_eq(){ if [ "$1" = "$2" ]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); echo "FAIL: $3: expected [$2] got [$1]"; fi; }
assert_rc(){ if [ "$1" -eq "$2" ]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); echo "FAIL: $3: expected rc $2 got $1"; fi; }

# source the SUT in LIB mode (functions only, no main) — set DESKTOP_UPDATE_LIB=1
DESKTOP_UPDATE_LIB=1 source "$SUT"

test_sanity(){ assert_eq "$(type -t du_identity_matches)" "function" "du_identity_matches defined"; }

test_sanity
echo "---"; echo "PASS=$PASS FAIL=$FAIL"; [ "$FAIL" -eq 0 ]
```

**Step 2 — run, expect FAIL:** `bash ~/.hermes/fleet/test_desktop_update.sh`
Expected: `FAIL: du_identity_matches defined` (SUT doesn't exist / no function).

**Step 3 — minimal SUT** (`desktop-update.sh`) with the LIB guard + stub:
```bash
#!/usr/bin/env bash
# desktop-update.sh — per-Mac desktop app update + supervised sign-in (PRD 2026-07-01 v0.5).
set -uo pipefail

du_identity_matches(){ :; }   # placeholder; real impl in Task 2

# --- main (skipped when sourced as a lib) ---
if [ "${DESKTOP_UPDATE_LIB:-0}" != "1" ]; then
  echo "desktop-update.sh: main not yet implemented" >&2; exit 2
fi
```

**Step 4 — run, expect PASS:** `bash ~/.hermes/fleet/test_desktop_update.sh` → `PASS=1 FAIL=0`.

**Step 5 — commit:**
```bash
cd ~/.hermes && git add -f fleet/desktop-update.sh fleet/test_desktop_update.sh && \
  git commit -m "feat(desktop-update): scaffold script + bash test harness (lib-mode source)"
```

### Task 2: `du_identity_matches` — installed embedded SHA vs pinned ref SHA (AC-1)

**Objective:** the pre-build "already current" gate + INV-1, keyed on the git SHA in `install-stamp.json`.

**Step 1 — failing tests** (append to harness):
```bash
test_identity_match(){
  local d; d=$(mktemp -d); echo '{"schemaVersion":1,"commit":"abc123","dirty":false}' > "$d/install-stamp.json"
  du_identity_matches "$d/install-stamp.json" "abc123"; assert_rc $? 0 "same SHA -> match(0)"
  du_identity_matches "$d/install-stamp.json" "def456"; assert_rc $? 1 "diff SHA -> nomatch(1)"
}
test_identity_dirty(){   # a dirty local build is never "already current" — force rebuild
  local d; d=$(mktemp -d); echo '{"schemaVersion":1,"commit":"abc123","dirty":true}' > "$d/install-stamp.json"
  du_identity_matches "$d/install-stamp.json" "abc123"; assert_rc $? 1 "dirty stamp -> nomatch even if SHA equal"
}
test_identity_missing(){  # no stamp (pre-embed 0.15.1 app) -> nomatch (fall through to version check)
  du_identity_matches "/nonexistent/install-stamp.json" "abc123"; assert_rc $? 1 "missing stamp -> nomatch"
}
test_identity_match; test_identity_dirty; test_identity_missing
```
Add the three calls above the summary line.

**Step 2 — run, expect FAIL** (stub returns 0 for everything).

**Step 3 — implement** (replace the placeholder):
```bash
# du_identity_matches <stamp_path> <pinned_sha> -> 0 if installed build == pinned & clean, else 1
du_identity_matches(){
  local stamp="$1" pinned="$2"
  [ -f "$stamp" ] || return 1
  local commit dirty
  commit=$(jq -r '.commit // empty' "$stamp" 2>/dev/null) || return 1
  dirty=$(jq -r '.dirty // false' "$stamp" 2>/dev/null)
  [ -n "$commit" ] || return 1
  [ "$dirty" = "true" ] && return 1     # dirty build is non-reproducible identity -> never "current"
  [ "$commit" = "$pinned" ] && return 0 || return 1
}
```

**Step 4 — run, expect PASS.**

**Step 5 — commit:** `... -m "feat(desktop-update): du_identity_matches (SHA gate, dirty=never-current, missing=nomatch)"`

### Task 3: `du_resolve_ref` + `du_ref_sha` — pin the deploy ref (AC-1, D-5)

**Objective:** default `--ref` to the deploy tree's `fork/main` tip; resolve any ref to a SHA with
`git rev-parse` (free, no build).

**Step 1 — failing test:**
```bash
test_ref_sha(){
  # in a throwaway git repo, du_ref_sha HEAD == git rev-parse HEAD
  local d; d=$(mktemp -d); ( cd "$d" && git init -q && git commit -q --allow-empty -m x )
  local want; want=$(git -C "$d" rev-parse HEAD)
  assert_eq "$(du_ref_sha "$d" HEAD)" "$want" "du_ref_sha resolves HEAD"
}
test_ref_sha
```

**Step 2 — run, expect FAIL.**

**Step 3 — implement:**
```bash
DEPLOY_TREE_DEFAULT="$HOME/.hermes/runtime/hermes-agent"
du_ref_sha(){ git -C "$1" rev-parse "$2^{commit}" 2>/dev/null; }
du_resolve_ref(){  # echo default ref when none given
  local given="${1:-}"; [ -n "$given" ] && { echo "$given"; return; }
  echo "fork/main"
}
```

**Step 4 — run, expect PASS. Step 5 — commit.**

### Task 4: `du_installed_version` + `du_repo_version` — the human-readable secondary (AC-1)

**Step 1 — failing test** (fixture Info.plist + package.json):
```bash
test_versions(){
  local d; d=$(mktemp -d)
  printf '{"version":"0.17.0"}' > "$d/package.json"
  assert_eq "$(du_repo_version "$d/package.json")" "0.17.0" "repo version from package.json"
}
test_versions
```

**Step 2 — FAIL. Step 3 — implement:**
```bash
du_repo_version(){ jq -r '.version // empty' "$1" 2>/dev/null; }
# installed version via macOS plist reader (real path: /Applications/Hermes.app/Contents/Info.plist)
du_installed_version(){
  local app="${1:-/Applications/Hermes.app}"
  /usr/bin/defaults read "$app/Contents/Info" CFBundleShortVersionString 2>/dev/null
}
```
**Step 4 — PASS. Step 5 — commit.**

### Task 5: `du_backup_and_prune` — versioned backup dir, keep-last-N (INV-5, AC-4)

**Objective:** move the old app to `/Applications/.hermes-app-backups/Hermes.app.old-<ts>`, prune to N=3.

**Step 1 — failing test** (operate on a temp "Applications" + "backups"):
```bash
test_backup_prune(){
  local base; base=$(mktemp -d); local bdir="$base/.hermes-app-backups"; mkdir -p "$bdir"
  # seed 4 fake old backups with increasing ts
  for ts in 001 002 003 004; do mkdir -p "$bdir/Hermes.app.old-$ts"; done
  du_prune_backups "$bdir" 3
  local n; n=$(ls -d "$bdir"/Hermes.app.old-* 2>/dev/null | wc -l | tr -d ' ')
  assert_eq "$n" "3" "prune keeps last 3"
  # oldest (001) removed, newest (004) kept
  [ -d "$bdir/Hermes.app.old-004" ]; assert_rc $? 0 "newest kept"
  [ -d "$bdir/Hermes.app.old-001" ]; assert_rc $? 1 "oldest pruned"
}
test_backup_prune
```

**Step 2 — FAIL. Step 3 — implement:**
```bash
du_prune_backups(){  # <backup_dir> <keep_n>
  local bdir="$1" keep="$2"
  local all; all=$(ls -d "$bdir"/Hermes.app.old-* 2>/dev/null | sort)   # ts is sortable
  local total; total=$(echo "$all" | grep -c . )
  [ "$total" -le "$keep" ] && return 0
  echo "$all" | head -n "$((total - keep))" | while read -r old; do [ -n "$old" ] && rm -rf "$old"; done
}
du_backup_app(){  # <app_path> <backup_dir> -> echoes the backup path
  local app="$1" bdir="$2" ts; ts=$(date +%Y%m%d-%H%M%S)
  mkdir -p "$bdir"; local dest="$bdir/Hermes.app.old-$ts"
  [ -d "$app" ] && ditto "$app" "$dest"    # copy (ditto preserves app bundle); caller removes old after install
  echo "$dest"
}
```
*(Note: use `ditto`-copy then swap, not `mv`, so a failed install still has the live app; timestamps in
tests use lexically-sortable strings.)*

**Step 4 — PASS. Step 5 — commit.**

### Task 6: `du_launch_verify` — beyond process-exists (INV-5, pass-4 change-4)

**Objective:** verify a launched app reached "ready", not just that a PID exists.

**Step 1 — failing test** (inject a fake "ready probe"):
```bash
test_launch_verify(){
  # du_launch_verify uses a READY_PROBE_CMD hook we can stub
  READY_PROBE_CMD='echo ready' du_launch_verify "/Applications/Hermes.app"; assert_rc $? 0 "ready probe ok -> 0"
  READY_PROBE_CMD='false'       du_launch_verify "/Applications/Hermes.app"; assert_rc $? 1 "ready probe fail -> 1"
}
test_launch_verify
```

**Step 2 — FAIL. Step 3 — implement** (the real probe = the app's backend `/api/status` reachable from the
app host, or a bounded wait on the app's own readiness file; injectable for tests):
```bash
du_launch_verify(){  # <app_path> -> 0 if the app reached ready within a bounded wait
  local app="$1" deadline=$((SECONDS+30))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if eval "${READY_PROBE_CMD:-du_default_ready_probe '$app'}" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}
du_default_ready_probe(){ pgrep -f "$1/Contents/MacOS/" >/dev/null; }   # replaced by a real renderer-ready check in Phase 2 smoke
```
*(Phase-2 smoke replaces `du_default_ready_probe` with a genuine readiness signal if `pgrep` proves too
weak on the live app; the injection hook keeps the unit test honest either way.)*

**Step 4 — PASS. Step 5 — commit.**

---

## Phase 2 — backend-signable precondition + supervised-signin state record (INV-2/-3, AC-2/AC-3/AC-7/AC-9)

### Task 7: `du_backend_signable` — the precondition probe (AC-7), DI-testable

**Objective:** `/api/status`→`auth_required:true` AND `/api/auth/providers` contains `basic`, with the curl
**injectable** so unit tests need no network, and **run in the target's context** for `--host mbp` (AC-9).

**Step 1 — failing tests** (stub `CURL_CMD` to return canned JSON):
```bash
test_signable_ok(){
  CURL_CMD='du_stub_curl_ok' du_backend_signable "http://mac-studio-m3u:9119"; assert_rc $? 0 "auth_required+basic -> signable"
}
test_signable_noauth(){
  CURL_CMD='du_stub_curl_noauth' du_backend_signable "http://x:9119"; assert_rc $? 1 "auth_required:false -> not signable"
}
# stubs: $1=url $2=path
du_stub_curl_ok(){ case "$2" in */api/status) echo '{"auth_required":true}';; */api/auth/providers) echo '{"providers":[{"name":"basic"}]}';; esac; }
du_stub_curl_noauth(){ case "$2" in */api/status) echo '{"auth_required":false}';; */api/auth/providers) echo '{"providers":[]}';; esac; }
test_signable_ok; test_signable_noauth
```

**Step 2 — FAIL. Step 3 — implement:**
```bash
du_curl(){  # <base_url> <path> ; overridable via CURL_CMD for tests. Correct Host header from the URL host.
  if [ -n "${CURL_CMD:-}" ]; then "$CURL_CMD" "$1" "$2"; return; fi
  local host; host=$(echo "$1" | sed -E 's#https?://([^/]+).*#\1#')
  curl -fsS -m 8 -H "Host: $host" "$1$2"
}
du_backend_signable(){  # <base_url> -> 0 if signable
  local base="$1" status providers
  status=$(du_curl "$base" "/api/status") || return 1
  [ "$(echo "$status" | jq -r '.auth_required // false')" = "true" ] || return 1
  providers=$(du_curl "$base" "/api/auth/providers") || return 1
  echo "$providers" | jq -e '.providers[]? | select(.name=="basic")' >/dev/null 2>&1 || return 1
  return 0
}
```
*(For `--host mbp`, `main` wraps `du_curl` to run over `ssh .88` so the probe executes in the MBP's network
context — AC-9. That wrapping is exercised in the Phase-3 live smoke, not the unit test.)*

**Step 4 — PASS. Step 5 — commit.**

### Task 8: `du_write_state` — the PENDING record the SCRIPT writes (AC-2, D-7)

**Objective:** the script only ever writes `signin_state=PENDING-SUPERVISED-SIGNIN` to
`~/.hermes/state/desktop-signin/<host>.json`. The agent flips CONFIRMED later (not the script).

**Step 1 — failing test:**
```bash
test_write_state(){
  local sdir; sdir=$(mktemp -d)
  DU_STATE_DIR="$sdir" du_write_state self abc123 0.17.0 "http://x:9119" ok
  local f="$sdir/self.json"
  [ -f "$f" ]; assert_rc $? 0 "state file written"
  assert_eq "$(jq -r .signin_state "$f")" "PENDING-SUPERVISED-SIGNIN" "script writes PENDING only"
  assert_eq "$(jq -r .sha "$f")" "abc123" "records sha"
}
test_write_state
```

**Step 2 — FAIL. Step 3 — implement:**
```bash
du_state_dir(){ echo "${DU_STATE_DIR:-$HOME/.hermes/state/desktop-signin}"; }
du_write_state(){  # <host> <sha> <version> <backend> <precondition>
  local host="$1" sha="$2" ver="$3" backend="$4" pre="$5" dir; dir="$(du_state_dir)"; mkdir -p "$dir"
  jq -n --arg h "$host" --arg s "$sha" --arg v "$ver" --arg b "$backend" --arg p "$pre" \
    --arg ts "$(date -u +%FT%TZ)" \
    '{host:$h, sha:$s, version:$v, backend:$b, precondition:$p, signin_state:"PENDING-SUPERVISED-SIGNIN", ts:$ts}' \
    > "$dir/$host.json"
}
```

**Step 4 — PASS. Step 5 — commit.**

### Task 9: `du_confirm_signin` — the AGENT's attestation flip (AC-2)

**Objective:** a *separate* function (called by Apollo when Ace attests, NOT by `main`) that flips the
record to CONFIRMED. Keeps the script↔agent boundary honest.

**Step 1 — failing test:**
```bash
test_confirm(){
  local sdir; sdir=$(mktemp -d); DU_STATE_DIR="$sdir" du_write_state self abc 0.17.0 url ok
  DU_STATE_DIR="$sdir" du_confirm_signin self
  assert_eq "$(jq -r .signin_state "$sdir/self.json")" "CONFIRMED" "agent flips to CONFIRMED"
  assert_eq "$(jq -r 'has(\"confirmed_at\")' "$sdir/self.json")" "true" "records confirmed_at"
}
test_confirm
```

**Step 2 — FAIL. Step 3 — implement:**
```bash
du_confirm_signin(){  # <host> — AGENT-only; flips PENDING->CONFIRMED with a timestamp
  local host="$1" dir; dir="$(du_state_dir)"; local f="$dir/$host.json"
  [ -f "$f" ] || { echo "no state record for $host" >&2; return 1; }
  local tmp; tmp=$(mktemp)
  jq --arg ts "$(date -u +%FT%TZ)" '.signin_state="CONFIRMED" | .confirmed_at=$ts' "$f" > "$tmp" && mv "$tmp" "$f"
}
```

**Step 4 — PASS. Step 5 — commit.**

---

## Phase 3 — `main` orchestration + LIVE smoke on Studio (AC-1/-4/-5/-6, the real proof)

### Task 10: wire `main` (arg parse, gate order, --host self path)

**Objective:** assemble the helpers into the §5 flow for `--host self`. Arg parse: `--host`, `--ref`,
`--force`. Order: resolve ref → precondition gates → FF+`npm ci` → identity gate (skip if current & !force)
→ `dist:mac` → backup+install+prune → assert identity → `test:desktop:platforms` → stage sign-in → write
PENDING → report.

**Step 1 — failing test** (arg-parse + dry-run planning only, no build):
```bash
test_main_argparse(){
  # DU_DRY_RUN=1 makes main print the resolved plan and exit 0 without building
  out=$(DU_DRY_RUN=1 bash "$SUT" --host self --ref fork/main 2>&1); assert_rc $? 0 "dry-run self exits 0"
  echo "$out" | grep -q "host=self"; assert_rc $? 0 "reports host"
  echo "$out" | grep -q "ref=fork/main"; assert_rc $? 0 "reports ref"
}
test_main_argparse
```
*(This test invokes the SUT as a real process, not lib-mode — so `main` runs.)*

**Step 2 — FAIL. Step 3 — implement `main`** (guarded by the existing `DESKTOP_UPDATE_LIB` check; honor
`DU_DRY_RUN`), calling the Task 2–9 helpers in order, `set -e`-style fail-fast at each gate with a LOUD
cause line. (Full body: ~80 lines — write it to match the §5 ASCII flow exactly; each gate echoes
`GATE <name>: OK|FAIL <cause>`.)

**Step 4 — PASS. Step 5 — commit.**

### Task 11: LIVE smoke on Studio — the end-to-end proof (AC-1, AC-4, AC-5, AC-6)

> This is the SMOKE-TEST-AS-A-STEP. Not a unit test — actually run it against the real Mac Studio.

**Step 1 — dry-run first:** `bash ~/.hermes/fleet/desktop-update.sh --host self --ref fork/main` with
`DU_DRY_RUN=1` — confirm the resolved ref SHA == `git -C ~/.hermes/runtime/hermes-agent rev-parse fork/main`,
and the identity gate correctly reports the installed `6351c7ed` as **stale** (not current).

**Step 2 — real run (SUPERVISED, Ace present):** `bash ~/.hermes/fleet/desktop-update.sh --host self`.
Watch for each `GATE …: OK`. Expected evidence, one row each:
- precondition backend-signable → OK (`auth_required:true`, `basic` present)
- `npm ci` at `apps/desktop` workspace root → 0
- `dist:mac` → `release/mac-arm64/Hermes.app` built, its `install-stamp.json` commit == pinned ref SHA
- backup: old app in `/Applications/.hermes-app-backups/Hermes.app.old-<ts>`; prune keeps ≤3
- install: `/Applications/Hermes.app` `Info.plist` version == repo version (0.17.0); embedded SHA == pinned
- `test:desktop:platforms` → node suite green
- state record `~/.hermes/state/desktop-signin/self.json` → `signin_state=PENDING-SUPERVISED-SIGNIN`

**Step 3 — supervised sign-in:** the script `open`s the fresh app; Ace signs in; on his "signed-in",
Apollo runs `du_confirm_signin self` → record flips to CONFIRMED. Capture the record.

**Step 4 — idempotency re-run (AC-4):** `bash desktop-update.sh --host self` again → prints "already
current" (installed SHA now == pinned), skips rebuild. Then a restore rehearsal:
`du` restore the backup, `du_launch_verify` → clean.

**Step 5 — if any variant fails:** debug, fix the script, re-run, capture the gotcha as a pitfall in the
`hermes-desktop-remote-backend` skill (or a new `desktop-update` skill). Do NOT proceed to commit until the
smoke row is green.

**Step 6 — commit** the smoke evidence into the PRD's closeout section / a `docs/desktop/SMOKE-studio.md`.

---

## Phase 4 — MBP arm + `deploy.sh --desktop` fold + skills (AC-8, AC-9, D-6)

### Task 12: `--host mbp` path — SSH-context precondition + install, degraded launch (AC-9, D-7)

**Objective:** `main` for `--host mbp`: SSH to `.88`; the backend-signable curl runs **on `.88`** (AC-9);
rebuild/install there; write PENDING with a "launch & sign in on the MBP" instruction; do NOT assert
foregrounded (no Aqua over SSH, D-7). MBP `op` token precondition checked.

**Step 1 — failing test** (stub the ssh transport):
```bash
test_mbp_curl_context(){
  # when host=mbp, du_curl must route through SSH_CMD (injected)
  SSH_CMD='echo SSH:' DU_HOST=mbp out=$(du_curl_hostaware "http://mac-studio-m3u:9119" "/api/status")
  echo "$out" | grep -q "^SSH:"; assert_rc $? 0 "mbp routes curl over ssh"
}
test_mbp_curl_context
```

**Step 2 — FAIL. Step 3 — implement** `du_curl_hostaware` (self → `du_curl`; mbp → `${SSH_CMD:-ssh
alexgierczyk@192.168.1.88} <the curl>`), and wire `main --host mbp` to use it + the SSH build path.

**Step 4 — PASS. Step 5 — commit.**

### Task 13: LIVE smoke on MBP (AC-9) — supervised

Run `bash ~/.hermes/fleet/desktop-update.sh --host mbp`. Evidence: the precondition curl ran MBP-side (log
shows it); build+install on `.88`; version/SHA asserted; state = PENDING with the MBP instruction; Ace
signs in on the MBP; `du_confirm_signin mbp`. Capture the row. (If MBP GUI/Aqua isn't available, the record
stays PENDING with the "launch on the MBP" note — honest, per D-7.)

### Task 14: fold into `deploy.sh --desktop` + alert routing (AC-8, D-6)

**Objective:** `deploy.sh --desktop` calls `desktop-update.sh`; a hard FAIL under `deploy.sh` posts LOUD to
#alerts; a PENDING stays quiet.

**Step 1 — failing test** (in `~/.hermes/fleet/test_deploy_sh.py` or a new bash test): `deploy.sh --desktop
--dry-run` invokes `desktop-update.sh`; a stubbed hard-FAIL triggers the notify path.

**Step 2 — FAIL. Step 3 — wire the hook** (the `--desktop` flag already parses; make it exec
`desktop-update.sh --host self` and map a non-zero hard-FAIL to `notify` #alerts, PENDING→quiet).

**Step 4 — PASS. Step 5 — commit.**

### Task 15: skills + docs (D-6, closeout)

- Add a "Desktop app update" step to the `hermes-agent` skill (pointer to `desktop-update.sh` + the
  supervised-signin model + `du_confirm_signin` for the agent's attest step).
- Extend `hermes-desktop-remote-backend` with the backend-signable precondition + the MBP-context curl gotcha.
- Run `prd-closeout` against the 9 ACs with the captured smoke evidence.

**Commit:** `docs(desktop): fold desktop-update into runbook skills + closeout`.

---

## Acceptance-criteria → task map
- AC-1 (build identity) → Tasks 2,3,4,11 · AC-2 (supervised, script/agent split) → Tasks 8,9,11,13
- AC-3 (no credential) → Task 7 (unauth probes only) + closeout gitleaks · AC-4 (reversible+retention) →
  Tasks 5,6,11 · AC-5 (`npm ci`) → Tasks 10,11 · AC-6 (mechanics) → Task 11 · AC-7 (preconditions) →
  Tasks 7,10 · AC-8 (alert routing) → Task 14 · AC-9 (MBP context) → Tasks 7,12,13.

## Notes / risks carried from the PRD
- **R-3 dissolved:** `install-stamp.json` already ships the git SHA — no app/electron-builder change (verified
  live). Phase 1 reads it; no embed to build.
- **Live cutover discipline:** the `dist:mac` build + `/Applications` swap is the mutating step — run
  supervised (Task 11/13), never folded into an unattended cron. `deploy.sh --desktop` default is
  operator-run.
- **The `dirty` flag:** a locally-built stamp with `dirty:true` is treated as never-"already-current"
  (Task 2) — forces a rebuild rather than trusting a non-reproducible identity.
