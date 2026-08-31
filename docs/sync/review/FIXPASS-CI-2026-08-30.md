# FIXPASS-CI 2026-08-30 — remaining heavy-ci reds on `sync/upstream-2026-08-29`

Branch: `sync/upstream-2026-08-29` · base HEAD at start: `e833cdb38e`
Source of the red set: heavy-ci run `33344885412` (Kyzcreig/hermes-heavy-ci), minus the 4
already closed in `e833cdb38e`.

Run recipe used for every verdict below (the shared venv is stale and gives false reds):

```bash
cd /Users/alexgierczyk/.hermes/worktrees/parity-2026-08-29
SB=$(mktemp -d); HERMES_HOME=$SB timeout 600 \
  uv run --no-progress --extra all --extra dev \
  python -m pytest <file> -q -o addopts= -p no:randomly
```

---

## Summary

| # | File | Tests | Class | Verdict |
|---|------|-------|-------|---------|
| 1 | `tests/agent/test_anthropic_borrowed_row_authority.py` | 6 | fork hermeticity fixture too narrow | **FIXED** (impl-side, `tests/conftest.py`) |
| 2 | `tests/agent/test_anthropic_credential_persist_failure.py` | 3 | same cause as #1 | **FIXED** (same one-line seam) |
| 3 | `tests/agent/test_anthropic_spent_rotation_verdict.py` | 6 | same cause as #1 | **FIXED** (same one-line seam) |
| 4 | `tests/plugins/blackbox/test_loader_e2e.py::test_full_turn_lifecycle_through_loader` | 1 | upstream profile-scoped plugin namespacing vs. fork test's hardcoded module name | **FIXED** (test retargeted to the real seam) |
| 5 | `tests/tools/test_browser_real_profile.py` (3 tests) | 3 | container-vs-VM env gap in upstream-verbatim code | **NOT MERGE DAMAGE** — root-caused, OPERATOR-DECISION below |

All 15 anthropic tests + the blackbox e2e are green, **confirmed on heavy-ci Linux**.
Two edits, both narrow.

### Verified on heavy-ci

