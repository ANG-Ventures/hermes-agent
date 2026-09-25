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
# So the policy is three-valued, and the middle value is NOT a silent green:
#
#   RED     — any real publish defect: missing artifact from a producer that
#             ran, bad credentials, a broken gh extension, a validation
#             failure. Exits non-zero.
#   NEUTRAL — a transient GitHub API condition (rate limit or 5xx) that
#             survived a bounded retry. Exits 0 so an innocent PR is not
#             reddened, but ALWAYS leaves a visible trace: a ``neutral``
#             check run on the head SHA, a ``::warning::`` annotation and a
#             job-summary line. Never a bare green.
#   GREEN   — evidence published, or nothing was ever meant to exist
#             (no open PR at this SHA; every producer skipped).
#
# Everything else, INCLUDING a missing evidence artifact from a producer
# that actually ran, is RED. A missing artifact means an artifact-generation
# regression upstream, and reporting success for it is the precise silent
# failure this wrapper was written to prevent.
#
# The OSV scan's SARIF upload deliberately gets no such treatment:
# ``osv-scanner`` IS in ``all-checks-pass.needs``, so silencing it would
# hide a required check. This workflow is not in that list.

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
# The publisher's own output is kept OUT of LOG_FILE: it echoes filenames
# from the untrusted PR artifact, and LOG_FILE is what the transient grep
# reads. It is printed for humans but never classified.
PUBLISHER_LOG="$WORK_DIR/publisher.log"
# Same treatment for `gh run download`, which echoes PR-controlled artifact
# names and paths on failure.
UNTRUSTED_LOG="$WORK_DIR/untrusted.log"

# Signatures GitHub uses for primary and secondary rate limits, and for the
# server-side 5xx class. Both are transient: neither is a defect in this
# repository, and neither should red an innocent PR.
#
# Matched ONLY against gh/API transport output, never against the Python
# publisher's own messages: that publisher echoes filenames taken from the
# untrusted PR artifact, so a manifest naming a file `RateLimitError.png`
# would otherwise let a validation failure masquerade as a rate limit and
# exit 0. The publisher's exit code is therefore classified separately and
# is never eligible for transient tolerance.
RATE_LIMIT_PATTERN='API rate limit exceeded|secondary rate limit|rate limit exceeded for installation|HTTP 429|HTTP Error 429|X-RateLimit-Remaining: 0'
# 5xx server errors only: the whole 500-599 range EXCEPT 501.
#
# 501 (Not Implemented) is excluded deliberately and must stay excluded: it
# means the request itself is wrong, which retrying cannot fix. The former
# `50[0-4]` class was wrong in both directions — it matched 501 (so a
# malformed request was retried and then exited 0 as a tolerated transient,
# publishing nothing), and it missed 505/507/etc (so a genuine server-side
# fault reddened an innocent PR).
#
# `50[02-9]|5[1-9][0-9]` is exactly 500-599 minus 501, the same set as
# TRANSIENT_SERVER_ERROR_CODES in scripts/ci/publish_e2e_evidence.py; a
# test pins the two against each other across all 100 codes.
#
# Boundary-anchored so a longer status string cannot prefix-match: without
# `[^0-9]`, `HTTP 500` would also match a hypothetical `HTTP 5001`.
SERVER_ERROR_PATTERN='HTTP (Error )?(50[02-9]|5[1-9][0-9])([^0-9]|$)|Bad gateway|Service Unavailable|Gateway Timeout'
TRANSIENT_PATTERN="$RATE_LIMIT_PATTERN|$SERVER_ERROR_PATTERN"

# Exit code the Python publisher uses for "transient GitHub API condition"
# (rate limit or 5xx). Kept in sync with TRANSIENT_EXIT_CODE in
# scripts/ci/publish_e2e_evidence.py; a test pins the two together.
PUBLISHER_TRANSIENT_RC=75

# Exit code for "a third-party binary did not match its pinned digest".
# Distinct so it can never be confused with a transport failure, and
# deliberately outside the transient class: a mismatched binary is a
# supply-chain event, not an API hiccup, and must always go RED.
SUPPLY_CHAIN_EXIT_CODE=78

