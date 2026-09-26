# CI runner venues: local is opt-in

Ruling (Ace, 2026-09-25 22:04 PT; reaffirmed 23:51 PT): hermes-agent CI runs on
**GitHub-hosted runners plus Blacksmith** for `pull_request`, `push` and `merge_group`.
The self-hosted boxes (ACE-AI, ACE-MEDIA) are busy with agents. An idle CI pool says
nothing about host load, so it is never a reason to move CI back onto them. Local
runners stay **registered but offline** until a dedicated CI box exists.

## How routing works

No workflow hardcodes a self-hosted label. Every route to the local pool goes through one
of three repo-variable paths:

| Path | Where | Parked value |
|---|---|---|
| `vars.CI_RUNNER_LABELS` | `runs-on` of the pool workflows (deploy-site, docker-lint, fleet-ci-fail-alert, history-check, js-autofix, lockfile-diff, publish-e2e-evidence, skills-index, supply-chain-audit) and `tests.yml` `e2e` | `["ubuntu-latest"]` |
| `tests.yml` slice matrix | `run_tests_parallel.py --generate-slices --self-hosted-labels "$CI_RUNNER_LABELS" --self-hosted-slots "$CI_SELF_HOSTED_SLOTS"` stamps `matrix.slice.runs_on`. The paid Blacksmith rung is the last `CI_BLACKSMITH_SLICES` hosted slices. | slices get `ubuntu-latest` / ARM / Blacksmith |
| merge_group placement | Only with `CI_OVERFLOW_PLACEMENT_ENABLED=true`. With no validated plan, `merge_group` falls back to the static split above (#1177), never to an all-local matrix. | same as the static split |

When `CI_RUNNER_LABELS` holds no `self-hosted` label, all three paths resolve to
GitHub-hosted or Blacksmith. The generator does not need a placement plan to produce the
Blacksmith slices: it reads `CI_BLACKSMITH_SLICES` directly. On the host, `ci-placement.py`
writes that variable.

**No local-hardware allowlist.** As of `19d782ddb3` no job in `.github/workflows/` needs local
hardware. The `ubuntu-latest-32-*` rows in `docker.yml` are GitHub larger runners, gated to
`github.repository == 'NousResearch/hermes-agent'`. `tests-os.yml` uses `macos-latest` and
`windows-latest`. If a job ever needs the local boxes, add it here with its reason. Do not
route it through `CI_RUNNER_LABELS`.

## What may bring local back

Only an **explicit, ledgered operator write**:

    scripts/ci_routing_ledger.py set ANG-Ventures/hermes-agent CI_RUNNER_LABELS \
      '["self-hosted","hermes-ci"]' --who <operator/session> --why '<reason + rollback>'

(hermes-home). Automation never makes this write:

- `ci-runner-autofailover.py` has `LOCAL_CI_OPTIN = {"ANG-Ventures/hermes-agent": False}`.
  When the pool is offline or saturated it still **fails over** to hosted, and it still pages.
  It never **fails back**, however long the pool sits idle.
- `ci-runner-labels-watch.sh` has `LOCAL_CI_OPTIN=0`. It treats a ledgered
  `["ubuntu-latest"]` as the expected value, so it sends no drift page and no
  "unowned off-pool park" page. An **unledgered** write still pages.

Turn local back on as the default (for example, once a dedicated CI box exists) by flipping
both flags in one reviewed hermes-home PR. A test pins the two flags equal.
