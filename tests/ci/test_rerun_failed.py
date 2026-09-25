"""scripts/ci/rerun_failed.py: selective re-run with full-rerun fallback.

The fake ``gh`` below replays what GitHub returned for run 36170775010
(PR #1083, 2026-09-25): attempt 2 from ``--failed`` = startup_failure with
0 jobs; attempt 3 from a full re-run = jobs started.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "ci"))

import rerun_failed as rf  # noqa: E402

REPO = "ANG-Ventures/hermes-agent"
RUN = 36170775010


class FakeGh:
    def __init__(self, event="pull_request", status="completed", outcomes=()):
        self.event, self.status = event, status
        self.attempt = 1
        self.outcomes = list(outcomes)  # per new attempt: "started" | "startup_failure"
        self.calls: list[list[str]] = []
        self.results: dict[int, str] = {}

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["run", "rerun"]:
            self.attempt += 1
            self.results[self.attempt] = self.outcomes.pop(0)
            return ""
        path = args[1]
        if path == f"repos/{REPO}/actions/runs/{RUN}":
            return json.dumps({"status": self.status, "run_attempt": self.attempt, "event": self.event})
        n = int(path.split("/attempts/")[1].split("/")[0])
        if n not in self.results:
            raise subprocess.CalledProcessError(1, "gh")
        res = self.results[n]
        if path.endswith("/jobs?per_page=1"):
            return json.dumps({"total_count": 0 if res == "startup_failure" else 46})
        return json.dumps({"conclusion": "startup_failure" if res == "startup_failure" else None})


def _go(gh):
    out = io.StringIO()
    rc = rf.rerun_failed(REPO, RUN, run=gh, timeout_s=0, sleep=lambda s: None, clock=lambda: 0.0, out=out)
    return rc, [c for c in gh.calls if c[:2] == ["run", "rerun"]], out.getvalue()


def test_selective_rerun_that_starts_is_left_alone():
    rc, reruns, _ = _go(FakeGh(outcomes=["started"]))
    assert rc == 0
    assert reruns == [["run", "rerun", str(RUN), "--repo", REPO, "--failed"]]


def test_startup_failure_falls_back_to_one_full_rerun():
    rc, reruns, out = _go(FakeGh(outcomes=["startup_failure", "started"]))
    assert rc == 0
    assert reruns == [
        ["run", "rerun", str(RUN), "--repo", REPO, "--failed"],
        ["run", "rerun", str(RUN), "--repo", REPO],
    ]
    assert "::warning::attempt 2" in out


def test_full_rerun_also_failing_is_an_error_not_a_loop():
    rc, reruns, out = _go(FakeGh(outcomes=["startup_failure", "startup_failure"]))
    assert rc == 1
    assert len(reruns) == 2
    assert "::error::" in out


def test_merge_group_gets_a_full_rerun_only():
    rc, reruns, _ = _go(FakeGh(event="merge_group", outcomes=["started"]))
    assert rc == 0
    assert reruns == [["run", "rerun", str(RUN), "--repo", REPO]]


def test_in_progress_run_is_refused():
    rc, reruns, _ = _go(FakeGh(status="in_progress"))
    assert rc == 2
    assert reruns == []


def test_label_rerun_workflow_uses_the_fallback_script():
    wf = (Path(__file__).resolve().parents[2] / ".github/workflows/label-rerun.yml").read_text()
    assert 'python3 scripts/ci/rerun_failed.py --repo "$REPO" "$RUN_ID"' in wf
    # The helper runs from the base commit (actions:write token, no PR code).
    assert "ref: ${{ github.event.pull_request.base.sha }}" in wf
    # A bare selective re-run survives only as the bootstrap else-branch.
    assert wf.count('--failed || true') == 1
    assert wf.index("rerun_failed.py ]") < wf.index('--failed || true')