# gh-image release pin. The TAG is mutable, so the digest below — not the
# tag — is what actually constrains which bytes get executed.
#
# Refresh procedure when bumping the tag:
#   gh api repos/drogers0/gh-image/releases/tags/<tag> \
#     --jq '.assets[] | select(.name|startswith("linux-")) | .name+" "+.digest'
GH_IMAGE_REPO='drogers0/gh-image'
GH_IMAGE_TAG='v1.2.0'
# Digests read from the release API on 2026-09-20 (tag v1.2.0, which then
# resolved to commit 44f4b93ecbbe22de6c45fa2f62f519aee564ca8c).
GH_IMAGE_SHA256_linux_amd64='0505f8c46d63bd603a445fdbfdd6be45e75a80778d97f1edf3580697fa6b7919'
GH_IMAGE_SHA256_linux_arm64='f36fd26e1920e217eb2bd1d5f7e0378c00f64214f3b011f5697d883943f0d1ee'
# The publish job runs on `CI_RUNNER_LABELS` (the ACE-AI self-hosted Linux
# pool) or ubuntu-latest, so only the Linux assets are pinned. Any other
# architecture fails loudly rather than installing an unpinned binary.
case "$(uname -m 2>/dev/null)" in
  x86_64|amd64)
    GH_IMAGE_ASSET='linux-amd64'
    GH_IMAGE_SHA256="$GH_IMAGE_SHA256_linux_amd64" ;;
  aarch64|arm64)
    GH_IMAGE_ASSET='linux-arm64'
    GH_IMAGE_SHA256="$GH_IMAGE_SHA256_linux_arm64" ;;
  *)
    GH_IMAGE_ASSET=''
    GH_IMAGE_SHA256='' ;;
esac
# Tests pin a known asset+digest pair so the verification path is exercised
# regardless of the host architecture the suite happens to run on.
GH_IMAGE_ASSET="${PUBLISH_EVIDENCE_GH_IMAGE_ASSET:-$GH_IMAGE_ASSET}"
GH_IMAGE_SHA256="${PUBLISH_EVIDENCE_GH_IMAGE_SHA256:-$GH_IMAGE_SHA256}"

# Bounded retry. A secondary rate limit and a 5xx both clear in seconds, so
# a short retry recovers the common case for ~20s of wall clock against the
# job's timeout-minutes: 10.
#
# A PRIMARY budget exhaustion does NOT clear in seconds — its reset is up to
# an hour away — so retrying it is pure waste. `_transient_wait_is_hopeless`
# asks the trusted rate_limit endpoint and short-circuits straight to
# NEUTRAL rather than burning the clock on a wait that cannot succeed.
MAX_ATTEMPTS=3
RETRY_BACKOFF_SECONDS=(5 15)
# Tests override the backoff so the retry path is exercised in milliseconds
# rather than 20 seconds of real sleeping. Production never sets this, and
# the default above is what ships; a test pins that default so shrinking it
# here cannot silently become the real behaviour.
if [ -n "${PUBLISH_EVIDENCE_RETRY_BACKOFF:-}" ]; then
  # shellcheck disable=SC2206  # deliberate word-splitting of a numeric list.
  RETRY_BACKOFF_SECONDS=(${PUBLISH_EVIDENCE_RETRY_BACKOFF})
fi


