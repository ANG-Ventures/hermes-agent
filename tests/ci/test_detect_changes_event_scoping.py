"""Execute the detect-changes composite action's bash for real, per event.

The lane classifier itself is covered by ``test_classify_changes.py``. What is
NOT covered there — and what merges untested code if it regresses — is the
*event gating* in ``.github/actions/detect-changes/action.yml``: which events
resolve a real ``base...head`` diff and which fail OPEN to the full matrix.

These tests run the action's actual ``run:`` block in bash with a stub ``gh``
on PATH, so the assertions are over the script that CI executes, not a
paraphrase of it.

The property under test:

* ``pull_request``  → diff from ``pull_request.base.sha``/``head.sha``.
* ``merge_group``   → diff from ``merge_group.base_sha``/``head_sha``. Safe to
  narrow because the batched candidate's diff is the UNION of its member PRs'
  diffs (argument in full at the action).
* every other event (``push``, ``release``, ``workflow_dispatch``, ...) → no
  SHAs, empty diff, every lane ``true``, ``test_scope=full``.
* a malformed/absent SHA pair on a gated event → fails OPEN, same as push.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTION = REPO_ROOT / ".github" / "actions" / "detect-changes" / "action.yml"

PR_BASE = "1" * 40
PR_HEAD = "2" * 40
MG_BASE = "3" * 40
MG_HEAD = "4" * 40


def _action_script() -> str:
    doc = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    (step,) = [s for s in doc["runs"]["steps"] if s.get("id") == "classify"]
    return step["run"]


def _run(tmp_path: Path, env_overrides: dict[str, str], changed: list[str]):
    """Run the action script with a stub ``gh`` that prints ``changed``.

    The stub echoes the requested compare ref to a sidecar file so a test can
    assert WHICH SHAs the script asked about.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {tmp_path / "gh_args.txt"}\n'
        + "".join(f'printf "%s\\n" {p!r}\n' for p in changed),
        encoding="utf-8",
    )
    (bindir / "gh").chmod(0o755)

    github_output = tmp_path / "github_output"
    github_output.touch()

    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GH_TOKEN": "stub",
        "REPO": "ANG-Ventures/hermes-agent",
        "EVENT_NAME": "",
        "PR_BASE_SHA": "",
        "PR_HEAD_SHA": "",
        "MG_BASE_SHA": "",
        "MG_HEAD_SHA": "",
        "GITHUB_OUTPUT": str(github_output),
        **env_overrides,
    }
    proc = subprocess.run(
        ["bash", "-c", _action_script()],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"action script failed:\n{proc.stdout}\n{proc.stderr}"

    outputs: dict[str, str] = {}
    for line in github_output.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    gh_args_path = tmp_path / "gh_args.txt"
    gh_args = gh_args_path.read_text(encoding="utf-8") if gh_args_path.exists() else ""
    return outputs, gh_args, proc.stdout + proc.stderr


@pytest.fixture(autouse=True)
def _need_bash():
    if shutil.which("bash") is None:  # pragma: no cover - CI always has bash
        pytest.skip("bash unavailable")


# --------------------------------------------------------------------------
# (a) merge_group with a tests-only batch: narrowed lanes.
# --------------------------------------------------------------------------
def test_merge_group_tests_only_batch_narrows_lanes(tmp_path):
    outputs, gh_args, _ = _run(
        tmp_path,
        {"EVENT_NAME": "merge_group", "MG_BASE_SHA": MG_BASE, "MG_HEAD_SHA": MG_HEAD},
        ["tests/gateway/test_foo.py", "tests/tools/test_bar.py"],
    )
    # It asked about the merge_group SHAs, not the (empty) PR ones.
    assert f"{MG_BASE}...{MG_HEAD}" in gh_args
    # pytest must still run (tests changed), but nothing that ships the product.
    assert outputs["python"] == "true"
    assert outputs["python_prod"] == "false"
    assert outputs["frontend"] == "false"
    assert outputs["site"] == "false"
    assert outputs["docker"] == "false"
    assert outputs["nix"] == "false"
    assert outputs["uv_lock"] == "false"
    assert outputs["rust"] == "false"
    assert outputs["installer"] == "false"
    assert outputs["test_scope"] == "full"


def test_merge_group_single_plugin_batch_narrows_test_scope(tmp_path):
    outputs, _, _ = _run(
        tmp_path,
        {"EVENT_NAME": "merge_group", "MG_BASE_SHA": MG_BASE, "MG_HEAD_SHA": MG_HEAD},
        ["plugins/kanban/plugin_api.py", "tests/plugins/kanban/test_api.py"],
    )
    assert outputs["test_scope"] == "plugin:kanban"


# --------------------------------------------------------------------------
# (b) merge_group touching a core path: full matrix, unchanged.
# --------------------------------------------------------------------------
def test_merge_group_core_batch_runs_full_matrix(tmp_path):
    outputs, _, _ = _run(
        tmp_path,
        {"EVENT_NAME": "merge_group", "MG_BASE_SHA": MG_BASE, "MG_HEAD_SHA": MG_HEAD},
        ["agent/run_agent.py", "tests/agent/test_run_agent.py"],
    )
    assert outputs["python"] == "true"
    assert outputs["python_prod"] == "true"
    assert outputs["docker"] == "true"
    assert outputs["nix"] == "true"
    assert outputs["scan"] == "true"
    assert outputs["test_scope"] == "full"


def test_merge_group_workflow_change_forces_every_lane(tmp_path):
    outputs, _, _ = _run(
        tmp_path,
        {"EVENT_NAME": "merge_group", "MG_BASE_SHA": MG_BASE, "MG_HEAD_SHA": MG_HEAD},
        [".github/workflows/ci.yaml"],
    )
    for lane in ("python", "python_prod", "frontend", "site", "scan", "deps",
                 "uv_lock", "npm_lock", "installer", "rust", "nix", "ci_review"):
        assert outputs[lane] == "true", lane
    assert outputs["test_scope"] == "full"


# --------------------------------------------------------------------------
# (c) malformed / absent payload: fails OPEN.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"EVENT_NAME": "merge_group"}, id="merge_group-absent-shas"),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_SHA": MG_BASE, "MG_HEAD_SHA": ""},
            id="merge_group-half-empty",
        ),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_SHA": "not-a-sha", "MG_HEAD_SHA": MG_HEAD},
            id="merge_group-garbage-base",
        ),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_SHA": MG_BASE[:39], "MG_HEAD_SHA": MG_HEAD},
            id="merge_group-short-sha",
        ),
        pytest.param({"EVENT_NAME": "pull_request"}, id="pull_request-absent-shas"),
    ],
)
def test_malformed_payload_fails_open(tmp_path, overrides):
    outputs, gh_args, _ = _run(tmp_path, overrides, ["tests/gateway/test_foo.py"])
    assert gh_args == "", "must not call the compare API without a valid SHA pair"
    for lane in ("python", "python_prod", "frontend", "site", "scan", "deps",
                 "uv_lock", "npm_lock", "installer", "rust", "nix"):
        assert outputs[lane] == "true", lane
    assert outputs["test_scope"] == "full"


