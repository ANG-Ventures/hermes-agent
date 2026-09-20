#!/usr/bin/env bash
# Download the E2E evidence artifact for a CI run and attach it to the PR.
#
# Called by ``.github/workflows/publish-e2e-evidence.yml``. It lives in a
# file rather than inline in the workflow so its failure handling is
# testable (tests/ci/test_publish_evidence_step.py runs it with a stubbed
# ``gh``).
#
# Failure policy — the reason this wrapper exists:
#
# Every call here spends the shared installation rate-limit budget. When
# that budget ran out on 2026-09-19 this step failed 10 times and painted a
# red X on PRs that had nothing wrong with them. The first fix was a blanket
# ``continue-on-error: true`` on the step, which also swallowed missing
# artifacts, bad credentials and a broken gh extension — i.e. the workflow
# could report success while attaching nothing, which is its whole job.
#
# So exactly two outcomes exit 0:
#
#   1. The shared installation rate-limit budget is exhausted. Not our bug,
#      and not a reason to red an innocent PR.
#   2. No OPEN PR has this head at this SHA — a superseded run has nothing
#      to attach to.
#
# Everything else, INCLUDING a missing evidence artifact, exits non-zero.
# A missing artifact means an artifact-generation regression upstream, and
# reporting success for it is the precise silent failure this wrapper was
# written to prevent.
#
# The OSV scan's SARIF upload deliberately gets no such treatment:
# ``osv-scanner`` IS in ``all-checks-pass.needs``, so silencing it would
# hide a required check.

set -uo pipefail

: "${SOURCE_REPO:?SOURCE_REPO is required}"
: "${SOURCE_RUN_ID:?SOURCE_RUN_ID is required}"
: "${HEAD_OWNER:?HEAD_OWNER is required}"
: "${HEAD_BRANCH:?HEAD_BRANCH is required}"
: "${HEAD_SHA:?HEAD_SHA is required}"

# A private working directory. Without this the log and the evidence dir sit
# at predictable shared paths, so on a shared host another user can
# pre-create the log as a symlink and have ``tee`` truncate an unrelated
# file, or seed the evidence dir with stale content.
WORK_DIR=$(mktemp -d "${RUNNER_TEMP:-/tmp}/publish-e2e-evidence.XXXXXX") || {
  echo "mktemp -d failed under ${RUNNER_TEMP:-/tmp}; refusing to run with an unset working directory." >&2
  exit 1
}
if [ -z "$WORK_DIR" ] || [ ! -d "$WORK_DIR" ]; then
  # An empty WORK_DIR would redirect the log to /publish.log and download
  # into /e2e-evidence — on a privileged runner that can clobber root data.
  echo "mktemp -d produced no usable directory; refusing to run." >&2
  exit 1
fi
trap 'rm -rf "$WORK_DIR"' EXIT
LOG_FILE="$WORK_DIR/publish.log"

# Signatures GitHub uses for primary and secondary rate limits.
#
# Matched ONLY against gh/API transport output, never against the Python
# publisher's own messages: that publisher echoes filenames taken from the
# untrusted PR artifact, so a manifest naming a file `RateLimitError.png`
# would otherwise let a validation failure masquerade as a rate limit and
# exit 0. The publisher's exit code is therefore classified separately and
# is never eligible for rate-limit tolerance.
RATE_LIMIT_PATTERN='API rate limit exceeded|secondary rate limit|rate limit exceeded for installation|HTTP 429|HTTP Error 429|X-RateLimit-Remaining: 0'

# Marker printed immediately before the publisher runs. Everything after it
# is untrusted-influenced output and is excluded from the rate-limit grep.
PUBLISHER_MARKER='=== invoking publish_e2e_evidence.py ==='