ensure_extension() {
  # The gh-image extension install also spends the shared installation
  # budget. It used to run as a separate workflow step OUTSIDE this
  # classifier, so a budget exhaustion there reddened the PR before the
  # tolerance could ever apply.
  #
  # SUPPLY CHAIN — why this is not `gh extension install --pin v1.2.0`:
  #
  # A git tag is mutable. `--pin v1.2.0` re-resolves the tag at install
  # time, so whoever controls that upstream repository can retarget it (or
  # replace the release asset) and this step would execute the new binary
  # with the workflow token's permissions. Documenting the expected commit
  # in a comment does not constrain anything at runtime.
  #
  # `--pin` cannot fix this: MEASURED 2026-09-20, `gh extension install
  # drogers0/gh-image --pin 44f4b93ecbbe22de6c45fa2f62f519aee564ca8c` exits
  # 1 with "Could not find a release of drogers0/gh-image for 44f4b9…".
  # gh-image ships release binaries, and for a BINARY extension gh's
  # `--pin` accepts a release tag only — a commit is accepted for script
  # extensions. So a commit pin is simply unavailable here.
  #
  # What IS immutable is the content. So: download the release asset,
  # verify its SHA-256 against the digest pinned below, and only then
  # install it from a local directory (which gh installs verbatim — the
  # bytes on disk after a local install are byte-identical to the asset,
  # verified 2026-09-20). A retargeted tag or a swapped asset fails the
  # digest check and the step goes RED. It is never tolerated as transient:
  # a binary that does not match its pin is a supply-chain event, not a
  # GitHub API hiccup.
  if extension_present; then
    return 0
  fi

  local asset_dir ext_dir asset actual
  if [ -z "$GH_IMAGE_ASSET" ] || [ -z "$GH_IMAGE_SHA256" ]; then
    echo "No pinned gh-image asset for architecture $(uname -m 2>/dev/null); refusing to install an unverified binary." >&2
    return "$SUPPLY_CHAIN_EXIT_CODE"
  fi
  asset_dir="$WORK_DIR/gh-image-download"
  ext_dir="$asset_dir/gh-image"
  rm -rf "$asset_dir"
  mkdir -p "$ext_dir"
  asset="$ext_dir/gh-image"

  # The download itself spends the budget, so a failure here stays inside
  # the classifier and is eligible for ordinary transient tolerance.
  gh release download "$GH_IMAGE_TAG" --repo "$GH_IMAGE_REPO" \
    --pattern "$GH_IMAGE_ASSET" --output "$asset" --clobber || return $?

  actual=$(sha256_of "$asset") || {
    echo "Could not compute a SHA-256 for the downloaded gh-image asset; refusing to install it." >&2
    return "$SUPPLY_CHAIN_EXIT_CODE"
  }
  if [ "$actual" != "$GH_IMAGE_SHA256" ]; then
    # Deliberately loud and deliberately NOT transient.
    echo "gh-image $GH_IMAGE_TAG ($GH_IMAGE_ASSET) does not match its pinned digest." >&2
    echo "  expected sha256: $GH_IMAGE_SHA256" >&2
    echo "  actual   sha256: $actual" >&2
    echo "Refusing to execute an unverified third-party binary with the workflow token." >&2
    return "$SUPPLY_CHAIN_EXIT_CODE"
  fi

  chmod +x "$asset"
  # `gh extension install` treats ONLY "." as a local directory; any other
  # argument -- including an absolute path -- is parsed as [HOST/]OWNER/REPO
  # and fails with 'expected the "[HOST/]OWNER/REPO" format'. That broke every
  # publish on a runner without a cached install (2026-09-23: 3 of the last 12
  # main runs). Install from inside the directory.
  (cd "$ext_dir" && gh extension install .)
}


extension_present() {
  # `gh extension list` renders an extension installed from a local
  # directory WITHOUT its owner/repo column — measured 2026-09-20, a local
  # install prints `gh image\t\t` where a remote one prints
  # `gh image\tdrogers0/gh-image\tv1.2.0`. Matching on the repo slug
  # therefore misses our own install and the retry loop would reinstall on
  # every attempt. Match the COMMAND gh reports, which is stable across
  # both install kinds.
  gh extension list 2>/dev/null | grep -qE '(^|[[:space:]])gh[[:space:]]+image([[:space:]]|$)'
}


sha256_of() {
  # Runners have sha256sum (Linux) or shasum (macOS); neither is
  # guaranteed, so require one rather than silently skipping the check.
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    return 1
  fi
}


