Title: Importing hermes_cli.psutil_android runs a full update and restarts the account's launchd gateway

## Summary

`hermes_cli/psutil_android.py` calls `stop_for_relaunch()` unconditionally at module level. Any
process that imports it runs the historical-updater takeover: an import smoke, a doc or
introspection tool, `pkgutil.walk_packages`, a test collector that imports every module. The
takeover does a dependency sync, publishes launchers into `~/.local/bin`, then runs
`finish_update` → `_restart_gateway_fleet_after_update`. On macOS that rewrites and restarts the
account's launchd gateway.

A second defect makes the restart hit the wrong install.
`_restart_launchd_gateway_after_update` (the invoking label) has no ownership check, though the
sibling loop in `_restart_macos_launchd_gateways` checks `launchd_label_foreign_home` (#93349).
`get_launchd_plist_path()` resolves through `pwd` (the real account home), and a HERMES_HOME
at `$HOME/.hermes` derives the bare `ai.hermes.gateway` label. A scratch home therefore
refreshes the real install's plist to point at itself and restarts the real gateway on it.
`_temp_home_in_service_definition` only refuses `/tmp`-style paths, so a scratch home on any
other volume gets through.

## Reproduction (main @ b20281dc, also ff40cfe272)

```
T=$(mktemp -d /some/non-tmp/dir/x.XXXX); mkdir -p $T/home/.hermes
HOME=$T/home HERMES_HOME=$T/home/.hermes python -c "import hermes_cli.psutil_android"
```

- Prints `→ Handing off to the new updater (package manager)...`, runs a full update into
  `$T/home/.hermes`, and exits.
- On a macOS account with an installed `ai.hermes.gateway` LaunchAgent, the update writes
  `~/Library/LaunchAgents/ai.hermes.gateway.plist` pointing at `$T/home/.hermes` and
  bootout/bootstraps it.
- Stack: `update_cmd_fleet._restart_launchd_gateway_after_update` → `gateway_launchd.launchd_restart`
  → `refresh_launchd_plist_if_needed` → `plist_path.write_text`.

We hit this on 2026-10-01: a whole-tree import check restarted a production gateway onto an
empty home for ~4 minutes.

## Proposed fix

1. Gate the module-level handoff the same way `tools.lazy_deps.install_specs` does:
   `if in_historical_update(): stop_for_relaunch()`. Every shipped caller imported this module
   inside `_cmd_update_impl`, so real old updaters still hand off before downloading.
2. In `_restart_launchd_gateway_after_update`, skip the invoking label when
   `launchd_label_foreign_home(current_label)` names another home. That is the same rule and
   message as the sibling loop.
3. Add a regression canary that imports every shipped module with HOME/HERMES_HOME in a temp dir
   and service managers stubbed, asserting zero service-manager calls, no child interpreter, and
   no writes outside the temp root. Then drop the `psutil_android` exclusion from the
   fresh-process import smoke.

A patch with tests is ready. A PR from Kyzcreig will follow.
