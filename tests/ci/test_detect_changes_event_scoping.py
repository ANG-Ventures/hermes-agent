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
* ``merge_group``   → diff from the TARGET BRANCH
  (``merge_group.base_ref``) to ``merge_group.head_sha``. Deliberately NOT
  ``merge_group.base_sha``: queue candidate refs are chained, so base_sha is
  the previous candidate and its diff is only the topmost entry's delta, which
  under-tests every other member of the batch (measured 2026-09-19; argument in
  full at the action).
* every other event (``push``, ``release``, ``workflow_dispatch``, ...) → no
  base resolved, empty diff, every lane ``true``, ``test_scope=full``.
* a malformed/absent payload on a gated event → fails OPEN, same as push.
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
# The PREVIOUS queue candidate. A chained batch's payload carries this as
# base_sha; the classifier must never diff from it.
MG_PREV_CANDIDATE = "3" * 40
MG_HEAD = "4" * 40
MG_BASE_REF = "refs/heads/main"


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
        "MG_BASE_REF": "",
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
        {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD},
        ["tests/gateway/test_foo.py", "tests/tools/test_bar.py"],
    )
    # It asked about the TARGET BRANCH, not the chained previous candidate.
    assert f"main...{MG_HEAD}" in gh_args
    # and never about the chained previous candidate.
    assert MG_PREV_CANDIDATE not in gh_args
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
        {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD},
        ["plugins/kanban/plugin_api.py", "tests/plugins/kanban/test_api.py"],
    )
    assert outputs["test_scope"] == "plugin:kanban"


# --------------------------------------------------------------------------
# (b) merge_group touching a core path: full matrix, unchanged.
# --------------------------------------------------------------------------
def test_merge_group_core_batch_runs_full_matrix(tmp_path):
    outputs, _, _ = _run(
        tmp_path,
        {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD},
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
        {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD},
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
        pytest.param({"EVENT_NAME": "merge_group"}, id="merge_group-absent-payload"),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": ""},
            id="merge_group-half-empty",
        ),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_REF": "main", "MG_HEAD_SHA": MG_HEAD},
            id="merge_group-base-ref-not-refs-heads",
        ),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_REF": "refs/tags/v1", "MG_HEAD_SHA": MG_HEAD},
            id="merge_group-base-ref-is-a-tag",
        ),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_REF": "refs/heads/m ain;rm", "MG_HEAD_SHA": MG_HEAD},
            id="merge_group-base-ref-hostile-charset",
        ),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": "not-a-sha"},
            id="merge_group-garbage-head",
        ),
        pytest.param(
            {"EVENT_NAME": "merge_group", "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD[:39]},
            id="merge_group-short-head-sha",
        ),
        pytest.param({"EVENT_NAME": "pull_request"}, id="pull_request-absent-shas"),
        pytest.param(
            {"EVENT_NAME": "pull_request", "PR_BASE_SHA": PR_BASE, "PR_HEAD_SHA": "not-a-sha"},
            id="pull_request-garbage-head",
        ),
    ],
)
def test_malformed_payload_fails_open(tmp_path, overrides):
    outputs, gh_args, _ = _run(tmp_path, overrides, ["tests/gateway/test_foo.py"])
    assert gh_args == "", "must not call the compare API without a valid base/head pair"
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
        {"EVENT_NAME": event, "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD,
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
         "MG_BASE_REF": MG_BASE_REF, "MG_HEAD_SHA": MG_HEAD},
        ["tests/gateway/test_foo.py"],
    )
    assert f"{PR_BASE}...{PR_HEAD}" in gh_args
    assert MG_HEAD not in gh_args
    assert outputs["python"] == "true"
    assert outputs["python_prod"] == "false"


# --------------------------------------------------------------------------
# REGRESSION GUARD — the multi-entry chained batch.
#
# This is the case that makes base_sha wrong and it is invisible on a
# single-entry batch (where base_sha == the target tip, so both bases agree).
# Measured on the live queue 2026-09-19: with 5 entries queued, each candidate
# ref's base_sha was the PREVIOUS candidate's head, so the payload pair on the
# candidate that fast-forwards main resolved 3 files where the true incoming
# set was 12 — two product files (gateway/platforms/base.py,
# plugins/platforms/discord/adapter.py) would have merged with their lanes
# never run on the tree that lands.
#
# The stub gh serves the batch UNION for the target-branch compare and only
# the topmost entry's delta for the chained-base compare, so this test goes RED
# if the action is ever pointed back at merge_group.base_sha.
# --------------------------------------------------------------------------
def test_merge_group_chained_batch_uses_union_not_topmost_delta(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    union = ["cli.py", "gateway/platforms/base.py", "plugins/platforms/discord/adapter.py"]
    topmost = ["cli.py"]
    # Route on which base the script asked about.
    (bindir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {tmp_path / "gh_args.txt"}\n'
        f'if [[ "$*" == *"{MG_PREV_CANDIDATE}"* ]]; then\n'
        + "".join(f'  printf "%s\\n" {p!r}\n' for p in topmost)
        + "else\n"
        + "".join(f'  printf "%s\\n" {p!r}\n' for p in union)
        + "fi\n",
        encoding="utf-8",
    )
    (bindir / "gh").chmod(0o755)

    github_output = tmp_path / "github_output"
    github_output.touch()
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GH_TOKEN": "stub",
        "REPO": "ANG-Ventures/hermes-agent",
        "EVENT_NAME": "merge_group",
        "PR_BASE_SHA": "",
        "PR_HEAD_SHA": "",
        # A real chained payload carries BOTH: base_ref (target branch) and
        # base_sha (the previous candidate). Export the wrong one too, so the
        # test proves the action ignores it rather than merely lacking it.
        "MG_BASE_REF": MG_BASE_REF,
        "MG_BASE_SHA": MG_PREV_CANDIDATE,
        "MG_HEAD_SHA": MG_HEAD,
        "GITHUB_OUTPUT": str(github_output),
    }
    proc = subprocess.run(
        ["bash", "-c", _action_script()],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    gh_args = (tmp_path / "gh_args.txt").read_text(encoding="utf-8")
    assert f"main...{MG_HEAD}" in gh_args, "must diff from the target branch"
    assert MG_PREV_CANDIDATE not in gh_args, (
        "must NOT diff from merge_group.base_sha — that is the previous chained "
        "candidate and yields only the topmost entry's delta"
    )

    outputs: dict[str, str] = {}
    for line in github_output.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    # The union contains a plugin path and a gateway path. Under the topmost
    # delta (cli.py alone) frontend/site stay false either way, but the lanes
    # the OTHER member PRs need must be ON.
    assert outputs["python"] == "true"
    assert outputs["python_prod"] == "true"
    assert outputs["scan"] == "true"
    # A multi-tree union must never resolve to a single plugin's scope.
    assert outputs["test_scope"] == "full"