publish() {
  set -euo pipefail

  ensure_extension

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
  # ALL matching jobs, not just the first: a matrix or a renamed sibling can
  # produce several, and if the first is `skipped` while another actually ran
  # and delivered nothing, skipping on the first would publish silence.
  # shellcheck disable=SC2016  # jq syntax, not shell expansion.
  PRODUCER_CONCLUSIONS=$(gh api --paginate \
    "repos/$SOURCE_REPO/actions/runs/$SOURCE_RUN_ID/jobs?per_page=100" \
    --jq '.jobs[] | select(.name | test("Desktop E2E")) | .conclusion')
  PRODUCER_COUNT=$(printf '%s' "$PRODUCER_CONCLUSIONS" | grep -c . || true)
  NON_SKIPPED=$(printf '%s\n' "$PRODUCER_CONCLUSIONS" | grep -vx 'skipped' | grep -c . || true)

  # EVERY unexpired evidence artifact, not just the first.
  #
  # `head -n1` published one artifact and reported success. Today the
  # producer uploads exactly one (`e2e-evidence-${{ github.sha }}`, a
  # single non-matrix job in e2e-desktop.yml), so that is currently
  # equivalent — but a matrix dimension or a renamed sibling silently turns
  # it into a partial publish: some evidence attached, the rest dropped,
  # and a green step. Enumerate them all and publish each.
  ARTIFACT_NAMES=$(gh api --paginate "repos/$SOURCE_REPO/actions/runs/$SOURCE_RUN_ID/artifacts?per_page=100" \
    --jq '.artifacts[] | select(.expired == false and (.name | startswith("e2e-evidence-"))) | .name')
  ARTIFACT_COUNT=$(printf '%s' "$ARTIFACT_NAMES" | grep -c . || true)
  if [ "$ARTIFACT_COUNT" -eq 0 ]; then
    if [ "$PRODUCER_COUNT" -eq 0 ]; then
      # No job matched the name filter. That is NOT evidence of a deliberate
      # skip — the job may have been renamed or removed, or the filter may
      # have drifted. Fail loudly rather than reporting success while
      # attaching nothing.
      echo "No Desktop E2E job found in CI run $SOURCE_RUN_ID; the producer may have been renamed or removed." >&2
      echo "Refusing to report success with no evidence attached." >&2
      return 1
    fi
    if [ "$NON_SKIPPED" -eq 0 ]; then
      # EVERY matching producer was skipped — deliberately disabled in
      # ci.yaml (`if: ${{ false && ... }}`). Nothing was meant to exist.
      echo "No E2E evidence artifact for CI run $SOURCE_RUN_ID: all $PRODUCER_COUNT Desktop E2E job(s) were skipped. Nothing to publish."
      return 0
    fi
    # At least one producer RAN and did not deliver.
    echo "Desktop E2E ran ($NON_SKIPPED of $PRODUCER_COUNT job(s) not skipped) and produced no evidence artifact for CI run $SOURCE_RUN_ID." >&2
    echo "Publishing evidence is this workflow's primary function, so this is a failure, not a skip." >&2
    return 1
  fi

  # Each producer that RAN must have delivered. Fewer artifacts than
  # non-skipped producers means at least one ran and uploaded nothing —
  # publishing the others and exiting 0 would be the partial-success
  # silence this wrapper exists to prevent.
  if [ "$NON_SKIPPED" -gt 0 ] && [ "$ARTIFACT_COUNT" -lt "$NON_SKIPPED" ]; then
    echo "$NON_SKIPPED Desktop E2E job(s) ran but only $ARTIFACT_COUNT evidence artifact(s) exist for CI run $SOURCE_RUN_ID." >&2
    echo "Publishing a partial result as success would hide the missing evidence." >&2
    return 1
  fi

  # Iterate over a here-doc rather than a pipeline: a `while read` on the
  # right of a pipe runs in a subshell, so a `return` inside it would not
  # propagate out of `publish`.
  while IFS= read -r ARTIFACT_NAME; do
    [ -n "$ARTIFACT_NAME" ] || continue
    publish_one_artifact "$ARTIFACT_NAME" "$PR_NUMBER" || return $?
  done <<EOF
$ARTIFACT_NAMES
EOF
  return 0
}


