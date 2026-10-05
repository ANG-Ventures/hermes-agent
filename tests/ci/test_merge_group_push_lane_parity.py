"""merge_group runs every slow lane push:main runs (t_07377d12).

push:main has no diff, so the classifier turns every lane on. merge_group classifies the
batch's real diff. If a path-gated lane is off in the queue but on for push, a PR can merge
green and turn main red on a job the queue never ran. That happened with a3fb14ead:
``Python tests / e2e`` was skipped in merge_group 37335559268 and failed on push 37338668965.

These tests run ci.yaml's real ``gate-lanes`` step, fed the real classifier output for
the empty push diff and for ordinary product diffs, and compare the lanes the two events get.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YAML = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))
import classify_changes  # noqa: E402

# Lanes a diff must not switch off in the merge candidate when push:main runs them.
SLOW_LANES = ("e2e", "e2e_upgrade", "e2e_desktop_core", "e2e_desktop_update", "desktop", "bootstrap")

DIFFS = [
    ["hermes_cli/gateway.py", "tests/hermes_cli/test_legacy_unit_stop_failures.py"],  # a3fb14ead
    ["README.md"],
    ["tests/test_something.py"],
    ["agent/turn_loop.py"],
]


def _gate_lanes_script() -> str:
    doc = yaml.safe_load(CI_YAML.read_text(encoding="utf-8"))
    (step,) = [s for s in doc["jobs"]["detect"]["steps"] if s.get("id") == "gate-lanes"]
    return step["run"]


def _gate(tmp_path: Path, event: str, files: list[str], desktop_jobs: str = "") -> dict[str, str]:
    classified = {k: str(v).lower() for k, v in classify_changes.classify(files).items()}
    out = tmp_path / f"out-{event}-{len(files)}-{desktop_jobs}"
    out.write_text("", encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"],
        "RELEASE": "false",
        "EVENT_NAME": event,
        "DESKTOP_JOBS": desktop_jobs,
        "CLASSIFIED": json.dumps(classified),
        "GITHUB_OUTPUT": str(out),
    }
    subprocess.run(["bash", "-c", _gate_lanes_script()], env=env, check=True, cwd=REPO_ROOT)
    return dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if line)


@pytest.mark.parametrize("desktop_jobs", ["", "on"])
@pytest.mark.parametrize("files", DIFFS, ids=lambda f: f[0])
def test_merge_group_runs_every_slow_lane_push_runs(tmp_path, files, desktop_jobs):
    push = _gate(tmp_path, "push", [], desktop_jobs)
    mg = _gate(tmp_path, "merge_group", files, desktop_jobs)
    for lane in SLOW_LANES:
        if push[lane] == "true":
            assert mg[lane] == "true", f"{lane}: push runs it, merge_group with {files} skips it"


def test_push_runs_the_e2e_lane(tmp_path):
    # The premise of the parity check: the empty push diff turns e2e on.
    assert _gate(tmp_path, "push", [])["e2e"] == "true"


@pytest.mark.parametrize("files", DIFFS, ids=lambda f: f[0])
def test_pull_request_keeps_its_path_gate(tmp_path, files):
    # The PR pre-filter is unchanged: an ordinary product diff still skips e2e there.
    if classify_changes.classify(files)["e2e"]:
        pytest.skip("diff routes to e2e by path")
    assert _gate(tmp_path, "pull_request", files)["e2e"] == "false"


def test_desktop_kill_switch_still_applies_to_merge_group(tmp_path):
    assert _gate(tmp_path, "merge_group", ["README.md"], desktop_jobs="")["desktop"] == "false"
    assert _gate(tmp_path, "merge_group", ["README.md"], desktop_jobs="on")["desktop"] == "true"
