#!/usr/bin/env bash
# e2e-internal-silence.sh <worktree> -- live E2E for t_bb4059d2 (internal reply + NO_REPLY).
#
# Runs <worktree>'s gateway in a scratch HERMES_HOME through the puppet lane (skill puppet-lane:
# Aegis bot in #daedalus, Apollo bot as driver) and drives three arms, each in its own thread:
#   long   completion reply = 3-sentence summary with E2E-LONG-<nonce> + NO_REPLY line -> must be
#          posted within 120 s, without NO_REPLY
#   short  completion reply = "Already handled." + NO_REPLY -> nothing posted for 60 s after it
#   bare   completion reply = NO_REPLY -> nothing posted
# The scratch config turns on display.background_process_agent_notify (production keeps it off),
# so the background completion is an internal=True turn: the path that swallowed replies.
# One line per arm: arm=<a> head=<sha> posted=<id|none> marker_leaked=<y|n> verdict=PASS|FAIL
# Exit 0 only when all three arms pass (and puppet-lane's zero-spill check passes).
# usage: [E2E_MODEL=claude-opus-5-5] e2e-internal-silence.sh <worktree>
#
# Run it on both heads: main (expect long=FAIL) and the PR head (expect all PASS).
set -euo pipefail
WT="${1:?usage: e2e-internal-silence.sh <hermes-agent worktree>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LANE="${PUPPET_LANE_SH:-$HOME/.hermes/skills-shared/hermes-harness/puppet-lane/scripts/puppet-lane.sh}"
[[ -x "$LANE" || -f "$LANE" ]] || { echo "error: puppet lane not found at $LANE (macOS Studio only)"; exit 2; }
# E2E_MODEL / E2E_PROVIDER pick the scratch route (puppet-lane default claude-alr/claude-fable-5-1;
# when that model is capped every arm FAILs with reason=launch_turn_failed, which is not a verdict).
exec bash "$LANE" "$WT" --reason "${E2E_REASON:-t_bb4059d2 internal-silence E2E}" \
  ${E2E_MODEL:+--model "$E2E_MODEL"} ${E2E_PROVIDER:+--provider "$E2E_PROVIDER"} -- \
  python3 "$HERE/e2e_internal_silence.py"
