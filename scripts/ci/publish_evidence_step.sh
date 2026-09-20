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
# So only the rate-limit class is tolerated. Every other failure still exits
# non-zero and reds the step.
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

WORK_DIR="${RUNNER_TEMP:-/tmp}"
LOG_FILE="$WORK_DIR/publish-e2e-evidence.log"

# Signatures GitHub uses for primary and secondary rate limits, across both
# the REST envelope and gh's own error rendering.
RATE_LIMIT_PATTERN='API rate limit exceeded|secondary rate limit|rate limit exceeded for installation|HTTP 429|X-RateLimit-Remaining: 0'

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

  ARTIFACT_NAME=$(gh api "repos/$SOURCE_REPO/actions/runs/$SOURCE_RUN_ID/artifacts" \
    --jq '.artifacts[] | select(.expired == false and (.name | startswith("e2e-evidence-"))) | .name' \
    | python3 -c 'import sys; print(next(iter(sys.stdin), "").strip())')
  if [ -z "$ARTIFACT_NAME" ]; then
    echo "No E2E evidence artifact was produced for CI run $SOURCE_RUN_ID."
    return 0
  fi

  EVIDENCE_DIR="$WORK_DIR/e2e-evidence"
  mkdir -p "$EVIDENCE_DIR"
  gh run download "$SOURCE_RUN_ID" --repo "$SOURCE_REPO" --name "$ARTIFACT_NAME" --dir "$EVIDENCE_DIR"

  python3 scripts/ci/publish_e2e_evidence.py \
    --evidence-dir "$EVIDENCE_DIR" \
    --source-repo "$SOURCE_REPO" \
    --pr-number "$PR_NUMBER"
}

publish 2>&1 | tee "$LOG_FILE"
rc=${PIPESTATUS[0]}

if [ "$rc" -eq 0 ]; then
  exit 0
fi

if grep -qiE "$RATE_LIMIT_PATTERN" "$LOG_FILE"; then
  echo "::warning::E2E evidence not published: the shared installation rate-limit budget is exhausted (exit $rc). Not failing the step."
  exit 0
fi

echo "E2E evidence publishing failed with exit $rc and no rate-limit signature — failing the step." >&2
exit "$rc"
