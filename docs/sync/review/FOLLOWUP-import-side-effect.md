# FOLLOWUP: an import restarted the live gateway (parity sync 2026-10-01, card t_e4cd4a23)

Incident: on 2026-10-01 at 13:29 PT, the parity orchestrator t_e45c8c8d imported every module of
the merged tree under a sandboxed HOME/HERMES_HOME. The import rewrote
`~/Library/LaunchAgents/ai.hermes.gateway.plist` to point at the scratch home, restarted
Apollo's gateway on that home, and created `~/.local/bin/hermes-agent`.

## Culprit (reproduced)

How it was reproduced: each import ran in a subprocess under `sandbox-exec`. The sandbox
denied writes to `~/Library`, `~/.local`, `~/.hermes` and the shared worktree, and blocked
exec of `/bin/launchctl`. A logging `launchctl` stub sat first on PATH, and an audit hook
recorded every exec and every write outside the temp root, with stacks. I bisected the
full-tree import by attributing each event to the import that triggered it. Only one module
starts an update: **`hermes_cli.psutil_android`**.

Import-time call chain (stacks from the probe):

1. `hermes_cli/psutil_android.py:24` calls `stop_for_relaunch()` at module level, unconditionally.
2. `hermes_cli/_old_updater.py` `stop_for_relaunch` calls `_run_child`, which runs
   `subprocess.run([sys.executable, -I -S -B -X utf8, hermes_cli/_update_takeover.py, request.json, result.json])`.
3. In `_update_takeover.prepare`: `ensure_tools_for_sync`, then `sync_venv`, then
   `publish_launchers(root)` and `expose_cli`. That writes
   `$HOME/.local/bin/{hermes,hermes-acp,hermes-agent}`, each exec'ing `<tree>/.hermes/bin/hermes`.
4. `hermes_cli/update_finish.py` `finish_update` runs `_restart_gateway_fleet_after_update`,
   then `_restart_macos_launchd_gateways`, then `_restart_launchd_gateway_after_update`, then
   `launchd_restart()`, then `refresh_launchd_plist_if_needed()`, then
   `plist_path.write_text(new_plist)` and `launchctl bootout/bootstrap/kickstart`.

Why the sandboxed HOME did not contain it:
- `get_launchd_plist_path()` resolves the plist through `pwd.getpwuid(uid).pw_dir`, so it is
  the REAL account's LaunchAgents directory whatever `HOME` says.
- When HERMES_HOME sits at `$HOME/.hermes`, it matches the native-default shape, so
  `_profile_suffix()` returns `""` and the label is the bare `ai.hermes.gateway`, which is
  Apollo's.
- `_restart_launchd_gateway_after_update` (the invoking label) has **no ownership check**. The
  sibling loop in `_restart_macos_launchd_gateways` checks `launchd_label_foreign_home(label)`,
  but it skips `current_label`. The plist pinned `HERMES_HOME=/Users/alexgierczyk/.hermes`, a
  foreign home, so the check would have caught it.
- `_temp_home_in_service_definition` only refuses `/tmp`, `/var/tmp` and `tempfile.gettempdir()`.
  A scratch home under `/Volumes/fleet-scratch/...` passes it.

Probe evidence:
- An import of the merged-tree COPY, with the plist write sandbox-denied, produced the receipt
  `gateway_restart.phase_error = "[Errno 1] Operation not permitted:
  '/Users/alexgierczyk/Library/LaunchAgents/ai.hermes.gateway.plist'"`.
- The stack for that write ran `update_cmd_fleet.py:913 launchd_restart()` →
  `gateway_launchd.py:781 refresh_launchd_plist_if_needed` → `gateway_launchd.py:549 plist_path.write_text`.
- The live plist mtime stayed at 13:33:20, the recovery time, and the probes left no
  `~/.local/bin/hermes-agent`.

A second, weaker import-time path: `agent.agent_init` → `agent.process_bootstrap` →
`hermes_bootstrap` (module level, line 538) → `venv_sync.prepare_launch` → `_finish_source_update`.
That path syncs dependencies and publishes launchers whenever the tree carries an
`install-stamp.json` with `updateMechanism: self` (the merged wt has one, written 14:15). It does
not restart services, and it exits before the restart. It is upstream's "every launch finishes an
owed update" design, not an import accident, so it is out of scope for this package. Note it for
upstream.

## Ownership