# --------------------------------------------------------------------------
# Non-PR, non-merge_group events keep failing open — the narrowing is not
# widened beyond merge_group.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("event", ["push", "release", "workflow_dispatch", "schedule"])
def test_other_events_still_fail_open(tmp_path, event):
    outputs, gh_args, _ = _run(
        tmp_path,
        # Even if a payload happened to carry SHAs, a non-gated event ignores them.
        {"EVENT_NAME": event, "MG_BASE_SHA": MG_BASE, "MG_HEAD_SHA": MG_HEAD,
         "PR_BASE_SHA": PR_BASE, "PR_HEAD_SHA": PR_HEAD},
        ["tests/gateway/test_foo.py"],
    )
    assert gh_args == "", f"{event} must not resolve a diff"
    assert outputs["python"] == "true"
    assert outputs["python_prod"] == "true"
    assert outputs["test_scope"] == "full"


def test_pull_request_still_uses_pull_request_shas(tmp_path):
    outputs, gh_args, _ = _run(
        tmp_path,
        {"EVENT_NAME": "pull_request", "PR_BASE_SHA": PR_BASE, "PR_HEAD_SHA": PR_HEAD,
         "MG_BASE_SHA": MG_BASE, "MG_HEAD_SHA": MG_HEAD},
        ["tests/gateway/test_foo.py"],
    )
    assert f"{PR_BASE}...{PR_HEAD}" in gh_args
    assert MG_BASE not in gh_args
    assert outputs["python"] == "true"
    assert outputs["python_prod"] == "false"