publish_one_artifact() {
  # Download and publish ONE evidence artifact. Split out of `publish` so
  # the multi-artifact loop above cannot accidentally share mutable state
  # between iterations.
  local ARTIFACT_NAME="$1" PR_NUMBER="$2"
  local EVIDENCE_DIR download_rc publisher_rc PROBE_LOG
  EVIDENCE_DIR="$WORK_DIR/e2e-evidence"
  rm -rf "$EVIDENCE_DIR"
  mkdir -p "$EVIDENCE_DIR"
  # `gh run download` echoes PR-CONTROLLED artifact names and file paths on
  # failure, so its output must not reach the classified transport log
  # either — a crafted path could otherwise spoof a rate-limit signature.
  # Its exit code is the trusted signal; a real transient condition there is
  # caught by the dedicated probe below.
  set +e
  gh run download "$SOURCE_RUN_ID" --repo "$SOURCE_REPO" \
    --name "$ARTIFACT_NAME" --dir "$EVIDENCE_DIR" >"$UNTRUSTED_LOG" 2>&1
  download_rc=$?
  set -e
  if [ "$download_rc" -ne 0 ]; then
    # Distinguish a transient API condition from a real download failure
    # with a TRUSTED probe rather than by grepping attacker-influenced
    # output.
    #
    # The probe's FAILURE is not the signal — its OUTPUT is. Any probe
    # failure used to be rewritten into a hard-coded "API rate limit
    # exceeded" line, which `is_transient` then matched downstream. A
    # revoked or expired credential fails the download AND the probe, so
    # it was retried and finally exited 0 having published nothing: the
    # masked failure this wrapper exists to prevent, one layer down.
    #
    # Emitting the probe's real output instead lets the ordinary
    # classifier decide. That output is trusted — it names only
    # $SOURCE_REPO, never anything from the artifact — so a rate limit
    # still reaches NEUTRAL while a 401 carries no transient signature and
    # stays RED. No fabricated line, and no second classifier to drift.
    PROBE_LOG="$WORK_DIR/probe.log"
    if ! gh api "repos/$SOURCE_REPO" --jq '.full_name' >"$PROBE_LOG" 2>&1; then
      echo "The artifact download failed (exit $download_rc) and the control API call also failed:" >&2
      cat "$PROBE_LOG" >&2
      return "$download_rc"
    fi
    echo "Artifact download failed (exit $download_rc); see the untrusted log above." >&2
    return "$download_rc"
  fi

  # The publisher signals a transient condition with a DEDICATED EXIT CODE
  # rather than log text: its stdout echoes filenames taken from the
  # untrusted PR artifact, so classifying it by grep would let a manifest
  # spoof the tolerance.
  # Its output goes to a SEPARATE file so it never reaches the transport
  # log that the transient grep reads.
  set +e
  python3 scripts/ci/publish_e2e_evidence.py \
    --evidence-dir "$EVIDENCE_DIR" \
    --source-repo "$SOURCE_REPO" \
    --pr-number "$PR_NUMBER" >"$PUBLISHER_LOG" 2>&1
  publisher_rc=$?
  set -e
  return "$publisher_rc"
}


is_transient() {
  # $1 = exit code of the attempt.
  #
  # Two independent signals, and BOTH are trusted:
  #   * the publisher's dedicated exit code (its log text is untrusted, so
  #     the code is the only thing that can carry the class);
  #   * a transient signature in the gh/API transport log, which is NOT
  #     attacker-influenced (the untrusted publisher and download logs are
  #     written to separate files this grep never reads).
  [ "$1" -eq "$PUBLISHER_TRANSIENT_RC" ] && return 0
  grep -qiE "$TRANSIENT_PATTERN" "$LOG_FILE"
}


transient_wait_is_hopeless() {
  # True when the primary REST budget is exhausted and its reset is further
  # away than the retry budget can ever cover. Retrying then only spends
  # more quota and more of the job's 10-minute timeout to reach the same
  # NEUTRAL outcome, so short-circuit.
  #
  # Best-effort and FAIL-OPEN: if the probe itself cannot be read (it is an
  # API call, and the API is by hypothesis unwell), fall through to the
  # ordinary retry. A broken probe must never be the thing that skips a
  # retry that would have succeeded.
  local reset now remaining
  reset=$(gh api rate_limit --jq '.resources.core.reset' 2>/dev/null) || return 1
  remaining=$(gh api rate_limit --jq '.resources.core.remaining' 2>/dev/null) || return 1
  case "$reset$remaining" in *[!0-9]*|'') return 1 ;; esac
  [ "$remaining" -gt 0 ] && return 1
  now=$(date +%s)
  [ $((reset - now)) -gt 60 ]
}


