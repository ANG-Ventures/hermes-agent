# Vault page targeting: deployment and acceptance

## Root cause

Vault operations used the raw task ID instead of navigation's last owned session key, and selected the first origin/form-matching supervisor tab rather than the driver's current page. Exact `https://google.com` versus `https://accounts.google.com` mismatch is separately valid and remains enforced.

## Contract

Bind fill/save-login/enter-code to an operation-local original document witness, owned session key and session generation. Inspection runs through the navigation driver. Secret-bearing writes use only supervisor CDP, synchronously witness-guarded around the existing exact-origin guarded fill script; never CLI argv. Recheck driver and session after prompts/resolution AND supervisor selection. Nested save-login -> fill keeps its outer binding.

Browser Use (the default backend when browser-harness is installed) has its own current-target adapter. `browser_exec` records navigation's route per task: the harness daemon name (`BU_NAME`), the CDP endpoint and the vault supervisor it attached. Vault ops pin the page the harness daemon is attached to right now. A driver query, sent on stdin and never carrying secrets, reads the daemon's `connection_status` (target id + CDP session) and evaluates in that exact session. It sets the witness and records `(target_id, session_id, daemon pid)`. Every inspection and the pre-write rechecks re-query the daemon and refuse on any change to the target, the session, the daemon pid, the route or the registered supervisor. The supervisor must then select exactly the pinned target id, not just any document carrying the witness. Browser Use never starts or reuses a built-in agent-browser session for vault ops. With no CDP route (BU direct cloud, `BU_AUTOSPAWN`) there is no supervisor, and vault ops refuse.

Camofox stays fail-closed: it has no CDP target API (Firefox, custom HTTP API), so vault ops refuse with `page_binding_required` even if a stale built-in session remains.

This is not global navigation serialization. Replacing the pinned document removes its witness and refuses at the write. A tab/session change detected before dispatch refuses. A change after final admission can leave a write to the originally approved document; strict revocation of an already-dispatched write is not claimed. No expression may redirect to an unmarked/new document.

## Executed evidence

- Existing classifier/backend unit suites: 82 passed. They intentionally unwrap the binding decorator and do not certify targeting.
- Real Chromium/CDP fixtures: 22 passed, bare and ::local session keys, wrong-tab, mismatch, foreign owner, navigation/tab/session changes during resolution and target selection, unsupported backend refusal.
- Real agent-browser 0.26.0 subprocess through production `_run_browser_command` plus public vault handler: 8 passed, including success, mismatch, tab switch and session swap. Only package discovery is isolated from fleet PM state; transport is not replaced.
- Combined hosted Linux workflow run 42455359295 at c18bf80ebe50f64507a2f713c93cddc20b15260b: **112 passed, 0 skipped**, artifact vault-target-results/vault-target.xml. Later CI wiring-only changes require fresh head CI proof before merge.
- Original 16 CDP fixtures against base source: 9 failed, 7 control passes (previous worker evidence).

Workflow is reusable and called by the allowlisted CI orchestrator, included in all-checks-pass needs. Workers App lane supports workflow pushes; interactive lane does not.

Local XML: ~/.hermes/cache/scratch/vault-pr115-smoke.xml. Hosted XML: ~/.hermes/cache/scratch/pr115-ci-artifact/vault-target.xml.

## Stage/load boundary

No runtime edits or gateway restart. User authorizes land and stage only, not restart. Supported runtime writer: fleet/deploy.sh with explicit --no-restart, subject to its current deploy-window gate. Never set DEPLOY_WINDOW manually to evade that gate. If only a restart-approved window can advance disk, report the staging blocker rather than manufacturing approval.

Disk-stage is not live: already-imported browser vault modules need a gateway-specific approved restart, then real-account tool proof. Fresh-process fixtures do not prove a long-lived gateway loaded code.

Rollback: revert via new PR and FF-only deployment rail; preserve deployment backup, never reset/edit the runtime tree. Google credential website metadata may be corrected via supported op item edit --url after exact item metadata lookup and Google-domain verification; never retrieve its password or weaken matching.