| piece | upstream b20281dc | fork/main before merge (HEAD^1) | staged merge |
|---|---|---|---|
| `hermes_cli/psutil_android.py` (module-level stop) | yes | **no**: the fork had the real installer, with no import side effect | identical to upstream |
| `hermes_cli/_old_updater.py`, `_update_takeover.py`, `update_finish.py`, `venv_sync.py`, `update_cmd_fleet.py`, `gateway_launchd.py`, `update_fleet_scope.py` | yes | absent | identical to upstream |

The code is **upstream-owned** and the **sync merge introduced it** into the fork. Upstream
commits: 529050eab7 ("stop retired installers and finish on fresh launch", 2026-09-11) and
cdf48a1fd2 (2026-09-19, which restored the shim). Upstream main ff40cfe272 (2026-10-02) still
carries both defects.

## Fix (patch: `FOLLOWUP-import-side-effect.patch`, branch `fork/daedalus/t_e4cd4a23-import-side-effect`)

1. `hermes_cli/psutil_android.py`: gate the import-time handoff on
   `_old_updater.in_historical_update()`. This is the same frame-walk gate
   `tools.lazy_deps.install_specs` already uses. Every shipped caller of this module ran inside
   `_cmd_update_impl` (`main.py` before 927463efcc, `update_cmd.py` after, and
   `update_cmd_deps._sync_python_dependencies_after_pull` called from it until 92686159d1). A
   real old updater therefore still hands off before its download, and any other importer gets
   the inert module.
2. `hermes_cli/update_cmd_fleet.py` `_restart_launchd_gateway_after_update`: before touching
   the invoking label, apply the same `launchd_label_foreign_home` rule the sibling loop already
   applies (#93349). A plist pinning another home is skipped with the standard `↷ ... left alone`
   line. This is defence in depth: it also covers a real `hermes update` run from a scratch home
   shaped like the native default.

## Tests

- NEW `tests/hermes_cli/test_import_side_effect_canary.py`: this is the canary the card asked
  for. It imports every module of every package in `pyproject [tool.setuptools.packages.find]`
  (≈2000 modules) in ONE fresh interpreter. HOME and HERMES_HOME point at a temp dir, and
  `launchctl/systemctl/loginctl/schtasks` are stubbed on PATH. An audit hook RECORDS AND
  REFUSES service-manager execs, child Python interpreters (they escape the hook) and writes
  outside the temp root. The test fails on any refusal, any stub call, or any import that exits
  the process. It runs in ~11 s.
- `test_old_updater_shims.py`: `test_old_android_updater_handoffs_before_download` now runs
  the frozen old function under a historical `_cmd_update_impl`-shaped frame, which is how it
  shipped. A new test, `test_psutil_android_import_outside_an_update_starts_nothing`, covers
  the plain-import case.
- `test_update_launchd_unloaded_gateway.py`: a new test,
  `test_invoking_label_of_another_home_is_left_alone`, checks that the plist bytes are
  unchanged, that no restart or launchctl call happens, and that the `left alone` line is
  printed.
- `tests/e2e/core/upgrade/test_fresh_process_entrypoints.py`: the import-smoke exclusion of
  `psutil_android` is dropped. The module is safe to import now, and the AST assertion is
  inverted so the unconditional top-level call cannot come back.

Results (Mac Studio, venv py3.11, everything run under `sandbox-exec` with the deny profile):
- Before the fix (b20281dc): the canary FAILS and names only `hermes_cli.psutil_android`
  (exit code 1 plus the `_update_takeover.py` python-child). The two new unit tests also FAIL.
- After the fix: canary, shim and launchd tests give 18 passed.
- The broader before/after diff over `test_old_updater_shims.py`,
  `test_update_launchd_unloaded_gateway.py` and `test_old_updater_compat_surface.py` was
  39 failed / 627 passed before and 39 failed / 630 passed after. The same 39 fail both times,
  and every one is the local `TEST BUG: file I/O against the REAL hermes home` guard, which
  trips because this worktree's gitdir lives under `~/.hermes`. That is an environment
  artifact, not a regression.
- Patch applied to a COPY of the merged wt (t_e45c8c8d/wt @ b6c8bfe8ab, staged): canary + the
  same targeted tests = **15 passed**. Unpatched, the merged copy fails the canary on
  `hermes_cli.psutil_android` exactly as upstream does.

## Carrying it

For the parity orchestrator: apply `FOLLOWUP-import-side-effect.patch` onto the staged merge
with `git apply --index`. Every touched file in the staged tree is byte-identical to upstream
b20281dc. The upstream PR, filed as Kyzcreig, is the long-term home. Drop the fork delta once
upstream lands it.
