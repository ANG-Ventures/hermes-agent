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
| 5 | `tests/tools/test_browser_real_profile.py` (3 tests) | 3 | not reproducible | **NO DEFECT** — 77/77 green on this worktree |

All 15 anthropic tests + the blackbox e2e are green. Two edits, both narrow.

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

## 5. `tests/tools/test_browser_real_profile.py` — NO DEFECT FOUND

The 3 named tests (`TestReviewRound3::test_relaunch_path_does_snapshot`,
`TestSnapshotRealProfile::test_existing_lax_snapshot_heals_on_refresh`,
`TestSnapshotRealProfile::test_snapshot_files_are_owner_only`) do **not** reproduce on
this worktree under the mandated recipe:

```
tests/tools/test_browser_real_profile.py   77 passed in 3.39s
```

Verified before touching anything, per the brief. These are almost certainly
linux-only/umask- or ordering-sensitive on the CI runner (two of the three are about
file **mode** bits, which are umask-dependent). **No change made** — a speculative edit
to a green test would only risk disarming an owner-only-permissions guard. If the
triggered heavy-ci run reds them again, they want a linux-side reproduction, not a
macOS-side guess.

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