emit_neutral_status() {
  # A tolerated transient must NEVER look like an ordinary green. Three
  # traces, in descending order of how hard they are to miss:
  #
  #   1. a `neutral` check run on the head SHA — shows in the PR's check
  #      list as an explicit non-green result;
  #   2. a `::warning::` annotation on the job;
  #   3. a line in the job summary.
  #
  # (1) is best-effort: it is itself an API call, and the API is by
  # hypothesis unwell. (2) and (3) are local and always emitted, so the
  # condition is visible even when the check run cannot be created. This
  # is the floor that makes "never a silent green" true.
  local detail="$1"
  # The bracketed field names are quoted whole: unquoted, `output[title]` is
  # a shell glob and would be silently rewritten if a matching file existed
  # in the working directory.
  gh api -X POST "repos/$SOURCE_REPO/check-runs" \
    -f name='Publish E2E evidence' \
    -f head_sha="$HEAD_SHA" \
    -f status=completed \
    -f conclusion=neutral \
    -f 'output[title]=E2E evidence not published (transient GitHub API condition)' \
    -f "output[summary]=$detail" \
    >/dev/null 2>&1 \
    || echo "Could not create the neutral check run (the API is unwell); the warning annotation and job summary still record this." >&2

  echo "::warning::E2E evidence not published: $detail"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      echo "### :warning: E2E evidence not published"
      echo
      echo "$detail"
    } >> "$GITHUB_STEP_SUMMARY"
  fi
}


print_untrusted() {
  # Both of these files contain artifact-controlled filenames and paths.
  # Emitted raw into the job log, a crafted filename containing a newline
  # followed by `::error::` or `::add-mask::` is parsed by the Actions
  # command processor — forging annotations or masking later output.
  #
  # `stop-commands` turns that processing off for the span. The token must
  # be unguessable, otherwise the untrusted content could simply emit the
  # matching resume command and re-enable parsing mid-span.
  local token
  token=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n') || token="publish-evidence-$$-$RANDOM"
  echo "::stop-commands::$token"
  [ -s "$UNTRUSTED_LOG" ] && cat "$UNTRUSTED_LOG"
  [ -s "$PUBLISHER_LOG" ] && cat "$PUBLISHER_LOG"
  echo "::$token::"
}


attempt=1
while : ; do
  # Run in an explicit SUBSHELL, not a pipeline. A pipeline would work too
  # (PIPESTATUS[0] is accurate), but with `publish` running in the current
  # shell its `set -e` aborts this script before the log can be classified.
  # The subshell confines errexit to the function and still yields its status.
  ( publish ) >"$LOG_FILE" 2>&1
  rc=$?
  cat "$LOG_FILE"
  # Printed for humans, deliberately NOT part of the classified transport
  # log — and with workflow-command parsing disabled, since the content is
  # attacker-influenced.
  print_untrusted

  [ "$rc" -eq 0 ] && exit 0
  is_transient "$rc" || break
  [ "$attempt" -ge "$MAX_ATTEMPTS" ] && break

  if transient_wait_is_hopeless; then
    echo "The primary rate-limit budget is exhausted and resets too far out for a bounded retry; not retrying."
    break
  fi

  sleep_for=${RETRY_BACKOFF_SECONDS[$((attempt - 1))]}
  echo "Transient GitHub API condition (exit $rc); retrying in ${sleep_for}s (attempt $((attempt + 1))/$MAX_ATTEMPTS)."
  sleep "$sleep_for"
  attempt=$((attempt + 1))
done

if is_transient "$rc"; then
  emit_neutral_status "a transient GitHub API condition (rate limit or 5xx) persisted across $attempt attempt(s) (last exit $rc). This is not a defect in this pull request, and it is deliberately reported as neutral rather than green."
  exit 0
fi

echo "E2E evidence publishing failed with exit $rc and no transient-API signature — failing the step." >&2
exit "$rc"
