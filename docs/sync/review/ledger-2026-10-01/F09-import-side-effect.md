# F09-import-side-effect ledger (parity 2026-10-01, round 4)

Card t_c5db8921 (source fix: t_e4cd4a23, package `docs/sync/review/FOLLOWUP-import-side-effect.md`).
Applied `git diff b20281dc..fork/daedalus/t_e4cd4a23-import-side-effect -- <9 paths> | git apply --3way` onto
the staged merge at HEAD `0a524a940c`. All 9 paths were byte-identical to upstream b20281dc in the index, so
the patch applied cleanly; the staged result equals the branch except for one fork-only fixup (row 6).

| path | status | note |
|---|---|---|
| hermes_cli/psutil_android.py | APPLIED | import-time `stop_for_relaunch()` gated on `_old_updater.in_historical_update()` |
| hermes_cli/update_cmd_fleet.py | APPLIED | `_restart_launchd_gateway_after_update` skips an invoking label whose plist pins another home (`launchd_label_foreign_home`) |
| tests/hermes_cli/test_import_side_effect_canary.py | APPLIED | new canary: import every shipped module under temp HOME, refuse service-manager/child-python/out-of-root writes |
| tests/hermes_cli/test_update_launchd_unloaded_gateway.py | APPLIED | `test_invoking_label_of_another_home_is_left_alone` |
| tests/e2e/core/upgrade/test_fresh_process_entrypoints.py | APPLIED | import-smoke exclusion of psutil_android dropped; AST assertion inverted |
| tests/hermes_cli/test_old_updater_shims.py | APPLIED + FORK FIXUP | branch did `sys.modules.pop("hermes_cli.psutil_android")`; the fork-only `tests/sys_modules_leak_gate.py` (absent upstream) errors on an unrestored purge, so changed to `monkeypatch.delitem(sys.modules, ..., raising=False)` (same as the sibling new test) |
| docs/sync/review/FOLLOWUP-import-side-effect{.md,.patch,.upstream-issue.md} | APPLIED | package docs |

Verification (test-gate --local, ~/.hermes/hermes-agent/venv py3.11):
- In the wt: `test_old_android_updater_handoffs_before_download`, `test_psutil_android_import_outside_an_update_starts_nothing`,
  `test_invoking_label_of_another_home_is_left_alone` PASS. `test_update_launchd_unloaded_gateway.py` 12 passed.
  `test_old_updater_shims.py` + launchd file: 39 failed / 59 passed; the 39 are exactly the pre-fix set, all
  `TEST BUG: file I/O against the REAL hermes home` (this worktree's gitdir lives under ~/.hermes; env artifact, same as t_e4cd4a23 reported).
- Canary, clean copy of the staged tree (`git checkout-index -a`): before fix (patch reversed) FAILS naming only
  `hermes_cli.psutil_android` (exit 1 at import + `_update_takeover.py` python-child); after fix PASSES.
- Canary, IN the wt: FAILS on `acp_adapter.entry: python-child [.../pm-runtime/... -c "import packaging, tomli_w, truststore; ..."]`.
  Cause (stack captured): `hermes_bootstrap.py:538 prepare_launch` → `venv_sync._finish_source_update` →
  `pm.ensure_tools_for_sync` → `pm/runtime_stage.py:44`. It arms only when the root has `.git` AND the
  gitignored `install-stamp.json` with `updateMechanism: self` (this wt has one, written 16:03 by a launch from it).
  A/B on an rsync of the full wt: `.git`+stamp → red; `.git`, no stamp → green. This is the "second, weaker
  import-time path" the FOLLOWUP doc scopes out (upstream launch-finishes-owed-update design; no service
  restart). Not a regression of this lane; the stamp is outside this card's 9-path edit scope, so not removed.