Run [`33348781737`](https://github.com/Kyzcreig/hermes-heavy-ci/actions/runs/33348781737)
against `sync/upstream-2026-08-29` @ `54afd0c5e5`:

```
=== Summary: 3745 files, 46340 tests passed, 3 failed, 299 skipped (100% complete) ===
```

**22 reds → 3.** Every red in the assigned set is closed on Linux CI; the 3 survivors are
the pre-existing browser trio (§5), which were red for the same reasons in the baseline
run `33344885412` and are not attributable to this merge.

---

## 1–3. The anthropic cluster (15 tests, ONE root cause)

**File(s):** `tests/agent/test_anthropic_borrowed_row_authority.py`,
`tests/agent/test_anthropic_credential_persist_failure.py`,
`tests/agent/test_anthropic_spent_rotation_verdict.py`

**Cause.** These three suites are upstream-new (absent at fork baseline `de200ebbf5`).
Their test bodies AND every impl they exercise (`agent/anthropic_credentials.py`,
`agent/credential_pool.py`, `agent/credential_persistence.py`) are **byte-identical to
upstream target `26350357d7`** — verified with `git diff 26350357d7 HEAD -- <paths>`
(empty). So the divergence was never in the code under test.

The divergence is in `tests/conftest.py`. Upstream's autouse
`_neutralize_macos_keychain_creds` stubs **only** the Keychain source. The fork
extended it (correctly — a developer's real `~/.claude/.credentials.json` was leaking
a phantom `source=claude_code` row into every pool built in tests) to also suppress the
**file** source. But the fork's "is this a deliberate sandbox redirect?" check
re-derived the path itself:

```python
target = Path.home() / ".claude" / ".credentials.json"
if target.resolve() != _real_claude_creds.resolve():
    return _orig_read_file(...)     # sandbox → let the real reader run
return None                          # real home → suppress
```

That only recognises redirects done via `Path.home` / `HOME`. Upstream's new suites
redirect the **supported** way — monkeypatching the seam itself:

```python
monkeypatch.setattr(AA, "claude_code_credentials_path", lambda: cred_path)
```

With that style the re-derived `Path.home()/.claude/...` still resolves to the real
developer home, so the guard classified a legitimate tmp-file fixture as "the real
home" and suppressed the test's **own** fake credentials. `read_claude_code_credentials()`
returned `None`, `load_pool("anthropic")` produced an **empty** pool, and the cluster's
first failure was exactly the reported shape:

```
StopIteration on next(e for e in pool._entries if e.source == "claude_code")
```

Instrumented (not guessed) with a throwaway probe under the real conftest:

```
PLATFORM.SYSTEM = Linux
EXISTS  = True        # the fixture's tmp credentials file is on disk
FILE_READ = None      # ...but the hermeticity stub swallowed it
COMBINED = None
ENTRIES = []          # → empty pool → StopIteration
```

**Fix** (`tests/conftest.py`) — resolve the target through the same seam the reader
calls, instead of re-deriving it:

```python
_path_fn = getattr(_mod_ref, "claude_code_credentials_path", None)
target = Path(_path_fn()) if callable(_path_fn) else Path.home()/".claude"/".credentials.json"
```

This **preserves the fork's leak guard in full** — an unredirected read still resolves
to the real developer home and is still suppressed — while honouring every redirect
style (`Path.home`, `HOME`, and the seam patch). No production code changed; no fork
feature removed.

**Proof tail:**

```
tests/agent/test_anthropic_borrowed_row_authority.py
tests/agent/test_anthropic_credential_persist_failure.py
tests/agent/test_anthropic_spent_rotation_verdict.py
27 passed in 0.60s
```

**Blast radius** (neighbouring credential/auth suites):

```
tests/agent/test_anthropic_keychain.py tests/agent/test_anthropic_adapter.py tests/agent/test_credential_pool.py
177 passed in 8.29s

tests/hermes_cli/ -k "auth or credential or claude or anthropic"
781 passed, 3 skipped, 1 xpassed          (the 1 red, test_mcp_catalog_env_boundary, passes SOLO → batch artifact)

tests/agent/  (whole dir)
7051 passed, 23 skipped, 4 failed         (all 4 pre-existing / host artifacts — see "Not charged" below)
```

---

## 4. `tests/plugins/blackbox/test_loader_e2e.py::test_full_turn_lifecycle_through_loader`

**Cause.** Order-dependent, and the ordering is **intra-file** — the test passes solo
and fails whenever `test_blackbox_registers_hooks_and_cost_command` runs first
(bisected pairwise; no other file in the directory is a polluter).

The test is fork-authored (absent from `26350357d7`). It hardcoded the loader's module
name:

```python
bb = sys.modules["hermes_plugins.blackbox"]
```

Upstream's merge brought **profile-scoped plugin namespacing**
(`_BARE_MODULE_SCOPE` / `PluginManager._directory_module_name` in `hermes_cli/plugins.py`):
the bare `hermes_plugins.<slug>` name is handed to the *first* manager scope that claims
a slug; every later scope gets `hermes_plugins.<slug>__home_<sha256[:12]>`. Every test in
this file uses its own tmp `HERMES_HOME` = its own scope, so from the second loader test
onward the bare name still points at the **previous** test's module object. Instrumented:

```
[A] loaded module: hermes_plugins.blackbox
[B] loaded module: hermes_plugins.blackbox__home_6f055432bd58
[B] is sys.modules['hermes_plugins.blackbox']? False
```

The `monkeypatch.setattr(bb, "_turn_id", ...)` therefore patched a stale module and
silently no-op'd — precisely the failure mode the test's own comment warns about
("patch the instance the loader actually wired the hooks from, or the patches no-op").
The real hooks ran with a random turn id, so `store.get_turn("turn_e2e")` was `None`.

**Fix** (test-side; the impl is upstream's and is correct) — read the module off the
manager, which is the seam-accurate lookup and immune to both ordering and the
scope-suffix scheme:

```python
bb = mgr._plugins["blackbox"].module
assert bb is not None, "loader did not attach a module to the blackbox plugin"
```

**Teeth preserved (RED-proven).** The test exists to catch loader-wiring drift, so the
fix was mutation-checked: neutering the persistence wiring
(`store.insert_turn(record)` → `pass` in `plugins/blackbox/__init__.py`) still fails the
test. A conversion that can no longer fail would be worse than the flake.

```
mutant applied  →  1 failed, 3 passed      (guard still fires)
mutant reverted →  4 passed
```

**Proof tail:**

```
tests/plugins/blackbox/   141 passed in 3.40s        (was: 1 failed, 140 passed)
tests/plugins/ (whole)    2065 passed, 8 skipped, 1 failed
                          → the 1 (test_a2a_plugin path-routed card) passes SOLO; batch artifact
```

---

## 5. `tests/tools/test_browser_real_profile.py` (3 tests) — NOT MERGE DAMAGE

### OPERATOR-DECISION — env-conditional reds in upstream-verbatim code; no fix applied

These 3 do **not** reproduce on macOS under the mandated recipe
(`77 passed`), and they reproduce on heavy-ci for reasons that are **structural to the
runner, not to this merge**. Root-caused rather than guessed, after the post-fix
heavy-ci run kept exactly these 3 red.

**Provenance first.** The test file is **byte-identical to upstream target
`26350357d7`** (`git diff 26350357d7 HEAD -- tests/tools/test_browser_real_profile.py`
→ empty), and so are the three impl functions they depend on:
`hermes_cli/config.py::{_is_container,_secure_file,_secure_dir}` (md5-compared
per-function against upstream: IDENTICAL). The fork's diffs to `config.py`
(reasoning_effort validation, terminal.* env bridges) and `browser_connect.py`
(`--remote-allow-origins`, mock-keychain flags) do not touch either seam. The error
string in the third test is upstream's too (present in
`git show 26350357d7:tools/browser_tool.py`). **Nothing the merge did causes these.**

**Cause A — the two permission tests
(`test_snapshot_files_are_owner_only`, `test_existing_lax_snapshot_heals_on_refresh`).**
`_secure_file` / `_secure_dir` deliberately **no-op inside a container**:

```python
if is_managed() or _is_container():
    return          # Docker/Podman volume mounts often need broader permissions
```

`_is_container()` returns True on `/.dockerenv`, which exists on the heavy-ci
self-hosted runner. So `snapshot_real_profile()` never tightens the copies, they keep
`copy2`'s inherited 0644, and the tests' `mode & 0o077` walk reports every file.
Proven locally by simulating only the marker file (nothing else changed):

```
IS_CONTAINER (simulated /.dockerenv) = True
MODE after _secure_file = 0o644          # ← the carve-out fires; chmod skipped
```

The CI failure list matches exactly (`Cookies`, `Login Data`, `Local State`,
`Preferences`, `Network/Cookies`, the done-marker — all `0o644`).

**Cause B — `TestReviewRound3::test_relaunch_path_does_snapshot`.** Different cause, not
a permissions issue at all. The container has no Chrome/Chromium installed, so
`chromium_executable(browser)` returns `None` and `_real_profile_cdp()` returns
upstream's guard string instead of `None`:

```
assert "browser.use_real_profile is on, but the real browser binary for 'chrome'
        could not be found. Reinstall it or turn the toggle off." is None
```

The test patches `detect_default_chromium` and `snapshot_real_profile` but not
`chromium_executable`, so the real lookup runs against a binary-less image.

**Why no fix was applied here** (deliberate, per the brief's "preserve fork-critical
behavior / write it up rather than force it green" rule):

- Making Cause A pass would mean weakening or bypassing `_is_container()` — a
  **security/deployment carve-out that upstream owns**, whose whole purpose is to not
  fight Docker volume mounts. Doing that to satisfy a test would be exactly the
  "fix that destroys the feature it secures" the rubric rejects. The alternative
  (teaching the test to pin the non-container branch, à la the platform-branch class)
  is a legitimate change — but it is an edit to an **upstream-verbatim test of an
  upstream feature**, which belongs upstream, not smuggled into a parity merge.
- Cause B is a **missing-binary environment gap** in the runner image, not a code
  defect. The correct fixes are either installing Chromium in the heavy-ci image or
  patching `chromium_executable` in the test — again an upstream test change.
- These 3 are also **pre-existing**: they were red in the *baseline* run
  `33344885412` for the same reasons, i.e. they are not a regression this branch
  introduced.

**Recommended disposition (operator's call):** this is precisely what
`known-env-failures.txt` in `Kyzcreig/hermes-heavy-ci` exists for — its header says
"add entries ONLY with a documented container-vs-VM root cause." The file is currently
empty (`allowlisted-env: 0`), which is why the run gates red. Either
(a) add these 3 with the root causes above, or (b) fix the image (install Chromium) and
send the `_is_container` interaction upstream. Both are decisions outside this fix
pass's mandate.

---

## Not charged to this fix pass (measured, with evidence)

These surfaced during blast-radius sweeps. None is in the assigned red set, and none is
caused by the two edits above.

| Test | Why not charged |
|---|---|
| `tests/plugins/test_a2a_plugin.py::TestMultiAgentRouting::test_path_routed_agent_card_uses_prefix_and_canonical_path` | Passes **solo**; only reds inside a 2000-test single-process batch → cross-file state artifact (suite-runner-fidelity), not a defect. |
| `tests/hermes_cli/test_mcp_catalog_env_boundary.py::test_catalog_accepts_declared_credential` | Passes **solo**; same batch-shape artifact. |
| `tests/agent/test_curator_shared_e2e.py::test_post_split_skill_manage_patch_succeeds` | Passes when its file runs alone (3 passed); directory-batch artifact. |
| `tests/agent/test_org_skill_namespace.py::TestOrgSkillsAreEditableInPlace::{test_patch_is_allowed_and_applied,test_edit_tells_the_user_how_to_share_it_back}` | Pass when the file runs alone (26 passed); directory-batch artifact. |
| `tests/agent/test_system_prompt_restore.py::TestLegitimateFreshBuild::test_no_history_skips_db_and_builds_fresh` | **Class I host-environment artifact.** The operator's real `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` env var is not blanked by conftest, so `plugins/dashboard_auth/basic` logs a WARNING and the test's blanket `assert not [r for r in caplog.records if r.levelno >= WARNING]` trips. **Proven:** reverting `tests/conftest.py` to `HEAD` still fails it (so not mine), and `env -u HERMES_DASHBOARD_BASIC_AUTH_{USERNAME,PASSWORD,SECRET} pytest <nodeid>` → `1 passed`. Linux CI has no such env var. Not merge damage. |

Also note: `.gitleaks.toml` carries an uncommitted gitleaks-allowlist edit from a prior
session in this worktree (dated `2026-08-30 parity sync` in its own comment). It is
unrelated to this fix pass and was left exactly as found.

---

## Files modified

- `tests/conftest.py` — hermeticity guard resolves the claude-code credentials path
  through `claude_code_credentials_path()` instead of re-deriving `Path.home()/...`.
  Fork leak guard fully preserved; +1 closure parameter, +1 seam lookup.
- `tests/plugins/blackbox/test_loader_e2e.py` — loader module resolved from the manager
  rather than a hardcoded `sys.modules` key; RED-proven to still catch wiring drift.

No production code changed. No fork feature removed. No test deleted or weakened.
