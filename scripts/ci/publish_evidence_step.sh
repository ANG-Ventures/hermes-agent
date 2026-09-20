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
WORK_DIR=$(mktemp -d "${RUNNER_TEMP:-/tmp}/publish-e2e-evidence.XXXXXX")
trap 'rm -rf "$WORK_DIR"' EXIT
LOG_FILE="$WORK_DIR/publish.log"

# Signatures GitHub uses for primary and secondary rate limits, across the
# REST envelope, gh's own error rendering, and the Python publisher's
# urllib error text.
RATE_LIMIT_PATTERN='API rate limit exceeded|secondary rate limit|rate limit exceeded for installation|HTTP 429|HTTP Error 429|X-RateLimit-Remaining: 0|RateLimitError'

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

  # --paginate, not the default first page: this CI run produces well over
  # 30 artifacts (16 test-slice artifacts alone, plus every reusable job's
  # review-status upload), so a single page can easily exclude the evidence
  # artifact and make a present artifact look absent.
  ARTIFACT_NAME=$(gh api --paginate "repos/$SOURCE_REPO/actions/runs/$SOURCE_RUN_ID/artifacts?per_page=100" \
    --jq '.artifacts[] | select(.expired == false and (.name | startswith("e2e-evidence-"))) | .name' \
    | head -n1)
  if [ -z "$ARTIFACT_NAME" ]; then
    echo "No E2E evidence artifact was produced for CI run $SOURCE_RUN_ID." >&2
    echo "Publishing evidence is this workflow's primary function, so this is a failure, not a skip." >&2
    return 1
  fi

  EVIDENCE_DIR="$WORK_DIR/e2e-evidence"
  mkdir -p "$EVIDENCE_DIR"
  gh run download "$SOURCE_RUN_ID" --repo "$SOURCE_REPO" --name "$ARTIFACT_NAME" --dir "$EVIDENCE_DIR"

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

if grep -qiE "$RATE_LIMIT_PATTERN" "$LOG_FILE"; then
  echo "::warning::E2E evidence not published: the shared installation rate-limit budget is exhausted (exit $rc). Not failing the step."
  exit 0
fi

echo "E2E evidence publishing failed with exit $rc and no rate-limit signature — failing the step." >&2
exit "$rc"