publish() {
  set -euo pipefail

  # The run's own ``pull_requests`` payload is always empty for a fork PR,
  # so resolve the PR from its head reference instead. The head-SHA match
  # skips runs that a newer push superseded.
  # shellcheck disable=SC2016  # $ENV.HEAD_SHA is jq syntax, not a shell expansion.
  PR_NUMBER=$(gh api -X GET "repos/$SOURCE_REPO/pulls" \
    -f head="$HEAD_OWNER:$HEAD_BRANCH" -f state=open \
    --jq '.[] | select(.head.sha == $ENV.HEAD_SHA) | .number' \
    | head -n1)
  if [ -z "$PR_NUMBER" ]; then
    echo "No open pull request has head $HEAD_OWNER:$HEAD_BRANCH at $HEAD_SHA (CI run $SOURCE_RUN_ID)."
    return 0
  fi

  # A missing artifact is only a REGRESSION if the job that produces it
  # actually ran. `Desktop E2E` is currently hard-disabled in ci.yaml
  # (`if: ${{ false && ... }}`, pending upstream's #76627), so it reports
  # `conclusion: skipped` and no run produces an `e2e-evidence-*` artifact
  # at all. Treating "no artifact" as fatal unconditionally would therefore
  # red the publish workflow on 100% of PRs.
  #
  # So ask the producer. Skipped/absent => nothing was supposed to be
  # produced => clean skip. Producer ran and succeeded but produced no
  # evidence => a real regression => fail.
  # shellcheck disable=SC2016  # jq syntax, not shell expansion.
  PRODUCER_CONCLUSION=$(gh api --paginate \
    "repos/$SOURCE_REPO/actions/runs/$SOURCE_RUN_ID/jobs?per_page=100" \
    --jq '.jobs[] | select(.name | test("Desktop E2E")) | .conclusion' \
    | head -n1)

  ARTIFACT_NAME=$(gh api --paginate "repos/$SOURCE_REPO/actions/runs/$SOURCE_RUN_ID/artifacts?per_page=100" \
    --jq '.artifacts[] | select(.expired == false and (.name | startswith("e2e-evidence-"))) | .name' \
    | head -n1)
  if [ -z "$ARTIFACT_NAME" ]; then
    if [ "$PRODUCER_CONCLUSION" != "success" ]; then
      echo "No E2E evidence artifact for CI run $SOURCE_RUN_ID, and its producer did not run (Desktop E2E: ${PRODUCER_CONCLUSION:-absent}). Nothing to publish."
      return 0
    fi
    echo "Desktop E2E succeeded but produced no evidence artifact for CI run $SOURCE_RUN_ID." >&2
    echo "Publishing evidence is this workflow's primary function, so this is a failure, not a skip." >&2
    return 1
  fi

  EVIDENCE_DIR="$WORK_DIR/e2e-evidence"
  mkdir -p "$EVIDENCE_DIR"
  gh run download "$SOURCE_RUN_ID" --repo "$SOURCE_REPO" --name "$ARTIFACT_NAME" --dir "$EVIDENCE_DIR"

  echo "$PUBLISHER_MARKER"
  python3 scripts/ci/publish_e2e_evidence.py \
    --evidence-dir "$EVIDENCE_DIR" \
    --source-repo "$SOURCE_REPO" \
    --pr-number "$PR_NUMBER"
}

# Run in an explicit SUBSHELL, not a pipeline. A pipeline would work too
# (PIPESTATUS[0] is accurate), but with `publish` running in the current
# shell its `set -e` aborts this script before the log can be classified.
# The subshell confines errexit to the function and still yields its status.
( publish ) >"$LOG_FILE" 2>&1
rc=$?
cat "$LOG_FILE"

if [ "$rc" -eq 0 ]; then
  exit 0
fi

# Classify ONLY the transport portion of the log: everything from the
# publisher marker onward can echo attacker-chosen filenames.
sed "/$PUBLISHER_MARKER/,\$d" "$LOG_FILE" > "$WORK_DIR/transport.log"

if grep -qiE "$RATE_LIMIT_PATTERN" "$WORK_DIR/transport.log"; then
  echo "::warning::E2E evidence not published: the shared installation rate-limit budget is exhausted (exit $rc). Not failing the step."
  exit 0
fi

echo "E2E evidence publishing failed with exit $rc and no rate-limit signature — failing the step." >&2
exit "$rc"
